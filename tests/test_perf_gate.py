# -*- coding: utf-8 -*-
"""scripts/perf_gate.py 的测试。

**这个文件的第一条用例，来自一次真实的翻车**
-------------------------------------------
性能门禁的第一版把仪表装在 `clustering.similarity()` 上，结果
`similarity` 计数恒为 **0** —— 因为 `_find_similar` 直接用
`SequenceMatcher`，根本不经过那个模块级函数。门禁于是每一条都绿、
每一条都「通过」，而它守的路径**一次都没被走到**。

这就是「报数为 0 的计数器等于没装」。下面 `test_counters_are_not_silently_zero`
就是为了让那种翻车**当场变红**，而不是等到半年后有人问「这数字怎么是 0」。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "perf_gate", ROOT / "scripts" / "perf_gate.py")
assert _SPEC and _SPEC.loader
perf = importlib.util.module_from_spec(_SPEC)
sys.modules["perf_gate"] = perf
_SPEC.loader.exec_module(perf)

#: 计数器有下限。定这些下限的理由是「宁可误报也不放过 0」——
#: 真正的负载跑出来是四位数，留两个数量级的余量给机器差异，
#: 但 0 / 1 这种数量级一定说明仪表装错了地方。
MIN_COUNTERS = {"mask_text": 100, "find_similar": 50, "ratio": 100}


def _measure_small():
    return perf.measure(lines=2500)


@pytest.fixture(scope="module")
def measured():
    return _measure_small()


# ---------------------------------------------------------------------------
# 负载本身
# ---------------------------------------------------------------------------
def test_workload_is_byte_identical_across_calls():
    a = "\n".join(perf.workload_lines(500))
    b = "\n".join(perf.workload_lines(500))
    assert a == b, "负载必须逐字节可复现 —— 否则跨平台基线不可比"


def test_workload_does_not_depend_on_local_timezone():
    """负载用 gmtime 而不是 localtime。

    用 localtime 的话，同一个 seed 在 UTC 和 UTC+8 机器上生成的是不同
    字节的日志，基线计数会以「平台差异」的名义变红 —— 而那是门禁自己的
    bug，不是产品的回归。这里钉住「不碰 localtime」这条约定。

    判据只认**调用**（`localtime(`），不认出现在散文里的那个词 ——
    这个文件顶部的 docstring 恰恰要解释为什么不用它，所以第一版按
    「源码里出现 localtime 就红」写的判据当场把自己判红了。那是判据
    指错了对象，不是代码有问题。
    """
    import re
    src = (ROOT / "scripts" / "perf_gate.py").read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#"))
    assert not re.search(r"localtime\s*\(", code), \
        "性能负载不得调用 localtime（跨时区不可比）"
    assert re.search(r"gmtime\s*\(", code), "负载应当使用 gmtime"


# ---------------------------------------------------------------------------
# 计数器：非零 + 确定性
# ---------------------------------------------------------------------------
def test_counters_are_not_silently_zero(measured):
    for key, floor in MIN_COUNTERS.items():
        got = measured["counters"][key]
        assert got >= floor, (
            "计数器 %s 只有 %d —— 仪表很可能装在了没被执行的路径上。"
            "第一版就是这样的：similarity 恒为 0，而门禁每条都绿。"
            % (key, got))


def test_counters_are_deterministic():
    """同一份负载跑两遍，每个计数必须逐项相等。

    这是硬门禁成立的前提：如果计数本身会抖，那它拦不住任何回归。
    """
    a, b = _measure_small(), _measure_small()
    assert a["counters"] == b["counters"]
    assert a["clusters"] == b["clusters"]
    assert a["entries"] == b["entries"]


def test_measurement_restores_the_monkeypatched_attributes():
    """仪表是靠改模块属性装的，跑完必须原样装回去。

    漏掉还原的后果是：在这之后跑的**任何**测试都在被污染的模块上跑，
    而它们仍然是绿的 —— 门禁自己污染了后续所有检查。
    """
    from log_ai_compressor.core import clustering as C
    before = (C.mask_text, C.SequenceMatcher, C.ErrorClusterer._find_similar)
    try:
        _measure_small()
    finally:
        after = (C.mask_text, C.SequenceMatcher, C.ErrorClusterer._find_similar)
        assert before[0] is after[0]
        assert before[1] is after[1]
        assert before[2] is after[2]


# ---------------------------------------------------------------------------
# 门禁判定
# ---------------------------------------------------------------------------
def _write_baseline(tmp_path, monkeypatch, doc):
    p = tmp_path / "perf_baseline.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(perf, "BASELINE", p)
    return p


def test_gate_passes_on_exact_match(tmp_path, monkeypatch, capsys, measured):
    _write_baseline(tmp_path, monkeypatch, {
        "counters": measured["counters"],
        "clusters": measured["clusters"],
        "entries": measured["entries"],
    })
    assert perf.check(measured) == 0
    assert "通过" in capsys.readouterr().out


def test_gate_fails_when_work_grows(tmp_path, monkeypatch, capsys, measured):
    """真实的性能回归：相似度回退次数变多（比如有人把剪枝提前去掉）。"""
    grown = dict(measured)
    grown["counters"] = dict(measured["counters"],
                              ratio=measured["counters"]["ratio"] + 1)
    _write_baseline(tmp_path, monkeypatch, {
        "counters": measured["counters"],
        "clusters": measured["clusters"],
        "entries": measured["entries"],
    })
    assert perf.check(grown) == 1
    out = capsys.readouterr().out
    assert "counters 回归" in out
    # 失败信息必须点名是哪个计数、从多少变到多少，只说「counters 回归」
    # 等于把 diff 两个 dict 的活儿推给看到红的人
    assert "ratio 基线 %d -> 实测 %d" % (measured["counters"]["ratio"],
                                        grown["counters"]["ratio"]) in out


def test_gate_fails_when_clusters_change(tmp_path, monkeypatch, measured):
    """簇数变了：要么变慢（多扫了桶），要么结果变了（更严重）。"""
    other = dict(measured, clusters=measured["clusters"] + 1)
    _write_baseline(tmp_path, monkeypatch, {
        "counters": measured["counters"],
        "clusters": measured["clusters"],
        "entries": measured["entries"],
    })
    assert perf.check(other) == 1


def test_gate_fails_when_baseline_field_missing(tmp_path, monkeypatch, capsys,
                                                measured):
    _write_baseline(tmp_path, monkeypatch, {"counters": measured["counters"]})
    assert perf.check(measured) == 1
    assert "基线里没有" in capsys.readouterr().out


def test_gate_never_judges_on_wall_clock(tmp_path, monkeypatch, capsys,
                                         measured):
    """墙钟慢 100 倍也**不得**让门禁变红。

    这条是刻意的负向测试：墙钟跨机器不可比，噪声只会向下，把它做成
    红线只会收获一堆假红，然后某天被人加 --continue-on-error 关掉。
    """
    _write_baseline(tmp_path, monkeypatch, {
        "counters": measured["counters"],
        "clusters": measured["clusters"],
        "entries": measured["entries"],
    })
    slow = dict(measured, seconds=measured["seconds"] * 100,
                lines_per_second=1)
    assert perf.check(slow) == 0
    out = capsys.readouterr().out
    assert "只报告，不判定" in out


# ---------------------------------------------------------------------------
# 仓库里的真实基线
# ---------------------------------------------------------------------------
def test_real_baseline_file_exists_and_documents_the_split():
    doc = json.loads((ROOT / "docs" / "perf_baseline.json")
                     .read_text(encoding="utf-8"))
    for key in ("counters", "clusters", "entries"):
        assert key in doc, "基线缺少 %s —— 门禁会永远报「基线里没有」" % key
    assert doc["counters"]["ratio"] >= MIN_COUNTERS["ratio"], (
        "基线里的 ratio 计数低于下限，说明它是在仪表装错的那一版生成的")
    assert doc["counters"]["find_similar"] >= MIN_COUNTERS["find_similar"]
    assert doc["workload"]["lines"] == perf.LINES, (
        "基线记录的负载行数与脚本当前用的不一致 —— 换了负载却不刷新基线，"
        "下一次运行必然红，而红的原因与产品无关")
    assert "不参与判定" in json.dumps(doc, ensure_ascii=False), (
        "基线里必须写明墙钟不参与判定，否则半年后会有人拿它当门槛")
