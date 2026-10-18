# -*- coding: utf-8 -*-
"""三档表必须能从原始实测 CSV 逐格重算 —— 形状同 `test_benchmark_table_gate.py`。

**为什么这张表必须有门禁**
--------------------
它是 `docs/BENCHMARK.md` 里唯一一张**主基准脚本不生成**的表。上一版的十行
基准表被 `prove.py` 逐格对账 `benchmark_baseline.json`，所以它改错了会红；
这一张如果只靠人抄，就会变成 v2.9.9 那个事故的重演 —— 数字变了、文档原地
不动、六道门禁全绿。

所以判据是**反向**的：不看文档声称什么，直接从 `docs/similarity_tiers.csv`
（`scripts/benchmark_similarity_tiers.py --csv` 的原始输出）重算，再逐格比。
连汇总的「独占最优 8/10」也从 CSV 算，而不是从文档读。

**负向**：把 CSV 里的一个数改掉，表就必须对不上。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "docs" / "similarity_tiers.csv"
BENCHMARK = ROOT / "docs" / "BENCHMARK.md"

TIERS = ("strict", "standard", "lenient")
SECTION_HEAD = "### 三档相似度全测"


@pytest.fixture(scope="module")
def measured() -> dict:
    """原始实测：{(dataset, tier): row}。"""
    assert CSV_PATH.is_file(), (
        "docs/similarity_tiers.csv 不存在 —— 三档表的原始实测没随文档走。"
        "先跑 python scripts/benchmark_similarity_tiers.py --csv docs/similarity_tiers.csv")
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows, "similarity_tiers.csv 是空的"
    return {(r["dataset"], r["tier"]): r for r in rows}


@pytest.fixture(scope="module")
def section() -> str:
    text = BENCHMARK.read_text(encoding="utf-8")
    start = text.index(SECTION_HEAD)
    tail = text[start + len(SECTION_HEAD):]
    end = tail.find("\n### ")
    return tail[:end] if end > 0 else tail


def _pct(s: str) -> float:
    return float(s.replace("%", "").replace("*", "").strip())


def _split_tables(section: str, first_col_values: set[str]) -> list[list[dict]]:
    """把节里的 markdown 表按「表头」切开，返回每张表的数据行。

    为什么不能只按数据集抓行就完事：这一节里**先后有两张表**都以数据集
    开头（交付口径、全量口径），只抓「名字匹配的行」会让后一张覆盖前一张
    —— 于是拿全量数字去比交付数字，**每格都错，而看起来判据在正常工作**。
    第一版就是这么写的。

    所以判据必须认表头：带「标注事件」的那张是交付口径，另一张是全量口径。
    """
    tables: list[list[dict]] = []
    current: list[dict] | None = None
    header_has_events = False
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "数据集":
            header_has_events = any("标注事件" in c for c in cells)
            current = []
            tables.append(current)
            continue
        if current is None or not cells or cells[0] not in first_col_values:
            continue
        # 逐格抓 N.N% —— 比按位置切稳，表格加列也不会错位
        current.append({"dataset": cells[0],
                        "values": [float(m) for m in re.findall(r"(\d+\.\d)%", line)],
                        "is_delivered_table": header_has_events})
    return [t for t in tables if t]


def _row_map(section: str, datasets: set[str], delivered: bool) -> dict[str, list[float]]:
    for tbl in _split_tables(section, datasets):
        if not all(r["is_delivered_table"] == delivered for r in tbl):
            continue
        out = {r["dataset"]: r["values"] for r in tbl}
        if out:
            return out
    return {}


class TestTierTableMatchesRawMeasurement:
    def test_every_dataset_and_tier_is_present(self, measured, section):
        datasets = {d for d, _ in measured}
        delivered = _row_map(section, datasets, delivered=True)
        assert set(delivered) == datasets, (
            f"交付口径那张表缺数据集：{sorted(datasets - set(delivered))}")
        for d, row in delivered.items():
            assert len(row) == len(TIERS), (
                f"{d} 那一行是 {len(row)} 个百分数，应为 {len(TIERS)} 个"
                f"（strict / standard / lenient 各一格）")

    def test_delivered_metric_matches_csv(self, measured, section):
        datasets = {d for d, _ in measured}
        rows = _row_map(section, datasets, delivered=True)
        assert rows, "节里找不到交付口径那张表（表头带「标注事件」）"
        bad = []
        for d, nums in rows.items():
            if len(nums) != len(TIERS):
                continue
            for tier, claimed in zip(TIERS, nums):
                real = float(measured[(d, tier)]["delivered_coverage"])
                if abs(claimed - real) > 0.05:
                    bad.append(f"{d}/{tier}: 文档 {claimed} vs 实测 {real}")
        assert not bad, "交付口径这几格对不上原始实测：" + "；".join(bad)

    def test_full_metric_matches_csv(self, measured, section):
        datasets = {d for d, _ in measured}
        rows = _row_map(section, datasets, delivered=False)
        assert rows, "节里找不到全量口径那张表"
        bad = []
        for d, nums in rows.items():
            if len(nums) != len(TIERS):
                continue
            for tier, claimed in zip(TIERS, nums):
                real = float(measured[(d, tier)]["full_coverage"])
                if abs(claimed - real) > 0.05:
                    bad.append(f"{d}/{tier}: 文档 {claimed} vs 实测 {real}")
        assert not bad, "全量口径这几格对不上原始实测：" + "；".join(bad)


class TestTierTableSummaryIsDerived:
    """汇总那一行也必须是从 CSV 算出来的，不是人写的。"""

    @staticmethod
    def _exclusive_wins(measured, key) -> dict:
        wins = {t: 0 for t in TIERS}
        by_ds: dict[str, dict] = {}
        for (d, t), r in measured.items():
            by_ds.setdefault(d, {})[t] = float(r[key])
        for per_tier in by_ds.values():
            best = max(per_tier.values())
            winners = [t for t in TIERS if abs(per_tier[t] - best) < 1e-9]
            if len(winners) == 1:
                wins[winners[0]] += 1
        return wins

    def test_summary_row_matches_recomputation(self, measured, section):
        """文档写的「8 / 0 / 0」与「0 / 0 / 5」必须等于从 CSV 重算的结果。

        这一条是整份守卫里最关键的一条：汇总是**最容易被顺手改掉**的一行 ——
        逐格数字对了，胜负被写成 9 胜，没人发现。v2.9.9 就干过一次。
        """
        lines = [ln.strip() for ln in section.splitlines()]
        full_line = next((ln for ln in lines
                          if ln.startswith("| 全量口径独占最优")), None)
        deliv_line = next((ln for ln in lines
                           if ln.startswith("| 交付口径独占最优")), None)
        assert full_line and deliv_line, "三档表缺汇总行（独占最优）"

        for line, key in ((full_line, "full_coverage"),
                          (deliv_line, "delivered_coverage")):
            wins = self._exclusive_wins(measured, key)
            cells = [c.strip() for c in line.strip("|").split("|")]
            # 单元格里写的是 `**8 / 10**` 或裸的 `0` / `5` —— 取分子那���个数
            nums = [int(re.search(r"\d+", c).group()) for c in cells[1:] if c]
            assert nums == [wins[t] for t in TIERS], (
                f"汇总行写着 {nums}，从 CSV 重算是 "
                f"{[wins[t] for t in TIERS]}（{key}）—— 逐格对上了而胜负没对上，"
                "那是最容易漏的一种漂移")


class TestTierTableCarriesItsOwnLimitations:
    """这张表最容易的误读是「strict 97.3% 说明它更好」—— 必须写清不是。"""

    def test_it_states_strict_wins_delivered_zero_times(self, section):
        assert "0 / 10" in section, (
            "表里没有「交付口径 strict 0 次最优」这一格 —— "
            "只列全量口径会让人读成「严格档其实更好」")
        assert "一次都没赢" in section or "0 次" in section, (
            "缺少那句结论：严格档在全量口径赢、在交付口径一次都没赢")

    def test_it_refuses_to_retune_the_default(self, section):
        """交付口径的赢家是 lenient，而本项目**不能**因此改默认值。

        改了就正是 §2.3 禁止的「对着评测集调参」，而且会让簇更粗、
        根因更不准 —— 指标涨了产品变坏（形态六）。这一段必须在文档里。

        判据认的是**那句话本身**。第一版写的是「这一节里有没有 §2.3 /
        形态六」—— 于是把「不会因此改默认值」删掉，它照样绿：判据在看
        邻居，不在被守的东西。已用「删掉那句话」实测过它会红。
        """
        assert "不会因此改默认值" in section, (
            "三档表缺「不会因此改默认值」这句声明 —— 而这恰恰是这张表最容易被"
            "读成「建议改默认值」的地方（交付口径的赢家不是当前默认值）")
        assert "§2.3" in section, (
            "缺与「不许对着基准调参」的关联 —— 没有它，读者不知道为什么"
            "明知 lenient 更好却不改")
        assert "形态六" in section, (
            "缺少与形态六的关联：lenient 赢在覆盖率上，是因为簇更粗，"
            "根因会更不准 —— 这就是「指标涨了产品变坏了」")
