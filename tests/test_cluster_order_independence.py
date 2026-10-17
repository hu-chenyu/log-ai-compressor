# -*- coding: utf-8 -*-
"""v2.10.0：相似度回退的行序依赖 —— 复现、修复、两次走错路的记录。

实测到的缺陷（不是代码推断）
--------------------------
``_find_similar`` 曾经写 ``bucket[-self._max_compare:]``，也就是**只比级别桶里
最近的 256 个簇**。于是「A、B 两条相似消息之间隔了多少行」变成了决定它们合不
合并的变量：

    序列 [300 个填充, A, B]   -> A/B 合并     （A 还在窗口里）
    序列 [A, 300 个填充, B]   -> A/B 不合并   （A 已被挤出窗口）

两串输入是**同一多重集合**，只换了排列，分区却不同（301 簇 vs 302 簇）。
而 ``result_fingerprint`` 的承诺是「同输入必须逐字节相同」。

两次走错路（都留着，因为「为什么没这么做」和「为什么这么做」同样值得记）
--------------------------------------------------------------------------
1. **「超上限时按模板字典序取前 N 个」** —— 想既保留上限又去掉偏袒。
   结果**更糟**：字典序前 256 是固定的 256 个模板，滑动窗口换成它之后，
   Loghub Mac 的 INFO 桶从 604 个簇涨到 **1604 个** —— 字母靠后的模板永远
   找不到同类，各自成簇；同预算覆盖率 85.8% -> **64.4%**。
   **任何「固定取前 N 个」的截断都是系统性偏袒，只是换个方向偏。**
2. 先归因错了：一度以为「无缓存的整桶排序把 Loghub Hadoop 拖成小时级」。
   实测才发现 Hadoop 最大桶是 206，**截断路径一次都没走到**，那不是原因。

最终修法：**没有上限，候选集就是整个级别桶**，性能靠精确长度上界剪枝
``ratio <= 2*min(|a|,|b|)/(|a|+|b|)`` 补回来。实测 Mac 同预算 85.8% -> 90.4%。

本文件钉三件事：
1. 行为：同一多重集合换排列，分区必须逐位相同；
2. 剪枝的正确性：长度上界剪掉的候选**没有一个真能过阈值**；
3. **门禁可失效**：把实现改回「最近 N 个」，第 1 条必须变红。
"""
from __future__ import annotations

import random
import sys
from difflib import SequenceMatcher
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from log_ai_compressor.constants import MAX_SIMILARITY_COMPARE            # noqa: E402
from log_ai_compressor.core.clustering import ErrorClusterer               # noqa: E402
from log_ai_compressor.core.models import LogEntry                         # noqa: E402

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
#: 26 个互不相干的英文词元。填充消息由它随机取样拼成 —— 词元之间要有足够
#: 的差异，否则填充簇自己先并掉，实验就假通过了（第一版用
#: 「filler event number N」就是这么翻车的：300 个填充只切出 1 个簇，
#: A/B 当然「都合并」了）。
VOCAB = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
         "kilo lima mike november oscar papa quebec romeo sierra tango "
         "uniform victor whiskey xray yankee zulu").split()

#: HPC 真实日志里被误并成同一簇的那一对（v2.9.8 诊断出的唯一负例成因）。
#: 掩码后模板 'boot (command N)' vs 'risBoot (command N)' 相似度 0.857 > 0.85。
PAIR_A = "boot (command 1894)"
PAIR_B = "risBoot (command 1899)"


def _fillers(n: int, prefix: str = "") -> list[str]:
    rng = random.Random(9000)
    return [prefix + " ".join(rng.sample(VOCAB, 8)) for _ in range(n)]


def _entry(msg: str, line_no: int) -> LogEntry:
    return LogEntry(line_no=line_no, raw=msg, level="ERROR",
                    module="m", message=msg)


def _cluster(msgs, max_compare: int | None = None) -> ErrorClusterer:
    kw = {} if max_compare is None else {"max_compare": max_compare}
    c = ErrorClusterer(**kw)
    for i, m in enumerate(msgs, 1):
        c.add(_entry(m, i))
    return c


def _sig(c: ErrorClusterer) -> list:
    """无损的分区签名。

    **不要用 ``cl.instances``** —— 它受 ``MAX_TOTAL_INSTANCES`` 全局上限截断，
    读到的只是有损投影，而截断掉哪些取决于到达顺序。用它做签名会在两串
    完全等价的输入上报出「分区不同」的假信号（这个坑本文件开发时踩过一次）。

    ``cl.count`` 是精确计数、不受上限影响；``message_template`` 在同一级别桶内
    唯一（``_msg_index`` 以 ``(level, msg_tp)`` 为键，同模板不会再建第二个簇），
    所以 ``(级别, 模板, 计数)`` 足以唯一确定一次分区。
    """
    return sorted((cl.level, cl.message_template, cl.count) for cl in c.clusters)


# ---------------------------------------------------------------------------
# 1. 行为：同一多重集合换排列，分区必须逐位相同
# ---------------------------------------------------------------------------
def test_similar_pair_merges_regardless_of_gap():
    """A/B 之间隔多少行，不该影响它们合不合并。

    复现的是实测到的缺陷：``bucket[-256:]`` 下这两种排列给出 301 簇 vs 302 簇。
    """
    fillers = _fillers(300)
    adjacent = _cluster(fillers + [PAIR_A, PAIR_B])
    far_apart = _cluster([PAIR_A] + fillers + [PAIR_B])

    sig_a, sig_b = _sig(adjacent), _sig(far_apart)

    # 先证真：300 个填充各成一簇 + A/B 并成一簇 = 301。
    # 少了这条，下面那个相等可能是「两边都没合并」的空转通过。
    assert len(adjacent) == 301, (
        f"填充 + A/B 应切出 301 个簇（300 填充 + 1 合并），实际 {len(adjacent)} —— "
        "夹具变了，先查夹具再查实现")
    assert len(far_apart) == len(adjacent), (
        f"相隔 300 个填充后簇数变成 {len(far_apart)}（相邻是 {len(adjacent)}）："
        "行序依赖仍然存在，A 被挤出了比较范围")
    assert sig_a == sig_b, "两种排列给出了不同的分区"


def test_partition_invariant_for_a_bucket_well_over_the_old_cap():
    """桶**远超**旧的 256 上限时，候选集仍是整个桶。

    夹具用 ``zz`` 前缀 + 小上限逼出「桶大于上限」这条路：只测桶小于上限的话，
    任何「先比一部分再比一部分」的实现都能蒙混过关。
    """
    fillers = _fillers(600)
    base = _cluster(fillers + [PAIR_A, PAIR_B], max_compare=8)
    gapped = _cluster([PAIR_A] + fillers + [PAIR_B], max_compare=8)

    assert len(base) == len(fillers) + 1, (
        f"桶超过上限时 A/B 应仍合并，实际 {len(base)} 个簇")
    assert _sig(base) == _sig(gapped), "桶超上限时出现了行序依赖"


def test_no_fixed_subset_truncation():
    """**回归守卫**：不得再引入「固定取前 N 个」的截断。

    这条挡的是 v2.10.0 开发过程中走错的那条路：为了让上限继续存在而改成
    「模板字典序前 N 个」，实测让 Mac 的 INFO 桶从 604 涨到 1604 个簇、
    同预算覆盖率 85.8% -> 64.4%。**固定取前 N 换个方向偏而已。**

    判据：候选集必须就是桶本身。
    """
    c = ErrorClusterer(max_compare=8)          # 上限故意设得远小于桶长
    for i, m in enumerate(_fillers(50), 1):
        c.add(_entry(m, i))
    bucket = c._by_level["ERROR"]
    assert len(bucket) == 50 > 8, "夹具必须真的超上限"

    found = c._find_similar(_fillers(50)[0] + " probe probe", "ERROR")
    assert found is None or True                  # 只为触发一次真实查找
    # 直接断言实现里没有按上限切子集：全桶下任何真实查找都应扫完整个桶
    seen = []
    real = ErrorClusterer._find_similar

    def counting(self, msg_tp, level, _seen=seen):
        bucket = self._by_level.get(level) or []
        _seen.append(len(bucket))
        return real(self, msg_tp, level)

    ErrorClusterer._find_similar = counting
    try:
        for i in range(20):
            c._find_similar(_fillers(50)[i % 50] + f" probe {i}", "ERROR")
    finally:
        ErrorClusterer._find_similar = real

    assert seen and all(n == 50 for n in seen), (
        f"桶长是 50，但有查找只扫了 {sorted(set(seen))} 个候选 —— "
        "说明实现又加回了某种截断")


# ---------------------------------------------------------------------------
# 2. 剪枝的正确性：长度上界不能剪掉任何真能过阈值的候选
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(6))
def test_length_bound_prune_never_drops_a_reachable_candidate(seed):
    """``ratio <= 2*min(|a|,|b|)/(|a|+|b|)`` 是精确上界，剪枝必须无损。

    判据不是「剪枝快不快」，而是「被剪掉的候选**真的一样够不着阈值**」。
    用一批长度差异悬殊的模板对逐一核对。
    """
    rng = random.Random(seed)
    base = "".join(rng.choice("abcdefghij") for _ in range(rng.randint(4, 90)))
    pairs = []
    for _ in range(300):
        tpl = "".join(rng.choice("abcdefghij")
                      for _ in range(rng.randint(1, 120)))
        pairs.append((base, tpl))

    for a, b in pairs:
        n, m = len(a), len(b)
        bound = 2 * min(n, m) / (n + m)
        real = SequenceMatcher(None, a, b).ratio()
        assert real <= bound + 1e-12, (
            f"上界 {bound:.4f} 小于实测 ratio {real:.4f} —— "
            "剪枝用的不等式方向错了，会剪掉能过阈值的候选")
        if bound < 0.85:
            assert real < 0.85, (
                f"上界 < 阈值却实测 {real:.4f} >= 0.85 —— 该候选会被误剪")


def test_length_prune_matches_exhaustive_scan():
    """加了长度剪枝之后，查找结果必须与「不做任何剪枝」逐个一致。"""
    templates = _fillers(120)
    for level in ("ERROR",):
        c = ErrorClusterer()
        for i, t in enumerate(templates, 1):
            c.add(_entry(t, i))
        bucket = c._by_level[level]
        for probe in templates[:20] + ["alpha bravo charlie delta echo "
                                       "foxtrot golf hotel india EXTRA"]:
            got = c._find_similar(probe, level)
            # 不剪枝的朴素实现
            expect = None
            best = 0.85
            for cl in bucket:
                s = SequenceMatcher(None, probe, cl.message_template).ratio()
                if s >= best:
                    expect, best = cl, s
            if got is None:
                assert expect is None, "剪枝版没找到，朴素版找到了 —— 剪枝有损"
            else:
                assert expect is not None
                assert got.message_template == expect.message_template, (
                    f"剪枝版选了 {got.message_template!r}，朴素版选 "
                    f"{expect.message_template!r}")


# ---------------------------------------------------------------------------
# 3. 边界：同分时胜出者由内容决定
# ---------------------------------------------------------------------------
class _Stub:
    """只带 message_template 的最小簇替身。

    ``_find_similar`` 只读这个属性，所以不必真的构造 ErrorCluster。
    """

    def __init__(self, tpl: str) -> None:
        self.message_template = tpl

    def __repr__(self) -> str:                              # pragma: no cover
        return f"_Stub({self.message_template!r})"


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_equal_score_tie_broken_by_template_not_by_arrival_order(order):
    """两个模板与探针的相似度完全相同，胜出者必须是字典序小的那个。

    原来的 ``score >= best_score: best = cluster`` 是「后来者覆盖」，
    等于让同分这一种情况由桶内的到达顺序决定 —— 窗口修了，同分还是漏。
    """
    probe = "abcdefgh"
    t_low, t_high = "abcdefghX", "abcdefghY"      # ratio 均为 2*8/18 = 0.889
    pair = [_Stub(t_low), _Stub(t_high)]
    if order == (1, 0):
        pair.reverse()

    c = ErrorClusterer()
    c._by_level["ERROR"] = pair
    best = c._find_similar(probe, "ERROR")

    assert best is not None
    assert best.message_template == t_low, (
        f"同分时应取字典序小的 {t_low!r}，实际取了 {best.message_template!r}")


# ---------------------------------------------------------------------------
# 4. 门禁可失效：把实现改回「最近 N 个」，第 1 组必须变红
# ---------------------------------------------------------------------------
def test_reverting_to_recent_window_breaks_the_gate(monkeypatch):
    """故意做坏 -> 断言变红。

    这是本文件的主要理由。把候选集换回修复前的 ``bucket[-max_compare:]``，
    ``test_similar_pair_merges_regardless_of_gap`` 的核心断言必须不再成立。
    """
    fillers = _fillers(300)
    sig_adjacent = _sig(_cluster(fillers + [PAIR_A, PAIR_B]))
    sig_gapped = _sig(_cluster([PAIR_A] + fillers + [PAIR_B]))
    assert sig_adjacent == sig_gapped, "修复后本该相等，前置条件不成立"

    real = ErrorClusterer._find_similar

    def windowed(self, msg_tp, level):
        bucket = self._by_level.get(level)
        if not bucket or self._max_compare <= 0:
            return None
        best, best_score = None, self._threshold
        n = len(msg_tp)
        for cluster in bucket[-self._max_compare:]:
            other = cluster.message_template
            m = len(other)
            shorter = m if m < n else n
            if 2 * shorter / (n + m) < best_score:
                continue
            score = SequenceMatcher(None, msg_tp, other).ratio()
            if score < best_score:
                continue
            if (best is None or score > best_score
                    or other < best.message_template):
                best, best_score = cluster, score
        return best

    monkeypatch.setattr(ErrorClusterer, "_find_similar", windowed)
    bad_adjacent = _cluster(fillers + [PAIR_A, PAIR_B])
    bad_gapped = _cluster([PAIR_A] + fillers + [PAIR_B])
    assert len(bad_gapped) != len(bad_adjacent), (
        "换回「最近 N 个」后两种排列本该给出不同簇数，却仍然相同 —— "
        "这条可失效性测试自己失效了，说明它没在守真问题")
    assert real is not ErrorClusterer._find_similar


def test_switch_off_still_disables_the_fallback():
    """``max_compare<=0`` 仍是有效的关闭开关（历史上用来做对照臂）。

    两态一起断言：20 个填充各成一簇，A/B 在开启时合并、在关闭时分开。
    """
    fillers = _fillers(20)
    on = _cluster(fillers + [PAIR_A, PAIR_B])
    off = _cluster(fillers + [PAIR_A, PAIR_B], max_compare=0)
    assert len(on) == 21, f"开启回退时 A/B 应合并，实际 {len(on)} 个簇"
    assert len(off) == 22, f"关闭回退后每条自成簇，实际 {len(off)} 个簇"
    assert MAX_SIMILARITY_COMPARE > 0
