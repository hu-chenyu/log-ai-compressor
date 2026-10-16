# -*- coding: utf-8 -*-
"""scripts/benchmark_loghub.py 的测试。

为什么这个文件需要测试
----------------------
基准脚本的两条口径一旦被改坏，出来的表仍然是「看起来很正常」的表格，
但结论会完全反过来 —— 而这张表是要对外发布的。这类静默失效最贵：

1. **LineId 对齐**：`structured.csv` 的 LineId 是 1-based 行号。写成 0-based
   或用 `enumerate()` 的下标直接查表，会整体错位一行，而且**不会报错**，
   只是保留率悄悄变低 —— 对我们是「有利」的假结果，更危险。
2. **预算封顶**：朴素基线必须真的停在 token 预算上。少截几行同样不会报错。

所以这里用构造的微型数据集，把口径钉死。
"""
from __future__ import annotations

import csv
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 脚本不在包内（scripts/ 无 __init__.py），按路径加载
_SPEC = importlib.util.spec_from_file_location(
    "benchmark_loghub", ROOT / "scripts" / "benchmark_loghub.py")
assert _SPEC and _SPEC.loader
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["benchmark_loghub"] = bench
_SPEC.loader.exec_module(bench)


# ---------------------------------------------------------------------------
# 微型数据集：3 行、2 种事件
# ---------------------------------------------------------------------------
RAW = [
    "[10.30 16:49:06] chrome.exe - proxy.example.org:5070 close, 0 bytes sent",
    "[10.30 16:49:07] chrome.exe - proxy.example.org:5070 close, 12 bytes sent",
    "[10.30 16:49:08] chrome.exe - opening session with proxy.example.org",
]
ROWS = [
    #  LineId  Content                          EventId  EventTemplate
    (1, "proxy.example.org:5070 close, 0 bytes sent", "E1",
     "proxy.example.org:5070 close, <*> bytes sent"),
    (2, "proxy.example.org:5070 close, 12 bytes sent", "E1",
     "proxy.example.org:5070 close, <*> bytes sent"),
    (3, "opening session with proxy.example.org", "E2",
     "opening session with proxy.example.org"),
]


@pytest.fixture()
def mini_dataset(tmp_path: Path) -> "bench.Dataset":
    """构造一个 3 行 / 2 种标注事件的 Loghub 格式数据集。"""
    d = tmp_path / "Mini" / "Mini"
    d.mkdir(parents=True)
    log = d / "Mini_full.log"
    log.write_text("\n".join(RAW) + "\n", encoding="utf-8")
    csv_path = d / "Mini_full.log_structured.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["LineId", "Content", "EventId", "EventTemplate"])
        for lid, content, eid, tpl in ROWS:
            w.writerow([lid, content, eid, tpl])
    return bench.Dataset("Mini", log, csv_path)


# ---------------------------------------------------------------------------
# LineId 对齐（错位测试的核心）
# ---------------------------------------------------------------------------
def test_line_event_uses_one_based_lineid(mini_dataset):
    """LineId 必须按 1-based 解释；错位一行会让保留率悄悄变错。"""
    assert mini_dataset.line_event[1] == "E1"
    assert mini_dataset.line_event[2] == "E1"
    assert mini_dataset.line_event[3] == "E2"
    # 第 1 行是 RAW[0]，第 3 行是 RAW[2]
    assert mini_dataset.event_of_text(RAW[0]) == "E1"
    assert mini_dataset.event_of_text(RAW[2]) == "E2"
    assert mini_dataset.event_of_line_no(3) == "E2"


def test_event_of_line_no_out_of_range_returns_none(mini_dataset):
    """越界行号返回 None，不能抛异常也不能错配到别的簇。"""
    assert mini_dataset.event_of_line_no(0) is None
    assert mini_dataset.event_of_line_no(999) is None


def test_events_are_deduplicated(mini_dataset):
    """3 行只对应 2 种事件，保留率的分母是 2 不是 3。"""
    assert mini_dataset.events == {"E1", "E2"}
    assert len(mini_dataset.events) == 2


# ---------------------------------------------------------------------------
# token 预算封顶
# ---------------------------------------------------------------------------
def test_tail_never_exceeds_budget(mini_dataset):
    """预算封顶是硬不变量：任何档位下输出都不得越界（否则压缩比会被悄悄做高）。"""
    for b in (1, 5, 15, 30, 60, 200):
        text, _ = bench.method_tail(mini_dataset, budget_tokens=b)
        assert bench.estimate_tokens(text) <= b, f"预算 {b} 被突破"


def test_tail_takes_from_end_of_file(mini_dataset):
    """预算只够一行时必须给文件末尾那行 —— tail 的语义就是「看最后 N 行」。

    如果基线从文件开头取，基线就被冤枉了，曲线不可信。
    """
    one = bench.estimate_tokens(RAW[2]) + 1
    text, covered = bench.method_tail(mini_dataset, budget_tokens=one)
    assert text.splitlines() == [RAW[2]]
    assert covered == {"E2"}


def test_tail_full_budget_covers_all_events(mini_dataset):
    """预算充足时 tail 应覆盖全部标注事件（基线不该被冤枉）。"""
    _, covered = bench.method_tail(mini_dataset, budget_tokens=10 ** 6)
    assert covered == {"E1", "E2"}


def test_grep_respects_budget_and_filters(mini_dataset):
    """grep 只保留命中关键词的行，且不越预算。"""
    text, covered = bench.method_grep(mini_dataset, budget_tokens=10 ** 6)
    assert bench.estimate_tokens(text) <= 10 ** 6
    # 三个 EventId 都不含 error/exception 等关键词 -> 命中为空
    assert covered == set()


def test_grep_matches_keywords(mini_dataset, tmp_path):
    """把一条原始行改成含 ERROR，grep 就必须能命中并映射回事件。"""
    assert bench.GREP_PATTERN.search("2024 ERROR something failed")
    assert not bench.GREP_PATTERN.search("opening session with proxy")


# ---------------------------------------------------------------------------
# 模板归一化（跨方法比较的基础）
# ---------------------------------------------------------------------------
def test_canon_folds_params_and_numbers():
    """可变值折叠后才可比：数字与 <*> 都变 <P>。"""
    a = bench.canon("proxy:5070 close, 0 bytes sent")
    b = bench.canon("proxy:5070 close, <*> bytes sent")
    assert a == b


def test_canon_is_case_and_space_insensitive():
    assert bench.canon("Hello   World 12") == bench.canon("hello world 12")


def test_match_template_maps_to_annotated_event(mini_dataset):
    """Drain3 式模板 -> 标注事件 的模糊映射要能命中。"""
    assert mini_dataset.match_template("proxy.example.org:5070 close, <*> bytes sent") == "E1"
    assert mini_dataset.match_template("opening session with proxy.example.org") == "E2"


def test_match_template_rejects_unrelated(mini_dataset):
    """无关文本不该被硬凑到某个事件上（阈值要真的起作用）。"""
    assert mini_dataset.match_template("completely unrelated zzz qqq") is None


# ---------------------------------------------------------------------------
# percent 展示
# ---------------------------------------------------------------------------
def test_pct_handles_zero_denominator():
    """分母为 0 时不能 ZeroDivisionError —— 数据集无标注时应优雅显示。"""
    assert bench.pct(3, 0) == "—"
    assert bench.pct(1, 4) == "25.0%"


# ---------------------------------------------------------------------------
# 下载脚本与基准脚本的目录约定必须对得上
# ---------------------------------------------------------------------------
def _load_fetch():
    spec = importlib.util.spec_from_file_location(
        "fetch_datasets", ROOT / "scripts" / "fetch_datasets.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetch_datasets"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_discover_accepts_fetch_datasets_output_layout(tmp_path, monkeypatch):
    """fetch_datasets.py 解出来的目录，必须能被 discover() 找到。

    这条不是洁癖：v2.9.2 之前 `fetch_datasets.unpack()` 解到
    `data/loghub_full/`，而 `discover()` 只扫 `data/loghub/`。
    照 README「先 fetch 再 benchmark」跑的人**必然**得到
    「没有可用数据集。请先下载 Loghub 2.0 到 data/loghub/」——
    而那句话反过来指着一个 fetch 根本不会写进去的目录。

    复现门槛应该长在「做不做得成」上，不是长在「知道怎么做」上。

    这里走的是 fetch **真实的** unpack()，不是照着它的目录规则另写一遍
    —— 后者只能校我自己那份副本，恰恰漏掉真正要防的分叉。
    """
    fetch = _load_fetch()

    zip_path = tmp_path / "Demo.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Demo/Demo_full.log",
                    "2024-01-01 10:00:00 ERROR [svc] boom\n")
        zf.writestr("Demo/Demo_full.log_structured.csv",
                    "LineId,Content,EventTemplate,EventId\n1,boom,E1,E1\n")

    monkeypatch.setattr(fetch, "DATA", tmp_path)
    # unpack() 结尾会 out.relative_to(ROOT) 打一行日志；不把 ROOT 一起挪走，
    # 临时目录下的路径就 relative_to 不动，测试挂在**打印**上而不是断言上。
    monkeypatch.setattr(fetch, "ROOT", tmp_path)
    fetch.unpack(zip_path, "Demo")

    monkeypatch.setattr(bench, "DATA_DIRS",
                        (tmp_path / "loghub", tmp_path / "loghub_full"))
    found = bench.discover(None)
    assert [d.name for d in found] == ["Demo"], (
        "fetch 的产物 discover 认不出来 —— 复现链断了")


def test_discover_dedups_same_name_across_roots(tmp_path, monkeypatch):
    """两个根里同名时只取一份，且**先到的有效副本**优先。"""
    for root in ("loghub", "loghub_full"):
        d = tmp_path / root / "Demo" / "Demo"
        d.mkdir(parents=True)
        (d / "Demo_full.log").write_text("x\n", encoding="utf-8")
        (d / "Demo_full.log_structured.csv").write_text(
            "LineId,Content,EventTemplate,EventId\n1,x,E1,E1\n", encoding="utf-8")
    monkeypatch.setattr(bench, "DATA_DIRS",
                        (tmp_path / "loghub", tmp_path / "loghub_full"))
    found = bench.discover(None)
    assert len(found) == 1, f"同名数据集重复收录：{[d.name for d in found]}"


def test_discover_does_not_let_a_bad_copy_shadow_a_good_one(tmp_path, monkeypatch):
    """坏副本不能遮蔽好副本。

    `seen` 只在**真正收进来**之后才记 —— 否则 data/loghub/ 里一个
    没有标注的残缺副本，会让 loghub_full/ 里完好的那份永远不被看见，
    而报错只说「标注事件类型为 0」，完全看不出是被遮蔽了。
    """
    good = tmp_path / "loghub" / "Demo" / "Demo"
    good.mkdir(parents=True)
    (good / "Demo_full.log").write_text("x\n", encoding="utf-8")
    (good / "Demo_full.log_structured.csv").write_text(
        "LineId,Content,EventTemplate,EventId\n1,x,E1,E1\n", encoding="utf-8")

    bad = tmp_path / "loghub_full" / "Demo" / "Demo"
    bad.mkdir(parents=True)
    (bad / "Demo_full.log").write_text("x\n", encoding="utf-8")   # 没有 csv

    monkeypatch.setattr(bench, "DATA_DIRS",
                        (tmp_path / "loghub_full", tmp_path / "loghub"))
    found = bench.discover(None)
    assert [d.name for d in found] == ["Demo"], "被坏副本遮蔽了"