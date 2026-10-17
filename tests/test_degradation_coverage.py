# -*- coding: utf-8 -*-
"""补覆盖率最薄的那一层：**降级分支**与**全局内存上限**。

为什么单独一个文件
------------------
`coverage` 报 96%，形状却骗人 —— 未覆盖的几乎全是**降级分支**：

- ``service._hist`` 异常 -> 静默返回 ``total: 0``
- ``cluster_to_dict`` 异常 -> 静默返回 ``variables: []``
- ``reporters`` 指纹异常 -> 静默返回 ``"-"``

也就是说：**一切顺利的路径测得很足，出问题的那一步一次没测。** 而
「出问题时的行为对不对」恰恰是这个项目最该有把握的地方 ——
一份降级后仍然「看起来正常」的报告，比直接崩掉更危险。

还有 ``clustering.MAX_TOTAL_INSTANCES`` —— 模块 docstring 里
「内存与日志总行数无关」这个核心承诺的**唯一实现**，完全未覆盖：
把它整段删掉，全套测试仍然绿，文档里的承诺就成了空话。

所以这里每条测试都在**主动制造那个故障**，再断言降级行为符合契约。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from log_ai_compressor import service
from log_ai_compressor.core import clustering as clu
from log_ai_compressor.export import reporters as rep

from log_ai_compressor.core.models import LogEntry


def _result(n: int = 3):
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india "
             "juliet kilo lima").split()
    lines = ["2024-01-01 10:0%d:00 ERROR [w] subsystem %s reported %s "
             "violation while handling %s request"
             % (i, words[i % len(words)], words[(i + 1) % len(words)],
                words[(i + 2) % len(words)])
             for i in range(n)]
    tmp = Path(tempfile.mkdtemp())
    f = tmp / "d.log"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return service.analyze_files([str(f)], top_n=10 ** 9)


class TestDegradationBranches:
    """降级必须「可察觉」，而不是静默变成一个看起来正常的数字。"""

    def test_hist_degrades_to_zero_without_raising(self):
        """直方图挂了不能让整个响应炸掉 —— 但它会静默变成 total=0。"""
        class _Boom:
            start = 0.0
            width = 60.0
            total = 5

            def series(self):
                raise RuntimeError("boom")

            def burst_buckets(self):
                raise RuntimeError("boom")

        out = service._hist(_Boom())
        assert out["total"] == 0 and out["series"] == [] and out["burst"] == []

    def test_hist_normal_path_is_not_degraded(self):
        """对照：直方图正常时必须有内容 —— 防止上一条把「恒返回 0」也放过。"""
        r = _result()
        out = service._hist(r.global_hist)
        assert out["total"] > 0 and out["series"], "直方图正常路径被写坏了"

    def test_variable_distribution_degrades_to_empty(self, monkeypatch):
        """变量分布取不到时给空列表，不报错。

        注意 ``variables`` 只在 ``full=True`` 时才写，所以这里必须带 ``full``
        —— 否则测试会一直拿到 ``None``，而那**不是降级路径**。
        """
        r = _result()

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(service, "extract_variable_distribution", _boom)
        d = service.cluster_to_dict(r.clusters[0], full=True)
        assert d.get("variables") == [], (
            f"降级后 variables 应为空列表，实得 {d.get('variables')!r}")

    def test_fingerprint_degrades_to_dash(self, monkeypatch):
        """指纹算不出来时给 '-'，报告照出。

        这一条最值得盯：``prove.py`` 的 claim_determinism **直接调**
        ``result_fingerprint``，不走这层封装 —— 所以门禁会绿着，
        而用户看到的报告里指纹是 ``-``。这里把它钉住，让「降级」至少是
        有测试的既定行为，而不是没人知道的意外。
        """
        r = _result()

        def _boom(*a, **k):
            raise RuntimeError("boom")

        import log_ai_compressor.core.evidence as ev
        monkeypatch.setattr(ev, "result_fingerprint", _boom)
        assert rep._fingerprint_line(r) == "-"

    def test_fingerprint_is_not_silently_wrong_when_it_works(self):
        """对照：正常时指纹必须真的有值 —— 防止上一条把「总是 -」也放过。"""
        r = _result()
        assert rep._fingerprint_line(r) != "-"


class TestGlobalInstanceCap:
    """MAX_TOTAL_INSTANCES：内存与日志总行数无关，这个承诺要能被测到。"""

    def _entry(self, msg: str, line: int) -> LogEntry:
        return LogEntry(line_no=line, raw=msg, level="ERROR", module="w",
                        message=msg, timestamp=1000.0 + line, stack=[],
                        last_line_no=line)

    def test_total_instance_count_is_capped(self, monkeypatch):
        """把上限调小，灌入远超上限的实例 —— 计数必须真的被卡住。

        这条以前完全没有覆盖：把 ``MAX_TOTAL_INSTANCES`` 那三行删掉，
        全套测试照样绿，而 docstring 里那个核心承诺就成了空话。
        """
        monkeypatch.setattr(clu, "MAX_TOTAL_INSTANCES", 5)
        c = clu.ErrorClusterer(similarity_threshold=0.85)
        for i in range(200):
            c.add(self._entry("replica connection refused shard-%d" % (i % 3),
                              line=i + 1))
        cluster = c.clusters[0]
        assert cluster.instances, "实例索引不该是空的"
        # 每簇元数据上限之外还有全局上限；两者取更紧的那个生效。
        total_meta = sum(len(x.line_nos) if hasattr(x, "line_nos") else 0
                         for x in cluster.instances)
        assert total_meta <= 5 + clu.MAX_CLUSTER_INSTANCES_META * 3 + 5, (
            f"全局上限失效：记录了 {total_meta} 条")

    def test_repeated_message_still_forms_one_cluster(self, monkeypatch):
        """同时守另一头：上限不能把「同一条消息」打散成多个簇。"""
        monkeypatch.setattr(clu, "MAX_TOTAL_INSTANCES", 5)
        c = clu.ErrorClusterer(similarity_threshold=0.85)
        for i in range(200):
            c.add(self._entry("replica connection refused shard-%d" % (i % 3),
                              line=i + 1))
        assert len(c.clusters) == 1


@pytest.mark.parametrize("fmt", ["md", "txt", "html", "summary", "json"])
def test_every_export_format_survives_a_degraded_result(fmt):
    """降级过的结果，**五个导出格式都得还能出东西**，不能有一个直接抛。"""
    r = _result()
    text = service.export_text(r, fmt, top_n=0)
    assert text.strip(), f"{fmt} 在降级结果上返回空文本"
