# -*- coding: utf-8 -*-
"""v2.10.0：README 基准表必须与实测基线逐格对账 —— 以及这条门禁本身可失效。

它治的病
--------
`claim_readme_metrics` 校的是**工程指标**（行数 / 模块数 / 主张条数）。
README 里另一张更显眼的表 —— 十行数据集 × 六个百分数 —— **从来没有被任何
东西校过**。后果已经发生，不是理论上的：

    v2.9.9 修了 mask_text 的两处误伤（read/write、input/output 被误判成路径）。
    那次改动本身是对的，但它改变了聚类结果，于是

        HPC          76 簇 / 85.1%  ->  79 簇 / 81.1%
        Zookeeper    83 簇 / 92.1%  ->  83 簇 / 91.0%
        Proxifier   117 簇 / 81.8%  ->  115 簇 / 72.7%

    README、docs/BENCHMARK.md、「唯一负例只差 2 个事件」—— 全部原地不动，
    而六道门禁全绿。这与 v2.9.5 撤回的那次是同一个病：**对外数字，
    没有任何东西在校它。**

怎么修才不会复发
----------------
全量基准要跑两小时，进不了 CI，所以这里**不重跑，只对账**：

    docs/benchmark_baseline.json   <- --write-baseline 生成的实测快照
    README 的表                     <- 人类维护的展示层

改任何影响聚类的代码 -> 两者不一致 -> 这条主张变红。

本文件证明这条门禁**真的会响**：大半用例在故意把 README 或基线改坏，
断言它变红。一条恒为真的检查等于没有检查。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_REAL_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_BASELINE = _ROOT / "docs" / "benchmark_baseline.json"
_TABLE_ROW = re.compile(
    r"^\|\s*(?P<name>[A-Za-z0-9_]+)\s*\|(?P<events>\s*\d+\s*)\|"
    r"(?P<delivery>[^|]*)\|(?P<budget>[^|]*)\|(?P<full>[^|]*)\|"
    r"(?P<drain>[^|]*)\|(?P<logslop>[^|]*)\|", re.M)


def _load(script: str):
    path = _ROOT / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{path.stem}_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


prove = _load("prove.py")
bench = _load("benchmark_loghub.py")


# ---------------------------------------------------------------------------
# 先证真：基线存在、完整，且当前 README 与它一致
# ---------------------------------------------------------------------------
def test_baseline_file_exists():
    assert _BASELINE.is_file(), (
        f"缺 {_BASELINE.name}。跑：python scripts/benchmark_loghub.py "
        "--budgets 32000 --write-baseline")


def test_baseline_covers_every_dataset_in_the_readme_table():
    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    in_table = {m.group(1) for m in _TABLE_ROW.finditer(_REAL_README)}
    in_base = set(base["datasets"])
    assert in_table, "README 的基准表一行都没扫到 —— 正则或表头变了，先更新本测试"
    assert in_table <= in_base, (
        f"README 表里有、基线里没有：{sorted(in_table - in_base)}")


def test_baseline_records_both_baselines():
    """基线必须含两个对照臂，否则「胜负」无从对账。"""
    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    for name, rec in base["datasets"].items():
        assert rec["drain3"] is not None, f"{name} 缺 Drain3 对照"
        assert rec["logslop"] is not None, f"{name} 缺 LogsLop 对照"


def test_current_readme_matches_baseline():
    """先证真：照现状跑是绿的。后面几条变红才有意义。"""
    assert prove.claim_benchmark_table()["holds"] is True


# ---------------------------------------------------------------------------
# 可失效性：把展示层或基线改坏，门禁必须变红
# ---------------------------------------------------------------------------
@pytest.fixture
def gate(tmp_path, monkeypatch):
    """把 prove.ROOT 指向一个只含 README.md + 基线的临时目录。"""

    def _run(readme_text: str, baseline_obj) -> dict:
        (tmp_path / "README.md").write_text(readme_text, encoding="utf-8")
        if baseline_obj is None:
            (tmp_path / "docs").mkdir(exist_ok=True)
        else:
            (tmp_path / "docs").mkdir(exist_ok=True)
            (tmp_path / "docs" / "benchmark_baseline.json").write_text(
                json.dumps(baseline_obj, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(prove, "ROOT", tmp_path)
        return prove.claim_benchmark_table()

    return _run


_REAL_BASE = json.loads(_BASELINE.read_text(encoding="utf-8"))


def test_gate_is_not_a_noop(gate):
    """把 README 里一个百分数改掉 0.1pp，门禁必须变红。"""
    row = _TABLE_ROW.search(_REAL_README)
    assert row, "README 的基准表格式变了，先更新本测试"
    ds, _events, delivery, budget, _full, _d, _l = row.groups()

    def bump(cell: str) -> str:
        m = re.search(r"(-?[\d.]+)\s*%", cell)
        assert m, f"这一格不是百分数：{cell!r}"
        return f"{float(m.group(1)) + 0.1:.1f}%"

    broken_cell = bump(budget)
    at = _REAL_README.find(budget)
    broken = (_REAL_README[:at] + broken_cell
              + _REAL_README[at + len(budget):])

    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert ds in c["detail"], "失败信息必须点名是哪个数据集"
    assert "同预算" in c["detail"]


def _bump_cell(text: str, m, group: str, delta: float = 0.2) -> str:
    """把 ``text`` 里第 ``m`` 次匹配那一行的 ``group`` 单元格调高 ``delta`` 个百分点。

    **按 span 偏移拼接，不能按单元格文本替换。** 这一条踩了两层坑：

    1. 拿单元格文本去全文 ``find``：表里「72.7%」出现两次（Proxifier 的交付列
       与 Drain3 列），改到的是**交付列**。
    2. 改成 ``row.replace(cell, bumped_cell, 1)`` 仍然错：同一行里交付列与
       Drain3 列的文本**就是同一个** ``" 72.7%"``，``replace(..., 1)`` 改的
       还是第一处。

    两层都表现为「这条测试自己绿着，而它守的那一列压根没被改」——
    与「用自己举的例子跑一遍判据」是同一类错。

    另外注意 ``Match.span(group)`` 返回的是**相对整篇文本的绝对偏移**，
    不是相对 ``m.group(0)``；再叠一次 ``m.start()`` 会翻倍，落到无关文本上
    （这里的表现是「delivery 列不是百分数写法：' 3.12 三'」——
    '三' 字来自 CI 矩阵那一行）。
    """
    cell_start, cell_end = m.span(group)
    cell = text[cell_start:cell_end]
    num = re.search(r"(-?[\d.]+)\s*%", cell)
    assert num, f"{group} 列不是百分数写法：{cell!r}"
    bumped = cell.replace(num.group(1),
                           f"{float(num.group(1)) + delta:.1f}", 1)
    assert bumped != cell, f"{group} 列没能改掉"
    return text[:cell_start] + bumped + text[cell_end:]


def test_drift_in_every_column_is_caught(gate):
    """六个百分数每一列都要校 —— 只校第一列等于其余五列可以随便漂。"""
    m = next(_TABLE_ROW.finditer(_REAL_README))
    groups = ("delivery", "budget", "full", "drain", "logslop")
    labels = ("交付", "同预算", "全量", "Drain3", "LogsLop")

    # 先证真：不改任何东西时门禁是绿的
    assert gate(_REAL_README, _REAL_BASE)["holds"] is True

    for label, group in zip(labels, groups):
        broken = _bump_cell(_REAL_README, m, group)
        assert broken != _REAL_README, f"{label} 列没能落到 README 里"

        c = gate(broken, _REAL_BASE)
        assert c["holds"] is False, f"{label} 列漂移了 0.2pp 却没被发现"
        assert label in c["detail"], (
            f"{label} 列的失败信息没点名该列（说明改的不是那一列）："
            f"{c['detail']}")


def test_dataset_missing_from_baseline_is_caught(gate):
    """README 多出一个基线里没有的数据集 —— 不能因为「扫不到就不判」。"""
    broken = _REAL_README + (
        "\n| Ghost | 10 | 50.0% | **50.0%** | 50.0% | 40.0% | 10.0% |\n")
    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert "Ghost" in c["detail"]


def test_event_count_drift_is_caught(gate):
    """标注事件数也是表里的一列，同样要对账。"""
    row = _TABLE_ROW.search(_REAL_README)
    ds, events, delivery, budget, full, drain, logslop = row.groups()
    bumped = str(int(events.strip()) + 1)
    at = _REAL_README.find(events)
    broken = (_REAL_README[:at] + f" {bumped} "
              + _REAL_README[at + len(events):])
    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert "标注事件" in c["detail"]


def test_missing_baseline_fails_loudly(gate):
    """基线文件缺失必须变红，且给出**可执行**的重跑命令。

    「没有基线所以跳过」是最危险的失败模式：门禁绿着，而它根本没比过。
    """
    c = gate(_REAL_README, None)
    assert c["holds"] is False
    assert "--write-baseline" in c["detail"]


def test_empty_readme_table_fails_loudly(gate):
    """表被整块删掉也不能绿 —— 正则扫不到行不等于「没有漂移」。"""
    stripped = re.sub(r"^\|[^\n]*\n", "", _REAL_README, flags=re.M)
    assert stripped != _REAL_README
    c = gate(stripped, _REAL_BASE)
    assert c["holds"] is False


# ---------------------------------------------------------------------------
# --write-baseline 的拒绝条件：残缺基线比没有基线更糟
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv, why", [
    (["--budgets", "32000", "--write-baseline", "--no-drain3"], "缺 Drain3 对照臂"),
    (["--budgets", "32000", "--write-baseline", "--no-logslop"], "缺 LogsLop 对照臂"),
    (["--budgets", "32000", "--write-baseline", "--datasets", "HPC"], "只跑了子集"),
])
def test_write_baseline_refuses_incomplete_runs(argv, why, monkeypatch, capsys):
    """前置条件在**跑之前**就查 —— 跑完两小时才发现白跑更糟。"""
    monkeypatch.setattr(sys, "argv", ["benchmark_loghub.py"] + argv)
    assert bench.main() == 1, f"{why} 时居然写出了基线"
    assert "拒绝写基线" in capsys.readouterr().out
