# -*- coding: utf-8 -*-
"""一键下载复现所需的公开数据集（Loghub 2.0 + Android_v1）。

为什么需要它
------------
`docs/BENCHMARK.md` 里的每一张表都标着「数据来源：zenodo.org/record/8275861」，
但复现者要自己知道下哪些文件、放哪个目录、解压完还要改名 —— 门槛全在
「知道怎么做」而不是「做不做得成」。这个脚本把那段知识固化下来。

两个记录号，别搞混
-----------------
- **8275861** = Loghub **2.0**，14 个数据集（含 Hadoop，但**不含 Android**）
- **8196385** = Loghub **1.0**，含 Android_v1.zip（23.7 MB / 解压 183 MB）

Android_v1 只在 1.0 里 —— v2.5.0 补 Android 全量时踩过这个坑：
在 2.0 的记录里翻了半天找不到，最后发现要找的是另一个记录。

用法
----
    python scripts/fetch_datasets.py                # 下全部（约 350 MB）
    python scripts/fetch_datasets.py --android      # 只补 Android_v1
    python scripts/fetch_datasets.py --list         # 看要下什么、多大

下载走 HTTP Range 断点续传：这些包在不稳定网络下会反复中途断流，
逐次重下往往拿不到完整的包（实测 Android_v1 首轮只拿到 11.68 / 23.71 MB）。
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "log-ai-compressor-fetch-datasets/1.0"

# (记录号, 文件名, 解压目标子目录, 说明)
LOGS_2_0 = [
    ("8275861", "Proxifier.zip", "Proxifier", "网络代理日志，11 种事件"),
    ("8275861", "Apache.zip", "Apache", "Web 服务器日志，29 种事件"),
    ("8275861", "Zookeeper.zip", "Zookeeper", "分布式协调，89 种事件"),
    ("8275861", "HealthApp.zip", "HealthApp", "健康应用，156 种事件"),
    ("8275861", "OpenStack.zip", "OpenStack", "云平台，48 种事件（最难）"),
]
ANDROID = ("8196385", "Android_v1.zip", "Android_v1",
           "Android logcat，1,555,005 行 / 131 条 Caused-by 链")


def url_for(record: str, name: str) -> str:
    return f"https://zenodo.org/api/records/{record}/files/{name}/content"


def total_size(url: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Range"].rsplit("/", 1)[1])


def fetch(url: str, dest: Path) -> bool:
    """Range 断点续传。返回是否拿到完整文件。"""
    part = dest.with_suffix(dest.suffix + ".part")
    total = total_size(url)
    have = part.stat().st_size if part.exists() else 0
    if have > total:
        part.unlink()
        have = 0
    if have == total:
        print(f"    已完整 {dest.name}")
        return True

    window = 1 << 20
    stall = 0
    print(f"    {dest.name}  {total/1048576:.1f} MB"
          + (f"（已有 {have/1048576:.1f} MB，断点续传）" if have else ""))
    while have < total:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Range": f"bytes={have}-{min(have+window, total)-1}"})
            with urllib.request.urlopen(req, timeout=120) as r, \
                    part.open("ab") as fh:
                while True:
                    buf = r.read(1 << 18)
                    if not buf:
                        break
                    fh.write(buf)
                    have += len(buf)
        except Exception as exc:                       # noqa: BLE001
            print(f"      中断于 {have/1048576:.1f} MB: {exc}")
            window = max(1 << 16, window // 2)
            stall += 1
            if stall > 40:
                print("      连续失败过多，放弃该文件")
                return False
            time.sleep(1.5)
            continue
        stall = 0
        window = min(1 << 23, window * 2)
        print(f"      {have/1048576:6.1f} / {total/1048576:.1f} MB "
              f"({have/total*100:.0f}%)")
    part.replace(dest)
    return True


def unpack(dest: Path, subdir: str) -> None:
    out = DATA / "loghub_full" / subdir
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest) as z:
        z.extractall(out)
    n = sum(1 for _ in out.rglob("*.log"))
    print(f"    解压到 {out.relative_to(ROOT)}  （{n} 个 .log）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--android", action="store_true", help="只补 Android_v1")
    ap.add_argument("--all", action="store_true", help="全部（含 Loghub 2.0 五个）")
    ap.add_argument("--list", action="store_true", help="只列出清单")
    args = ap.parse_args()

    todo = list(LOGS_2_0)
    if args.android:
        todo = [ANDROID]
    elif not args.all:
        todo = [ANDROID]

    if args.list:
        print("将下载：")
        for rec, name, sub, note in todo:
            print(f"  {name:<20} -> data/loghub_full/{sub:<12}  {note}")
        return 0

    print("数据来源：Loghub-2.0 (ISSTA'24) / Loghub 1.0（ISSRE'23），均为公开数据集")
    print(f"目标目录：{(DATA / 'loghub_full')}\n")
    bad = 0
    for rec, name, sub, note in todo:
        print(f"  [{name}] {note}")
        dest = DATA / "loghub" / name
        if not dest.exists():
            if not fetch(url_for(rec, name), dest):
                bad += 1
                continue
        else:
            print(f"    已存在 {dest.name}")
        unpack(dest, sub)
        print()

    print()
    print("完成。下一步：")
    print("  python scripts/benchmark_loghub.py          # 压缩比 vs 证据保留率")
    print("  python scripts/eval_rootcause.py            # 根因召回/精确率/自信错误率")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
