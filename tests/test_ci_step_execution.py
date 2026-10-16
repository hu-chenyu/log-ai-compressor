# -*- coding: utf-8 -*-
"""把 CI 里那段内嵌 Python **真跑一遍** —— 不是 compile()。

背景：v2.9.1 的覆盖率对账第一次进 CI 时，我用 `compile()` 验证了它语法
没问题，然后推了。它照样挂了：`cov = Path("coverage.xml")` 那行被我第二次
编辑吃掉了，代码里还引用着 `cov` → **NameError**。`compile()` 是静态检查，
查不出运行期的名字解析错误 —— **静态检查验证契约，不验证代码能跑**。

所以这个 harness 干两件事：
  1. 从 ci.yml 里**抽取**那段源码（改 CI 时它会跟着变，不会测一份副本）
  2. 用真实的 / 合成的 coverage.xml 与 README **执行**它，断言返回码

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
    """切出 pytest 之后的「覆盖率对账」那段。

    pytest 本身不用重跑（那要几分钟，且与本段无关）—— 我们要验的是
    **我写的那段对账逻辑**，给它一份真实的 coverage.xml 就够了。
    """
    src = _step_source()
    lines = src.splitlines()
    # 按行找而不是按固定缩进猜：YAML 块标量会把公共缩进剥掉，
    # 照着文件里的缩进量手写 marker 是探针写错的典型（第一版就这么栽了）。
    idx = next((i for i, ln in enumerate(lines) if ln.strip() == "sys.exit(rc)"), None)
    assert idx is not None, "ci.yml 的 pytest 收尾写法变了，先更新本 harness"
    body = textwrap.dedent("\n".join(lines[idx + 1:]).lstrip("\n"))
    assert "coverage.xml" in body, "切出来的不是覆盖率对账那段"
    return body


def _run(body: str, os_name: str, workdir: pathlib.Path) -> subprocess.CompletedProcess:
    code = body.replace("${{ matrix.os }}", os_name)
    prog = "import re, sys\nfrom pathlib import Path\n" + code
    # env 必须显式给：本 harness 跑的是 python 子进程且按 utf-8 解码，
    # 不钉 PYTHONIOENCODING 就会静默继承开发机控制台代码页 —— 这正是
    # tests/test_subprocess_encoding_contract.py 守的那条，它会当场抓住本行。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.ImportFrom,)):
            defined |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, ast.Assign):
            for t in ast.walk(node):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    defined.add(t.id)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
            and isinstance(n.ctx, ast.Load)}
    missing = sorted(used - defined)
    assert not missing, f"对账段用到了未定义的名字：{missing}"
