#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出 PyPI 上**每个已发布版本**的产物里有没有那批措辞。

为什么要有这个脚本
------------------
「已发布版本」是**另一份**东西：PyPI 上的字节是当年构建的，
清理仓库**不会**重建它们。v2.12.4 的实测就摆在那儿 ——
仓库当时 0 命中，而 2.12.3 的产物里实测 88 处、38 个文件
（含一个真实内部主机名）。

所以「哪些旧版本带词、必须删」这件事必须**可复现**，而不是靠记忆。

它做什么
--------
对 PyPI 上每个版本，取它的 **sdist**（sdist 包含 wheel 的全部内容，
外加文档与测试 —— 扫它就够），用**当前**的词表扫一遍，列出每个版本的
命中情况。命中版本排在最前面。

用法
----
    python scripts/audit_published_versions.py                # 全部版本
    python scripts/audit_published_versions.py --keep 2.12.4   # 只看除它以外的
    python scripts/audit_published_versions.py --json         # 机器可读

退出码：0 = 跑完了（**不代表干净**，看输出）；2 = 网络/依赖失败。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "tests" / "test_no_job_wording.py"
HDR = {"User-Agent": "lac-audit-published-versions"}


def load_gate():
    spec = importlib.util.spec_from_file_location("wording_gate_published", GATE)
    if spec is None or spec.loader is None:
        raise SystemExit("读不到门禁文件 %s" % GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not getattr(mod, "FORBIDDEN", None):
        raise SystemExit("%s 里没有 FORBIDDEN" % GATE)
    return mod


def _require_https(url: str) -> str:
    """只放行 https —— 与 scripts/fetch_datasets.py 里的同名守卫同一套做法。

    为什么要有它：URL 的一部分来自命令行参数（包名）与 API 响应（文件下载
    地址）。bandit 认的是 `urlopen` 调用本身而不是校验逻辑，所以它会报；
    而真正的防护是**这个守卫** —— 非 https 直接拒绝。

    守卫本身由 tests/test_audit_published_versions_scheme_guard.py 守着：
    那条测试把 `file://` 喂进来并要求它抛异常。所以这里不是一句注释式的
    豁免。
    """
    if not url.lower().startswith("https://"):
        raise ValueError("只允许 https，收到：%r" % url[:80])
    return url


def get_json(url: str):
    req = urllib.request.Request(_require_https(url), headers=HDR)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.load(resp)


def fetch(url: str, dest: Path, size: int) -> bool:
    # 守卫在**重试循环之外**：不合规的 URL 是编程错误，不是网络抖动 ——
    # 放进重试里会被吞掉，然后返回 False，看起来像「下载失败」。
    _require_https(url)
    if dest.exists() and dest.stat().st_size == size:
        return True
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HDR)
            with urllib.request.urlopen(req, timeout=300) as resp:
                dest.write_bytes(resp.read())
            return True
        except Exception as exc:                            # noqa: BLE001
            print("    重试 %d：%r" % (attempt + 1, exc), file=sys.stderr)
            time.sleep(2)
    return False


def scan_tar(path: Path, words, count):
    hits = {}
    nfiles = 0
    with tarfile.open(path, "r:gz") as t:
        for m in t.getmembers():
            if not m.isfile():
                continue
            fh = t.extractfile(m)
            if fh is None:
                continue
            raw = fh.read()
            if b"\0" in raw[:8000]:
                continue        # 二进制不当文本扫（与门禁同口径）
            nfiles += 1
            text = raw.decode("utf-8", "replace")
            for w in words:
                n = count(text, w)          # 与门禁同一套匹配规则
                if n:
                    hits[w] = hits.get(w, 0) + n
    return hits, nfiles


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="列出 PyPI 上每个已发布版本的产物里有没有那批措辞")
    ap.add_argument("--package", default="log-ai-compressor")
    ap.add_argument("--keep", default=None,
                    help="这个版本不算问题（例如当前最新版本）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    gate = load_gate()
    words = list(gate.FORBIDDEN)
    count = gate._count
    work = ROOT / "dist" / "published-audit"
    work.mkdir(parents=True, exist_ok=True)

    try:
        meta = get_json("https://pypi.org/pypi/%s/json" % args.package)
    except Exception as exc:                                 # noqa: BLE001
        print("读不到 PyPI 的元数据：%r" % exc, file=sys.stderr)
        return 2

    versions = sorted(meta["releases"].keys())
    rows = []
    for v in versions:
        files = meta["releases"][v]
        sdist = next((f for f in files if f["packagetype"] == "sdist"), None)
        if sdist is None:
            rows.append({"version": v, "status": "没有 sdist", "hits": {}, "files": 0})
            continue
        dest = work / sdist["filename"]
        if not fetch(sdist["url"], dest, sdist["size"]):
            rows.append({"version": v, "status": "下载失败", "hits": {}, "files": 0})
            continue
        try:
            hits, nfiles = scan_tar(dest, words, count)
        except Exception as exc:                             # noqa: BLE001
            rows.append({"version": v, "status": "解包失败：%r" % exc, "hits": {}, "files": 0})
            continue
        rows.append({"version": v, "status": "ok", "hits": hits, "files": nfiles})

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("包：%s   已发布版本：%d" % (args.package, len(rows)))
    print()
    tainted = [r for r in rows if r["hits"]]
    for r in sorted(rows, key=lambda x: (-sum(x["hits"].values()), x["version"])):
        if r["version"] == args.keep:
            continue
        total = sum(r["hits"].values())
        detail = ", ".join("%s×%d" % (w, n)
                           for w, n in sorted(r["hits"].items(), key=lambda kv: -kv[1])[:6])
        print("  %-10s %5d 文件  命中 %4d 处  %s%s"
              % (r["version"], r["files"], total, detail,
                 ("  [" + r["status"] + "]") if r["status"] != "ok" else ""))
    print()
    print("带词版本：%d 个%s" % (len(tainted),
          ("（" + ", ".join(r["version"] for r in tainted) + "）") if tainted else ""))
    if args.keep:
        keep_row = next((r for r in rows if r["version"] == args.keep), None)
        if keep_row:
            print("保留的 %s：命中 %d 处" % (args.keep, sum(keep_row["hits"].values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
