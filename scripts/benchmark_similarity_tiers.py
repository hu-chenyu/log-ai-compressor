#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相似度三档的实测：产品里有三个档位，而基准只测了中间那一个。

**这个脚本为什么存在**
-------------------
`benchmark_loghub.py` 的 `method_ours()` 一直带着 `similarity` 参数，可
`run_one()` 的四次调用**全部用默认值 `"standard"`** —— 也就是脚本测得了三档，
跑的时候一档都没测。

而 v2.9.4 的旧表恰恰报过「严格档 0.95」那一栏，并且它在 HPC 上赢了
Drain3（97.3% vs 87.8%）。那个结论在 v2.9.5 被撤回，理由是严格档切出的簇
**报告里装不下**（OpenSSH 上 1,831 个簇 / 809 个 token）—— 指标在数用户
没收到的东西。

于是现在有一种很难解释的形状：**赢过的那一档不再出现在任何表里。**
外部评审据此指控「只报对自己有利的那一档」。指控的动机猜错了（撤回的
理由在上面，动机恰恰相反），但**形状是真的**，而且只有重测才能消除。

**本脚本只做一件事**：三个档位**全部**测、**全部**公布，两个口径都报。

**为什么两个口径都要**
--------------------
v2.9.4 那张表用的是**全量口径**（聚类质量上界），严格档正是在那个口径下
看起来赢的。只报交付口径，读者完全有理由说「你恰好挑了严格档难看的
那一栏」—— 那等于又挑了一次数据。两个口径都列，读者自己判断。

| 口径 | 含义 | 与 README 的关系 |
| --- | --- | --- |
| 交付口径 | 用户真收到的那 `DEFAULT_TOP_N` 个簇 | README 主表第一列（主指标） |
| 全量口径 | 聚类质量上界，切片数不限 | README 主表「全量」列 |

用法
----
    python scripts/benchmark_similarity_tiers.py
    python scripts/benchmark_similarity_tiers.py --datasets Proxifier,Apache
    python scripts/benchmark_similarity_tiers.py --csv docs/similarity_tiers.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from log_ai_compressor import service                              # noqa: E402
from log_ai_compressor.constants import (                          # noqa: E402
    DEFAULT_TOP_N, LEVEL_ORDER, SIMILARITY_PRESETS,
)
from log_ai_compressor.export.reporters import estimate_tokens     # noqa: E402

from benchmark_loghub import discover                              # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 全级别：从 constants 派生，不手写（手写过的那份漏了 FAIL，见主脚本的记录）
ALL_LEVELS = list(LEVEL_ORDER)

# 三档都测。刻意**不**给「默认跳过某档」的选项：只报其中一档正是这次要消除的形状。
TIERS = ("strict", "standard", "lenient")


def measure(ds, tier: str) -> dict:
    """一个数据集 × 一个档位，**一次分析**产出两个口径。

    两口径来自同一次 `service.analyze`（分析贵、切片便宜）：
      · 交付口径 = 用户真收到的那 DEFAULT_TOP_N 个簇
      · 全量口径 = 聚类质量上界
    """
    t0 = time.time()
    result = service.analyze(
        paths=[str(ds.log_path)],
        params={"levels": ALL_LEVELS, "top_n": 10 ** 9,
                "context_lines": 0, "similarity": tier},
    )
    total = max(len(ds.events), 1)

    def slice_metrics(n_clusters: int):
        shown = result.clusters[:n_clusters]
        text = service.export_text(result, "summary", top_n=n_clusters)
        covered = set()
        for c in shown:
            sample = getattr(c, "sample", None)
            if sample is None or sample.entry is None:
                continue
            eid = ds.event_of_line_no(sample.entry.line_no)
            if eid:
                covered.add(eid)
        return (100.0 * len(covered) / total, len(covered), n_clusters,
                estimate_tokens(text))

    n_del = min(DEFAULT_TOP_N, len(result.clusters))
    d_cov, d_hit, d_n, d_tok = slice_metrics(n_del)
    f_cov, f_hit, f_n, f_tok = slice_metrics(len(result.clusters))

    return {
        "dataset": ds.name,
        "tier": tier,
        "threshold": SIMILARITY_PRESETS[tier],
        "delivered_coverage": round(d_cov, 1),
        "delivered_hits": d_hit,
        "full_coverage": round(f_cov, 1),
        "full_hits": f_hit,
        "clusters_all": f_n,
        "clusters_delivered": d_n,
        "delivered_tokens": d_tok,
        "full_tokens": f_tok,
        "total_events": total,
        "seconds": round(time.time() - t0, 1),
    }


def _print_table(rows, key, title):
    print("\n%s" % title)
    print("-" * 78)
    print("%-16s %-10s %-10s %-10s" % ("数据集", *TIERS))
    for name in sorted({r["dataset"] for r in rows}):
        by = {r["tier"]: r for r in rows if r["dataset"] == name}
        best = max(by[t][key] for t in TIERS)
        cells = []
        for t in TIERS:
            v = by[t][key]
            same = [x for x in TIERS if abs(by[x][key] - best) < 1e-9]
            mark = " *" if (v == best and len(same) == 1) else (" =" if v == best else "")
            cells.append("%-10s" % ("%.1f%%%s" % (v, mark)))
        print("%-16s %s" % (name, "".join(cells)))


def main() -> int:
    ap = argparse.ArgumentParser(description="相似度三档实测（两个口径）")
    ap.add_argument("--datasets", default="", help="逗号分隔；留空=全部")
    ap.add_argument("--csv", default="", help="把结果写到 CSV")
    args = ap.parse_args()

    names = [n.strip() for n in args.datasets.split(",") if n.strip()]
    datasets = discover(names or None)
    if not datasets:
        print("没有可用数据集。先跑 python scripts/fetch_datasets.py --all")
        return 2

    print("=" * 78)
    print("相似度三档实测")
    print("档位：%s" % ", ".join(f"{t}={SIMILARITY_PRESETS[t]}" for t in TIERS))
    print("两个口径都报：交付口径（top_n=%d）与全量口径（聚类上界）" % DEFAULT_TOP_N)
    print("=" * 78)

    rows = []
    for ds in datasets:
        print("\n■ %s（标注事件 %d 种，%.1f MB）"
              % (ds.name, len(ds.events),
                 ds.log_path.stat().st_size / 1024 / 1024), flush=True)
        for tier in TIERS:
            r = measure(ds, tier)
            rows.append(r)
            print("   %-9s %-5.2f  交付 %5.1f%% (%d/%d, %d 簇)   "
                  "全量 %5.1f%% (%d/%d, %d 簇)   %ss"
                  % (tier, r["threshold"],
                     r["delivered_coverage"], r["delivered_hits"], r["total_events"],
                     r["clusters_delivered"],
                     r["full_coverage"], r["full_hits"], r["total_events"],
                     r["clusters_all"], r["seconds"]), flush=True)

    _print_table(rows, "delivered_coverage", "交付口径（README 主表那一列）")
    _print_table(rows, "full_coverage", "全量口径（v2.9.4 那张表用的就是它）")

    for key in ("delivered_coverage", "full_coverage"):
        wins = {t: 0 for t in TIERS}
        for name in {r["dataset"] for r in rows}:
            by = {r["tier"]: r[key] for r in rows if r["dataset"] == name}
            best = max(by.values())
            winners = [t for t in TIERS if abs(by[t] - best) < 1e-9]
            if len(winners) == 1:
                wins[winners[0]] += 1
        print("\n%s 各档独占最优：%s"
              % (key, "  ".join("%s %d" % (t, wins[t]) for t in TIERS)))

    if args.csv:
        import csv as _csv
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("\n已写出 %s" % args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
