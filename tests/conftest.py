# -*- coding: utf-8 -*-
"""从**源码交付**（sdist）里跑测试时的统一约定。

为什么需要这个文件
------------------
sdist 是「源码交付」：拿到它的人应该能跑测试、读文档、复现 README 里的示例
命令 —— 这是 `MANIFEST.in` 开头写下的承诺。

但 sdist 里**没有 `.git`**（也不该有：那是构建机的东西，不是交付物），
而有一批测试需要**仓库专属的上下文**：

* `.git` 历史 —— 措辞门禁扫历史 blob、变异记分卡比对工作区与锚点唯一性；
* `.github/workflows/*.yml` —— CI 接线与发布工作流的判据；
* `.gitignore` 的实际生效规则。

那些用例在 sdist 里必须**跳过，而不是失败**：失败等于说「这个包坏了」，
而包没坏 —— 它只是不是 git 仓库。

实测（2026-10-23 第十一轮外部复核后）：修好 MANIFEST（把 `.github/` 与
`.gitignore` 收进包）之后，从 sdist 跑 pytest 仍有 45 条红，其中 44 条属于
这一类；剩下 1 条是 README 的活数字没回填（那是另一回事，已单独修）。

名单在下面，并由 `tests/test_sdist_can_run_its_own_tests.py` 盯着：
**新增一个需要仓库上下文的测试模块却忘了登记，那条门禁会红。**
（判据是「这个模块的源码里出现了 `.git` / `.github` / `.gitignore`」）——
这条判据是**启发式**的，所以有元测试守着它本身没坏。
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

#: 需要仓库专属上下文的测试模块（文件名）。
#:
#: 登记它们的理由只有一个：**从 sdist 跑测试时它们必须跳过**。
#: 判据（哪些模块属于这一类）见模块文档 —— 「源码里出现了 .git / .github /
#: .gitignore」。
NEEDS_REPO_CONTEXT = (
    "test_audit_release_artifact.py",      # 要 git ls-tree 拿源码树清单
    "test_audit_wording_script.py",        # 要 git 历史（它自己建临时仓库，但历史那份对照要真仓库）
    "test_build_repro_scope.py",           # 要真的 git archive 出快照
    "test_build_snapshot.py",              # 要 git ls-tree / git show
    "test_ci_step_execution.py",           # 要把 ci.yml 那段正文抽出来在本地跑
    "test_gitignore_report_artifacts.py",  # 要 git check-ignore 的实际行为
    "test_mutation_scorecard.py",          # 记分卡要 git status 比对工作区
    "test_no_job_wording.py",              # 措辞门禁要扫 .git 历史
    "test_wording_matcher_equivalence.py",  # 要拿真实历史 blob 做对照
)


def repo_context_available() -> bool:
    """当前这份源码是不是在 git 仓库里（且带着 CI 配置）。"""
    return (_ROOT / ".git").exists() and (_ROOT / ".github" / "workflows" / "ci.yml").is_file()


def pytest_collection_modifyitems(config, items):  # noqa: ARG001 - pytest 钩子签名
    """没有仓库上下文时，把那一批用例**跳过**。

    为什么用「跳过」而不是「让它们过」：那些用例断言的是**仓库里**的事实
    （历史 blob 有没有词、CI 接线对不对）。在 sdist 里那些事实不存在，
    断言无从成立 —— 跳过是诚实的说法，「过」是假的。
    """
    if repo_context_available():
        return
    reason = ("需要 git 仓库上下文（.git 历史 / .github / .gitignore），"
              "而这份源码是**源码交付**（sdist）—— 从这里跑测试时跳过")
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        if Path(str(item.fspath)).name in NEEDS_REPO_CONTEXT:
            item.add_marker(marker)
