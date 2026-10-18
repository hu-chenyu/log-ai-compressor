# -*- coding: utf-8 -*-
"""变异记分卡这个脚本本身，也得有守卫。

一个会**改仓库文件**的门禁，坏的代价比别的门禁高：它可能悄悄弄脏工作区，
可能永远判绿，可能在某次代码变动后把一条**已经无意义**的变异当成证据继续
报「有牙齿」。所以这里钉住三件事：

1. 每条变异的锚点必须真的还在（否则那张表在骗人）；
2. 脚本必须能把工作区还原成**逐字节相同**；
3. **有变异活下来时它必须红** —— 一条从来没红过的门禁等于没有门禁。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "mutation_scorecard.py"


def _load():
    spec = importlib.util.spec_from_file_location("mutation_scorecard", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MS = _load()


def test_audit_doc_agrees_with_the_card_size():
    """`AUDIT.md` 写的变异条数必须等于这张卡的实际长度。

    AUDIT.md 里「**当前 N 条变异**」是本文档最有说服力的一句 —— 它宣称
    门禁本身一直在被测量。而它天然不在任何对账名单里，于是这句话本身
    就成了一个**没人看守的对外数字**：加了几条变异、忘了回头改文档，
    读的人会拿一个旧数字当现状。

    形态三的教训是「报「检查通过」但其实什么都没扫」；这里更朴素一点：
    数字本身没人算。
    """
    audit = (_ROOT / "AUDIT.md").read_text(encoding="utf-8")
    claimed = re.findall(r"当前\s*(\d+)\s*条变异", audit)
    assert claimed, "AUDIT.md 里找不到「当前 N 条变异」—— 判据失效了"
    for n in claimed:
        assert int(n) == len(MS.MUTATIONS), (
            f"AUDIT.md 写 {n} 条变异，记分卡实际 {len(MS.MUTATIONS)} 条")


# ---------------------------------------------------------------------------
# 1. 表本身不许坏掉
# ---------------------------------------------------------------------------
class TestTheMutationTableIsHonest:
    def test_the_table_is_not_empty(self):
        assert MS.MUTATIONS, "变异表是空的 —— 那这张卡什么都没证明"

    def test_every_anchor_still_exists(self):
        """代码变了、锚点没了 —— 那条变异记的是一个已经不存在的缺陷。

        **不要**为了让脚本跑通就把锚点改松；先判断它是否还有意义，
        没意义就删掉并在 CHANGELOG 里说明。
        """
        stale = []
        for m in MS.MUTATIONS:
            p = m.path()
            if not p.is_file() or m.old not in p.read_text(encoding="utf-8"):
                stale.append(m.id)
        assert not stale, (
            f"这些变异的锚点在目标文件里找不到了：{stale}\n"
            f"先判断它们是否还有意义，别直接改松锚点。")

    def test_every_mutation_names_the_tests_that_must_catch_it(self):
        """不声明判定测试的变异，记的是谁的账都说不清。"""
        missing = [m.id for m in MS.MUTATIONS if not m.tests]
        assert not missing, f"这些变异没声明判定它的测试：{missing}"

    def test_every_mutation_explains_which_incident_it_is(self):
        """`why` 是这张表作为文档的全部价值 —— 空着等于删了它。"""
        thin = [m.id for m in MS.MUTATIONS if len(m.why.strip()) < 8]
        assert not thin, f"这些变异没写清它代表哪次事故：{thin}"

    def test_no_mutation_targets_the_scorecard_itself(self):
        """自己改自己 = 自证清白。"""
        bad = [m.id for m in MS.MUTATIONS
               if m.target.replace("\\", "/") == "scripts/mutation_scorecard.py"]
        assert not bad, f"这些变异改的是记分卡自己：{bad}"

    def test_ids_are_unique(self):
        """重名会让表里的两行指向同一条，出错时看不出是哪条。"""
        ids = [m.id for m in MS.MUTATIONS]
        assert len(ids) == len(set(ids)), f"变异 id 重复：{ids}"

    def test_every_declared_test_file_exists(self):
        for m in MS.MUTATIONS:
            for t in m.tests:
                assert (_ROOT / t).is_file(), f"[{m.id}] 判定测试不存在：{t}"


# ---------------------------------------------------------------------------
# 2. 它必须能红
# ---------------------------------------------------------------------------
class TestTheScorecardCanGoRed:
    def test_a_surviving_mutation_fails_the_verdict(self):
        """构造一条**注定活下来**的变异：改一个没有任何测试会看它读的文件。"""
        m = MS.Mutation(
            id="probe/does-not-care",
            why="故意选一个与判定测试无关的目标：这条必然存活",
            target="docs/FAQ.md",
            old="本项目",
            new="本项目 ",
            tests=["tests/test_adversarial_scorecard_gate.py"],
        )
        summary = MS.scorecard(mutations=[m])
        assert summary["survived"] == 1, summary
        assert summary["killed"] == 0, summary
        assert MS.verdict(summary) == 1, "有变异活下来却判通过 —— 这道门是哑的"

    def test_a_clean_run_passes_the_verdict(self):
        assert MS.verdict({"survived": 0, "total": 1, "killed": 1}) == 0

    def test_there_is_no_survival_allowance(self):
        """不许有「允许 N 条存活」这种配额。

        活着的每一条都是一句没兑现的承诺，而且没人说得清它什么时候该被豁免。
        """
        src = _SCRIPT.read_text(encoding="utf-8")
        assert "survived >" not in src, (
            "出现「survived > N」这种放行条件 —— 门禁被加了配额")


# ---------------------------------------------------------------------------
# 3. 它不许弄脏工作区
# ---------------------------------------------------------------------------
class TestRestorationIsByteExact:
    @pytest.mark.parametrize("mid", [
        "host-guard/no-port-strip",       # 2.2s
        "scorecard/banned-emptied",       # 12.2s
    ])
    def test_target_is_byte_identical_afterwards(self, mid):
        m = next(x for x in MS.MUTATIONS if x.id == mid)
        p = m.path()
        before = p.read_bytes()
        summary = MS.scorecard(mutations=[m])
        after = p.read_bytes()
        assert before == after, (
            f"{p} 跑完变异后没有逐字节还原 —— 脚本把变异状态留在了仓库里")
        assert summary["results"][0]["killed"] is True, summary


def test_it_refuses_a_stale_anchor_instead_of_passing():
    """锚点没了必须**拒绝跑**，不能「跳过这条」然后照样报 PASS。

    跳过一条失效变异再报「全部有牙齿」，是本脚本最容易犯的假绿。
    """
    bad = MS.Mutation(id="probe/stale", why="锚点不存在",
                      target="README.md", old="这段文字不在 README 里",
                      new="x", tests=["tests/test_adversarial_scorecard_gate.py"])
    with pytest.raises(SystemExit) as e:
        MS.scorecard(mutations=[bad])
    assert "锚点" in str(e.value)
