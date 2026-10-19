# -*- coding: utf-8 -*-
"""docs/BENCHMARK.md「结果四·五」的 GA 失分分解表 ←→ docs/ga_gap_decomposition.csv。

**为什么这张新表也要有守卫**
--------------------------
它回答的是一个很具体的问题：**「PA / GA 能不能做到全胜？」**
答案落在「过合并占失分多少、过分裂占多少」上，而这两个百分比是**从失分
里分的** —— 分母是失分行数，不是标注行数。

分母写错一个档次，表上的结论就会整个反过来（同一个分子，分母从**失分**
换成**标注行数**，占比会掉到原来的五分之一左右，主导方向直接翻面），
而表本身看起来完全正常。所以逐格对着 CSV 核对，
**包括那三个占比与它们自己的分母**。

**期望值只能来自 CSV，不能从被测文档里解析出来**
------------------------------------------------
这是 `tests/test_logparser_pa_ga_table.py` 第一版踩过的坑：期望值从文档
现读，于是测试「把表改成 51.4%」时夹具跟着变成 51.4%，对账拿自己跟
自己比，从一开始就是绿的。正确值必须来自**独立于被测输入**的快照。

**CSV 缺了不许 skip**
---------------------
CSV 是**提交进仓库**的（`docs/ga_gap_decomposition.csv`，由
`python scripts/diagnose_ga_gap.py --csv ...` 生成）。所以它不在，就是
有人删了它 —— 而那正是这张表失去对账链的时刻。此时 `pytest.skip` 会让
整组判据**安静地不跑**，CI 照样绿。这里选择直接判红。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "docs" / "BENCHMARK.md"
CSV_PATH = ROOT / "docs" / "ga_gap_decomposition.csv"

_ANCHOR = "表 D · GA 的失分丢在哪一摊"

_NUM = re.compile(r"[-+]?[\d,]*\.?\d+")


def _load_csv() -> dict:
    assert CSV_PATH.is_file(), (
        "找不到 %s —— 它是提交进仓库的对账快照，删掉它这张表就没有"
        "任何对账了。重新生成：python scripts/diagnose_ga_gap.py --csv %s"
        % (CSV_PATH.name, CSV_PATH))
    out: dict[tuple[str, str], dict] = {}
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "CSV 没有数据行 —— 判据失效了"
    assert set(rows[0]) >= {"method", "dataset", "GA", "lost", "over_merge",
                            "over_split", "unparsed"}, (
        "CSV 列变了：%s —— 脚本改了列就要同步重跑 --csv" % sorted(rows[0]))
    for row in rows:
        out[(row["method"], row["dataset"])] = row
    return out


def _table_after(anchor: str, first_header: str = "数据集") -> list[list[str]]:
    """取紧跟 `anchor` 之后的第一个 Markdown 表（表头首格 = `first_header`）。

    **认表头而不是认「第一个以 `|` 开头的行」**：后者会把锚点附近那张
    引用的表格认成目标表，而它看起来完全正常。
    """
    lines = BENCH.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if anchor in ln), None)
    assert start is not None, "docs/BENCHMARK.md 里找不到表锚点：%s" % anchor
    want = "| " + first_header
    head = next((i for i in range(start, min(start + 12, len(lines)))
                 if lines[i].lstrip().startswith(want)), None)
    assert head is not None, "锚点 %s 后面没有以「%s」开头的表" % (anchor, want)
    rows, j = [], head
    while j < len(lines) and lines[j].lstrip().startswith("|"):
        rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
        j += 1
    return rows


def _cell_number(cell: str) -> float | None:
    c = cell.replace("**", "").replace("%", "").replace(",", "").strip()
    m = _NUM.fullmatch(c)
    return float(m.group(0)) if m else None


def _body(anchor: str) -> list[list[str]]:
    rows = _table_after(anchor)
    body = [r for r in rows[2:] if r and r[0] and "合计" not in r[0]]
    assert body, "%s 的表体是空的" % anchor
    return body


# ---------------------------------------------------------------------------
# 1. 表的形状
# ---------------------------------------------------------------------------
def test_the_table_lists_every_dataset_in_the_csv():
    data = _load_csv()
    ours = sorted(ds for (m, ds) in data if m == "ours")
    body = _body(_ANCHOR)
    assert len(body) == len(ours), (
        "表里 %d 行，CSV 里 %d 个数据集 —— 表少了几格"
        % (len(body), len(ours)))
    assert [r[0] for r in body] == ours, "表里的数据集顺序/名字与 CSV 不一致"


def test_every_column_has_a_header():
    """判据认的是**表头**。CSV 字段名不是表头（第一版踩过）。"""
    header = _table_after(_ANCHOR)[0]
    for i, cell in enumerate(header):
        assert cell.strip(), "第 %d 列表头是空的 —— 表结构变了" % i
    assert header[0].strip() == "数据集"


# ---------------------------------------------------------------------------
# 2. 逐格对账
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("idx,fmt", [
    (1, "{:.2f}"),      # 我方 GA
    (2, "{:.2f}"),      # Drain3 GA
    (3, "{:.1f}"),      # 我方过合并占比
    (4, "{:.1f}"),      # Drain3 过合并占比
    (5, "{:.1f}"),      # 我方过分裂占比
    (6, "{:.1f}"),      # Drain3 过分裂占比
])
def test_each_cell_matches_the_csv(idx, fmt):
    data = _load_csv()
    for cells in _body(_ANCHOR):
        ds = cells[0]
        ours, drain = data.get(("ours", ds)), data.get(("drain3", ds))
        if ours is None:
            continue
        raw = (ours if idx in (1, 3, 5) else drain)
        if raw is None:
            continue
        field = {1: "GA", 3: "pct_over_merge", 5: "pct_over_split",
                 2: "GA", 4: "pct_over_merge", 6: "pct_over_split"}[idx]

        if field.startswith("pct_") and int(raw["lost"]) == 0:
            # **零失分不是「占比 0%」。** Apache 的 Drain3 是 GA 100.00%，
            # 分母为 0，`_pct` 返回 0.0 —— 写成 `0.0%` 会被读成
            # 「失分全在另一桶」，而真相是**根本没有失分**。所以文档
            # 在这些格上必须写「—」，判据也不许拿数字去比。
            assert _cell_number(cells[idx]) is None, (
                "[%s] 这一列零失分，第 %d 格应该写「—」而不是数字：%r"
                % (ds, idx, cells[idx]))
            continue

        got = _cell_number(cells[idx])
        assert got is not None, "[%s] 第 %d 格不是数字：%r" % (ds, idx, cells[idx])
        want = float(raw[field])
        if field == "GA":
            assert abs(got - want) < 0.005, (
                "[%s] GA 写 %s，CSV 是 %.2f" % (ds, cells[idx], want))
        else:
            # **比的是「CSV 值四舍五入到文档那一档」，不是差值容差。**
            #
            # CSV 存 2 位，文档显示 1 位 —— 中间隔着**两次**四舍五入。
            # 真值 v 满足 |v − CSV| ≤ 0.005，文档格 d 满足 |d − v| ≤ 0.05，
            # 于是 |d − CSV| 的正确上界是 **0.055 而不是 0.05**。
            #
            # 第一版写的是 `abs(got - want) <= 0.05`，当场红在 HPC 上：
            # 27955/88876 = 31.45495% → CSV 存 31.45 → 文档显示 31.5，
            # 差正好 0.05，而 31.5 − 31.45 在浮点里是 0.05000000000000071。
            # **边界容差在这里是「判定失效」，不是「容差不够」** ——
            # 把 0.05 调成 0.06 只会把同一个错误推迟到下一个数据集。
            allowed = {round(want - 0.005, 1), round(want + 0.005, 1)}
            assert got in allowed, (
                "[%s] 第 %d 格写 %s，CSV 的 %s 是 %s（应为 %s）"
                % (ds, idx, cells[idx], field, want,
                   " 或 ".join("%.1f" % a for a in sorted(allowed))))
        assert _cell_number(fmt.format(want)) is not None   # 精度只到一位


def test_each_lost_line_count_matches_the_csv():
    """最后一列是**绝对行数**，不是百分比 —— 列的语义变了，判据也要变。

    用同一个 `_cell_number` 去比百分比然后发现「差得太多」，是没用的 ——
    这里比的是整数，且要求**精确相等**。
    """
    data = _load_csv()
    for cells in _body(_ANCHOR):
        ours = data.get(("ours", cells[0]))
        if ours is None:
            continue
        assert _cell_number(cells[7]) == int(ours["lost"]), (
            "[%s] 失分行数写 %s，CSV 是 %s" % (cells[0], cells[7], ours["lost"]))


# ---------------------------------------------------------------------------
# 3. 失分桶合计表：分母必须是「失分行数」，不是标注行数
# ---------------------------------------------------------------------------
_BUCKET_ANCHOR = "表 E · 全量失分的两桶合计"

#: 表头 -> (CSV 字段, kind)。kind 决定拿绝对数还是占比去比。
_BUCKET_COLS = {"过合并": ("over_merge", "count"), "过分裂": ("over_split", "count"),
                "未解析": ("unparsed", "count")}


def _bucket_table() -> list[list[str]]:
    return _table_after(_BUCKET_ANCHOR, "失分桶")


def _bucket_row(name: str) -> list[str] | None:
    """按**前缀**找那一行：文档里写的是「过合并（条件 a 失败）」。

    用全等去匹配，判据就得跟着文档的措辞走 —— 而措辞是给人看的，
    改一个词就红，数字却没错。判据要认的是「这一行是哪一桶」。
    """
    for r in _bucket_table()[2:]:
        if r and r[0].strip().startswith(name):
            return r
    return None


def test_the_bucket_table_lists_every_bucket():
    header = _bucket_table()[0]
    assert header[0].strip() == "失分桶", header
    names = [r[0].strip() for r in _bucket_table()[2:] if r and r[0]]
    for want in _BUCKET_COLS:
        assert any(n.startswith(want) for n in names), (
            "失分桶表里没有「%s」这一行，少一桶比例就加不到 100%%：%s"
            % (want, names))


@pytest.mark.parametrize("name", list(_BUCKET_COLS))
def test_each_bucket_count_matches_the_csv(name):
    field, kind = _BUCKET_COLS[name]
    data = _load_csv()
    ours = [r for (m, _), r in data.items() if m == "ours"]
    want = sum(int(r[field]) for r in ours)
    row = _bucket_row(name)
    assert row is not None, "失分桶表里没有「%s」这一行" % name
    got = _cell_number(row[1])
    assert got == pytest.approx(want, abs=1), (
        "%s 行数写 %s，CSV 合计是 %d" % (name, row[1], want))


def test_the_bucket_percentages_use_lost_lines_as_the_denominator():
    """**这一格是本节最容易写错、也最要命的一格。**

    分母写成标注行数（2,190,444 行量级）而不是失分行数（852,661 行量级），
    三个百分比会加起来**远不到 100%** —— 而读者不会去加。
    判据直接从 CSV 重算，不看文档里的任何百分号。
    """
    data = _load_csv()
    ours = [r for (m, _), r in data.items() if m == "ours"]
    lost = sum(int(r["lost"]) for r in ours)
    assert lost > 0, "CSV 里没有任何失分 —— 分母为 0，本节无从谈起"
    for r in ours:
        assert int(r["over_merge"]) + int(r["over_split"]) + int(r["unparsed"]) \
            == int(r["lost"]), ("CSV 自己就不自洽：%s" % r["dataset"])
    for name, (field, _k) in _BUCKET_COLS.items():
        want = 100.0 * sum(int(r[field]) for r in ours) / lost
        row = _bucket_row(name)
        assert row is not None
        got = _cell_number(row[2])
        assert got is not None, "%s 的占比不是数字：%r" % (name, row[2])
        allowed = {round(want - 0.005, 1), round(want + 0.005, 1)}
        assert got in allowed, (
            "%s 占比写 %s，按失分 %d 行重算是 %.4f%%（应为 %s）"
            % (name, row[2], lost, want,
               " 或 ".join("%.1f" % a for a in sorted(allowed))))
    total_rows = [r for r in _bucket_table()[2:] if r and r[0]]
    assert len(total_rows) == len(_BUCKET_COLS) + 1, (
        "失分桶表应恰好三桶 + 一个合计行，实际 %d 行" % len(total_rows))
    total = _cell_number(total_rows[-1][1])
    assert total == pytest.approx(lost, abs=1), (
        "合计行写 %s，失分总和是 %d" % (total, lost))


def test_the_two_claims_about_the_split_are_both_derivable_from_the_csv():
    """文里那两句结论（「几乎对半」「主导方向相反」）必须能从 CSV 算出来。

    没有这条，那两句就是**没人看守的结论** —— 而它们正是本节要回答用户
    那个问题的全部内容。
    """
    data = _load_csv()
    ours = [r for (m, _), r in data.items() if m == "ours"]
    lost = sum(int(r["lost"]) for r in ours)
    merge = sum(int(r["over_merge"]) for r in ours)
    split = sum(int(r["over_split"]) for r in ours)
    share, share_split = 100.0 * merge / lost, 100.0 * split / lost
    # 「几乎对半」：**两桶都**得落在 40%~60% 之间。只查过合并那一桶的话，
    # 0% / 100% 也能过 —— 而那是「方向相反」的另一种写法，不是「对半」。
    for label, pct in (("过合并", share), ("过分裂", share_split)):
        assert 40.0 <= pct <= 60.0, (
            "%s占失分 %.1f%% —— 「几乎对半」这句话已经不成立了" % (label, pct))

    split_dom, merge_dom = [], []
    for r in ours:
        m_pct, s_pct = float(r["pct_over_merge"]), float(r["pct_over_split"])
        if s_pct > m_pct:
            split_dom.append(r["dataset"])
        elif m_pct > s_pct:
            merge_dom.append(r["dataset"])
    assert len(split_dom) >= 4 and len(merge_dom) >= 4, (
        "「主导方向因数据集相反」这句话要求两类都至少 4 个，实际 %s / %s"
        % (split_dom, merge_dom))
    bench = BENCH.read_text(encoding="utf-8")
    for ds in split_dom + merge_dom:
        assert ds in bench, "文档里没有点名 %s，而它是某个方向的例子" % ds


# ---------------------------------------------------------------------------
# 4. 阈值取舍曲线（表 F）：内部一致性
# ---------------------------------------------------------------------------
_SWEEP_ANCHOR = "表 F · 相似度阈值的取舍曲线"

#: 表头里的阈值列，按出现顺序
_SWEEP_THRESHOLDS = ["0.40", "0.70", "0.85", "0.90", "0.95"]


def _sweep_table() -> tuple[dict, list[list[str]]]:
    """拆曲线表 -> ({数据集: {"PA": [...], "GA": [...], "bold": [...]}}, 均值行)。

    均值行单独返回，因为**它的列语义和别的行不同**（只有 GA，没有 PA）——
    把它当普通数据行解析，第一版就会在 "GA" 那一格上找不到 "/"。
    """
    rows = _table_after(_SWEEP_ANCHOR)
    header = rows[0]
    assert header[1].strip() == "指标", "曲线表第二列必须是「指标」：%s" % header
    assert header[2:7] == _SWEEP_THRESHOLDS, (
        "阈值列变了（%s）—— 重跑 sweep 并同步 _SWEEP_THRESHOLDS" % header[2:7])

    # **先说清「表还没填完」这件事**，再谈解析。
    # 第一版没有这一步，于是待测格让判据报「不是 PA/GA 两格」——
    # 一个红得很干脆、却指向完全无关位置的消息。读者会去改解析器，
    # 而真正缺的是四个还没跑出来的数字。
    holes = [(r[0].strip(), _SWEEP_THRESHOLDS[j])
             for r in rows[2:] if r and r[0]
             for j in range(5) if "待测" in r[2 + j]]
    assert not holes, (
        "曲线表还有 %d 个待测格：%s —— 先跑 "
        "python scripts/sweep_threshold_pa_ga.py 再回填"
        % (len(holes), holes))

    out: dict = {}
    mean_rows: list[list[str]] = []
    for cells in rows[2:]:
        if not cells or not cells[0]:
            continue
        ds = cells[0].strip().replace("**", "")
        if cells[1].strip().replace("**", "") == "GA":
            mean_rows.append(cells)
            continue
        kind = cells[1].strip()
        assert kind == "PA / GA", "认的是「PA / GA」这一列：%r" % kind
        entry = out.setdefault(ds, {"PA": [], "GA": [], "bold": []})
        for j in range(5):
            cell = cells[2 + j]
            pa_ga = cell.replace("**", "")
            assert "/" in pa_ga, (
                "[%s@%s] 不是「PA / GA」两格：%r"
                % (ds, _SWEEP_THRESHOLDS[j], cell))
            pa_s, ga_s = [p.strip() for p in pa_ga.split("/", 1)]
            entry["PA"].append(_cell_number(pa_s))
            entry["GA"].append(_cell_number(ga_s))
            if "**" in cell:
                entry["bold"].append(j)
    assert out, "曲线表没有数据集行"
    assert len(mean_rows) == 1, "曲线表应当恰好一行「四集算术平均」，实际 %d 行" \
        % len(mean_rows)
    return out, mean_rows[0]


def _sweep_rows() -> dict:
    return _sweep_table()[0]


def _mean_row() -> dict:
    """均值行 -> {"GA": [五个数], "bold": [被加粗的列号]}。"""
    _, cells = _sweep_table()
    vals, bold = [], []
    for j in range(5):
        cell = cells[2 + j]
        n = _cell_number(cell.replace("**", ""))
        assert n is not None, "均值行第 %s 列不是数字：%r" % (_SWEEP_THRESHOLDS[j], cell)
        vals.append(n)
        if "**" in cell:
            bold.append(j)
    return {"GA": vals, "bold": bold}


def test_the_sweep_table_has_no_placeholder_cells():
    """**表里不许出现「待测」。**

    这条看起来多余，但它守的是一件很具体的事：一轮里没跑到的阈值组合
    很容易在草稿里先写成估算值，而表格**看起来完全正常**。真数字跑出来
    之前，那一格必须是「待测」。
    """
    rows = _table_after(_SWEEP_ANCHOR)
    for cells in rows[2:]:
        for j, cell in enumerate(cells):
            assert "待测" not in cell, (
                "曲线表第 %d 列还有「待测」：%r —— 跑完 "
                "scripts/sweep_threshold_pa_ga.py 再回填" % (2 + j, cell))


def test_the_bold_cell_marks_each_rows_own_peak():
    """每一行的加粗，必须落在**那一行自己**的 GA 最高点上。

    粗体是「这一行顶点在哪」的唯一标记；改一格数字而粗体没跟着动，
    读者就会照着一个错的顶点读下去。
    """
    swept = _sweep_rows()
    for ds, e in swept.items():
        best = max(range(5), key=lambda j: e["GA"][j])
        assert e["bold"] == [best], (
            "[%s] 加粗标在 %s，但最高点在 %s（%.2f vs %.2f）"
            % (ds,
               [_SWEEP_THRESHOLDS[j] for j in e["bold"]] or "没有加粗",
               _SWEEP_THRESHOLDS[best],
               e["GA"][e["bold"][0]] if e["bold"] else float("nan"),
               e["GA"][best]))


def test_the_mean_peaks_at_085():
    """文里那句「0.85 是四集算术平均的顶点」必须能从均值行算出来。

    判据读的是**表里的均值行**，不是某一行数据集 —— 「0.85 对某几个
    数据集好」与「0.85 对平均好」是两句话，混淆它们正是本节踩过的坑。
    """
    mean = _mean_row()
    best = max(range(5), key=lambda j: mean["GA"][j])
    assert _SWEEP_THRESHOLDS[best] == "0.85", (
        "均值行的 GA 最高点在 %s（%.2f），而文中说 0.85"
        % (_SWEEP_THRESHOLDS[best], mean["GA"][best]))
    assert mean["bold"] == [best], (
        "均值行的加粗在 %s，最高点在 %s"
        % ([_SWEEP_THRESHOLDS[j] for j in mean["bold"]],
           _SWEEP_THRESHOLDS[best]))
    # 均值行必须真的是那几行的算术平均 —— 否则它是一个自己编的数。
    # 各行 GA 是**两位小数**，所以 `real` 本身就是精确的，文档那一格是
    # 它四舍五入到两位的结果，正确容差就是半个末位 0.005（不是 0.05，
    # 也不是 0 —— 这三档都实测踩过）。
    swept = _sweep_rows()
    for j in range(5):
        real = sum(e["GA"][j] for e in swept.values()) / len(swept)
        assert abs(mean["GA"][j] - real) <= 0.005 + 1e-9, (
            "均值行在 %s 列写 %.2f，按四行重算是 %.4f"
            % (_SWEEP_THRESHOLDS[j], mean["GA"][j], real))


def test_not_every_dataset_peaks_at_085():
    """**这条守的是本节最核心、也最容易被写错的一句话。**

    文里说「0.85 是平均值顶点，**但没有一个数据集的顶点统一落在它上面**」。
    实测：Linux 的顶点在 0.70（60.20），比 0.85 的 23.25 高 36.95pp。

    如果哪天所有数据集真的都在 0.85 取到最高点，那句话就该改 ——
    而这条判据会在**改之前**先红，提醒人重新想清楚：是数据变了，
    还是结论写宽了。

    第一版把判据写成「每一行顶点必须在 0.85/0.90」，被 Linux 立刻打脸 ——
    **守卫自己写错了**，比没有守卫更糟：它会把正确的文档判红，
    于是下一个人会去改文档而不是改判据。
    """
    swept = _sweep_rows()
    peaks = {ds: max(range(5), key=lambda j: e["GA"][j])
             for ds, e in swept.items()}
    distinct = sorted({_SWEEP_THRESHOLDS[j] for j in peaks.values()})
    assert len(distinct) >= 2, (
        "所有数据集的 GA 顶点都落在 %s —— 「每个数据集的顶点位置不一样，"
        "所以不能靠调参解决」这句话已经不成立了（%s）"
        % (distinct[0] if len(distinct) == 1 else distinct, peaks))
    for ds, e in swept.items():
        # 每行的顶点必须真的明显高于别处，否则「顶点」这个词没有意义
        best = peaks[ds]
        second = max(e["GA"][j] for j in range(5) if j != best)
        assert e["GA"][best] - second > 0.1, (
            "[%s] 顶点 %.2f 与次高 %.2f 只差 %.2f —— 「顶点」不成立"
            % (ds, e["GA"][best], second, e["GA"][best] - second))


def test_pa_is_insensitive_to_the_threshold():
    """文里那句「PA 对阈值完全不敏感」必须真的成立。

    四个数据集一个百分点都没动 —— 所以判据取**全表**的最大跨度，
    超过 0.05pp 就说明那句话要改，而不是数字要「修」。
    """
    swept = _sweep_rows()
    worst_ds, worst = None, 0.0
    for ds, e in swept.items():
        span = max(e["PA"]) - min(e["PA"])
        if span > worst:
            worst, worst_ds = span, ds
    assert worst <= 0.05, (
        "[%s] 的 PA 在阈值间跨度 %.2fpp —— 「PA 对阈值完全不敏感」"
        "这句话已经不成立了" % (worst_ds, worst))
