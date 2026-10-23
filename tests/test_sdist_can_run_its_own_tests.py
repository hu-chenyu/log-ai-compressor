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

**改坏会红吗**：把 `conftest.py` 里的 `pytest_collection_modifyitems` 删掉，
第 3 条立刻红；把名单清空，第 2 条立刻红。
"""
from __future__ import annotations

import importlib.util
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
