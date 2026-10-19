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

    def test_every_anchor_is_unique_in_its_target(self):
        """锚点必须**恰好出现一次**。

        施加时是 ``text.replace(m.old, m.new, 1)`` —— 只改第一处。锚点不唯一时，
        变异很可能落到判据根本不看的那一处，于是报出「变异存活」。而那个结论
        是**假的**：守卫没变弱，是变异压根没落到被读的那份上 ——
        它看起来完全像「守卫已经失去牙齿」。

        实测踩到过：``publish-mcp.yml`` 里那个 ``?search=`` 的 URL 在 publish
        的回查与 verify 的自检中各有一处。锚点从唯一变成两处之后，那条变异
        改到了 publish，而判据读 verify，于是「存活」。
        """
        bad = []
        for m in MS.MUTATIONS:
            p = m.path()
            if not p.is_file():
                continue
            n = p.read_text(encoding="utf-8").count(m.old)
            if n != 1:
                bad.append((m.id, n))
        assert not bad, (
            f"这些变异的锚点在目标文件里不是恰好出现一次：{bad}\n"
            "不唯一意味着 replace(old, new, 1) 可能改到判据不看的地方，"
            "报出来的「存活」是假结论。把锚点加长到唯一，别改短。")

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
        """构造一条**注定活下来**的变异：改一个没有任何测试会看它读的文件。

        锚点必须**唯一** —— 记分卡施加时是 `replace(old, new, 1)`，只改第一处。
        这条探针原先用 `old="本项目"`，而它在 FAQ.md 里出现 24 次，于是变异
        落在了一个判据不看的位置上，被新增的唯一性守卫直接拒绝跑 ——
        **而它本来只是想造一条「存活」的变异，并不靠这条锚点当缺陷**。
        """
        m = MS.Mutation(
            id="probe/does-not-care",
            why="故意选一个与判定测试无关的目标：这条必然存活",
            target="docs/FAQ.md",
            old="全历史 0 处密钥泄露",
            new="全历史 0 处密钥泄露 ",
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


def test_it_refuses_an_ambiguous_anchor_instead_of_passing():
    """锚点在目标文件里出现多次时必须**拒绝跑**，而不是改第一处就继续。

    与上面那条「锚点没了」的同类：跳过一个可疑变异再报「全部有牙齿」，
    是本脚本最容易犯的假绿。这里更隐蔽 —— 它**不会**报跳过，它会诚实地
    报「这条变异存活」，而那个结论恰恰是错的。
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    doubled = next(w for w in ("log-ai-compressor", "pytest", "README")
                   if readme.count(w) > 1)
    bad = MS.Mutation(id="probe/ambiguous", why="锚点不唯一",
                      target="README.md", old=doubled, new="x",
                      tests=["tests/test_adversarial_scorecard_gate.py"])
    with pytest.raises(SystemExit) as e:
        MS.scorecard(mutations=[bad])
    msg = str(e.value)
    assert "次" in msg and "唯一" in msg, (
        f"拒绝的理由必须讲清「锚点出现了几次」，实际：{msg!r}")


def test_the_restore_survives_one_transient_write_failure(tmp_path, monkeypatch):
    """还原必须经得起**一次**瞬时的写失败。

    本机实测两次：刚写过的文件立刻再打开写入会失败
    （`OSError: [Errno 22] Invalid argument`，路径完全正常、事后又能写）。
    而一次失败就把**变异态留在工作区** —— 那正是这个脚本最不能留下的东西。

    这条走真实机制：真的让写失败一次，看它是不是重试并最终还原。
    """
    target = tmp_path / "x.py"
    target.write_bytes(b"original\n")
    real = pathlib.Path.write_bytes
    calls = {"n": 0}

    def flaky(self, data):
        if self == target:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(22, "Invalid argument")
        return real(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", flaky)
    MS._restore(target, b"restored\n")
    assert target.read_bytes() == b"restored\n", "重试之后必须真的还原了"
    assert calls["n"] == 2, "第一次失败后必须重试，而不是直接放弃"


def test_a_stale_anchor_blames_the_worktree_when_the_file_is_dirty():
    """锚点找不到时，要分清「代码变了」与「工作区本来就被改脏了」。

    实测踩到过：一次运行崩了，把目标文件留在了变异态；下一次运行看到
    「锚点找不到了」，报的是「**代码已经变了**」—— 而代码没变，是工作区
    脏了。照着那个诊断去查代码会白查：该查的是 `git status`。

    走**真实机制**：真的在仓库里造一个「工作区不干净的文件」，
    看它判不判得出来。monkeypatch 掉那个判断的话，就测不到
    「判断本身恒为 False」这种退化 —— 而那正是本项目最贵的一课。
    """
    rel = "docs/_worktree_dirty_probe.tmp"
    probe = _ROOT / rel
    try:
        probe.write_text("探针\n", encoding="utf-8")
        # 纯函数那一半：porcelain 的「未跟踪」行长这样
        assert MS._is_dirty("?? %s\n" % rel, rel) is True, (
            "工作区里明明有一个未跟踪的文件，却判成干净的 —— "
            "这个判断恒为 False 的话，下面那条提示永远不会出现")
        assert MS._is_dirty("?? docs/另一个文件.tmp\n", rel) is False, (
            "不存在的路径被判成脏 —— 判断的方向反了")
        assert MS._is_dirty("", rel) is False, "空状态被判成脏"

        # 端到端那一半：真的让 scorecard 撞上「工作区脏 + 锚点不存在」
        bad = MS.Mutation(id="probe/stale-dirty", why="锚点不存在",
                          target=rel, old="这段文字不在这个文件里",
                          new="x",
                          tests=["tests/test_adversarial_scorecard_gate.py"])
        with pytest.raises(SystemExit) as e:
            MS.scorecard(mutations=[bad])
        msg = str(e.value)
        assert "工作区" in msg, (
            f"拒绝的理由必须指出「工作区被改脏」这个可能，实际：{msg!r}")
    finally:
        if probe.exists():
            probe.unlink()
