#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计**将要发布出去的产物**：解包 → 用当前门禁扫一遍 → 再跑它自己的门禁。

为什么要有这个脚本
------------------
**仓库干净 ≠ 已发布版本干净。** 这是 2026-10-21 实测出来的：

* 措辞门禁在仓库上 0 命中（它守的是**工作区**）；
* 但 PyPI 上的最新发布版 **2.12.3** 是**措辞清理之前**构建的 ——
  产物里实测 **88 处**、**38 个文件**，其中**产品代码 9 处**
  （注释、文档字符串，以及**一个真实内部主机名**）。

清理只改了仓库，**没重建产物** —— 于是发出去的那一版装的还是旧代码。
而 PyPI 不允许覆盖已发布文件，那一版**永远留在那儿**。

所以发版清单必须多这一步：**产物本身要过门禁**，不是源码树过就算。

它查两件事
----------
1. **内容口径**：产物里的每个文本文件，用 `tests/test_no_job_wording.py`
   里那套词表与匹配规则扫一遍（同一个模块，同一份口径）。
2. **自洽性**：把 sdist 解包后，**在解包目录里**跑它自带的
   `scripts/sast_gate.py` —— 产物里带的基线与产物里带的代码必须对得上
   （v2.12.2 的 sdist 就是这么红的：基线写 3 条，代码实测 4 条）。

用法
----
    python scripts/audit_release_artifact.py                 # 用 pyproject 里的版本号
    python scripts/audit_release_artifact.py --version 2.12.4
    python scripts/audit_release_artifact.py --dist dist

退出码：0 = 通过；1 = 产物里有禁用词；2 = 产物缺失或跑不起来。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "tests" / "test_no_job_wording.py"

# Windows 控制台默认 cp1252/cp936，打印中文会抛 UnicodeEncodeError ——
# 症状极具误导性：审计本身跑得好好的，却因为「打印表格时编码失败」让
# 整个 step 变红，看起来像功能坏了。scripts/audit_wording.py 顶部就有
# 这段重配，这里照抄。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass


def load_gate():
    """加载门禁模块 —— 词表与匹配规则只从它那里来（口径只有一份）。"""
    spec = importlib.util.spec_from_file_location("wording_gate_artifact", GATE)
    if spec is None or spec.loader is None:
        raise SystemExit("读不到门禁文件 %s" % GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not getattr(mod, "FORBIDDEN", None):
        raise SystemExit("%s 里没有 FORBIDDEN —— 门禁的形状变了" % GATE)
    return mod


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("pyproject.toml 里找不到 version")
    return m.group(1)


def scan_bytes(raw: bytes) -> list[tuple[str, int]]:
    """一段字节里的禁用词（与门禁同口径；二进制跳过）。"""
    if b"\0" in raw[:8000]:
        return []
    text = raw.decode("utf-8", "replace")
    out = []
    for w in FORBIDDEN:
        n = COUNT(text, w)
        if n:
            out.append((w, n))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="审计将要发布出去的产物：解包 → 用当前门禁扫 → 跑它自己的门禁")
    ap.add_argument("--version", default=None, help="版本号（默认取 pyproject.toml）")
    ap.add_argument("--dist", default=str(ROOT / "dist"), help="产物目录")
    args = ap.parse_args(argv)

    global FORBIDDEN, COUNT
    gate = load_gate()
    FORBIDDEN = list(gate.FORBIDDEN)
    COUNT = gate._count

    version = args.version or project_version()
    dist = Path(args.dist)
    wheel = dist / ("log_ai_compressor-%s-py3-none-any.whl" % version)
    sdist = dist / ("log_ai_compressor-%s.tar.gz" % version)
    for p in (wheel, sdist):
        if not p.is_file():
            print("产物不存在：%s" % p, file=sys.stderr)
            return 2
    print("审计产物：%s / %s" % (wheel.name, sdist.name))
    print("词表：%d 条" % len(FORBIDDEN))

    hits = []
    n_files = 0
    extract_failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lac-artifact-") as tmp:
        tmpdir = Path(tmp)
        # ---- 1) 内容口径：逐文件扫 ----
        with zipfile.ZipFile(wheel) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                n_files += 1
                found = scan_bytes(z.read(info.filename))
                if found:
                    hits.append((wheel.name + " :: " + info.filename, found))
        with tarfile.open(sdist, "r:gz") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                n_files += 1
                fh = t.extractfile(m)
                if fh is None:
                    continue
                found = scan_bytes(fh.read())
                if found:
                    hits.append((sdist.name + " :: " + m.name, found))
                # 把 sdist 解出来，后面要在解包目录里跑它自己的门禁
                try:
                    t.extract(m, tmpdir)
                except Exception as exc:                    # noqa: BLE001
                    extract_failures.append("%s：%r" % (m.name, exc))

        # ---- 2) 自洽性：在解包目录里跑它自带的 SAST 门禁 ----
        roots = [p for p in tmpdir.iterdir() if p.is_dir()]
        sast_rc = None
        sast_note = "（没找到解包后的目录，跳过）"
        if roots:
            pkg = roots[0]
            gate_py = pkg / "scripts" / "sast_gate.py"
            if gate_py.is_file():
                proc = subprocess.run([sys.executable, str(gate_py)],
                                      cwd=str(pkg), capture_output=True,
                                      text=True, encoding="utf-8", errors="replace",
                                      env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                      timeout=900)
                sast_rc = proc.returncode
                sast_note = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
                sast_note = sast_note[0] if sast_note else ""
            else:
                sast_note = "（sdist 里没有 scripts/sast_gate.py，跳过）"

    print()
    print("扫了 %d 个文件" % n_files)
    if extract_failures:
        print("解包失败 %d 个成员（自洽性那一半因此跑不完整）：" % len(extract_failures))
        for f in extract_failures[:5]:
            print("   %s" % f)
    print("产物自带的 SAST 门禁：%s —— %s"
          % ("通过" if sast_rc == 0 else ("未跑" if sast_rc is None else "**失败**"),
             sast_note))

    if not hits:
        print()
        print("**产物内容 0 命中**")
    else:
        print()
        print("**产物里有禁用词：%d 个文件**" % len(hits))
        for name, found in hits:
            print("  %s" % name)
            for w, n in found:
                print("     [%s] x%d" % (w, n))

    if hits or sast_rc not in (0, None) or extract_failures:
        print()
        print("**产物审计未通过** —— 不要发布。"
              "（已发布到 PyPI 的版本不可覆盖，只能再发一版。）")
        return 1
    print()
    print("产物审计通过：内容 0 命中，产物自带门禁也绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
