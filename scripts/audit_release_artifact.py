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

2026-10-23 第十一轮外部复核后补了两件事
----------------------------------------
上面两件事**查不到**「产物里混进了源码树没有的文件」与「产物里带着构建机的
绝对路径」。实测：已发布的 2.12.6 sdist 里夹着三个**从未进过任何 commit** 的
文件（``examples/app_report.md`` 等），它们嵌着构建机的绝对路径与账户名
（各出现 7～11 次），而这一版的门禁在**这份产物上照样通过**。

机制是三件各自都正确的事凑在一起：

* ``MANIFEST.in`` 收了 ``examples/*`` —— 生成物也匹配；
* ``.gitignore`` 把 ``*_report.*`` 挡住 —— 它们在 ``git status`` 里**隐身**；
* 构建在工作区里跑 —— 它们被静默打包。

⇒ 现在补两道：

3. **产物里的每个文件都必须来自源码树**（构建工具自己生成的元数据除外）
   —— 否则产物里就有**从未被复核过**的内容。
4. **产物里不能出现构建机的路径**（仓库根目录、用户主目录）——
   那是个人信息，一旦发到 PyPI 就撤不回来。

「239 个文件、0 命中」是真的，但**由它推不出「产物干净」**：词表里没有
「个人路径」这一类。

用法
----
    python scripts/audit_release_artifact.py                 # 用 pyproject 里的版本号
    python scripts/audit_release_artifact.py --version X.Y.Z
    python scripts/audit_release_artifact.py --dist dist

退出码：0 = 通过；1 = 产物里有禁用词/野文件/构建机路径；
2 = 产物缺失、跑不起来，**或某一项检查无法执行（无法判定）**。

2026-10-24 第十二轮外部复核后补的一条纪律：**「未执行」不许说成「通过」**。
这个脚本随 sdist 发布，而 sdist 里没有 ``.git`` ⇒ 在那里跑它拿不到源码树
清单 —— 而那一项一旦「跳过」，就不能再打印「产物里的文件全部来自源码树」。
（详见 :func:`committed_files` 的注释。）
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
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


#: 构建工具自己生成的元数据 —— 它们**不来自源码树**，但也不是「野生文件」。
_BUILD_METADATA = re.compile(
    r"^(PKG-INFO|setup\.cfg|.*\.egg-info/.*|.*\.dist-info/.*)$")


def committed_files() -> set[str] | None:
    """源码树（HEAD）里有哪些文件，相对仓库根目录。

    用 ``git ls-tree`` 而不是遍历工作区 —— 工作区里有未入库/被忽略的文件，
    而那正是要查出来的东西。

    git 用**绝对路径**起（SAST 的 B607：部分路径启动进程）。解析不到就返回
    ``None`` —— 意思是「**这一项无法执行**」，而不是「没有野文件」。

    为什么不能返回空集合：空集合与「真的一个文件都没有」长得一模一样，
    而调用方会把它当成「没有野文件」⇒ **报检查通过，但什么都没扫**。
    2026-10-24 第十二轮外部复核抓到的正是这个：脚本随 sdist 发布，
    而从 sdist 里跑它（很自然的用法）拿不到 git 上下文，于是它一边打印
    「跳过这一项」，一边打印「**产物里的文件全部来自源码树**」，退出码 0。
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # 不是 git 仓库（或 git 报错）⇒ 同样拿不到清单
        return None
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


def build_machine_markers() -> list[tuple[str, bytes]]:
    """构建机上**不该出现在产物里**的路径。

    只查**构建机自己的**路径（仓库根目录、用户主目录），不做泛化的
    「像不像路径」猜测 —— 那种猜测会把测试夹具里刻意写的假路径
    （``C:\\Users\\bob\\app.log``）也报成命中。
    """
    out = []
    for label, p in (("仓库根目录", str(ROOT)), ("用户主目录", str(Path.home()))):
        if not p or p in ("/", "\\", "C:\\"):
            continue
        for variant in (p, p.replace("\\", "/")):
            raw = variant.encode("utf-8", "replace")
            if raw and (label, raw) not in out:
                out.append((label, raw))
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
        artifact_names: list[tuple[str, str]] = []      # (产物名, 相对路径)
        path_leaks: list[tuple[str, str, int]] = []      # (产物名, 路径, 次数)
        markers = build_machine_markers()
        with zipfile.ZipFile(wheel) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                n_files += 1
                raw = z.read(info.filename)
                artifact_names.append((wheel.name, info.filename))
                for label, marker in markers:
                    c = raw.count(marker)
                    if c:
                        path_leaks.append((wheel.name + " :: " + info.filename, label, c))
                found = scan_bytes(raw)
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
                raw = fh.read()
                rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
                artifact_names.append((sdist.name, rel))
                for label, marker in markers:
                    c = raw.count(marker)
                    if c:
                        path_leaks.append((sdist.name + " :: " + m.name, label, c))
                found = scan_bytes(raw)
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

    # ---- 3) 产物里的文件必须来自源码树 ----
    committed = committed_files()
    extra = []
    tree_checked = committed is not None
    if tree_checked:
        for art, rel in artifact_names:
            if rel in committed or _BUILD_METADATA.match(rel):
                continue
            extra.append("%s :: %s" % (art, rel))
    else:
        print("（拿不到源码树清单 —— 「文件是否来自源码树」这一项**未执行**）")
        print("   **未执行不等于通过**：这一项没查，就不能说产物里的文件都来自源码树。"
              "请在**仓库**里跑这个脚本（sdist 里没有 .git，拿不到那份清单）。")

    if not tree_checked:
        print()
        print("**「文件是否来自源码树」这一项未执行** —— 不构成通过")
    elif not extra:
        print()
        print("**产物里的文件全部来自源码树**（构建元数据除外）")
    else:
        print()
        print("**产物里有 %d 个文件不在源码树里** —— 它们从未被复核过：" % len(extra))
        for name in extra[:20]:
            print("  %s" % name)
        if len(extra) > 20:
            print("  …… 还有 %d 个" % (len(extra) - 20))

    # ---- 4) 产物里不能带构建机的路径 ----
    if not path_leaks:
        print("**产物里没有构建机的路径**")
    else:
        print()
        print("**产物里有构建机的路径（个人信息，发出去撤不回来）：**")
        for name, label, c in path_leaks[:20]:
            print("  %-64s [%s] x%d" % (name, label, c))
        if len(path_leaks) > 20:
            print("  …… 还有 %d 处" % (len(path_leaks) - 20))

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

    if hits or sast_rc not in (0, None) or extract_failures or extra or path_leaks:
        print()
        print("**产物审计未通过** —— 不要发布。"
              "（已发布到 PyPI 的版本不可覆盖，只能再发一版。）")
        return 1
    if not tree_checked:
        print()
        print("产物审计**无法判定**：「文件是否来自源码树」这一项**未执行**"
              "（拿不到 git 源码树清单）—— 请在**仓库**里跑这个脚本再发布。")
        return 2
    print()
    print("产物审计通过：内容 0 命中、产物自带门禁绿、文件全部来自源码树、没有构建机路径。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
