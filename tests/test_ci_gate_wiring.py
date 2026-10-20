# -*- coding: utf-8 -*-
"""三道新门禁**确实接在 CI 里**的守卫。

为什么需要这个文件
----------------
写了脚本、写了测试、本地跑绿了 —— 但**没接进 workflow**，这三道门禁
在 GitHub 上一天都不会跑一次。而这种状态从外面完全看不出来：README 里
写着「CI 有 SAST / 依赖扫描 / 性能回归」，仓库里也确实有那三个脚本。

判据只认 `run:` 正文，不认步骤名
---------------------------------
本项目记过一次的形状：`check_validate_precedes_login` 原来只查步骤**名字**
在不在、位置对不对，于是把 `run: ./mcp-publisher validate` 换成
`run: echo 'validate skipped'` 之后，**15 条变异里这条存活** ——
正对着它想防的那件事。

所以这里一律断言 `run` 正文里有那条命令，而不是断言「有个叫 SAST 的步骤」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _steps() -> list[tuple[str, dict]]:
    """ci.yml 里每一个**自己跑代码**的步骤：(名字, 该步骤的字典)。

    用 `yaml.safe_load` 解析，而不是正则切文本 —— 第一版用正则把
    `- name:` 到下一个 `- name:` 之间整段抓下来，于是**注释也一起被抓**，
    而本仓库的 CI 步骤带着大段解释**为什么不用** `continue-on-error` 的注释。
    判据于是在读「这个文件怎么谈论某个词」，而不是「这个步骤实际配了什么」。

    同族的三个错都犯过一遍：`localtime` 撞上了 docstring、
    `clean_regex_content` 撞上了「为什么不管它」的说明、
    `continue-on-error` 撞上了 CI 注释。
    **判据要认结构，不是认文本。**
    """
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    out = []
    for step in doc["jobs"]["test"]["steps"]:
        if "run" not in step:
            continue
        out.append((str(step.get("name", "")).strip(), step))
    return out


def _step_running(command: str) -> list[str]:
    """返回 `run` 正文里真的出现 `command` 的步骤名。"""
    return [name for name, step in _steps() if command in str(step["run"])]


# ---------------------------------------------------------------------------
# 三道新门禁
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command,label", [
    ("python scripts/sast_gate.py", "SAST"),
    ("python scripts/dep_audit_gate.py", "依赖漏洞扫描"),
    ("python scripts/perf_gate.py", "性能回归"),
])
def test_gate_is_actually_invoked_in_ci(command, label):
    hits = _step_running(command)
    assert hits, (
        "CI 里没有真正执行 `%s` 的步骤 —— %s 这道门禁在 GitHub 上"
        "一天都不会跑，而从外面完全看不出来" % (command, label))


def test_sast_gate_runs_on_every_matrix():
    """SAST 必须是**全矩阵**跑，不能只挂参考矩阵。

    只挂一条的理由（「与 OS/版本无关」）在别的门禁上成立，但在这里会
    变成「两台机器上三个月里恰好没人改到那个文件」，而那正是 SAST
    要抓的情形。
    """
    name, step = next((n, s) for n, s in _steps()
                      if "sast_gate.py" in str(s["run"]))
    assert "if" not in step, (
        "SAST 步骤带了 if: 条件 —— 它没有在所有矩阵上跑：%s" % name)
    assert step.get("continue-on-error") is None


def test_dep_gate_runs_self_check():
    """依赖扫描必须带 --self-check。

    不带的话，pip-audit 哪天静默不报了，CI 照样绿 —— 而「依赖安全」
    这句话就成了一句没人验证过的口头承诺。
    """
    assert _step_running("python scripts/dep_audit_gate.py --self-check")


def test_no_gate_is_allowed_to_fail_open():
    """三道门禁都不许 `continue-on-error` / `|| true`。

    这是「门禁必须可失效」的反面：门禁一旦可以失败后继续，它就不再是门禁。
    """
    for command in ("sast_gate.py", "dep_audit_gate.py", "perf_gate.py"):
        name, step = next((n, s) for n, s in _steps()
                          if command in str(s["run"]))
        assert step.get("continue-on-error") is None, (
            "%s 允许失败后继续（步骤：%s）" % (command, name))
        body = str(step["run"])
        assert "|| true" not in body and "|| exit 0" not in body, (
            "%s 允许失败后继续（步骤：%s）" % (command, name))


# ---------------------------------------------------------------------------
# Dependabot
# ---------------------------------------------------------------------------
def test_dependabot_config_exists_and_targets_the_real_manifest():
    cfg = ROOT / ".github" / "dependabot.yml"
    assert cfg.is_file(), (
        "缺 .github/dependabot.yml —— pip-audit 守的是「现在」，"
        "「三个月后」那段没有它就没人管")
    text = cfg.read_text(encoding="utf-8")
    assert 'package-ecosystem: "pip"' in text
    # 最容易静默失效的一处：directory 指错时 Dependabot 不报错，只是不再提 PR
    assert 'directory: "/"' in text
    assert "github-actions" in text
    # 不许自动合并：依赖变更可能连带改变聚类结果（AGENTS.md §2.3）
    assert "version:" in text.splitlines()[0] or "version:" in text


def test_dev_requirements_pin_the_gate_tools():
    """bandit / pip-audit 必须在 requirements-dev.txt 里。

    不在的话 CI 装不上它们，sast_gate.py 返回 2 —— 而 2 也是红，
    但红的原因与「发现漏洞」完全无关，久了会被当成「CI 老这样」。
    """
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r"^bandit>=", text, re.M)
    assert re.search(r"^pip-audit>=", text, re.M)
    assert text.count("pip-audit") == 1, "requirements-dev.txt 里 pip-audit 重复了"


# ---------------------------------------------------------------------------
# README 的门禁清单 ←→ ci.yml，双向
#
# **门禁「道数」那道判据不在这儿**：`tests/test_ci_step_execution.py`
# 的 `TestGateCountCannotDrift` 已经逐个 run 步骤分类、并跨 README /
# docs/FAQ.md / AGENTS.md 三份文档核对道数。那条更严，它连
# 「ci.yml 里新加了一步但没人归类」都会抓。
#
# 本文件补的是它管不到的另一半：**道数对上了但清单内容错了**。
# 写「十道门禁」而只列出九道、把「SAST」写成「静态检查」、
# 或者 README 承诺了一道而 ci.yml 里跑的是别的命令 —— 那些都是绿的。
# ---------------------------------------------------------------------------
#: (README 清单里出现的词, ci.yml 里必须真的执行的命令)
GATE_CONTRACT = [
    ("ruff", "ruff check log_ai_compressor tests scripts"),
    ("单测+覆盖率", "--cov-fail-under=90"),
    ("对抗评分卡", "python scripts/adversarial_eval.py"),
    ("确定性指纹", "determinism_fingerprint"),
    ("自证脚本", "python scripts/prove.py"),
    ("构建可复现性", "python scripts/build_repro.py --verify"),
    ("变异存活率记分卡", "python scripts/mutation_scorecard.py"),
    ("SAST", "python scripts/sast_gate.py"),
    ("依赖漏洞扫描", "python scripts/dep_audit_gate.py"),
    ("性能回归", "python scripts/perf_gate.py"),
]


def _readme_ci_row() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    row = next((ln for ln in readme.splitlines()
                if ln.startswith("| CI |")), "")
    assert row, "README 的工程指标表里找不到 CI 那一行"
    return row


def test_every_documented_gate_is_really_run_in_ci():
    """README 清单里的每一道，都必须有一个真正执行它的 CI 步骤。"""
    row = _readme_ci_row()
    for label, command in GATE_CONTRACT:
        assert label in row, "README 的门禁清单里没有「%s」" % label
        assert _step_running(command), (
            "README 承诺了「%s」这道门禁（命令 %s），"
            "但 ci.yml 里没有真正执行它的步骤" % (label, command))


def test_every_contract_entry_is_a_real_gate_step():
    """反向：清单里每一条都必须对应一个**真实存在**的步骤，而不是一段散落的文本。

    认的是解析后的 `run:` 正文，不是 `ci.yml` 全文 ——
    命令写在注释里、或者写在某个步骤的注释里，都不算那道门禁真的在跑。
    """
    names = {name for name, _ in _steps()}
    for label, command in GATE_CONTRACT:
        hits = _step_running(command)
        assert hits, "清单里的「%s」在 ci.yml 里没有对应的 run 步骤" % label
        assert all(h in names for h in hits)


def test_dependency_gate_is_not_reported_as_a_bare_bare_list():
    """依赖扫描那一步必须带 --self-check，而不是只跑一次裸扫。

    这条与上面 `test_dep_gate_runs_self_check` 有意重复：上面那条钉
    「存在」，这里钉「README 承诺的那道就是带自检的那道」——
    README 与 FAQ 写的都是「依赖漏洞扫描」，而「裸扫」和「带阳性对照
    的扫」是两种不同的承诺。
    """
    assert _step_running("python scripts/dep_audit_gate.py --self-check")
