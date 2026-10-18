# -*- coding: utf-8 -*-
"""docs/BENCHMARK.md「结果四」的 PA/GA 三张表 ←→ docs/logparser_pa_ga.csv。

**为什么这张表也要有守卫**
------------------------
v2.9.9 那次事故的形状是：改了代码 → 基准结果变了 → **README / docs/BENCHMARK
的表格原地不动 → 全部门禁绿着**。PA/GA 那三张表是本项目第一次把外部指标
写进文档，同样的事故会在这里重演一次：改了解析器，PA 变了，表没变，
而「换个不是你设计的指标，你还敢跑吗」这句话仍然立在文档里。

所以三张表的每一格都对着 CSV 逐格核对。CSV 由
`python scripts/eval_logparser_pa_ga.py --csv docs/logparser_pa_ga.csv` 生成。

**期望值的来源必须是 CSV，不能从被测的文档里解析出来**
--------------------------------------------------------
第一版把「正确的那个值」从 README 表里现读，于是测试「把表改成 1.00%」时
夹具同时变成 1.00%，对账拿自己和自己比，从一开始就是绿的。
正确值只能来自**独立于被测输入**的那份快照（CSV）。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "docs" / "BENCHMARK.md"
CSV_PATH = ROOT / "docs" / "logparser_pa_ga.csv"

_NUM = re.compile(r"[-+]?[\d,]*\.?\d+")


def _load_csv() -> dict:
    if not CSV_PATH.is_file():
        pytest.skip("先跑 python scripts/eval_logparser_pa_ga.py --csv %s"
                    % CSV_PATH)
    out: dict[tuple[str, str, str], dict] = {}
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out[(row["method"], row["source"], row["dataset"])] = row
    return out


def _table_after(anchor: str) -> list[list[str]]:
    """取紧跟在 `anchor` 那句话之后的第一个 Markdown 表。"""
    lines = BENCH.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if anchor in ln), None)
    assert start is not None, "docs/BENCHMARK.md 里找不到表锚点：%s" % anchor
    head = None
    for i in range(start, min(start + 12, len(lines))):
        if lines[i].lstrip().startswith("| 数据集"):
            head = i
            break
    assert head is not None, "锚点 %s 后面没有表" % anchor
    rows = []
    j = head
    while j < len(lines) and lines[j].lstrip().startswith("|"):
        cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
        rows.append(cells)
        j += 1
    return rows


def _cell_number(cell: str) -> float | None:
    c = cell.replace("**", "").replace("%", "").replace(",", "").strip()
    m = _NUM.fullmatch(c)
    return float(m.group(0)) if m else None


def _check_table(anchor: str, method: str, source: str,
                 cols: dict[str, int], tol: float = 0.005) -> None:
    """cols = {CSV 里的字段名: 该列在文档表里的下标}。

    键是 CSV 字段名、值是**列号**，而断言时比的是**文档里的中文表头**：
    第一版把 CSV 字段名直接当表头去比，于是「标注行」被判成
    「不是 annotated_lines」—— 判据拿错了对象，它读的是 CSV 的命名，
    而被守的是文档的表头。
    """
    data = _load_csv()
    rows = _table_after(anchor)
    header = rows[0]
    for field, idx in cols.items():
        assert header[idx].strip(), (
            "%s 的第 %d 列是空的 —— 表结构变了，先更新本测试" % (anchor, idx))

    body = [r for r in rows[2:] if r and r[0] != ""]
    datasets = [r for r in body if "算术平均" not in r[0]]
    assert len(datasets) == 10, (
        "%s 应有 10 个数据集行，实测 %d 行" % (anchor, len(datasets)))

    for cells in datasets:
        name = cells[0]
        row = data.get((method, source, name))
        assert row is not None, "CSV 里没有 (%s, %s, %s) 这一行" % (
            method, source, name)
        for field, idx in cols.items():
            got = _cell_number(cells[idx])
            want = _cell_number(row[field])
            assert got is not None, "%s / %s 那格 %r 解析不出数字" % (
                name, field, cells[idx])
            assert want is not None, "CSV 的 %s 不是数字：%r" % (field, row[field])
            assert abs(got - want) <= tol, (
                "%s 的 %s：文档写 %s，CSV 是 %s —— 逐格漂了"
                % (name, field, cells[idx], row[field]))


# ---------------------------------------------------------------------------
# 三张表
# ---------------------------------------------------------------------------
_TABLE_A_COLS = {"annotated_lines": 1, "gt_events": 2, "clusters": 3,
                 "PA": 4, "GA": 5}
_TABLE_B_COLS = {"annotated_lines": 1, "gt_events": 2, "clusters": 3,
                 "PA": 4, "GA": 5}
_TABLE_C_COLS = {"clusters": 1, "PA": 2, "GA": 3}


def test_table_a_raw_lines_matches_csv():
    _check_table("**表 A ·", "ours", "raw", _TABLE_A_COLS)


def test_table_b_content_matches_csv():
    _check_table("**表 B ·", "ours", "content", _TABLE_B_COLS)


def test_table_c_drain3_control_matches_csv():
    _check_table("**表 C ·", "drain3", "content", _TABLE_C_COLS)


# ---------------------------------------------------------------------------
# 汇总行：最容易被顺手改错的一行（v2.9.9 就干过一次）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("anchor,method,source,pa_idx,ga_idx", [
    ("**表 A ·", "ours", "raw", 4, 5),
    ("**表 B ·", "ours", "content", 4, 5),
    ("**表 C ·", "drain3", "content", 2, 3),
])
def test_average_row_matches_csv(anchor, method, source, pa_idx, ga_idx):
    data = _load_csv()
    rows = _table_after(anchor)
    avg = [r for r in rows[2:] if r and "算术平均" in r[0]]
    assert len(avg) == 1, "%s 应恰好有一行算术平均，实测 %d 行" % (
        anchor, len(avg))
    subset = [v for (m, s, _d), v in data.items() if m == method and s == source]
    assert len(subset) == 10, "CSV 里 (%s, %s) 应有 10 行，实测 %d" % (
        method, source, len(subset))
    want_pa = round(sum(_cell_number(r["PA"]) for r in subset) / len(subset), 2)
    want_ga = round(sum(_cell_number(r["GA"]) for r in subset) / len(subset), 2)
    got_pa = _cell_number(avg[0][pa_idx])
    got_ga = _cell_number(avg[0][ga_idx])
    assert got_pa == pytest.approx(want_pa, abs=0.01), (
        "%s 的 PA 算术平均：文档写 %s，实算是 %s" % (anchor, got_pa, want_pa))
    assert got_ga == pytest.approx(want_ga, abs=0.01), (
        "%s 的 GA 算术平均：文档写 %s，实算是 %s" % (anchor, got_ga, want_ga))


# ---------------------------------------------------------------------------
# 三条结论必须与数字一致（写错结论比写错数字更容易误导人）
# ---------------------------------------------------------------------------
def _claim_numbers() -> dict:
    data = _load_csv()

    def mean(method, source, field):
        vals = [_cell_number(v[field]) for (m, s, _d), v in data.items()
                if m == method and s == source]
        return round(sum(vals) / len(vals), 2)

    wins = {"PA": 0, "GA": 0}
    for ds in {d for (_m, _s, d) in data}:
        ours = data.get(("ours", "content", ds))
        theirs = data.get(("drain3", "content", ds))
        if not ours or not theirs:
            continue
        for f in wins:
            if _cell_number(ours[f]) > _cell_number(theirs[f]):
                wins[f] += 1
    return {
        "pa_ours": mean("ours", "content", "PA"),
        "pa_drain": mean("drain3", "content", "PA"),
        "ga_ours": mean("ours", "content", "GA"),
        "ga_drain": mean("drain3", "content", "GA"),
        "pa_raw": mean("ours", "raw", "PA"),
        "wins": wins,
    }


def test_headline_claim_matches_the_numbers():
    """正文里「PA 略胜、GA 明显落后」这句必须与 CSV 算出来的一致。

    数字对了但结论写反，比数字错更糟：读者会照着错的结论理解整张表。
    """
    n = _claim_numbers()
    text = BENCH.read_text(encoding="utf-8")
    assert n["pa_ours"] > n["pa_drain"], "PA 上本项目并不领先，正文结论却这么写"
    assert n["ga_ours"] < n["ga_drain"], "GA 上本项目并不落后，正文结论却这么写"
    assert "PA 略胜、GA 明显落后" in text
    assert "+3.65pp" in text, "PA 领先幅度变了，正文里的数字要一起改"
    assert "−13.40pp" in text or "-13.40pp" in text, \
        "GA 落后的幅度变了，正文里的数字要一起改"
    assert ("PA 上 %d 胜 %d 负" % (n["wins"]["PA"],
                                  10 - n["wins"]["PA"])) in text
    assert ("GA 上 %d 胜 %d 负" % (n["wins"]["GA"],
                                  10 - n["wins"]["GA"])) in text


def test_raw_vs_content_gap_claim_matches_the_numbers():
    n = _claim_numbers()
    text = BENCH.read_text(encoding="utf-8")
    gap = round(n["pa_ours"] - n["pa_raw"], 2)
    assert abs(gap - 38.47) < 0.02, "raw 与 content 的 PA 落差变了：%s" % gap
    assert "23.24%" in text and "61.71%" in text


# ---------------------------------------------------------------------------
# 同一句结论在**四份文档**里各写了一遍 —— 四处就有三处漏的可能
# ---------------------------------------------------------------------------
# 真发生过：「GA 上 3 胜 7 负」这个手算错在 docs/BENCHMARK.md 被抓出来改了，
# 而同一句在 CHANGELOG 里原封不动留到了提交前。docs/BENCHMARK.md 有测试
# 盯着，CHANGELOG 没有 —— 于是同一个错误在同一个 commit 里既改掉又留着。
# 所以判据不能只盯那一份：四份都盯，且**每处出现**都要对，不是「至少一处对」。
_WINNER_RE = {
    "PA": re.compile(r"PA\s*(?:上\s*)?(\d+)\s*胜\s*(\d+)\s*负"),
    "GA": re.compile(r"GA\s*(?:上\s*)?(\d+)\s*胜\s*(\d+)\s*负"),
}


@pytest.mark.parametrize("doc", ["README.md", "CHANGELOG.md", "docs/BENCHMARK.md"])
def test_written_win_loss_counts_are_correct_in_every_doc(doc):
    """三份文档，每一处出现都要对。

    特别地，**docs/BENCHMARK.md 自己也要过这条**：原来只有
    `test_headline_claim_matches_the_numbers`，而它是**存在性**断言
    （「GA 上 4 胜 6 负」在不在）。BENCHMARK 里另有一句正确的历史注文
    写着同一句话，于是把正文那处改错之后断言照样绿 —— 记分卡的
    `paga/summary-verdict-flipped` 实测存活，抓的就是这个洞。
    **「正确的值必须出现」不等于「错误的值没有出现」**，而后者才是这里
    要守的东西。
    """
    n = _claim_numbers()
    text = (ROOT / doc).read_text(encoding="utf-8")
    for metric, rx in _WINNER_RE.items():
        want_w, want_l = n["wins"][metric], 10 - n["wins"][metric]
        found = rx.findall(text)
        assert found, "%s 里找不到「%s 上 N 胜 M 负」这句话" % (doc, metric)
        for win, loss in found:
            assert (int(win), int(loss)) == (want_w, want_l), (
                "%s 里有一句「%s 上 %s 胜 %s 负」，实算是 %d 胜 %d 负 —— "
                "同一个手算错漏在了这一处" % (doc, metric, win, loss,
                                            want_w, want_l))
        # 代价是：文档**不能原样引用**一个历史上写错的胜场数
        # （本文件写这个 CHANGELOG 的修订时就撞上了：表格里引用
        # 「GA 上 3 胜 7 负」说明那次事故，被这条判据判成又错了一次）。
        # 只能写成不带指标前缀的「3 胜 7 负」。这是刻意的取舍 ——
        # 「每处出现都要对」比「允许一处历史引用」值钱。


# ---------------------------------------------------------------------------
# 这三张表**不得**被基准表对账认走
#
# prove.py 的 _bench_columns 按**表头词**认表，而 PA/GA 表里出现过
# 「Drain3」这类词 —— 一旦被认成基准表，它报的全是「基线里没有这一列」，
# 真漂移被埋在这类噪音里。这正是 v2.9.9 事故的形状，第二次。
# ---------------------------------------------------------------------------
def test_pa_ga_tables_are_not_mistaken_for_benchmark_tables():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prove", ROOT / "scripts" / "prove.py")
    prove = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prove)

    lines = BENCH.read_text(encoding="utf-8").splitlines()
    n_paga = 0
    for i, line in enumerate(lines):
        if not (line.lstrip().startswith("|") and "数据集" in line):
            continue
        if i + 1 >= len(lines) or not prove._MD_SEP_RE.match(lines[i + 1]):
            continue
        cols = prove._bench_columns(line)
        if cols:
            assert set(cols) & {"delivery", "budget32k", "full"}, (
                "PA/GA 表被基准对账认成了基准表（映射到 %s）—— "
                "表头里的列名与基准表的判定词撞了" % sorted(cols))
        else:
            n_paga += 1
    assert n_paga >= 3, (
        "只认出 %d 张非基准表；PA/GA 那三张应该都不被认成基准表" % n_paga)
