# -*- coding: utf-8 -*-
"""`.gitignore` 必须与 CLI 实际会产出的报告文件名保持同步。

**这条测试是被一次发布后的验收逼出来的**
--------------------------------------------------
v2.10.0 发完版，我按 README 的样子在干净 venv 里跑了一次
``log-ai-compressor run examples/sample_system.log -f json`` ——
命令成功、报告也生成了，但工作树多了一条：

```
?? examples/sample_system_report.json
```

原因不在代码，在**约定缺了一条**：``cli.py:282`` 规定不带 ``-o`` 时输出
``<日志名>_report.<格式>`` 写在输入日志旁边，``cli.py:323`` 的 compare 更直接，
默认就叫 ``compare_report.md``。这两条都落在用户的仓库里，而 ``.gitignore``
当时只忽略了 ``*.log.tmp`` 和 ``perf_bench_*.log``。

于是「照着 README 跑一遍」这件事本身会把仓库弄脏 —— 一个开源作品最不该有的观感。

为什么不只补几行就算完
----------------
后缀清单不是随手写的，它来自 ``cli.py::_SUFFIX_FORMAT``。哪天给导出加一种
html 格式，那张表多了 ``.html``，``.gitignore`` 不会跟着变，问题原样复发，
而且**不会有人报错** —— 只是 ``git status`` 里又冒出一条陌生文件。
所以这里测的是**同步**这件事本身，不是"某个文件里有没有某行字"。

判据
----
1. ``_SUFFIX_FORMAT`` 的每一个后缀，都必须有一条对应的忽略规则；
2. 规则要真的生效 —— 用 ``git check-ignore`` 问 git 本人，而不是比对文本；
3. **这条检查必须能变红**：逐个后缀把规则删掉，检查器必须恰好报出那一个。

第 3 条是本仓库的硬规矩：一个从未曾变红过的门禁，等于没有门禁。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from log_ai_compressor.cli import _SUFFIX_FORMAT

ROOT = Path(__file__).resolve().parent.parent
GITIGNORE = ROOT / ".gitignore"

# 显式给 env，不继承开发机的全部环境变量 —— tests/test_subprocess_encoding_contract.py
# 守的就是这条（v2.8.0 在 Windows CI 的 cp1252 控制台上这样挂过）。
# 别图省事删掉：子进程输出按什么编码解释，应该由测试说了算，不由运行它的机器说了算。
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _patterns(text: str) -> set[str]:
    """只取非注释、非空的行 —— 规则本身，不含说明文字。"""
    out = set()
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.add(s)
    return out


def _missing(text: str) -> list[str]:
    pats = _patterns(text)
    return [s for s in sorted(_SUFFIX_FORMAT) if f"*_report{s}" not in pats]


def test_gitignore_covers_every_cli_output_suffix():
    missing = _missing(GITIGNORE.read_text(encoding="utf-8"))
    assert missing == [], (
        f"CLI 会产出这些后缀的默认报告名，但 .gitignore 没忽略：{missing}。"
        f"不带 -o 时报告写在输入日志旁边，会把用户仓库弄脏。"
    )


def test_git_itself_ignores_the_default_report_names():
    """规则要真的被 git 认，不能只是文本看着像。"""
    for suffix in sorted(_SUFFIX_FORMAT):
        probe = f"app_report{suffix}"
        proc = subprocess.run(
            ["git", "check-ignore", "-q", probe],
            cwd=ROOT,
            capture_output=True,
            env=_ENV,
        )
        assert proc.returncode == 0, f"git 不忽略 {probe}"


def test_the_check_can_go_red():
    """检查器本身要有牙：拿掉任意一条规则，必须恰好报出那一个。

    起点是**自己拼出来的完整清单**，不是真实 ``.gitignore`` ——
    真实文件坏了的时候，本用例要证明的仍然是"检查器能不能发现"，
    而不是跟着一起报一句"删不掉"（那是另外两条测试该说的话）。
    """
    rules = [f"*_report{s}\n" for s in sorted(_SUFFIX_FORMAT)]
    base = GITIGNORE.read_text(encoding="utf-8")
    complete = base + "".join(r for r in rules if r not in base)
    for rule in rules:
        broken = complete.replace(rule, "", 1)
        assert broken != complete, f"删不掉 {rule.strip()}，这个用例什么也没验证"
        assert _missing(broken) == [rule[len("*_report"):-1]]


def test_gitignore_does_not_swallow_the_sample_log():
    """反向：别把样例日志本身也忽略了 —— 那是交付物，不是产物。"""
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "examples/sample_system.log"],
        cwd=ROOT,
        capture_output=True,
        env=_ENV,
    )
    assert proc.returncode != 0, "样例日志被忽略了"


# ---------------------------------------------------------------------------
# v2.11.1：行尾注释让整条规则失效（第四轮外部复核 D6/F6）
# ---------------------------------------------------------------------------
def test_no_ignore_rule_carries_a_trailing_comment():
    """**任何**一条忽略规则都不得带行尾注释。

    git 只把**行首**的 ``#`` 当注释；行尾的 ``#`` 属于模式本身。v2.11.1 实测
    ``.gitignore`` 里有两行写了行尾注释：

        .coverage.*        # pytest-xdist 并行模式留的分片……
        .coveragerc.ci      # CI 在 mcp 不可用时临时生成的……

    于是它们的实际模式是「那一串文字连同空格和中文」，匹配不到任何文件 ——
    ``git check-ignore .coverage.host.12345`` 返回 1（未忽略）。**一条写了
    却从不生效的忽略规则比不写更糟**：它让人以为工作区是干净的，于是
    「跑完测试 ``git status`` 兜底」这条安全网自己先失效了。

    这条检查测的是**形态**而不是某几个具体文件，所以同类问题换个文件名复发
    也照样抓得到 —— 而只测 ``*_report*`` 后缀的那几条永远碰不到它们。
    """
    offenders = []
    for i, line in enumerate(GITIGNORE.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "#" in s and "\\#" not in s:
            offenders.append(f"第 {i} 行: {line!r}")
    assert offenders == [], (
        "行尾注释被当成模式的一部分，整条规则从未生效（git 只认行首 #）："
        + "；".join(offenders))


@pytest.mark.parametrize("probe", [
    ".coverage",
    ".coverage.host.12345",       # pytest-xdist 的分片，真实文件名长这样
    ".coverage.machine.99.1",
    "coverage.xml",
    ".coveragerc.ci",
    ".pytest_cache",
    ".ruff_cache",
])
def test_artifact_rules_actually_take_effect(probe: str):
    """行为口径：问 git 本人「这个文件你管不管」，而不是比对文本。

    与上面那条形态检查互为对照 —— 形态检查保证以后不会再写出行尾注释，
    这一条保证**眼下这批规则真的生效**。两条都在，是因为 v2.11.1 实测里
    那个问题正是「文本看着完全正常，git 却不认」。
    """
    proc = subprocess.run(
        ["git", "check-ignore", "-q", probe],
        cwd=ROOT,
        capture_output=True,
        env=_ENV,
    )
    assert proc.returncode == 0, f"git 不忽略 {probe} —— 规则写了却没生效"
