# -*- coding: utf-8 -*-
"""scripts/diagnose_ga_gap.py 的测试。

**为什么这个分解必须有守卫**
--------------------------
`grouping_accuracy()` 报的是**一个**数，`decompose()` 报的是**三摊**。
如果两者对「哪些行算正确」的判断有一天不一致，那么这张分解表就在描述
**另一个量** —— 而它会被当成「GA 为什么低」的答案写进文档。

这正是本项目记过多次的形状：**判据指错了对象，却看起来完全正常**。
所以下面两条是硬的：

1. `decompose()` 的 `correct` 必须**逐位等于** `grouping_accuracy()` 的命中数；
2. 三摊之和恒等于 `total_lines - correct`（漏掉的那一行会让比例加不到 100%，
   而那恰好是最容易被忽略的错 —— 表上三个百分比加起来 97%，一眼看不出来）。

不需要数据集，纯离线。

**还有两条守着 sweep 脚本**
--------------------------
它只能**测量**，不能改产品。一个「顺手把默认值调成曲线顶点」的诊断脚本，
读起来仍然完全正常 —— 而它会让下一个看那张表的人以为那个默认是本来就有的。
"""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "diagnose_ga_gap", ROOT / "scripts" / "diagnose_ga_gap.py")
assert _spec and _spec.loader
gap = importlib.util.module_from_spec(_spec)
sys.modules["diagnose_ga_gap"] = gap
_spec.loader.exec_module(gap)

from eval_logparser_pa_ga import grouping_accuracy                  # noqa: E402


# ---------------------------------------------------------------------------
# 手算样例
# ---------------------------------------------------------------------------
def test_decompose_on_a_hand_computable_example():
    """三个事件、四个簇，把每一行的去处都摆出来。

        E1 -> 行 1,2,3,4    全在 c0，且 c0 正好是纯的 E1 全集 -> 正确 4 行
        E2 -> 行 5,6,7      摊在 c1(5,6) 与 c2(7) -> 两个都是条件 b 失败
        E3 -> 行 8,9,10     8,9 在 c3（纯但没收全），10 压根没解析

    于是：正确 4 / 过分裂 (2+1+2)=5 / 过合并 0 / 未解析 1，合计 10。

    **过分裂为什么是 5 而不是 2**：条件 (b) 的判据是「这个簇是不是该 EventId
    的**全部**出现」，所以一个被摊成 k 份的事件会贡献 **k 份**失分，而不是
    「每个事件算一次」。这也是过分裂在高频事件上格外痛的原因 —— 一个重复
    几千行的事件被摊成 20 簇，那几千行就全丢了。
    """
    gt = {1: "E1", 2: "E1", 3: "E1", 4: "E1",
          5: "E2", 6: "E2", 7: "E2",
          8: "E3", 9: "E3", 10: "E3"}
    pred = {1: "c0", 2: "c0", 3: "c0", 4: "c0",
            5: "c1", 6: "c1",
            7: "c2",
            8: "c3", 9: "c3"}
    d = gap.decompose(gt, pred, 10)
    assert d["correct"] == 4, "E1 的 4 行是唯一一组既纯又收全的"
    assert d["over_split"] == 5, "c1(2) + c2(1) + c3(2) 都丢了条件 (b)"
    assert d["over_merge"] == 0, "这个样例里没有混簇"
    assert d["unparsed"] == 1, "第 10 行不在任何簇里"
    assert d["correct"] + d["over_split"] + d["over_merge"] + d["unparsed"] == 10


def test_decompose_counts_a_mixed_cluster_as_over_merge():
    """一个簇里混了 E2 与 E3 -> 整簇的行都记在过合并，不拆到别处。"""
    gt = {1: "E1", 2: "E1", 3: "E2", 4: "E3"}
    pred = {1: "c0", 2: "c0", 3: "c1", 4: "c1"}      # c1 混了 E2/E3
    d = gap.decompose(gt, pred, 4)
    assert d["correct"] == 2, "E1 两行正确"
    assert d["over_merge"] == 2, "c1 的两行全算过合并"
    assert d["over_split"] == 0


def test_decompose_reports_unparsed_lines_separately():
    """没进任何簇的标注行既不是过合并也不是过分裂，必须单独一摊。

    否则它会被前两摊之外的东西吸收掉，而表上看不出来 —— 分母是标注总行数，
    未解析的行本来就留在分母里算错。
    """
    gt = {1: "E1", 2: "E1", 3: "E2"}
    pred = {1: "c0", 2: "c0"}                          # 第 3 行没解析
    d = gap.decompose(gt, pred, 3)
    assert d["correct"] == 2
    assert d["unparsed"] == 1
    assert d["over_merge"] == 0 and d["over_split"] == 0


# ---------------------------------------------------------------------------
# 两条硬约束
# ---------------------------------------------------------------------------
def _random_case(rng):
    """随机造一个 (地真值, 预测分组, 总行数) 三元组。

    故意让事件数少而行数多 —— Loghub 上真实数据集正是这个形状
    （一个事件重复成千上万次），而那正是过分裂最痛的地方。
    """
    total = rng.randint(20, 200)
    n_events = rng.randint(2, 6)
    events = ["E%d" % i for i in range(n_events)]
    gt = {i: rng.choice(events) for i in range(1, total + 1)}
    n_clusters = rng.randint(1, 12)
    pred = {}
    for i in range(1, total + 1):
        if rng.random() < 0.05:                 # 少量行不解析
            continue
        pred[i] = "c%d" % rng.randrange(n_clusters)
    return gt, pred, total


@pytest.mark.parametrize("seed", [1, 7, 13, 29, 101, 997])
def test_decompose_agrees_with_grouping_accuracy_on_correct_count(seed):
    """**这条是核心**：分解表描述的必须是被发布那个 GA 的同一个量。

    只要 `decompose()["correct"]` 与 `grouping_accuracy()` 的命中数有一天
    不相等，那这张表就在讲另一个指标的故事，而它长得完全正常。
    """
    rng = random.Random(seed)
    gt, pred, total = _random_case(rng)
    ga, ga_lines = grouping_accuracy(gt, pred, total)
    d = gap.decompose(gt, pred, total)
    assert d["correct"] == ga_lines, (
        "命中行数不一致：grouping_accuracy 报 %d，decompose 报 %d —— "
        "两者描述的不是同一个量" % (ga_lines, d["correct"]))
    assert abs(ga * total - ga_lines) < 1e-6


@pytest.mark.parametrize("seed", [2, 8, 14, 30, 102, 998])
def test_the_three_buckets_always_add_up_to_the_lost_lines(seed):
    """三摊之和 == total_lines - correct。

    漏掉一摊不会让任何一条变红，只会让表上的百分比**加起来不到 100%** ——
    而那正是最容易被当成「四舍五入」放过去的那种错。
    """
    rng = random.Random(seed)
    gt, pred, total = _random_case(rng)
    d = gap.decompose(gt, pred, total)
    parts = d["over_merge"] + d["over_split"] + d["unparsed"]
    assert parts == d["lost"], (
        "三摊合计 %d != 失分 %d（过合并 %d + 过分裂 %d + 未解析 %d）"
        % (parts, d["lost"], d["over_merge"], d["over_split"], d["unparsed"]))
    assert d["correct"] + d["lost"] == total


def test_a_perfect_prediction_loses_nothing():
    """完美预测时三摊必须全 0 —— 否则分解把正确的行也算成了错的。"""
    gt = {1: "E1", 2: "E1", 3: "E2", 4: "E2"}
    pred = {1: "c0", 2: "c0", 3: "c1", 4: "c1"}
    d = gap.decompose(gt, pred, 4)
    assert d == {"correct": 4, "over_merge": 0, "over_split": 0,
                 "unparsed": 0, "lost": 0}


# ---------------------------------------------------------------------------
# sweep 脚本只许测量，不许改产品
# ---------------------------------------------------------------------------
def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / ("%s.py" % name))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_importing_the_sweep_does_not_move_the_product_default():
    """导入 sweep 之后，产品的相似度阈值必须**一个数都没变**。

    判据读的是 `clustering` 模块的属性（对象），不是它的源码文本 ——
    「源码里没出现赋值」这种文本搜索，在脚本里写一句
    「# 只是展示一下 0.85 会怎样」都能糊弄过去。
    """
    from log_ai_compressor.core import clustering as C
    before = C.CLUSTER_SIMILARITY_THRESHOLD
    _load("sweep_threshold_pa_ga")
    assert C.CLUSTER_SIMILARITY_THRESHOLD == before, (
        "sweep 脚本动了产品默认值：%s -> %s"
        % (before, C.CLUSTER_SIMILARITY_THRESHOLD))
    # 注意这里**不断言它等于 0.85**：那条会把这个测试变成「默认不许变」的
    # 看门狗，而默认该不该变是产品判断。第一版写了 `== 0.85`，那会在一次
    # 合法的默认值变更上，让这条测试因为错误的理由变红。


def test_sweep_passes_the_threshold_per_instance():
    """它必须把阈值传给 `ErrorClusterer(...)`，而不是靠改全局。

    `ErrorClusterer` 的 `similarity_threshold` 是构造参数 —— 这条断言
    钉住「用构造参数」这个做法本身：哪天有人改成改全局，这条就红。
    """
    import inspect
    from log_ai_compressor.core.clustering import ErrorClusterer
    sig = inspect.signature(ErrorClusterer.__init__)
    assert "similarity_threshold" in sig.parameters, (
        "ErrorClusterer 不再接受按实例的阈值 —— sweep 就只能改全局了，"
        "那正是它被禁止做的事")
    sweep_src = (ROOT / "scripts" / "sweep_threshold_pa_ga.py").read_text(
        encoding="utf-8")
    assert "ErrorClusterer(similarity_threshold=" in sweep_src, (
        "sweep 没有用按实例的阈值")


# ---------------------------------------------------------------------------
# CSV：文档里的每一格都要能被它逐格对账
# ---------------------------------------------------------------------------
def _csv_fixture_rows():
    """一行我方 + 一行对照臂，直接喂给 `_csv_rows`，不碰数据集。"""
    ours_dec = gap.decompose(
        {1: "E1", 2: "E1", 3: "E2"}, {1: "c0", 2: "c0"}, 3)
    drain_dec = gap.decompose(
        {1: "E1", 2: "E1", 3: "E2", 4: "E2"},
        {1: "c0", 2: "c0", 3: "c1", 4: "c2"}, 4)
    return [{
        "dataset": "T",
        "ours": {"GA": 66.67, "annotated_lines": 3},
        "dec": ours_dec,
        "drain": {"GA": 50.0, "annotated_lines": 4},
        "drain_dec": drain_dec,
    }]


def test_csv_columns_are_declared_not_dumped():
    """列必须**写死**，不能靠把 `decompose()` 的键 dump 出来。

    那样每加一个内部字段（`_pred_group` / `_gt_event` 那两个是**整份分组
    字典**）就会悄悄改变 CSV 形状：要么多出没人认的列，要么一个几十万项的
    字典被写进文件。这条断言让「CSV 形状」成为需要**主动**改的东西。
    """
    assert gap.CSV_COLS == (
        "dataset", "method", "GA", "annotated_lines", "correct",
        "over_merge", "over_split", "unparsed", "lost",
        "pct_over_merge", "pct_over_split", "pct_unparsed")


def test_csv_rows_carry_no_private_keys():
    """每一行的键必须**恰好**是那十二列。

    `csv.DictWriter` 带 `extrasaction="ignore"`，多余键会被丢掉 ——
    也就是说泄露在 CSV 文件里看不见。判据必须落在**行本身**上。
    """
    for row in gap._csv_rows(_csv_fixture_rows()):
        assert set(row) == set(gap.CSV_COLS), (
            "CSV 行的键与列不符，多出来的是内部字段：%s"
            % sorted(set(row) - set(gap.CSV_COLS)))


def test_csv_percentages_are_fractions_of_the_lost_lines():
    """三个百分比必须加起来约等于 100（失分为 0 时全 0）。

    这一条看着像废话，但它是「文档里的百分比与 CSV 对得上」的前提：
    文档写的是**占失分**的百分比，不是占标注行数的百分比。
    """
    for row in gap._csv_rows(_csv_fixture_rows()):
        total = (row["pct_over_merge"] + row["pct_over_split"]
                 + row["pct_unparsed"])
        assert abs(total - 100.0) < 0.02 or row["lost"] == 0, row


def test_csv_omits_the_control_arm_when_drain3_is_absent():
    """本机没装 drain3 时那一行不能出现 —— 也不能因为缺键而崩。

    判据读的是**结构**（有没有 `drain_dec`），不是「源码里出现过
    `drain_dec`」这种文本搜索。
    """
    rows = _csv_fixture_rows()
    del rows[0]["drain_dec"]
    del rows[0]["drain"]
    flat = gap._csv_rows(rows)
    assert [r["method"] for r in flat] == ["ours"], flat


def test_diagnose_ga_gap_exposes_a_csv_flag():
    """`--csv` 是文档对账链的入口，少了它 BENCHMARK.md 那张表就没人算。"""
    src = (ROOT / "scripts" / "diagnose_ga_gap.py").read_text(encoding="utf-8")
    assert '"--csv"' in src, "diagnose_ga_gap.py 没有 --csv"
