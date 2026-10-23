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
import re
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


def _check_body(rc: int = 0) -> str:
    """切出「除 pytest 调用以外」的全部逻辑，供本地执行。

    为什么把 pytest 那几行摘掉：它要跑几分钟，与被测的对账逻辑无关。

    为什么**不能**从 `sys.exit(rc)` 之后开始切：stdout 的 utf-8 重配写在
    pytest 调用**之前**，只切后面就等于**把要验的那道防线排除在测试外** ——
    而那正是 run #43 windows-3.12 栽跟头的地方。harness 自己漏掉防线，
    比没有 harness 更危险。

    `rc` 是**注入的 pytest 退出码**：0 = pytest 全绿（默认），
    非 0 = pytest 红了。2026-10-22 修的那条「pytest 红了也要报出实测值」
    就靠它验 —— 而那条路径**只有 pytest 红了才走得到**。
    """
    lines = _step_source().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().startswith("rc = subprocess.call(")), None)
    assert start is not None, "ci.yml 的 pytest 调用写法变了，先更新本 harness"
    end = next((i for i in range(start, len(lines))
                if lines[i].strip() == "])"), None)
    assert end is not None, "ci.yml 的 pytest 调用收尾写法变了，先更新本 harness"
    body = textwrap.dedent("\n".join(lines[:start] + [f"rc = {rc}"] + lines[end + 1:]))
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


def _readme_detail(path: pathlib.Path) -> tuple[int, int]:
    """从 README 取「漏行 N / M 语句」的 (语句数, 漏行数)。"""
    m = re.search(r"漏行\s*([\d,]+)\s*/\s*([\d,]+)\s*语句",
                  path.read_text(encoding="utf-8"))
    assert m, f"{path.name} 里找不到「漏行 N / M 语句」—— CI 那段无从对账"
    return (int(m.group(2).replace(",", "")),
            int(m.group(1).replace(",", "")))


def _readme_pct(path: pathlib.Path) -> int:
    """从 README 取「覆盖率 **N%**」的 N —— **现读，绝不写死**。

    为什么不能写死（2026-10-22 实测踩过）：这个文件里有 12 处写过
    `**96%**` / `0.9598`。README 的覆盖率一改（它本来就该随代码变），
    这些用例立刻红 4 条 —— 而**红的原因与被守的东西无关**：不是门禁在
    报真问题，是**测试自己过期了**。

    更糟的是它把真信号淹了：那次本机全量红 8 条，其中 4 条是这条噪声、
    4 条才是真问题（GitHub API 额度耗尽）。**分不清哪条是信号、哪条是
    噪声的门禁，等于没有门禁。**

    所以期望值一律现读；只有**故意改坏 README** 的那条负向测试才写死
    具体值 —— 而它写的必须是**改坏后的那个值**。
    """
    m = re.search(r"覆盖率\s*\*\*(\d+)%\*\*", path.read_text(encoding="utf-8"))
    assert m, f"{path.name} 里找不到「覆盖率 **N%**」—— CI 那段无从对账"
    return int(m.group(1))


def _line_rate_for(pct: int) -> str:
    """造一个能被 `round(line-rate * 100)` 还原成 pct 的 line-rate。

    为什么用 `pct / 100` 而不是写死一个字符串：见 `_readme_pct` 的注释。
    写死的 `0.9598` 只在 README 恰好写 96% 时才等于那个百分比。
    """
    return f"{pct / 100:.10f}"


def test_this_file_never_hardcodes_the_readme_coverage_number():
    """**元测试**：本文件的**代码**里不得出现写死的覆盖率数字。

    2026-10-22 实测踩过：这个文件里有 12 处写过 `**96%**` / `0.9598`。
    README 的覆盖率一改（它本来就该随代码变），这些用例立刻红 4 条 ——
    而**红的原因与被守的东西无关**：不是门禁在报真问题，是**测试自己
    过期了**。

    更糟的是它把真信号淹了：那次本机全量红 8 条，其中 4 条是这条噪声、
    4 条才是真问题。**分不清哪条是信号、哪条是噪声的门禁，等于没有门禁。**

    所以期望值一律现读（`_readme_pct`），或由它算出来（`_line_rate_for`）。

    **判据用 AST 而不是正则**：注释与 docstring 里**正当性地**会出现
    这些字面量（讲清楚这件事本身就得写出「96%」），
    而正则分不清「代码」与「散文」。AST 只看**常量**，
    注释在 AST 里根本不存在。

    **改坏会红吗**：把 `_readme_pct(real)` 换回写死的 `96`，本条立刻红。
    """
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    # **docstring 要排除**：它讲清楚「为什么不能写死」时，
    # 正当地写着 `**96%**` / `0.9598` 这几个字面量 ——
    # 而 docstring 在 AST 里是「语句位置的第一个字符串常量」，
    # 可以精确地认出来（不需要猜）。
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    hard = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            # `**96%**` 形态：被包在 ** 里的百分数
            if re.search(r"\*\*\d{2}%\*\*", node.value):
                hard.append(node.value)
            # `0.9598` 形态：四位小数的 line-rate
            if re.search(r"0\.\d{4}\b", node.value):
                hard.append(node.value)
    assert not hard, (
        "本文件的代码里出现了写死的覆盖率数字：%s\n"
        "期望值必须**现读** README（_readme_pct），"
        "或由它算出来（_line_rate_for）—— "
        "写死的数字会随 README 更新而变成「测试自己过期了」的红，"
        "把真信号淹掉。" % sorted(set(hard)))


def test_the_ci_body_never_hardcodes_a_coverage_number():
    """**元测试**：ci.yml 那段对账正文**不许自己报出矩阵名**。

    同一条病在 ci.yml 上也犯过一次：那段本来就把「ubuntu-3.12 实测 X%」
    写进了消息（见 ci.yml 里的注释），于是 **3.11 那一格也自称
    ubuntu-3.12** —— 两格报出同一个数，而那个数来自测试夹具。

    这条守住的是：**矩阵身份必须当次求值**（`${{ matrix.os }}`），
    统一由 `matrix_label` 那个变量带出来。

    判据不是「源码里不许出现 ubuntu-3.12 这个词」—— 错误消息里
    **理应**说清口径是 ubuntu-3.12（那是给人看的解释）。
    判据是：**凡是要出现在「实测覆盖率」/「实测」旁边的矩阵名，
    必须来自 `matrix_label`**，不许是字面量。

    所以这里查的是**打印语句的格式串**：它若含矩阵名，就必须
    含 `{matrix_label}` 而不是写死的名字。

    **改坏会红吗**：把 ci.yml 里的 `matrix_label` 换回写死的
    「ubuntu-3.12」，本条立刻红。
    """
    tree = ast.parse(_check_body())
    # docstring 里正当性地写着「ubuntu-3.12 实测 X%」——
    # 正因为要讲清楚这个坑长什么样。docstring 在 AST 里是
    # 「语句位置的第一个字符串常量」，可以精确认出来。
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    hard = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant):
                if id(arg) in docstrings:
                    continue
                # 普通字符串常量：里面若写了矩阵名 + 「实测」⇒ 写死
                if ("ubuntu-3.12" in arg.value or "windows-3.12" in arg.value) \
                        and "实测" in arg.value:
                    hard.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                # f-string：把**词法单元**列出来（常量片段 / 插值），
                # 再看「实测」前面那一个单元是不是**写死的矩阵名**。
                #
                # 为什么不用「整句拼起来再搜」：那句里「{matrix_label} 实测 X%」
                # 与「—— README 的覆盖率口径就是 ubuntu-3.12」是**同一句**，
                # 后半句正当性地提到 ubuntu-3.12（那是解释口径给人看的）。
                # 真正要禁的是：**「实测」前面那个位置**写死了名字。
                tokens = []
                for v in arg.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        tokens.append(("const", v.value))
                    elif isinstance(v, ast.FormattedValue):
                        tokens.append(("var", v))
                for i, (kind, val) in enumerate(tokens):
                    if kind != "const" or "实测" not in val:
                        continue
                    # 往前找最近的那个「名字」单元
                    prev = None
                    for j in range(i - 1, -1, -1):
                        if tokens[j][0] == "const" and tokens[j][1].strip():
                            prev = tokens[j][1]
                            break
                        if tokens[j][0] == "var":
                            prev = tokens[j][1]
                            break
                    if prev is None:
                        continue
                    if isinstance(prev, str) and (
                            "ubuntu-3.12" in prev or "windows-3.12" in prev):
                        hard.append(str(arg))
    # **矩阵身份那个变量本身**也得是当次求值 ——
    # 上一版只查了 print 那一侧，于是把 `matrix_label = "ubuntu-3.12"`
    # 写回去时**一条都不红**（人工验红时逮到的装饰）。
    # 所以这里再查一遍：`matrix_label` 的**赋值**必须是
    # `${{ matrix.os }} ${{ matrix.python }}`。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets
                   if isinstance(t, ast.Name)]
        if "matrix_label" not in targets:
            continue
        if not isinstance(node.value, ast.Constant):
            continue
        val = node.value.value
        if "matrix.os" not in val or "matrix.python" not in val:
            hard.append("matrix_label = %r" % val)
    assert not hard, (
        "ci.yml 的对账正文里，矩阵身份**没有当次求值**：%s\n"
        "它必须来自 `${{ matrix.os }} ${{ matrix.python }}` —— "
        "写死会让别的矩阵自称 3.12，于是读到的数无从知道是哪一格测的。" % hard)


def test_the_measurement_channel_is_unconditional_and_identifies_its_matrix(
        tmp_path):
    """**测量通道**必须：无条件报出 + 带矩阵身份 + pytest 红了也报。

    为什么这是基础设施（2026-10-22 实测踩过）：

    * 「实测覆盖率到底是多少」这个问题，**只能**通过 annotation 问出来
      —— job 日志端点要 `actions:read` scope，普通 token 读不到。
    * 若那段只在**对账失败**时才说话，那么「README 那个数现在对不对」
      就没法在**不失败**的情况下确认 ⇒ 回填时只能靠猜。
    * 若 pytest 一红就提前退出（原先是 `sys.exit(rc)`），那么覆盖率
      **根本没被测**，而外面只看到一句「exit code 1」⇒ 那个数**永远问不出来**。
    * 若消息里写死矩阵名，另一格也会自称 3.12 ⇒ 读到的数无从知道
      是哪一格测的（**伪造观测**）。

    本条把这三条**一起**钉住：跑一遍真 body，看它有没有报、
    报的时候带没带自己的名字、pytest 红了报不报。

    **改坏会红吗**：
    - 把 `::notice title=覆盖率实测::` 那行删掉 ⇒ 本条红；
    - 把 `matrix_label` 换回写死 ⇒ 上一条红；
    - 把 `sys.exit(rc)` 挪回 pytest 后面 ⇒ pytest 红的那次就不报了 ⇒ 本条红。
    """
    body = _check_body()
    real = ROOT / "README.md"
    good = _fixture(tmp_path / "m", readme=real,
                    cov_line_rate=_line_rate_for(_readme_pct(real)))
    r = _run(body, "ubuntu-latest", good, python_version="3.12")
    assert "::notice title=覆盖率实测::" in r.stdout, (
        "实测值没有被无条件报出来 —— 那「实测覆盖率是多少」就问不出来：\n"
        + r.stdout)
    assert "ubuntu-latest 3.12" in r.stdout, (
        "实测值没有自报家门（矩阵身份）：\n" + r.stdout)

    # pytest 红了 ⇒ 仍然要报出实测值（否则那个数永远问不出来）
    body_red = _check_body(rc=1)
    r2 = _run(body_red, "ubuntu-latest", good, python_version="3.12")
    assert "::notice title=覆盖率实测::" in r2.stdout, (
        "pytest 红了就不再报实测值 —— 那个数就永远问不出来了：\n" + r2.stdout)
    # 且这一步**必须仍然红**（pytest 的失败不能被吞掉）
    assert r2.returncode != 0, (
        "pytest 全红，这一步却绿了 —— 最严重的信号被吞掉了：\n" + r2.stdout)


def _fixture(tmp: pathlib.Path, *, readme: pathlib.Path | None,
             cov_line_rate: str | None,
             detail: tuple[int, int] | None = None) -> pathlib.Path:
    """造一份只含 README.md 与 coverage.xml 的工作目录。

    `detail` = (语句数, 漏行数)，写进 coverage.xml 的
    lines-valid / lines-covered。**不传就从 README 解析** —— 那样
    「本该通过」的 fixture 永远与 README 一致，不会因为新加的对账而假红。

    但**测「README 写错必须红」时必须显式传真实值**：让 fixture 跟着
    被改坏的 README 走，就成了对账拿自己和自己比 —— 它从一开始就是绿的。
    **一个一开始就绿的「负向」测试，比没有它更糟**，因为它让覆盖看起来
    已经有了。
    """
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy(readme, work / "README.md")
    if cov_line_rate is not None:
        valid, missing = detail if detail else _readme_detail(work / "README.md")
        (work / "coverage.xml").write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.15.4" '
            f'timestamp="1" lines-valid="{valid}" '
            f'lines-covered="{valid - missing}" '
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
    # 这份 probe 全部命中，所以 line-rate=1.0 → 100%，与 README 写的那个值不等，
    # 那段必须因此红 —— 这才证明「它真的比了」，而不是只把数字打印出来。
    assert "实测覆盖率 100%" in r.stdout, r.stdout
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"README 写覆盖率 {_readme_pct(ROOT / 'README.md')}%" in r.stdout, r.stdout


def test_ubuntu_enforces_readme_number(tmp_path):
    body = _check_body()
    real = ROOT / "README.md"
    claimed = _readme_pct(real)
    good = _fixture(tmp_path / "a", readme=real,
                    cov_line_rate=_line_rate_for(claimed))
    assert _run(body, "ubuntu-latest", good).returncode == 0

    # 差一个百分点就必须红 —— 否则这条门禁是装饰。
    # 注意「差一个」是**相对 README 现值**算出来的，不是写死的 95/97：
    # 写死的话 README 一改，这条用例就变成「测试自己过期了」的红。
    wrong = claimed - 1
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        re.sub(r"(覆盖率\s*\*\*)\d+(%\*\*)", rf"\g<1>{wrong}\g<2>",
               real.read_text(encoding="utf-8"), count=1),
        encoding="utf-8")
    bad = _fixture(tmp_path / "b", readme=bad_readme,
                   cov_line_rate=_line_rate_for(claimed))
    r = _run(body, "ubuntu-latest", bad)
    assert r.returncode == 1, f"README 写错就必须红，却 rc={r.returncode}"
    assert f"README 写覆盖率 {wrong}%" in r.stdout


def test_pytest_failure_still_reports_the_measured_value(tmp_path):
    """**pytest 红了，实测值照样要报出来** —— 2026-10-22 修的那条。

    原来这段在 pytest 之后是 `sys.exit(rc)`：pytest 一红，
    **覆盖率根本没被测**，而外面只能看到一句「exit code 1」。
    于是「ubuntu-3.12 的覆盖率到底是多少」这个问题**再也问不出答案**。

    而这个问题**必须能问出来** —— README 里那个数就得靠它校。
    （本轮就栽在这：pytest 红了 → 对账没跑 → 我从 annotation 里读到的
    那个「96%」其实是**测试夹具里的假数**，差点被当成真值回填。）

    所以：pytest 红时**跳过对账**（残缺的一遍测出来的数不可信），
    但**实测值照样报出来**，且这一步**仍然要红**。

    **改坏会红吗**：把 ci.yml 里的 `pytest_green` 判断去掉（恢复成
    pytest 一红就 `sys.exit(rc)`），本条立刻红 —— 实测值不再被报出。
    """
    body = _check_body(rc=1)
    real = ROOT / "README.md"
    work = _fixture(tmp_path / "red", readme=real,
                    cov_line_rate=_line_rate_for(_readme_pct(real)))
    r = _run(body, "ubuntu-latest", work)
    # pytest 红了 ⇒ 这一步必须红（否则最严重的信号被吞掉了）
    assert r.returncode != 0, (
        "pytest 全红，这一步却绿了 —— 最严重的信号被吞了：\n"
        + r.stdout + r.stderr)
    # 但实测值必须被报出来
    assert f"实测覆盖率 {_readme_pct(real)}%" in r.stdout, (
        "pytest 红了就不再报实测值 —— "
        "那「实测覆盖率到底是多少」就再也问不出答案了：\n" + r.stdout)
    # 并且要说明白「对账被跳过了」，别让人把残缺的一遍当成结论
    assert "pytest 未全绿" in r.stdout, r.stdout


def test_the_measured_value_is_reported_even_when_the_gate_passes(tmp_path):
    """对账**通过**时，实测值也要报出来（不只在失败时报）。

    为什么：job 日志端点要 `actions:read` scope，普通 token 读不到 ——
    annotation 是**唯一**能让外面读到「实测值是多少」的通道。
    而如果它只在失败时说话，那么「README 那个数现在对不对」就
    **没法在不失败的情况下确认**。

    **改坏会红吗**：把 ci.yml 里那条 `::notice::` 删掉，本条立刻红。
    """
    body = _check_body()
    real = ROOT / "README.md"
    work = _fixture(tmp_path / "green", readme=real,
                    cov_line_rate=_line_rate_for(_readme_pct(real)))
    r = _run(body, "ubuntu-latest", work)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "::notice title=覆盖率实测::" in r.stdout, (
        "对账通过时不报实测值 —— 外面就无从知道 README 那个数"
        "当前对不对：\n" + r.stdout)


def test_the_measurement_names_its_own_matrix(tmp_path):
    """实测值必须**自报家门** —— 消息里要写明是哪一格测的。

    2026-10-22 实测踩过：那段正文里把「ubuntu-3.12 实测 X%」**写死**了，
    而它会被 tests/test_ci_step_execution.py 抽出来在本地跑 ——
    于是 **3.11 那一格也自称 ubuntu-3.12**，两格报出同一个数，
    读的人无从知道那个数是哪一格测的（更糟：那个数来自测试夹具）。

    **改坏会红吗**：把 ci.yml 里的 `matrix_label` 换回写死的
    「ubuntu-3.12」，本条立刻红。
    """
    body = _check_body()
    real = ROOT / "README.md"
    work = _fixture(tmp_path / "who", readme=real,
                    cov_line_rate=_line_rate_for(_readme_pct(real)))
    for os_name, py_ver in (("ubuntu-latest", "3.11"),
                             ("ubuntu-latest", "3.12"),
                             ("windows-latest", "3.12")):
        r = _run(body, os_name, work, python_version=py_ver)
        label = f"[{os_name} {py_ver}] 实测覆盖率"
        assert label in r.stdout, (
            f"实测值没自报家门（应为 {label!r}）：\n" + r.stdout)


def test_ubuntu_enforces_the_missing_lines_detail(tmp_path):
    """README 的「漏行 N / M 语句」必须与 ubuntu-3.12 实测相等。

    这两个数此前**没有任何门禁校过**：CI 那段只读 `line-rate`、比的是
    百分比，而 README 那一行同时印着这两个明细。

    「grep 不到所以没人校」这个推法本身不够硬 —— 门禁常常跨文件比对，
    源码里只会出现公式、不会出现结果。所以这里给的是正向证据：
    **把它们改坏，对账段必须红**。

    `detail` 传的是**真实** README 的值，也就是 coverage.xml 表示「实测值」；
    只改 README 一侧。第一版让 fixture 跟着被改坏的 README 走，于是对账
    拿自己和自己比，这条测试**从一开始就是绿的** —— 一个一开始就绿的
    「负向」测试比没有它更糟，它让覆盖看起来已经有了。
    """
    body = _check_body()
    real = ROOT / "README.md"
    detail = _readme_detail(real)
    text = real.read_text(encoding="utf-8")
    bad = re.sub(r"漏行\s*[\d,]+\s*/\s*[\d,]+\s*语句", "漏行 1 / 2 语句",
                 text, count=1)
    assert bad != text, "README 里找不到「漏行 N / M 语句」—— CI 那段无从对账"
    bad_readme = tmp_path / "bad-detail.md"
    bad_readme.write_text(bad, encoding="utf-8")
    work = _fixture(tmp_path / "detail", readme=bad_readme,
                    cov_line_rate=_line_rate_for(_readme_pct(real)),
                    detail=detail)
    r = _run(body, "ubuntu-latest", work)
    assert r.returncode == 1, f"明细写错却 rc={r.returncode}：{r.stdout}"
    assert "README 写漏行 1 / 2 语句" in r.stdout, r.stdout


def test_the_detail_gate_is_reference_matrix_only(tmp_path):
    """明细与百分比同口径：非参考矩阵照报不误，但不当成红。"""
    body = _check_body()
    real = ROOT / "README.md"
    detail = _readme_detail(real)
    text = real.read_text(encoding="utf-8")
    bad = re.sub(r"漏行\s*[\d,]+\s*/\s*[\d,]+\s*语句", "漏行 1 / 2 语句",
                 text, count=1)
    bad_readme = tmp_path / "bad-detail2.md"
    bad_readme.write_text(bad, encoding="utf-8")
    work = _fixture(tmp_path / "detail2", readme=bad_readme,
                    cov_line_rate=_line_rate_for(_readme_pct(real)),
                    detail=detail)
    r = _run(body, "windows-latest", work, python_version="3.12")
    assert r.returncode == 0, f"非参考矩阵不该判红：\n{r.stdout}{r.stderr}"
    assert f"实测覆盖率 {_readme_pct(real)}%" in r.stdout, "数字必须照样报出来，否则等于没测"


def test_the_real_readme_detail_matches_a_real_coverage_run(tmp_path):
    """**真 coverage 吐出来的** XML 必须带 lines-valid / lines-covered。

    新加的那段明细对账直接依赖这两个属性。若 coverage.py 哪天不写它们了，
    CI 上就会变成「每次都红」或「读不到就退出」—— 两种都是坏结果，而
    只有当场拿真的 coverage 产物验一次才能提前发现。

    合成 fixture 测不到这一点：它是我自己写的，写什么它就有什么。
    """
    work = tmp_path / "realxml"
    work.mkdir()
    _real_coverage_xml(work)
    xml = (work / "coverage.xml").read_text(encoding="utf-8")
    for attr in ("lines-valid", "lines-covered"):
        m = re.search(rf'{attr}="(\d+)"', xml)
        assert m, f"真 coverage.xml 里没有 {attr} —— CI 那段会永远读不到"
        assert int(m.group(1)) > 0, f"{attr} 为 0"


def test_every_statement_count_in_the_docs_is_the_same_number():
    """文档里**每一处**「N 语句」都必须是同一个数。

    为什么要有这条：CI 那段对账用的是 ``re.search`` —— 它只找**第一处**
    「漏行 N / M 语句」。所以同一份文档里**第二处**写错，它原理上看不见。

    实测（2026-10-24 第十二轮外部复核抓到的）：README 同一段里「4,298」
    出现两次、「4,279」出现一次 —— 而 4,279 是**旧值**
    （用 coverage 自己的配置重算，语句数是 4,298）。
    指标表那一行是对的、下一段的解释是旧的，而**全部门禁绿着**：
    没有一处看第二处。

    ⇒ 这条把「同一份文档里同一个量只能有一个值」变成判据。
    真正的「这个数对不对」由 CI 那段对账（拿实测值比）负责 ——
    这里是补上它看不见的那一半。

    **改坏会红吗**：把 README 里那处「4,298 语句」改成别的数，本条立刻红。
    """
    canonical = _readme_detail(ROOT / "README.md")[0]
    for name in ("README.md", "AGENTS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        found = {int(m.group(1).replace(",", ""))
                 for m in re.finditer(r"([\d,]+)\s*语句", text)}
        assert found, f"{name} 里一处「N 语句」都没有 —— 判据没生效，检查它"
        wrong = sorted(v for v in found if v != canonical)
        assert not wrong, (
            "%s 里的语句数 %s 与指标表里的 %d 不一致 —— "
            "同一个量在一份文档里只能有一个值（CI 那段对账只查第一处，"
            "看不见后面这些）" % (name, wrong, canonical))


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
    claimed = _readme_pct(real)
    wrong = claimed + 1
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        re.sub(r"(覆盖率\s*\*\*)\d+(%\*\*)", rf"\g<1>{wrong}\g<2>",
               real.read_text(encoding="utf-8"), count=1),
        encoding="utf-8")
    work = _fixture(tmp_path / f"{os_name}-{py_ver}", readme=bad_readme,
                    cov_line_rate=_line_rate_for(claimed))
    r = _run(body, os_name, work, python_version=py_ver)
    assert r.returncode == 0, (
        f"[{os_name} {py_ver}] 非参考矩阵不该判红：\n{r.stdout}{r.stderr}")
    assert f"实测覆盖率 {claimed}%" in r.stdout, "数字必须照样报出来，否则等于没测"


def test_reference_matrix_still_judges(tmp_path):
    """判据改严的**另一个方向**也要钉住：参考矩阵必须仍然会红。

    只测「非参考矩阵不红」是不够的 —— 那条与「哪里都不红」是同一条断言。
    """
    body = _check_body()
    real = ROOT / "README.md"
    claimed = _readme_pct(real)
    wrong = claimed + 1
    bad_readme = tmp_path / "bad.md"
    bad_readme.write_text(
        re.sub(r"(覆盖率\s*\*\*)\d+(%\*\*)", rf"\g<1>{wrong}\g<2>",
               real.read_text(encoding="utf-8"), count=1),
        encoding="utf-8")
    work = _fixture(tmp_path / "ref", readme=bad_readme,
                    cov_line_rate=_line_rate_for(claimed))
    r = _run(body, "ubuntu-latest", work, python_version="3.12")
    assert r.returncode == 1, (
        f"ubuntu-3.12 是参考矩阵，README 写错就必须红，却 rc={r.returncode}")
    assert f"README 写覆盖率 {wrong}%" in r.stdout


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
                    cov_line_rate=_line_rate_for(_readme_pct(ROOT / "README.md")))
    r = _run(body, "windows-latest", work, console_encoding="cp1252")
    out = r.stdout + r.stderr
    assert "UnicodeEncodeError" not in out, (
        f"cp1252 控制台下崩了，说明 stdout 没重配：\n{out}")
    assert "charmap" not in out, out
    assert r.returncode == 0, f"cp1252 控制台下必须跑完：\n{out}"
    assert f"实测覆盖率 {_readme_pct(ROOT / 'README.md')}%" in r.stdout, out


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


# ---------------------------------------------------------------------------
# 「N 道门禁」这个数不许漂
# ---------------------------------------------------------------------------
#: 哪几步算门禁 —— 白名单**两个方向都要封闭**：
#: 名单外多了一步没人发现，和名单里多写一步一样，都是同一个形状的假绿。
_GATE_STEPS = {
    "Lint (ruff)",
    "Adversarial evaluation (scorecard)",
    "Determinism check (same input -> same fingerprint)",
    "Self-proof (every README claim must hold)",
    "Reproducible build check (same source -> same bytes)",
    "Unit & integration tests with coverage",
    "Mutation scorecard (do the tests still have teeth?)",
    # v2.11.5 新增的三道行业标配门禁。
    # 它们挂在**全部三条矩阵**上而不是像变异记分卡那样只跑参考矩阵：
    # 变异记分卡量的是测试质量（与 OS / Python 无关），而这三道守的是
    # 「这个仓库此刻的安全与性能姿态」，那恰恰是最可能在某一条矩阵上
    # 被悄悄带进来的东西。
    "SAST (bandit baseline reconciliation)",
    "Dependency vulnerability scan (pip-audit)",
    "Performance regression (deterministic work counters)",
    # v2.12.4 新增：**产物本身**要过门禁。
    # 「仓库干净 ≠ 产物干净」—— 2.12.3 就是措辞清理之前构建的，产物里带着
    # 88 处禁用词（含一个真实内部主机名），而仓库当时已经是 0 命中。
    # 这一道查的是「**将要发布出去的那份字节**」，不是源码树。
    "Release artifact audit (content + self-consistency)",
    # v2.12.7 新增：**源码交付能不能自证**。
    # MANIFEST.in 开头承诺「拿到它的人应该能跑测试」—— 实测从 PyPI 拿到的
    # sdist 里跑 pytest 硬红 115 条（缺 .github/、.gitignore、.git 历史）。
    # 这一道查的是「**交付出去的那份源码**能不能自己跑绿」。
    "The sdist can run its own test suite",
}

#: 不是门禁的：装环境 / 装依赖 / 产物构建 / 传产物。
#: 「Build sdist and wheel」本身不是门禁 —— 它是「构建可复现」那一步的
#: 前置条件；把前置当门禁数进去，门禁数就和「有几道防线」脱钩了。
_NON_GATE_STEPS = {
    "Install dependencies",
    "Build sdist and wheel",
}


def _run_step_names() -> set:
    """只取**自己跑代码**（有 `run:`）的步骤 —— `uses:` 那类（checkout /
    setup-python / 传产物）是环境与搬运，不是门禁。"""
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    names = set()
    for job in doc["jobs"].values():
        for s in job.get("steps", []):
            if "run" in s and str(s.get("name", "")).strip():
                names.add(str(s.get("name", "")))
    return names


class TestGateCountCannotDrift:
    def test_the_two_lists_partition_every_run_step(self):
        steps = _run_step_names()
        assert "" not in steps, "ci.yml 里有一个没有名字的 run 步骤"
        unknown = steps - _GATE_STEPS - _NON_GATE_STEPS
        assert not unknown, (
            f"ci.yml 新增了这些会跑代码的步骤，但没被归类进门禁或非门禁：\n"
            f"  {sorted(unknown)}\n"
            f"请把它加进 _GATE_STEPS 或 _NON_GATE_STEPS —— "
            f"**不分类就会静默漏数**，而门禁数正是 README/AGENTS.md 对外写的。")
        ghost = (_GATE_STEPS | _NON_GATE_STEPS) - steps
        assert not ghost, f"名单里这些步骤在 ci.yml 里已经不存在了：{sorted(ghost)}"

    def test_the_documented_count_matches_the_workflow(self):
        actual = len(_GATE_STEPS)
        cn = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七",
              8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
        word = cn.get(actual, str(actual))
        docs = {
            "README.md": f"{word}道门禁",
            "docs/FAQ.md": f"**{word}道** CI 门禁",
            "AGENTS.md": f"CI 的{word}道门禁",
        }
        wrong = {}
        for rel, needle in docs.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            if needle not in text:
                wrong[rel] = needle
        assert not wrong, (
            f"ci.yml 实际有 {actual} 道门禁，但文档没这么写：{wrong}\n"
            f"对外数字必须能被数出来，不能靠人记得同步。")

    def test_the_mutation_gate_has_its_own_pinned_job(self):
        """变异测试量的是测试质量，与 OS / Python 版本无关；跑三遍是浪费。

        2026-10-21 起它不再挂在 `test` job 的某一格矩阵上，而是**自己一个
        job**（耗时差一个数量级，见 ci.yml 里的说明）。于是「只跑一条矩阵」
        这件事由 job 本身声明，而不是靠一个 `if:` 条件 ——
        **条件写错会静默地跑到三格上，job 写错至少看得见。**
        """
        doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
        job = doc["jobs"]["mutation"]
        assert job.get("runs-on") == "ubuntu-latest", (
            f"变异记分卡那个 job 必须钉在 ubuntu：runs-on={job.get('runs-on')!r}")
        setup = next(s for s in job["steps"]
                     if str(s.get("name", "")).startswith("Set up Python"))
        assert str(setup["with"]["python-version"]) == "3.12", (
            "变异记分卡那个 job 必须钉在 3.12："
            f"{setup['with']['python-version']!r}")
        step = next(s for s in job["steps"]
                    if str(s.get("name", "")).startswith("Mutation scorecard"))
        assert "if" not in step, (
            "独立 job 里不该再有 if: 条件 —— 那一维已经由 job 本身声明了")

