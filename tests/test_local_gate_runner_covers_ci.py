# -*- coding: utf-8 -*-
"""本地门禁运行器必须覆盖 ci.yml 里的**每一条**门禁步骤。

为什么要有这一条
----------------
2026-10-21 的事故：`b51ab7a` 把 `main` 推成了红的（三矩阵的
「Unit & integration tests with coverage」全挂），而本地那份门禁清单
报的是**「全绿」** —— 因为**那份清单漏了 pytest 全量与变异记分卡**
两道（最重的两道，也恰恰是能抓住这次事故的那一道）。

一条清单漏了一项，**从外面完全看不出来** —— 它照样打印「全绿」。
所以清单本身要有一道门禁：**ci.yml 里多一条门禁，本清单里就得有一条**。

**判据为什么两边各写各的**
--------------------------
本文件按**步骤名**排除「装依赖」，运行器里按 `run` 正文的前缀排除。
两条路各写各的 —— 同一个判据写两遍等于没写（改了一处另一处跟着改，
两边永远相等，这条断言就永远是绿的）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
_RUNNER = ROOT / "scripts" / "run_local_gates.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_local_gates_under_test", _RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ci_gate_steps() -> list[tuple[str, str]]:
    """ci.yml 里每一条**自己跑代码**的步骤：(job, 步骤名)。"""
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            if "run" not in step:
                continue
            name = str(step.get("name", "")).strip()
            if name.lower().startswith("install dependencies"):
                continue
            out.append((job_name, name))
    return out


def test_the_runner_covers_every_gate_step_in_ci() -> None:
    """本清单必须与 ci.yml 的门禁步骤**一一对应**。"""
    runner = _load_runner()
    planned = {(s["job"], s["name"]) for s in runner.steps()}
    expected = set(_ci_gate_steps())
    missing = sorted(expected - planned)
    extra = sorted(planned - expected)
    assert planned == expected, (
        "本地清单与 ci.yml 不一致\n  少的：%s\n  多的：%s"
        % (missing, extra))


def test_the_two_heavy_gates_are_in_the_plan() -> None:
    """**点名**最重的两道必须在清单里。

    为什么单独一条：上一轮本地清单漏的就是这两条，而清单**照样**报「全绿」。
    覆盖面那条断言也能抓住它，但报出来的信息要说人话 ——
    「少了哪一道」比「两个集合不等」有用得多。
    """
    runner = _load_runner()
    keys = {runner.key(s) for s in runner.steps()}
    assert any("coverage" in k for k in keys), "本地清单漏了 pytest 全量（覆盖率门禁）"
    assert any("mutation-scorecard" in k for k in keys), "本地清单漏了变异记分卡"


def test_inline_steps_with_placeholders_have_a_local_equivalent() -> None:
    """带 `${{ }}` 占位符的内联步骤必须声明**本地等价物**。

    没有等价物就**明确报错**，不许静默跳过 —— 静默跳过就是那次事故的形状。
    """
    runner = _load_runner()
    for step in runner.steps():
        if step.get("shell") != "python":
            continue
        if "${{" not in step["run"]:
            continue
        k = runner.key(step)
        assert k in runner.LOCAL_EQUIVALENTS, (
            "步骤 %r 带占位符却没有本地等价物 —— 本地会跳过它" % step["name"])


def test_the_runner_refuses_to_skip_a_step_without_an_equivalent() -> None:
    """没声明等价物时必须**报错退出**，不许默默不跑。"""
    runner = _load_runner()
    step = {
        "job": "test",
        "name": "Probe step with placeholder",
        "run": "print('${{ matrix.os }}')",
        "shell": "python",
    }
    try:
        runner.argv(step)
    except SystemExit as exc:
        assert "本地等价物" in str(exc), str(exc)
        return
    raise AssertionError("带占位符又没等价物的步骤竟然被放过了")
