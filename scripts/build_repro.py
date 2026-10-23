# -*- coding: utf-8 -*-
"""可复现构建：同一份源码产出**逐字节相同**的 wheel 与 sdist。

为什么需要这个
--------------
本项目的纪律是「同一版本号不得对应两套字节不同的产物」。但那只管住了
「不要把 A 版冒充成已发布的 A 版」，管不住另一件事：**能不能从同一份源码
重建出已发布的那一份**。实测：

- ``python -m build --wheel``：设 ``SOURCE_DATE_EPOCH`` 后**已经**可复现
- ``python -m build --sdist``：**不可复现**，而且不止 gzip 头 —— 内层 tar
  的成员 mtime 也每次不同

后者是 setuptools 的 sdist 不遵守 ``SOURCE_DATE_EPOCH`` 导致的。本脚本在
正常构建之后把 sdist 重打一遍：成员 mtime / uid / gid / uname / gname 全部
钉死，gzip 头的 MTIME 置 0，压缩级别固定。

用法
----
    python scripts/build_repro.py            # 正常构建 + 归一化，输出到 dist/
    python scripts/build_repro.py --verify   # 构建两次并断言字节一致（CI 用）

注意：``SOURCE_DATE_EPOCH`` 取 **最后一次提交的作者时间**，不是构建时间 ——
它必须只由内容决定，否则「同一个 commit 重建出同一份」就不成立。

构建在**干净快照**里跑，不在工作区
----------------------------------
2026-10-23 实测：已发布的 2.12.6 sdist 里夹着三个**从未进过任何 commit** 的
文件（``examples/app_report.md`` 等），而且它们嵌着构建机的**绝对路径与账户名**。
机制是三件各自都正确的事凑在一起：

* ``MANIFEST.in`` 收了 ``examples/*`` —— 生成物也匹配；
* ``.gitignore`` 把 ``*_report.*`` 挡住 —— 它们在 ``git status`` 里**隐身**；
* 构建在工作区里跑 —— 它们被静默打包。

⇒ 「三方 sha256 逐字节相同」对 sdist **不成立**（从 tag 重建出来是另一份字节），
而这一版的门禁（词表扫描 + 产物自带 SAST）原理上查不到这一类。

修法是**换掉构建的输入**：不是「工作区」，而是「已提交的树」。
``git archive`` 导出的是 blob 的内容 —— 未入库/被忽略的文件根本不在里面；
而且它按 ``.gitattributes`` 做行尾归一 ⇒ 同一份源码在任何机器上导出成同样的
字节（工作区里 ``LICENSE`` 在 Windows 检出后是 CRLF、在 Linux 上是 LF，
产物因此**随机器变**）。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def git_exe() -> str:
    """解析 git 的绝对路径，解析不到就明确报错。

    SAST（bandit B607）报的是「用部分路径启动进程」。这里不写 nosec，
    因为确实能修：写死 `"git"` 会在 git 不在 PATH 时抛一个
    `FileNotFoundError`，看不出是「git 缺失」还是「脚本跑错目录」。
    """
    exe = shutil.which("git")
    if exe is None:
        raise SystemExit("找不到 git —— 可复现构建需要用 git 取提交时间")
    return exe


def uncommitted_changes() -> list[str]:
    """工作区里**被跟踪文件**的未提交改动（不含未入库/被忽略的文件）。

    为什么构建前要查它：产物是从**已提交的树**导出的（见
    :func:`snapshot_into`），所以工作区里的改动**不在**产物里。
    如果不吭声，构建出来的东西会与眼前的工作区对不上 ——
    而「产物对应哪一笔提交」恰恰是这个项目最要紧的那条纪律。
    """
    proc = subprocess.run(
        [git_exe(), "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def last_commit_epoch(ref: str = "HEAD") -> int:
    """用最后一次提交的作者时间当 SOURCE_DATE_EPOCH。

    刻意不用「构建时刻」——那会让两次构建必然不同，可复现就无从谈起。
    """
    out = subprocess.run(
        [git_exe(), "log", "-1", "--format=%at", ref],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return int(out)


def normalize_sdist(path: Path, epoch: int) -> None:
    """把 .tar.gz 重打成逐字节可复现的形式。"""
    raw = gzip.decompress(path.read_bytes())
    src = tarfile.open(fileobj=io.BytesIO(raw), mode="r")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as out:
        for m in src.getmembers():
            m.mtime = epoch          # 成员时间 -> 提交时间
            m.uid = m.gid = 0        # 归属归零，避免受构建机 uid 影响
            m.uname = m.gname = ""
            # PAX 扩展头里存着**带小数**的原始 mtime，会盖掉上面的整数设置
            # （实测 1792063252.3845682 vs ...262.2095346，元信息就不一致）。
            # 不清掉它，mtime 设了也白设。
            m.pax_headers = {}
            if m.isdir():
                m.mode = 0o755
            elif m.mode & 0o111:
                m.mode = 0o755
            else:
                m.mode = 0o644
            f = src.extractfile(m) if m.isreg() else None
            out.addfile(m, f)
    src.close()

    # mtime=0 让 gzip 头不再嵌构建时刻；compresslevel 固定避免环境差异
    with path.open("wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(buf.getvalue())


def _extract_archive(archive: tarfile.TarFile, dest: Path) -> None:
    """把归档安全地解到 dest。

    ``filter="data"`` 是 Python 3.12 起的能力（3.11.4 / 3.10.12 等也回合了），
    但本项目声明支持 **3.11 起** —— 3.11.0～3.11.3 没有这个参数。
    所以：能用就用（它顺带挡住了路径穿越与特殊文件），不能用就**逐个成员**
    手工校验后解出来，而不是无校验地 extractall。

    为什么自己写校验：``extractall`` 不带 filter 时会照着归档里的路径写文件，
    归档里带 ``../`` 就能写到 dest 外面去。
    """
    try:
        archive.extractall(dest, filter="data")
        return
    except TypeError:
        pass
    dest.mkdir(parents=True, exist_ok=True)
    for member in archive.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise SystemExit("归档里有不安全的成员名：%r" % name)
        if member.isdir():
            (dest / name).mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = archive.extractfile(member)
        if fh is None:
            continue
        with target.open("wb") as out:
            out.write(fh.read())


def snapshot_into(dest: Path, ref: str = "HEAD") -> Path:
    """把**已提交的树**导出成一份干净快照，构建在它里面跑。

    为什么必须是快照而不是工作区
    --------------------------
    见模块文档：已发布的 2.12.6 sdist 夹带了三个从未入库的文件（还带着构建机
    的绝对路径），因为 ``MANIFEST.in`` 收了 ``examples/*``、``.gitignore`` 又把
    它们挡住（于是 ``git status`` 里看不见）、而构建在工作区里跑。

    ``git archive`` 导出的是 blob 的内容：未入库/被忽略的文件根本不在里面，
    而且它按 ``.gitattributes`` 归一行尾 ⇒ 同一份源码在任何机器上导出成
    同样的字节。
    """
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [git_exe(), "archive", "--format=tar", ref],
        cwd=ROOT, capture_output=True, check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r") as tf:
        _extract_archive(tf, dest)
    return dest


def build(outdir: Path, ref: str = "HEAD") -> list[Path]:
    epoch = last_commit_epoch(ref)
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), PYTHONHASHSEED="0")
    # 整秒取整：某些文件系统只存整秒的 mtime，而写文件发生在 started 之后，
    # 整秒比较在粗/细两种时间精度下都成立（细精度下 mtime 是小数，也 ≥ 整秒）。
    started = int(time.time())
    with tempfile.TemporaryDirectory(prefix="lac-snapshot-") as td:
        src = snapshot_into(Path(td) / "src", ref)
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(outdir.resolve())],
            cwd=str(src), env=env, check=True,
        )
    # 只归一化**本次构建产生**的 sdist，不能对 outdir 里所有 .tar.gz 一视同仁。
    #
    # 正式构建的 outdir 是 dist/，那里堆着历次发布的产物。而 epoch 取的是
    # **最后一次提交**的时间戳 —— 拿它去重写旧产物，等于把每个历史版本
    # 的字节都按新时间戳重打一遍。实测：本地 2.11.0 的 sdist 从
    # 1ff4b384…（= PyPI 上的值）变成 3cfef3bc…，README 里
    # 「本地/PyPI/GitHub 三方 sha256 对账」对**任何已发布版本**都当场失效。
    #
    # 判据是「mtime >= 本次构建开始的整秒」，不是文件名：重跑同一个版本时
    # 文件名不变但内容被重写过，只有时间戳能同时覆盖「新文件」和「重跑」。
    made = sorted(p for p in outdir.glob("*.tar.gz") if p.stat().st_mtime >= started)
    for tgz in made:
        normalize_sdist(tgz, epoch)
    return made


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="构建两次并断言字节一致")
    ap.add_argument("--ref", default="HEAD",
                    help="构建哪一笔提交（默认 HEAD；复核历史版本时用 tag）")
    args = ap.parse_args()

    # 工作区有未提交改动时**直接停**：产物是从已提交的树导出的，
    # 继续构建等于交付一份与眼前工作区对不上的产物 —— 而「产物对应哪一笔
    # 提交」是本项目最要紧的那条纪律。改了先提交（或 stash）再构建。
    dirty = uncommitted_changes()
    if dirty:
        print("**工作区有未提交的改动**，而产物是从已提交的树导出的：")
        for line in dirty:
            print("   %s" % line)
        print("\n先提交（或 git stash）再构建 —— 否则产物与工作区对不上。")
        return 2

    epoch = last_commit_epoch(args.ref)
    print(f"SOURCE_DATE_EPOCH = {epoch}  （{args.ref} 最后提交的作者时间）")

    if args.verify:
        import shutil
        a, b = DIST / "repro_a", DIST / "repro_b"
        for d in (a, b):
            shutil.rmtree(d, ignore_errors=True)
        build(a, args.ref)
        build(b, args.ref)
        bad = 0
        for f in sorted(x for x in a.iterdir() if x.suffix in (".whl", ".gz")):
            g = b / f.name
            same = g.exists() and sha(f) == sha(g)
            print(f"  {f.name:<42} {'一致' if same else '不一致'}")
            if not same:
                bad += 1
        print()
        print("构建可复现。" if not bad else f"{bad} 个产物不可复现。")
        return 1 if bad else 0

    made = build(DIST, args.ref)
    print()
    for f in made:
        print(f"  {f.name}  {f.stat().st_size} B")
        print(f"    sha256 {sha(f)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
