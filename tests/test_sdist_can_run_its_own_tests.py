# -*- coding: utf-8 -*-
"""从**源码交付**（sdist）里跑测试时的约定，必须真的生效。

为什么要有这条
--------------
`MANIFEST.in` 开头写着「拿到它的人应该能跑测试、读文档、复现 README 里的示例
命令」—— 那是一条**对外承诺**。2026-10-23 第十一轮外部复核实测：
从 sdist 里跑 pytest **硬红 115 条**（缺 `.github/`、`.gitignore`、`.git` 历史）。
于是有两种做法：

* 把承诺改成「测试只能在仓库里跑」（诚实，但交付变弱）；
* 让承诺成真（`.github/` 与 `.gitignore` 收进包，需要 git 上下文的用例**跳过**）。

本项目选了后者。所以「从 sdist 跑得绿」这件事必须**有门禁盯着** ——
否则下一次有人加了个需要 git 的测试模块，它在 sdist 里又红了，而 CI 全绿。

本文件盯三件事
--------------
1. 跳过名单里的模块**都存在**（文件被改名/删除时立刻红）；
2. 名单里的模块**确实涉及 git**（避免留下过期条目）；
3. 跳过钩子**只**跳过名单里的模块（不多跳 —— 多跳等于把测试藏起来）。

**为什么第 2 条是「涉及 git」而不是「源码里出现过 .git」**
2026-10-24 第十二轮外部复核实测过那个更严的判据：它**两个方向都会错** ——
用「源码里出现过 .git 字样」判，命中 16 个模块而真正需要仓库上下文的只有 9 个；
换成「.git 目录 / 起 git 子进程」，命中 9 个却漏掉 6 个已登记的（它们通过
helper 起进程，字面量抓不到）。
⇒ 判据只能是「这个模块**确实在用 git**」这种**弱但为真**的说法 ——
它守的是「名单里没有过期条目」，不是「名单完整」。

「名单完整」那件事由**第 12 道门禁**守（它把 sdist 解包、真跑一遍）：
新增一个需要仓库上下文的模块却忘了登记 ⇒ 那道门禁红，并**点名是哪个模块**。
**改坏会红吗**：把 `conftest.py` 里的 `pytest_collection_modifyitems` 删掉，
第 3 条立刻红；把名单清空，第 2 条立刻红。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"


def _load_conftest():
    spec = importlib.util.spec_from_file_location(
        "lac_conftest", _TESTS / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lac_conftest"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_gate():
    """加载第 12 道门禁那个脚本（`scripts/check_sdist_runs_its_own_tests.py`）。"""
    script = _ROOT / "scripts" / "check_sdist_runs_its_own_tests.py"
    spec = importlib.util.spec_from_file_location("lac_sdist_gate", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lac_sdist_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_gate_names_the_modules_that_failed():
    """第 12 道门禁红了要**点名是哪个模块**。

    为什么：2026-10-24 第十二轮外部复核指出，真正能兜住「新增一个需要仓库
    上下文的测试模块却忘了登记」的**就是这道门禁**（经验判据：真跑一遍）。
    所以它必须把「哪个模块红了」说出来 —— 否则「兜底」只是兜住了，
    却让人多花一轮排查（原先只说「两种可能」）。

    **改坏会红吗**：把 `failing_modules()` 改成永远返回空列表，本条立刻红。
    """
    mod = _load_gate()
    out = ("FAILED tests/test_alpha.py::test_one - 断言失败\n"
           "FAILED tests/test_beta.py::test_two - 断言失败\n"
           "FAILED tests/test_alpha.py::test_three - 断言失败\n")
    assert mod.failing_modules(out) == ["test_alpha.py", "test_beta.py"], (
        "它没点名红了的模块：%s" % mod.failing_modules(out))


def test_the_gate_names_nothing_when_nothing_failed():
    """对照组：没红的时候不许报模块（否则那条提示会变成噪音）。"""
    mod = _load_gate()
    assert mod.failing_modules("1 passed in 3.2s\n") == []


def test_the_gate_really_names_the_module_when_the_sdist_tests_fail(tmp_path):
    """**真跑一遍**：造一个必然红的 sdist，看门禁有没有点名是哪个模块。

    为什么不能只测辅助函数：辅助函数对了、而**调用点**被拆掉（比如把
    ``bad = failing_modules(...)`` 改成 ``bad = []``），上一条照样绿 ——
    而门禁实际上已经不点名了。这正是变异记分卡逮住过的一次：
    「**函数还在，但没人调它**」。

    所以这里端到端跑一遍：造 sdist → 跑门禁 → 看它有没有点名。

    **改坏会红吗**：把 ``scripts/check_sdist_runs_its_own_tests.py`` 里的
    ``failing_modules(...)`` 调用去掉，本条立刻红。
    """
    import io
    import tarfile

    sdist = tmp_path / "log_ai_compressor-0.0.1.tar.gz"
    top = "log_ai_compressor-0.0.1"
    with tarfile.open(sdist, "w:gz") as t:
        raw = b"def test_boom():\n    assert False, 'boom'\n"
        info = tarfile.TarInfo("%s/tests/test_boom.py" % top)
        info.size = len(raw)
        t.addfile(info, io.BytesIO(raw))

    script = _ROOT / "scripts" / "check_sdist_runs_its_own_tests.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(script), "--sdist", str(sdist)],
                          cwd=str(_ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=600)
    assert proc.returncode == 1, (
        "红的 sdist 居然过了门禁：\n%s" % (proc.stdout + proc.stderr)[-800:])
    # 断的是**门禁自己那句话** —— 而不是「test_boom.py 这个字符串出现过」：
    # pytest 自己的输出里就有它（FAILED tests/test_boom.py::…），
    # 那样断的话「门禁没点名」也照样绿。
    assert "红了的是这些模块" in proc.stdout, (
        "门禁红了却没说清是哪个模块：\n%s" % proc.stdout[-800:])
    assert "test_boom.py" in proc.stdout, (
        "门禁点了名但没点对：\n%s" % proc.stdout[-800:])


class _FakeItem:
    """够 pytest_collection_modifyitems 用的最小替身。"""

    def __init__(self, name: str):
        self.fspath = str(_TESTS / name)
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


class TestTheSdistSkipList:
    def test_every_listed_module_exists(self):
        """名单里的模块都必须在（改名/删除时立刻红）。"""
        mod = _load_conftest()
        missing = [n for n in mod.NEEDS_REPO_CONTEXT
                   if not (_TESTS / n).is_file()]
        assert not missing, (
            "跳过名单里的这些测试文件不存在了：%s —— "
            "改名或删除后要同步更新 tests/conftest.py" % missing)

    def test_in_a_repository_nothing_is_skipped(self):
        """在**仓库里**跑测试时，一条都不许被跳过。

        这是「跳过」这件事的边界：它只在「没有 git 上下文」时成立。
        一旦在仓库里也开始跳，那就是把测试藏起来了 —— 门禁会慢慢
        失去牙齿，而从外面完全看不出来。

        **改坏会红吗**：把 `conftest.py` 里的
        `if repo_context_available(): return` 去掉，本条立刻红。
        """
        mod = _load_conftest()
        items = [_FakeItem(n) for n in mod.NEEDS_REPO_CONTEXT]
        original = mod.repo_context_available
        try:
            mod.repo_context_available = lambda: True    # 有 git 仓库
            mod.pytest_collection_modifyitems(None, items)
        finally:
            mod.repo_context_available = original
        skipped = [Path(i.fspath).name for i in items if i.markers]
        assert not skipped, (
            "在仓库里也跳过了这些：%s —— 跳过只该发生在「没有 git 上下文」时" % skipped)

    def test_the_listed_modules_really_involve_git(self):
        """名单里的模块**确实涉及 git** —— 不许留下过期条目。

        为什么要这条：名单是**手写**的，而手写的名单会**过期** ——
        模块改名了、逻辑改了、它其实不需要 git 了，名单还留着它 ⇒
        那些用例在源码交付里被**无声地跳过**（"多跳等于把测试藏起来"）。

        判据用「源码里提到 git **或** 历史」这种**弱但为真**的说法 ——
        它守的是「没有过期条目」，**不是**「名单完整」。
        后者只能靠**经验判据**（真跑一遍）得到，那是第 12 道门禁的事。

        为什么不用更严的判据：实测过「源码里出现过 .git 字样」——
        命中 16 个模块而真正需要仓库上下文的只有 9 个；而换成
        「.git 目录 / 起 git 子进程」又漏掉 6 个已登记的（它们通过 helper
        起进程，字面量抓不到）。两个方向都会错的判据，不能当门禁。

        **改坏会红吗**：把 `NEEDS_REPO_CONTEXT` 里某个模块的名字改成一个
        不存在的文件，第 1 条立刻红；把某个已登记模块的源码改成完全不提 git
        （比如它已经被重写成不需要仓库上下文了），本条立刻红。
        """
        mod = _load_conftest()
        stale = []
        for name in mod.NEEDS_REPO_CONTEXT:
            src = (_TESTS / name).read_text(encoding="utf-8", errors="replace").lower()
            if "git" not in src and "历史" not in src:
                stale.append(name)
        assert not stale, (
            "名单里的这些模块源码里已经不提 git / 历史了 —— 它们多半不再需要"
            "仓库上下文，把它们从 NEEDS_REPO_CONTEXT 里去掉：%s" % stale)

    def test_the_hook_skips_exactly_the_listed_modules(self):
        """钩子必须**只**跳过名单里的模块。

        多跳等于把测试藏起来：比如「覆盖率那几条」被顺手跳过，
        门禁就再也发现不了「从 sdist 跑不出真数」这件事。
        """
        mod = _load_conftest()
        names = ["test_no_job_wording.py", "test_models.py",
                 "test_encoding_majority_vote.py"]
        items = [_FakeItem(n) for n in names]
        original = mod.repo_context_available
        try:
            mod.repo_context_available = lambda: False   # 假装没有 git 仓库
            mod.pytest_collection_modifyitems(None, items)
        finally:
            mod.repo_context_available = original
        skipped = {Path(i.fspath).name for i in items if i.markers}
        assert skipped == {"test_no_job_wording.py"}, (
            "跳过的模块不对：%s —— 只该跳过名单里的那一个" % sorted(skipped))
