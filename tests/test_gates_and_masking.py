# -*- coding: utf-8 -*-
"""v2.9.9：四类「测量环节静默失能」的回归测试。

这个文件里的每一条，对应一次**已经发生过的**真实缺陷。它们同属一个家族 ——
**一个对外数字，没有任何东西在校它，而且偏好看**：

1. ``claim_layering`` 把 ``cli.py``（文件）当目录扫，**扫到 0 个文件**，
   却报「4 个接入层 AST 核验通过」—— 那 4 是名字个数。
   而 ``cli.py`` 里有 4 处真实越层 import。
2. ``reporters.py`` 5 处 ``n = top_n or DEFAULT_TOP_N`` —— ``0`` 是 falsy，
   于是「不限簇数」被静默改成「只给 20 个」；``to_json`` 的 ``error_kinds``
   写全部簇数、实际只吐截断后的、且**零截断标记**。
   v2.9.5 只修了 service 层一处，**根因一动没动**。
3. ``claim_confidence`` 写成 ``... if not has_chain else True``，
   而样例日志恰好有链 —— 这条主张永远不会红。
4. ``mask_text`` 把任何含斜杠的词当路径（``read/write`` 与 ``input/output``
   被掩成完全相同的模板），以及把纯数字 ID 判成十六进制令牌 ``H``
   （而 ``H`` 在变量分布面板里被标为「标识符」）。

所以这里既测**行为**，也测**门禁本身可失效**（故意做坏 → 必须变红）。
后者是本文件的主要理由：一个从不曾变红过的门禁，等于没有门禁。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from log_ai_compressor import service                                    # noqa: E402
from log_ai_compressor.core.clustering import (                          # noqa: E402
    mask_text,
    similarity,
)


# ---------------------------------------------------------------------------
# 2. top_n 的 falsy-0：三种语义必须各归各位
# ---------------------------------------------------------------------------
def _many_cluster_result(n_clusters: int = 26):
    """造 n_clusters 个**结构上互不相似**的簇。

    注意：早先用「kind-0 / kind-1」那种只差一个数字的写法，全部会被掩码
    吃成同一簇（数字 -> N），于是测试「通过」了但其实什么都没测到 ——
    那是一个**假覆盖**。这里用互不相干的英文词元，确保真的切出多个簇。
    """
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
             "kilo lima mike november oscar papa quebec romeo sierra tango "
             "uniform victor whiskey xray yankee zulu").split()
    lines = ["2024-01-01 10:00:00 ERROR [w] subsystem %s reported %s "
             "violation while handling %s request"
             % (words[i % len(words)], words[(i + 1) % len(words)],
                words[(i + 2) % len(words)])
             for i in range(n_clusters)]
    tmp = Path(tempfile.mkdtemp())
    f = tmp / "many.log"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return service.analyze_files([str(f)], top_n=10 ** 9)


@pytest.fixture(scope="module")
def many():
    r = _many_cluster_result()
    # 先证真：夹具必须真的切出多个簇，否则下面全是空转
    assert len(r.clusters) > 20, (
        f"夹具只切出 {len(r.clusters)} 个簇 —— 本该 >20 才能触发截断。"
        f"换句话说：这条测试会假装通过而实际什么都没测。")
    return r


class TestTopNZeroIsUnlimitedNotFalsy:
    def test_zero_delivers_every_cluster(self, many):
        """`top_n=0` 的含义是「不限」，不是「没给」，更不是「零个」。

        修复前：`n = top_n or DEFAULT_TOP_N` -> 0 是 falsy -> 只给 20 个。
        """
        out = json.loads(service.export_text(many, "json", top_n=0))
        assert len(out["clusters"]) == len(many.clusters), (
            f"top_n=0 只交付了 {len(out['clusters'])}/{len(many.clusters)} 个簇"
            f" —— 「不限」被当成了「没给」")

    def test_none_falls_back_to_default(self, many):
        """`None` 才是「没给」。这条不能被上面的修复顺手改坏。"""
        out = json.loads(service.export_text(many, "json", top_n=None))
        assert len(out["clusters"]) == 20

    def test_explicit_number_is_honoured(self, many):
        out = json.loads(service.export_text(many, "json", top_n=5))
        assert len(out["clusters"]) == 5

    def test_zero_does_not_produce_an_empty_report(self, many):
        """0 不等于「零个簇」—— 曾经真的返回过空列表。"""
        for fmt in ("md", "txt", "html", "summary"):
            text = service.export_text(many, fmt, top_n=0)
            assert text.strip(), f"{fmt} 格式在 top_n=0 时返回了空文本"


class TestJsonReportsTruncationHonestly:
    """`error_kinds` 写全部簇数、实际只吐截断后的、且零标记 —— 交付物说谎。"""

    def test_truncation_is_marked(self, many):
        out = json.loads(service.export_text(many, "json", top_n=5))
        assert out["error_kinds"] == len(many.clusters)
        assert len(out["clusters"]) == 5
        assert out["total_clusters"] == len(many.clusters)
        assert out["shown_clusters"] == 5
        assert out["top_n_truncated"] is True, (
            "截断了却不标记 —— 用户无从知道少了几类")

    def test_no_truncation_means_flag_is_false(self, many):
        out = json.loads(service.export_text(many, "json", top_n=0))
        assert out["top_n_truncated"] is False

    def test_service_and_exporter_agree(self, many):
        """两个出口交付同样的簇数 —— 曾经一个给 21 一个给 20。"""
        d = service.result_to_dict(many, top_n=0)
        out = json.loads(service.export_text(many, "json", top_n=0))
        assert len(d["clusters"]) == len(out["clusters"]) == len(many.clusters)


# ---------------------------------------------------------------------------
# 4. mask_text 的两处误伤
# ---------------------------------------------------------------------------
class TestMaskDoesNotSwallowSlashWords:
    """`read/write` 与 `input/output` 掩码后曾**完全相同**（similarity=1.000）。"""

    def test_slash_words_are_not_paths(self):
        a = mask_text("failed on read/write channel")
        b = mask_text("failed on input/output channel")
        assert "P" not in a, f"read/write 被当成路径掩掉了 -> {a!r}"
        assert a != b, "两类不同事件被掩成同一模板"
        assert similarity(a, b) < 0.85

    def test_protocol_slash_words_survive(self):
        for text in ("protocol TCP/IP handshake failed", "and/or condition met"):
            assert mask_text(text) == text, f"{text!r} 被误掩"

    def test_real_paths_are_still_masked(self):
        """修「误掩」不能变成「漏掩」—— 真路径必须照样掩掉。"""
        for text in ("failed at src/db/pool.c:12",
                     "open /var/log/syslog failed",
                     "see ./config/app.yaml"):
            assert "P" in mask_text(text), f"真路径没被掩：{text!r} -> {mask_text(text)!r}"

    def test_windows_path_still_masked(self):
        assert "P" in mask_text(r"path C:\Users\bob\app.log not found")


class TestHexTokenDoesNotSwallowPureNumbers:
    """纯数字 ID 被判成 H，而 H 在变量分布面板里被标成「标识符」。"""

    def test_pure_digit_id_is_numeric(self):
        m = mask_text("user id 1234567890 not found")
        assert "H" not in m, f"纯数字 ID 被判成十六进制令牌 -> {m!r}"
        assert "N" in m

    def test_hex_with_letters_stays_hex(self):
        assert "H" in mask_text("trace deadbeefcafe1234")

    def test_literal_word_is_not_hex(self):
        """`deadbeef` 之类纯字母英文不该被当标识符 —— 这条一直是对的，别弄坏。"""
        assert "H" not in mask_text("node beaded status active")


# ---------------------------------------------------------------------------
# 1 & 3. 门禁本身可失效
# ---------------------------------------------------------------------------
def _load_prove():
    p = ROOT / "scripts" / "prove.py"
    spec = importlib.util.spec_from_file_location("prove_gates_299", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prove_gates_299"] = mod
    spec.loader.exec_module(mod)
    return mod


prove = _load_prove()


class TestLayeringGateCanActuallyFail:
    """`claim_layering` 曾把 cli.py 当目录扫、扫到 0 个文件却报通过。"""

    def test_current_repo_has_no_violation(self):
        c = prove.claim_layering()
        assert c["holds"] is True, c["detail"]

    def test_detail_reports_actual_scanned_count(self):
        """detail 必须报**实际扫描文件数**，不能报名字个数。"""
        detail = prove.claim_layering()["detail"]
        assert "实际扫描" in detail
        import re
        m = re.search(r"实际扫描 (\d+) 个文件", detail)
        assert m, f"detail 里找不到实际扫描文件数：{detail!r}"
        assert int(m.group(1)) > 0

    def test_gate_catches_an_injected_violation(self, tmp_path, monkeypatch):
        """故意放一个越层 import 的 cli.py，门禁必须变红。

        这条是本类的核心：门禁从没红过，就等于没有门禁。
        """
        pkg = tmp_path / "log_ai_compressor"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "cli.py").write_text(
            "from log_ai_compressor.core.pipeline import analyze_file\n",
            encoding="utf-8")
        monkeypatch.setattr(prove, "ROOT", tmp_path)
        c = prove.claim_layering()
        assert c["holds"] is False, "注入了越层 import 门禁仍报通过 —— 门禁失能"
        assert "cli.py" in c["detail"]

    def test_gate_fails_when_it_scans_nothing(self, tmp_path, monkeypatch):
        """扫到 0 个文件还判「通过」本身就是失能，必须是红的。"""
        (tmp_path / "log_ai_compressor").mkdir()
        monkeypatch.setattr(prove, "ROOT", tmp_path)
        c = prove.claim_layering()
        assert c["holds"] is False, "扫到 0 个接入层文件却判通过"


class TestConfidenceGateCanActuallyFail:
    """`claim_confidence` 曾是 `... if not has_chain else True` 的恒真分支。"""

    def _log(self, tmp_path: Path, text: str) -> Path:
        f = tmp_path / "t.log"
        f.write_text(text, encoding="utf-8")
        return f

    def test_no_chain_log_must_have_zero_confirmed(self, tmp_path):
        """无因果链的日志，一个 CONFIRMED 都不许有 —— 这条以前从没真跑过。"""
        log = self._log(tmp_path, "2024-01-01 10:00:00 ERROR [db] "
                                  "connection refused to replica-1\n"
                                  "2024-01-01 10:00:01 ERROR [db] "
                                  "connection refused to replica-2\n")
        c = prove.claim_confidence(log)
        assert "无 Caused-by" in c["detail"]
        assert c["holds"] is True

    def test_confirmed_without_any_chain_is_rejected(self, tmp_path, monkeypatch):
        """把分析结果伪造成「有 CONFIRMED、但日志无链」，门禁必须变红。"""
        log = self._log(tmp_path, "2024-01-01 10:00:00 ERROR [db] boom\n")

        real = service.analyze_files([str(log)], context_lines=2)

        class _Fake(list):
            pass

        faked = _Fake(real.clusters)
        # 伪造：给第一个簇挂上 CONFIRMED，但日志里没有 Caused-by
        faked[0].root_cause_confidence = "CONFIRMED"
        fake_result = type("R", (), {})()
        fake_result.clusters = faked
        fake_result.evidence = real.evidence
        fake_result.stats = real.stats
        monkeypatch.setattr(prove, "analyze_files",
                            lambda *a, **k: fake_result)
        c = prove.claim_confidence(log)
        assert c["holds"] is False, (
            "无链日志里出现 CONFIRMED，门禁仍判成立 —— 恒真没修掉")
