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
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def last_commit_epoch() -> int:
    """用最后一次提交的作者时间当 SOURCE_DATE_EPOCH。

    刻意不用「构建时刻」——那会让两次构建必然不同，可复现就无从谈起。
    """
    out = subprocess.run(
        ["git", "log", "-1", "--format=%at"],
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


def build(outdir: Path) -> list[Path]:
    epoch = last_commit_epoch()
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), PYTHONHASHSEED="0")
    # 整秒取整：某些文件系统只存整秒的 mtime，而写文件发生在 started 之后，
    # 整秒比较在粗/细两种时间精度下都成立（细精度下 mtime 是小数，也 ≥ 整秒）。
    started = int(time.time())
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir)],
        cwd=ROOT, env=env, check=True,
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
    args = ap.parse_args()

    epoch = last_commit_epoch()
    print(f"SOURCE_DATE_EPOCH = {epoch}  （最后提交的作者时间）")

    if args.verify:
        import shutil
        a, b = DIST / "repro_a", DIST / "repro_b"
        for d in (a, b):
            shutil.rmtree(d, ignore_errors=True)
        build(a)
        build(b)
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

    made = build(DIST)
    print()
    for f in made:
        print(f"  {f.name}  {f.stat().st_size} B")
        print(f"    sha256 {sha(f)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
