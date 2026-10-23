#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从**源码交付**（sdist）里跑它自己的测试，必须绿。

为什么要有这道门禁
------------------
`MANIFEST.in` 开头写着「拿到它的人应该能跑测试、读文档、复现 README 里的示例
命令」—— 那是一条**对外承诺**。

2026-10-23 第十一轮外部复核实测：从 PyPI 拿到的 sdist 里跑 pytest **硬红
115 条**（缺 `.github/workflows/*.yml`、`.gitignore`、`.git` 历史）。
承诺在、交付不跟 —— 那是一句空话。

修法分两半：
  · `.github/` 与 `.gitignore` 收进 sdist（MANIFEST.in）；
  · 确实需要 git 上下文的用例**跳过**（tests/conftest.py 有一份显式名单）。

但「修好了」不能靠人记 —— 所以这一条把承诺变成**可执行的判据**：
解包 sdist，在解包目录里跑它自己的测试，红了就是红。

用法
----
    python scripts/check_sdist_runs_its_own_tests.py                # 用 dist/ 里的 sdist
    python scripts/check_sdist_runs_its_own_tests.py --sdist <路径>   # 指定 sdist

退出码：0 = 绿；1 = 有测试没过；2 = 找不到 sdist / 跑不起来。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Windows 控制台默认 cp1252，打印中文会抛 UnicodeEncodeError ——
# 症状极具误导性：门禁本身跑得好好的，却因为「打印表格时编码失败」让整个
# step 变红，看起来像功能坏了。scripts/audit_wording.py 顶部就有这段重配，
# 这里照抄。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent


def find_sdist(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    cands = sorted((ROOT / "dist").glob("log_ai_compressor-*.tar.gz"))
    return cands[-1] if cands else None


def failing_modules(output: str) -> list[str]:
    """从 pytest 的输出里找出**红了哪些模块**。

    为什么要有它：原先这道门禁红了只说「两种可能」（MANIFEST 漏收 /
    新模块忘了登记），而排查的人得自己猜是哪个文件。
    2026-10-24 第十二轮外部复核指出：真正能兜住「新增一个需要仓库上下文
    的测试模块却忘了登记」的**就是这道门禁**（经验判据：真跑一遍），
    所以它必须**说清楚是哪个模块**红了 —— 否则「兜底」只是兜住了，
    却让人多花一轮排查。

    判据：pytest 短摘要里的 ``FAILED <路径>::<用例>`` 行。
    """
    out: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("FAILED "):
            continue
        path = line[len("FAILED "):].split("::", 1)[0].split(" - ", 1)[0].strip()
        name = Path(path).name
        if name and name not in out:
            out.append(name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 sdist 里跑它自己的测试")
    ap.add_argument("--sdist", default=None, help="sdist 路径（默认取 dist/ 里最新那个）")
    args = ap.parse_args(argv)

    sdist = find_sdist(args.sdist)
    if sdist is None:
        print("找不到 sdist —— 先构建（python scripts/build_repro.py）", file=sys.stderr)
        return 2
    print("sdist：%s（%d 字节）" % (sdist.name, sdist.stat().st_size))

    with tempfile.TemporaryDirectory(prefix="lac-sdist-test-") as td:
        tmp = Path(td)
        with tarfile.open(sdist, "r:gz") as t:
            try:
                t.extractall(tmp, filter="data")   # py3.12+；顺带挡住路径穿越
            except TypeError:
                # py3.11.0~3.11.3 没有 filter 参数 —— 逐个成员校验后解出来，
                # 而不是无校验地 extractall（归档里带 ../ 就能写到 temp 外面）。
                for m in t.getmembers():
                    name = m.name
                    if name.startswith("/") or ".." in Path(name).parts:
                        print("归档里有不安全的成员名：%r" % name, file=sys.stderr)
                        return 2
                    if m.isdir():
                        (tmp / name).mkdir(parents=True, exist_ok=True)
                    elif m.isfile():
                        target = tmp / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        fh = t.extractfile(m)
                        if fh is not None:
                            target.write_bytes(fh.read())
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if not roots:
            print("解包后没找到顶层目录", file=sys.stderr)
            return 2
        pkg = roots[0]
        print("解包到 %s" % pkg.name)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("PYTHONWARNDEFAULTENCODING", None)
        print("在解包目录里跑它自己的测试（需要 git 上下文的用例按 conftest 跳过）……")
        # 边跑边打印（这一道要跑十几分钟，闷着跑会让人以为卡住了），
        # 同时把输出留下来 —— 红了要靠它点名是哪个模块。
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
            cwd=str(pkg), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        collected: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            collected.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = proc.wait()
        if rc != 0:
            bad = failing_modules("".join(collected))
            print()
            print("**sdist 跑不了它自己的测试** —— 上面那些红就是原因。")
            if bad:
                print("红了的是这些模块：%s" % "、".join(bad))
            print("两种可能：")
            print("  · MANIFEST.in 漏收了某个文件（改 MANIFEST.in，别改测试）")
            print("  · 新增的测试需要 git 上下文，却没登记进 tests/conftest.py 的名单"
                  "（改 tests/conftest.py 的 NEEDS_REPO_CONTEXT）")
            return 1
    print()
    print("sdist 能跑自己的测试。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
