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

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
_CI = ROOT / ".github" / "workflows" / "ci.yml"
_STEP_PREFIX = "Unit &"
_DET_STEP_PREFIX = "Determinism check"


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
         console_encoding: str = "utf-8",
         python_version: str = "3.12") -> subprocess.CompletedProcess:
    code = (body.replace("${{ matrix.os }}", os_name)
                .replace("${{ matrix.python }}", python_version))
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


def _determinism_step_source() -> str:
    """抽出 CI 第 3 道门禁（确定性指纹）那段的**逐字原文**。

    和 `_step_source` 同一个思路：测的是 CI 里真正会跑的那份，不是副本。
    """
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    steps = doc["jobs"]["test"]["steps"]
    hit = [s for s in steps
           if str(s.get("name", "")).startswith(_DET_STEP_PREFIX)]
    assert hit, "ci.yml 里找不到确定性那一步，先更新本 harness"
    return hit[0]["run"]


def _run_determinism(prologue: str, workdir: pathlib.Path,
                     ) -> subprocess.CompletedProcess:
    """按 CI 的方式跑那段：`shell: python`，整段喂给 `python -c`。

    `prologue` 排在前面，用来在真跑之前替换掉 `result_to_dict` ——
    这是唯一能让「三次跑出不同指纹」在本地发生的办法，而**那正是要看
    门禁会不会红的那一侧**。
    """
    prog = textwrap.dedent(prologue) + "\n\n" + _determinism_step_source()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-c", prog], cwd=str(workdir),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


#: 注入到**子进程**里的补丁。CI 那段自己会 `subprocess.call([sys.executable,
#: "-c", code])` 起一个新进程 —— 第一版 harness 在父进程里替换
#: `result_to_dict`，子进程根本看不到，三次跑出来照样是同一个指纹，
#: 于是「三次不同指纹必须红」这条断言测的是一个**没被改坏的**程序。
#: （这是本仓库记过多次的同一个形状：观测到的成功值描述的是另一个对象。）
_SITECUSTOMIZE = '''\
import os
import sys

_MODE = os.environ.get("LAC_MUTATE_FP")
if _MODE:
    sys.path.insert(0, {root!r})
    from log_ai_compressor import service as S

    _orig = S.result_to_dict

    if _MODE == "diverge":
        _n = [0]

        def _patched(res, *args, **kwargs):
            d = _orig(res, *args, **kwargs)
            _n[0] += 1
            d["determinism_fingerprint"] = "fp-" + str(_n[0])
            return d
    else:
        def _patched(res, *args, **kwargs):
            d = _orig(res, *args, **kwargs)
            d["determinism_fingerprint"] = "fp-fixed"
            return d

    S.result_to_dict = _patched
'''


def _run_determinism(mode: str | None, scratch: pathlib.Path,
                     ) -> subprocess.CompletedProcess:
    """按 CI 的方式跑那段：`shell: python`，整段喂给 `python -c`。

    cwd 固定为仓库根（CI 就是在那儿跑的，所以那段里的 `'.'` 指仓库）；
    注入用的 sitecustomize 放在 `scratch` 里 —— 写进仓库会留下杂物目录。

    `mode` 为 None 时不注入任何补丁（跑现行代码）；"diverge" 让三次调用
    返回三个不同指纹；"agree" 让它们返回同一个。
    """
    site_dir = scratch / "_sitecustomize"
    site_dir.mkdir(exist_ok=True)
    (site_dir / "sitecustomize.py").write_text(
        _SITECUSTOMIZE.format(root=str(ROOT)), encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=str(site_dir))
    env.pop("LAC_MUTATE_FP", None)
    if mode:
        env["LAC_MUTATE_FP"] = mode
    return subprocess.run(
        [sys.executable, "-c", _determinism_step_source()],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )


class TestDeterminismGateHasTeeth:
    """确定性那段此前**一条测试都没有**，`== 1` 被改成 `>= 1` 时全量测试照绿。

    门禁写着「同一份日志连跑三次，指纹必须逐字节相同」，而把这条判据
    放松成「至少有一个指纹」之后，没有任何东西会叫 —— 一个自称守确定性
    的门禁，恰恰在最该叫的那一侧是哑的。
    """

    def test_the_real_tree_passes(self, tmp_path):
        """不加任何干预：现行代码跑三次必须得到同一个指纹。

        这条同时验的是 harness 抽出来的确实是 CI 会跑的那段。
        """
        r = _run_determinism(None, tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "fingerprints = 1" in r.stdout, r.stdout

    def test_it_goes_red_when_fingerprints_differ(self, tmp_path):
        r = _run_determinism("diverge", tmp_path)
        assert r.returncode == 1, (
            "三次跑出不同指纹却仍然放行 —— 确定性门禁是装饰的：\n"
            + r.stdout + r.stderr)
        assert "fingerprints = 3" in r.stdout, r.stdout
        assert "确定性校验失败" in r.stdout, r.stdout

    def test_it_stays_green_when_fingerprints_agree(self, tmp_path):
        """反例对照：不该红的别乱红（收紧判据的代价就在这一条上）。"""
        r = _run_determinism("agree", tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "fingerprints = 1" in r.stdout, r.stdout

    def test_the_source_asserts_exactly_one(self):
        """静态兜底：判据必须真的是 `== 1`。

        行为那条已经能抓住 `>= 1`；这条再钉一层，是为了让**只看 diff 的人**
        也能一眼看到这条判据没被放松。
        """
        src = _determinism_step_source()
        assert "len(fps) == 1" in src, (
            "确定性判据不再是「恰好一个指纹」了：\n"
            + "\n".join(ln for ln in src.splitlines() if "fps" in ln))


def _real_coverage_xml(work: pathlib.Path) -> None:
    """用本机**真的 coverage** 吐一份 coverage.xml 到 work。

    为什么不用「上一次跑剩的那个」：coverage.xml 是 pytest **会话结束时**
    才写出来的，而 CI 是干净 checkout —— 所以依赖 `ROOT/coverage.xml` 是否
    存在的那条测试，在 CI 三条矩阵上**全部 skip**，只有本地脏工作区才会真跑。

    这与 v2.11.1 那次「`git check-ignore` 目录规则的探针在本地和 CI 语义
    不同」是同一个形状：**本地那次绿什么也没证明。**
    skip 出来的绿和真跑一遍的绿在 CI 界面上没有任何区别。
    """
    probe = work / "probe.py"
    probe.write_text("def f(x):\n    return x + 1\n\n\nf(1)\n", encoding="utf-8")
    env = dict(os.environ, COVERAGE_FILE=str(work / ".coverage"))
    subprocess.run([sys.executable, "-m", "coverage", "run", str(probe)],
                   check=True, capture_output=True, cwd=str(work), env=env)
    subprocess.run([sys.executable, "-m", "coverage", "xml",
                    "-o", str(work / "coverage.xml")],
                   check=True, capture_output=True, cwd=str(work), env=env)


def test_check_body_parses_a_really_generated_coverage_xml(tmp_path):
    """正则要吃得下**真 coverage 吐出来的** XML —— 手写的合成文件测不到。

    这条在任何机器上都会跑，包括 CI 的三条矩阵。
    """
    work = tmp_path / "real"
    work.mkdir()
    shutil.copy(ROOT / "README.md", work / "README.md")
    _real_coverage_xml(work)
    r = _run(_check_body(), "ubuntu-latest", work)
    out = r.stdout + r.stderr
    assert "NameError" not in out, f"对账段引用了未定义的名字：\n{out}"
    assert "解析 coverage.xml 失败" not in out, f"正则吃不下真实 coverage 输出：\n{out}"
    # 这份 probe 全部命中，所以 line-rate=1.0 → 100%，与 README 的 96% 不等，
    # 那段必须因此红 —— 这才证明「它真的比了」，而不是只把数字打印出来。
    assert "实测覆盖率 100%" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr
    assert "README 写覆盖率 96%" in r.stdout, r.stdout


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


@pytest.mark.parametrize("os_name,py_ver", [
    ("windows-latest", "3.12"),
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.11"),   # 同一格重复一次也不该改变结论
])
def test_non_reference_matrix_reports_but_does_not_judge(
        tmp_path, os_name, py_ver):
    """非参考矩阵：数字照样报出来，但不参与判定。

    这是 v2.9.1 那次教训的直接对策 —— 覆盖率是**平台相关**的量，
    拿它去卡三个平台等于卡了一个不存在的约束。

    「参考矩阵」的定义是 **ubuntu + 3.12 两维同时成立**（AGENTS.md §4：
    「覆盖率对账只在 ubuntu-3.12 上做」）。原先 ci.yml 只比了 os，于是
    ubuntu-3.11 也参与判定 —— 今天两边数值恰好相同所以没红，但那不是设计。
    这条把 3.11 单独钉出来：**只比 os 的那版会在这里红**。
    """
    body = _check_body()
    real = ROOT / "README.md"
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        real.read_text(encoding="utf-8").replace("**96%**", "**97%**"),
        encoding="utf-8")
    work = _fixture(tmp_path / f"{os_name}-{py_ver}", readme=bad_readme,
                    cov_line_rate="0.9598")
    r = _run(body, os_name, work, python_version=py_ver)
    assert r.returncode == 0, (
        f"[{os_name} {py_ver}] 非参考矩阵不该判红：\n{r.stdout}{r.stderr}")
    assert "实测覆盖率 96%" in r.stdout, "数字必须照样报出来，否则等于没测"


def test_reference_matrix_still_judges(tmp_path):
    """判据改严的**另一个方向**也要钉住：参考矩阵必须仍然会红。

    只测「非参考矩阵不红」是不够的 —— 那条与「哪里都不红」是同一条断言。
    """
    body = _check_body()
    real = ROOT / "README.md"
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        real.read_text(encoding="utf-8").replace("**96%**", "**97%**"),
        encoding="utf-8")
    work = _fixture(tmp_path / "ref", readme=bad_readme, cov_line_rate="0.9598")
    r = _run(body, "ubuntu-latest", work, python_version="3.12")
    assert r.returncode == 1, (
        f"ubuntu-3.12 是参考矩阵，README 写错就必须红，却 rc={r.returncode}")
    assert "README 写覆盖率 97%" in r.stdout


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
