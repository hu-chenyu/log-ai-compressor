# -*- coding: utf-8 -*-
"""把 CI 里那段内嵌 Python **真跑一遍** —— 不是 compile()。

背景：v2.9.1 的覆盖率对账两次进 CI，两次都红，而我推之前的本地验证都是
某种**静态**检查：

  · 第一次 `compile()` 通过 → 实际是 `NameError`（`cov` 那行被编辑吃掉了）。
    **`compile()` 查不出运行期的名字解析错误。**
  · 第二次摘出 pytest 调用后才验 → **把 stdout 的 utf-8 重配排除在外**，
    于是 Windows runner 的 cp1252 崩溃复现不出来。

所以这个 harness 干两件事：
  1. 从 ci.yml 里**抽取**那段源码（改 CI 时它会跟着变，不会测一份副本）
  2. 用真实的 / 合成的 coverage.xml **执行**它，断言返回码

抽取范围是「除 pytest 调用以外的全部」—— 特别注意**不能**从 pytest 调用之后
开始切：stdout 重配写在它**之前**，只切后面等于把要验的防线排除在测试外。

它同时是回归网：以后谁再动这段，抽出来的还是最新那份。
"""
from __future__ import annotations

import ast
import builtins
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
_CI = ROOT / ".github" / "workflows" / "ci.yml"
_STEP_PREFIX = "Unit &"


def _step_source() -> str:
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    steps = next(s for s in doc["jobs"]["test"]["steps"]
                 if str(s.get("name", "")).startswith(_STEP_PREFIX))
    return steps["run"]


def _check_body() -> str:
    """切出「除 pytest 调用以外」的全部逻辑，供本地执行。

    为什么把 pytest 那几行摘掉：它要跑几分钟，与被测的对账逻辑无关。

    为什么**不能**从 `sys.exit(rc)` 之后开始切：stdout 的 utf-8 重配写在
    pytest 调用**之前**，只切后面就等于**把要验的那道防线排除在测试外** ——
    而那正是 run #43 windows-3.12 栽跟头的地方。harness 自己漏掉防线，
    比没有 harness 更危险。
    """
    lines = _step_source().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith("rc = subprocess.call(")), None)
    assert start is not None, "ci.yml 的 pytest 调用写法变了，先更新本 harness"
    end = next((i for i in range(start, len(lines))
                if lines[i].strip() == "])"), None)
    assert end is not None, "ci.yml 的 pytest 调用收尾写法变了，先更新本 harness"
    body = textwrap.dedent("\n".join(lines[:start] + ["rc = 0"] + lines[end + 1:]))
    assert "coverage.xml" in body, "切出来的不是覆盖率对账那段"
    return body


def _run(body: str, os_name: str, workdir: pathlib.Path, *,
         console_encoding: str = "utf-8") -> subprocess.CompletedProcess:
    code = (body.replace("${{ matrix.os }}", os_name)
                .replace("${{ matrix.python }}", "3.12"))
    prog = "import re, sys\nfrom pathlib import Path\n" + code
    # env 必须显式给：本 harness 跑的是 python 子进程且按 utf-8 解码，
    # 不钉 PYTHONIOENCODING 就会静默继承开发机控制台代码页 —— 这正是
    # tests/test_subprocess_encoding_contract.py 守的那条，它会当场抓住本行。
    # 传 cp1252 就是把 Windows runner 的敌对控制台搬进本地（v2.9.0 修 CLI
    # 时用的同一招），用来证明这段在那种环境下也跑得完。
    env = dict(os.environ, PYTHONIOENCODING=console_encoding)
    return subprocess.run(
        [sys.executable, "-c", prog], cwd=str(workdir),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


def _fixture(tmp: pathlib.Path, *, readme: pathlib.Path | None,
             cov_line_rate: str | None) -> pathlib.Path:
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy(readme, work / "README.md")
    if cov_line_rate is not None:
        (work / "coverage.xml").write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.15.4" '
            f'timestamp="1" lines-valid="3883" lines-covered="3727" '
            f'line-rate="{cov_line_rate}" branch-rate="0">\n</coverage>\n',
            encoding="utf-8")
    return work


def test_real_coverage_file_parses(tmp_path):
    """本机真跑出来的 coverage.xml 必须能被那段正则解析，且得到 96。

    这是对「正则与真实文件结构匹配」的验证 —— 合成文件测不到这一点。
    """
    real = ROOT / "coverage.xml"
    if not real.exists():
        import pytest
        pytest.skip("本机没跑过 --cov-report=xml，本条不校")
    r = _run(_check_body(), "ubuntu-latest", ROOT)
    out = (r.stdout + r.stderr)
    assert "NameError" not in out, f"对账段引用了未定义的名字：\n{out}"
    assert r.returncode == 0, f"用真实 coverage.xml 应当通过：\n{out}"
    assert "实测覆盖率 96%" in out, out


def test_ubuntu_enforces_readme_number(tmp_path):
    body = _check_body()
    real = ROOT / "README.md"
    good = _fixture(tmp_path / "a", readme=real, cov_line_rate="0.9598")
    assert _run(body, "ubuntu-latest", good).returncode == 0

    # 差一个百分点就必须红 —— 否则这条门禁是装饰
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        real.read_text(encoding="utf-8").replace("**96%**", "**95%**"),
        encoding="utf-8")
    bad = _fixture(tmp_path / "b", readme=bad_readme, cov_line_rate="0.9598")
    r = _run(body, "ubuntu-latest", bad)
    assert r.returncode == 1, f"README 写错就必须红，却 rc={r.returncode}"
    assert "README 写覆盖率 95%" in r.stdout


def test_non_reference_matrix_reports_but_does_not_judge(tmp_path):
    """非参考矩阵：数字照样报出来，但不参与判定。

    这是 v2.9.1 那次教训的直接对策 —— 覆盖率是**平台相关**的量，
    拿它去卡三个平台等于卡了一个不存在的约束。
    """
    body = _check_body()
    real = ROOT / "README.md"
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        real.read_text(encoding="utf-8").replace("**96%**", "**97%**"),
        encoding="utf-8")
    work = _fixture(tmp_path / "c", readme=bad_readme, cov_line_rate="0.9598")
    r = _run(body, "windows-latest", work)
    assert r.returncode == 0, f"非参考矩阵不该判红：\n{r.stdout}{r.stderr}"
    assert "实测覆盖率 96%" in r.stdout, "数字必须照样报出来，否则等于没测"


def test_missing_coverage_file_fails_only_on_reference_matrix(tmp_path):
    """coverage.xml 读不到时：参考矩阵必须红（别让它静默通过），其余只报。"""
    body = _check_body()
    real = ROOT / "README.md"
    none = _fixture(tmp_path / "d", readme=real, cov_line_rate=None)
    r = _run(body, "ubuntu-latest", none)
    assert r.returncode == 1, f"参考矩阵读不到 coverage.xml 必须红：\n{r.stdout}"
    assert "解析 coverage.xml 失败" in r.stdout

    r2 = _run(body, "windows-latest", none)
    assert r2.returncode == 0, r2.stdout
    assert "跳过判定" in r2.stdout


def test_every_name_used_is_defined():
    """静态兜底：`compile()` 查不出 NameError，但 `ast` 能列出实际用到的名字。

    把「用到的全局名」与「本段自己 import / 定义的名字 + 内建名」对账 ——
    少一个就在这里红，而不是等三个矩阵一起红。
    """
    body = _check_body()
    tree = ast.parse(body)
    defined = {"re", "sys", "Path"} | set(dir(builtins))
    # 绑定名不止出现在 Assign 里：for 目标、with ... as、except ... as、
    # 推导式都是 Store 上下文。直接扫全树的 Store 名，别按语句类型枚举 ——
    # 那种枚举本身就是下一个会漂的名单。
    defined |= {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
            and isinstance(n.ctx, ast.Load)}
    missing = sorted(used - defined)
    assert not missing, f"对账段用到了未定义的名字：{missing}"


def test_survives_cp1252_console(tmp_path):
    """把 Windows runner 的敌对控制台搬进**本地**跑这段。

    这是 v2.9.0 修 CLI 崩溃时用的同一招（cp1252 / cp1252+UTF8 关 / UTF8mode 关
    搬进本地），只不过这次的对象从 CLI 换成了 `shell: python` 那段。

    run #43 的 windows-3.12 就是这么红的：`print("实测覆盖率 …")` 在 cp1252
    下抛 UnicodeEncodeError，而红的表象是「覆盖率对账这一步挂了」——
    **失败原因与失败位置无关**，这是最难查的一类。
    """
    body = _check_body()
    work = _fixture(tmp_path / "cp", readme=ROOT / "README.md",
                    cov_line_rate="0.9598")
    r = _run(body, "windows-latest", work, console_encoding="cp1252")
    out = r.stdout + r.stderr
    assert "UnicodeEncodeError" not in out, (
        f"cp1252 控制台下崩了，说明 stdout 没重配：\n{out}")
    assert "charmap" not in out, out
    assert r.returncode == 0, f"cp1252 控制台下必须跑完：\n{out}"
    assert "实测覆盖率 96%" in r.stdout, out


def test_stdout_is_reconfigured_statically():
    """行为检查之外的静态兜底：这段必须先重配 stdout 再 print 中文。

    两条都留着：行为那条依赖本机有 cp1252 编码可用，静态这条在任何机器上
    都能报 —— 少一条，剩下的那条就成了单点。
    """
    body = _check_body()
    assert "reconfigure" in body, (
        "CI 这段没有 stdout 重配 —— Windows runner 的 cp1252 下会直接崩")
    tree = ast.parse(body)
    guards = [n for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and n.attr == "reconfigure"]
    assert guards, "对账段里找不到 stdout/stderr 的 reconfigure 调用"
