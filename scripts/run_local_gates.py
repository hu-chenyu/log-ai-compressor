# -*- coding: utf-8 -*-
"""本地把 CI 的门禁**全部**跑一遍 —— 一条命令，不许挑。

为什么要有这个脚本（2026-10-21 的一次真实事故）
-----------------------------------------------
`b51ab7a` 那次推送把 `main` 推成了**红的**：三矩阵的
「Unit & integration tests with coverage」全挂。而本地那份门禁清单报的是
**「全绿」**。

根因不是「忘了跑」——**那份清单本身就是残的**：它只列了 10 道，
**漏掉了 pytest 全量与变异记分卡**（恰恰是最重的两道，也恰恰是能抓住
这次事故的那一道）。

一份报「全绿」却没跑最重两道的清单，比没有清单更糟 ——
没有清单你知道那里没检查。

所以这个脚本住在**仓库里**（不在 TEMP 里），而且它**直接从
`.github/workflows/ci.yml` 派生**要跑哪些步骤 —— 结构上就不可能漏：

* ci.yml 里每个**自己跑代码**的步骤 → 本地跑一遍；
* 「Install dependencies」不算门禁（本地环境已经有了）；
* 两段带 `${{ matrix.* }}` 占位符的内联脚本用**声明在案**的本地等价物
  （占位符在本地没有意义）；其余内联脚本**原样跑**。

另有一道门禁盯着它：`tests/test_local_gate_runner_covers_ci.py`
断言「本清单覆盖 ci.yml 里的每一条门禁步骤」，且**点名** pytest 与
变异记分卡必须在里面 —— 少了哪一条都红。

用法
----
    python scripts/run_local_gates.py                  # 全跑（十几分钟到半小时）
    python scripts/run_local_gates.py --list           # 只列计划
    python scripts/run_local_gates.py --only unit-integration-tests-with-coverage
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

# Windows runner 的控制台代码页是 cp1252，而下面要 print 中文。
# 不先把 stdout/stderr 切到 utf-8，打印中文会抛 UnicodeEncodeError ——
# 门禁本身全绿，step 却红了，而红的原因与门禁毫无关系。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"

#: git 的**绝对路径**（写死 "git" 会让 bandit 的 B607 报「用部分路径启动进程」——
#: 那一处**可以修，所以修**：测试里用 shutil.which 解析，跨平台一致）。
_GIT = shutil.which("git")

#: 「装依赖」不算门禁（按**步骤名**排除）。
_SETUP_STEP = "install dependencies"

#: 本地怎么调用那些不在 PATH 约定里的可执行文件。用**当前解释器**跑，
#: 免得「本地跑的是另一个 Python」这种错位。
_LOCAL_BINARIES = {
    "python": [sys.executable],
    "ruff": [sys.executable, "-m", "ruff"],
}

#: 带 `${{ matrix.* }}` 占位符的内联脚本的**本地等价物**。
#: 占位符在本地没有意义（没有矩阵），所以这两步本地换一条等价的命令 ——
#: 但**换的是什么必须写在这里**，不许临时发挥，也不许静默跳过。
LOCAL_EQUIVALENTS = {
    "unit-integration-tests-with-coverage": [
        sys.executable, "-m", "pytest",
        "--cov=log_ai_compressor",
        "--cov-report=term-missing",
        "--cov-report=xml",
        "--cov-fail-under=90",
    ],
}


def key(step: dict) -> str:
    """步骤的稳定标识（用于 `--only` 与对账）。"""
    return re.sub(r"[^a-z0-9]+", "-", str(step["name"]).lower()).strip("-")


def steps() -> list[dict]:
    """ci.yml 里每一条**自己跑代码**的步骤。

    判据只认结构（`run` 在不在），不认步骤名 —— 步骤名可以随便改，
    `run` 正文才是「它到底跑没跑东西」。
    """
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    out: list[dict] = []
    for job_name, job in doc["jobs"].items():
        for step in job.get("steps", []):
            if "run" not in step:
                continue
            name = str(step.get("name", "")).strip()
            if name.lower().startswith(_SETUP_STEP):
                continue
            out.append({
                "job": job_name,
                "name": name,
                "run": str(step["run"]),
                "shell": step.get("shell"),
            })
    return out


def argv(step: dict) -> list[str]:
    """这一步本地怎么跑。

    **刻意不用 `shell=True`** —— 命令行里不需要 shell，少一条 SAST 豁免。
    """
    body = step["run"].strip()
    if step.get("shell") == "python":
        k = key(step)
        if "${{" in body:
            eq = LOCAL_EQUIVALENTS.get(k)
            if eq is None:
                raise SystemExit(
                    "步骤 %r 带占位符，却没有声明本地等价物 —— "
                    "不许静默跳过（那正是这次事故的形状）" % step["name"])
            return list(eq)
        return [sys.executable, "-c", body]
    parts = body.split()
    head = parts[0] if parts else ""
    if head in _LOCAL_BINARIES:
        return _LOCAL_BINARIES[head] + parts[1:]
    return parts


def _github_token() -> str:
    """给子进程准备一个 GitHub token（**匿名的额度是每 IP 每小时 60 次**）。

    面 6（Release 标题与正文）每次跑门禁都要拉一次 Release 列表；
    共享出口 IP 很容易撞上限，于是门禁会**红** —— 那是诚实的红，
    但红的原因与仓库无关。

    token 从哪来：环境变量里有就用；没有就从**本机已存的 git 凭据**里取
    （`git credential fill`）—— **不打印、不落盘、不写进任何文件**。
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if _GIT is None:
        return ""
    try:
        proc = subprocess.run(
            [_GIT, "credential", "fill"],
            input="protocol=https\nhost=github.com\nusername=hu-chenyu\n\n",
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, shell=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def main(argv_list: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地跑 CI 的全部门禁")
    parser.add_argument("--list", action="store_true", help="只列计划，不执行")
    parser.add_argument("--only", nargs="*", default=None,
                        help="只跑这几步（按 key，见 --list）")
    args = parser.parse_args(argv_list)

    plan = steps()
    if args.only:
        wanted = set(args.only)
        plan = [s for s in plan if key(s) in wanted]
    if args.list:
        for s in plan:
            print("%-52s %-8s %s" % (key(s), s["job"], s["name"]))
        return 0

    env = dict(os.environ)
    token = _github_token()
    if token:
        env["GITHUB_TOKEN"] = token
        print("（已带上 GitHub token —— 免得面 6 撞匿名额度）")
    else:
        print("（没有 GitHub token —— 面 6 走匿名额度，每 IP 每小时 60 次；")
        print("  连续自测很容易撞上限，而那时门禁红的原因与仓库无关）")
    results: list[tuple[str, int, float]] = []
    for s in plan:
        print("=" * 70)
        print("RUN", key(s), "|", s["name"])
        t0 = time.perf_counter()
        # shell=False 写明：命令逐词拆开跑，不需要 shell（少一条豁免）
        proc = subprocess.run(argv(s), cwd=str(ROOT), shell=False, env=env)
        dt = time.perf_counter() - t0
        print("   rc=%d  %.1f s" % (proc.returncode, dt))
        results.append((key(s), proc.returncode, dt))

    print("=" * 70)
    print("门禁汇总：")
    for k, rc, dt in results:
        print("  %-52s %-8s %6.1f s" % (k, "OK" if rc == 0 else "FAIL rc=%d" % rc, dt))
    bad = [k for k, rc, _ in results if rc != 0]
    print()
    print("结论：", "全绿" if not bad else "红了：%s" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
