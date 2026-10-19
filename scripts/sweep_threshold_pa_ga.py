#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PA 与 GA 对相似度阈值的取舍曲线 —— **这是测量，不是调参**。

**为什么要画这条曲线**
-------------------
v2.11.5 实测：PA 61.71%（赢 Drain3 3.65pp）而 GA 58.18%（输 13.40pp）。
随后 `scripts/diagnose_ga_gap.py` 把 GA 的失分拆开后发现：

    Hadoop：过合并 4.6% / 过分裂 95.4%     Drain3：过合并 58.9% / 过分裂 41.1%

**我们不是合多了，是拆得太碎。** 而阈值越高越拆得碎。所以「PA 赢、GA 输」
很可能不是「我们强」，而是**同一个操作点同时讨好了一个指标、得罪了另一个**：
拆得碎 -> 每个簇更纯 -> 模板更容易逐行对上（PA 好），但同一个事件被摊到
多个簇 -> 条件 (b) 收不全（GA 差）。

**如果这条判断成立，那么「把 GA 调上去」和「保住 PA」就是同一个旋钮的两端**，
而且方向相反。这时候最容易被做的事是：挑一个能让 GA 赢的阈值，改默认值，发版。
**本脚本存在的全部意义是让那个数字出现在曲线上，而不是出现在标题上。**

**它不动产品**
------------
`ErrorClusterer` 接受按实例的阈值，所以本脚本自己建聚类器，
`CLUSTER_SIMILARITY_THRESHOLD` 一个字都不改。输出里也不会出现
「建议改成 X」——**该不该改、往哪改，是产品判断，不是这张表的判断**。
这张表只回答一件事：**PA 与 GA 之间到底是不是零和的。**

用法
----
    python scripts/sweep_threshold_pa_ga.py
    python scripts/sweep_threshold_pa_ga.py --datasets Hadoop,Apache,Mac
    python scripts/sweep_threshold_pa_ga.py --thresholds 0.5,0.6,0.7,0.85
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from log_ai_compressor.core.clustering import ErrorClusterer        # noqa: E402
from log_ai_compressor.core.parser import LogParser                 # noqa: E402
from log_ai_compressor.rules.engine import load_ruleset             # noqa: E402
from eval_logparser_pa_ga import (                                   # noqa: E402
    annotated_lines, entry_template, grouping_accuracy,
    parsing_accuracy, run_drain,
)
from benchmark_loghub import discover                                # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass


def run_at(ds, threshold: float) -> dict:
    """与 `eval_logparser_pa_ga.run_dataset` 完全同口径，只把阈值换成传入值。

    刻意逐行照抄而不是改那个函数加参数：那 24 条口径测试守的是那个函数，
    而这里多一个维度（阈值），会让「同口径」这句话开始需要解释。
    """
    parser = LogParser(load_ruleset("generic"))
    clusterer = ErrorClusterer(similarity_threshold=threshold)
    pred_template: Dict[int, str] = {}
    pred_group: Dict[int, str] = {}

    def feed(entry) -> None:
        cluster, _r, _i = clusterer.add(entry)
        pred_template[entry.line_no] = entry_template(entry.message)
        pred_group[entry.line_no] = str(cluster.cluster_id)

    for line_no in sorted(ds.line_content):
        entry = parser.feed(ds.line_content[line_no], line_no)
        if entry is not None:
            feed(entry)
    if hasattr(parser, "flush"):
        tail = parser.flush()
        if tail is not None:
            feed(tail)

    gt_event = dict(ds.line_event)
    gt_template = {lid: ds.event_template[eid] for lid, eid in gt_event.items()
                   if eid in ds.event_template}
    total = annotated_lines(ds)
    ga, _ = grouping_accuracy(gt_event, pred_group, total)
    pa, _ = parsing_accuracy(gt_template, pred_template, total)
    return {"PA": round(100.0 * pa, 2), "GA": round(100.0 * ga, 2),
            "clusters": len(clusterer.clusters)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="PA/GA 对相似度阈值的取舍曲线（测量用）")
    ap.add_argument("--datasets", default="Hadoop,Apache,Mac")
    ap.add_argument("--thresholds", default="0.40,0.55,0.70,0.85")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.datasets.split(",") if n.strip()]
    ths = [float(t) for t in args.thresholds.split(",") if t.strip()]
    datasets = discover(names or None)
    if not datasets:
        print("没有可用数据集。先跑 python scripts/fetch_datasets.py --all")
        return 2

    print("对照（Drain3 0.9.11，同口径喂 Content）：")
    base = {}
    for ds in datasets:
        d = run_drain(ds)
        if d:
            base[ds.name] = (d["PA"], d["GA"])
            print("  %-14s PA %6.2f%%  GA %6.2f%%" % (ds.name, d["PA"], d["GA"]))
        else:
            base[ds.name] = (None, None)
            print("  %-14s （本机没装 drain3）" % ds.name)

    for ds in datasets:
        print("\n■ %s   （簇数随阈值变化；PA/GA 一升一降就是这个旋钮的两端）"
              % ds.name, flush=True)
        print("  %-9s %-9s %-9s %-9s  %s" % ("阈值", "PA", "GA", "簇数", "备注"))
        print("  " + "-" * 62)
        for th in ths:
            t0 = time.time()
            r = run_at(ds, th)
            mark = ""
            bp, bg = base[ds.name]
            if bp is not None:
                if r["PA"] >= bp and r["GA"] >= bg:
                    mark = "两个口径都不落后于 Drain3"
                elif r["PA"] >= bp:
                    mark = "PA 赢 / GA 输"
                elif r["GA"] >= bg:
                    mark = "PA 输 / GA 赢"
                else:
                    mark = "两个口径都输"
            print("  %-9.2f %-9.2f %-9.2f %-9d  %s  (%.1fs)"
                  % (th, r["PA"], r["GA"], r["clusters"], mark,
                     time.time() - t0), flush=True)

    print("\n" + "=" * 72)
    print("**这张表不产出「建议阈值」。**")
    print("它只回答一个问题：PA 与 GA 之间是不是零和的。")
    print("  * 如果存在「两个口径都不落后」的区间 -> 那里可能有**结构性**改进"
          "（例如分桶方式），")
    print("    值得按缺陷去查，而不是直接改默认值。")
    print("  * 如果曲线始终一升一降 -> 两个指标奖励相反的操作点，"
          "**任何「全胜」的改法都是")
    print("    在换一个指标上退让**，而 AGENTS.md §2.3 禁止的正是对着评测集"
          "挑那个点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
