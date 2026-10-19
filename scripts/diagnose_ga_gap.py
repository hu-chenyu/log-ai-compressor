#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GA 的失分到底丢在哪一摊？—— 把「GA 低」拆成过合并与过分裂。

**这个脚本为什么存在**
--------------------
v2.11.5 第一次按 Loghub 标准协议评测，结果是一个**很不对称**的组合：

    PA  61.71%  vs Drain3 58.06%   -> 赢 3.65pp
    GA  58.18%  vs Drain3 71.58%   -> 输 13.40pp

这个组合本身就是线索。**PA 衡量的是「每行的模板字符串对不对」，GA 衡量的是
「该归一组的行有没有全归进同一组」** —— 模板对得上而分组对不上，只可能是
分组出了问题，而不是掩码或解析出了问题。

而 `grouping_accuracy()` 的失分只有两个来源：

* 条件 (a) 失败：某个簇里混进了**不止一个** EventId -> **过合并**
* 条件 (b) 失败：某个簇是「纯」的（只一个 EventId），但**没收到该 EventId
  的全部出现** -> **过分裂**

这两种病因的修法完全相反：过合并要**更严**的合并门槛，过分裂要**更宽**的。
只报一个 GA 总数，等于把两个方向相反的病混成一个数 —— **一个数不能指导
两个相反的修法**。所以要拆。

**这个脚本不调参，只量**
----------------------
它不碰阈值、不碰掩码规则、不碰 `top_n`。原因写在 AGENTS.md §2.3：对着评测集
改出来的领先不是领先，而这里量的是「哪一摊占多少」，不是「怎么把总分抬上去」。
量完之后的结论有三种，各自对应不同的动作：

1. 某一摊压倒性占主导、且有结构性成因 -> 那是**真缺陷**，按缺陷修，
   并全量重跑 10 个数据集说明「哪些变了、为什么」；
2. 两摊都很大且互斥 -> 没有一条规则能同时改好，如实写成已知限制；
3. 差距主要来自口径而非分组（例如未解析行） -> 那是协议适配问题，
   与聚类算法无关，要说清楚。

**不要**拿着这个分解去反推阈值。看到「过合并 30%」就下调相似度阈值，
正是 §2.3 禁止的事 —— 而 v2.9.8 已经这么干过一次：HPC 覆盖率 85.1% → 93.2%，
而报告从 76 个簇炸成 2,690 个、交付口径从 23.0% 崩到 8.1%，那次改动被撤回。

用法
----
    python scripts/diagnose_ga_gap.py                    # 全部数据集
    python scripts/diagnose_ga_gap.py --datasets Hadoop Apache
    python scripts/diagnose_ga_gap.py --csv docs/ga_gap_decomposition.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from eval_logparser_pa_ga import run_dataset, run_drain  # noqa: E402


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

try:
    from benchmark_loghub import discover
except ImportError as exc:                                       # pragma: no cover
    print("拿不到 benchmark_loghub 的 discover：%s" % exc)
    raise SystemExit(2)


def decompose(gt_event: Dict[int, str], pred_group: Dict[int, str],
              total_lines: int) -> Dict[str, int]:
    """把 GA 的失分拆成三摊，逐行计数。

    返回的行数之和恒等于 `total_lines - GA_correct`：
    每一条标注行要么被正确归组，要么恰好落在下面三摊之一。**恒等式由
    `tests/test_diagnose_ga_gap.py` 断言** —— 一个会漏掉一部分行的分解，
    报出来的比例加起来不到 100%，而那正好是最容易被忽略的那种错。
    """
    gt_total = Counter(gt_event.values())
    groups: Dict[str, List[int]] = {}
    for line_no, group in pred_group.items():
        groups.setdefault(group, []).append(line_no)

    correct = 0
    over_merge = 0        # 条件 (a) 失败：簇里混了多个 EventId
    over_split = 0        # 条件 (b) 失败：纯簇但没收全
    for log_ids in groups.values():
        events = {gt_event[ln] for ln in log_ids if ln in gt_event}
        if len(events) != 1:
            over_merge += len(log_ids)
            continue
        only = next(iter(events))
        if len(log_ids) != gt_total[only]:
            over_split += len(log_ids)
            continue
        correct += len(log_ids)

    unparsed = total_lines - len(pred_group)
    return {
        "correct": correct,
        "over_merge": over_merge,
        "over_split": over_split,
        "unparsed": unparsed,
        "lost": total_lines - correct,
    }


def _pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


#: CSV 的列。**显式写死**，而不是直接 dump `decompose()` 的键 ——
#: 那样每加一个内部字段都会悄悄改变 CSV 的形状，而下游对账读的是列名。
CSV_COLS = ("dataset", "method", "GA", "annotated_lines", "correct",
            "over_merge", "over_split", "unparsed", "lost",
            "pct_over_merge", "pct_over_split", "pct_unparsed")


def _csv_rows(rows: List[dict]) -> List[dict]:
    """把逐数据集结果摊平成 CSV 行；对照臂没有时那一行不出现。"""
    flat: List[dict] = []
    for r in rows:
        for method, ours, dec in (("ours", r["ours"], r["dec"]),
                                  ("drain3", r.get("drain"), r.get("drain_dec"))):
            if not dec or not ours:
                continue
            lost = dec["lost"]
            flat.append({
                "dataset": r["dataset"],
                "method": method,
                "GA": ours["GA"],
                "annotated_lines": ours["annotated_lines"],
                "correct": dec["correct"],
                "over_merge": dec["over_merge"],
                "over_split": dec["over_split"],
                "unparsed": dec["unparsed"],
                "lost": lost,
                "pct_over_merge": round(_pct(dec["over_merge"], lost), 2),
                "pct_over_split": round(_pct(dec["over_split"], lost), 2),
                "pct_unparsed": round(_pct(dec["unparsed"], lost), 2),
            })
    return flat


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="GA 失分分解（只量，不调参）")
    ap.add_argument("--datasets", default="", help="逗号分隔；留空=全部")
    ap.add_argument("--csv", default="", help="把结果写到 CSV（供文档逐格对账）")
    args = ap.parse_args(argv)

    wanted = {d.strip() for d in args.datasets.split(",") if d.strip()}
    rows = []
    for ds in discover(sorted(wanted) if wanted else None):
        ours = run_dataset(ds, source="content")
        drain = run_drain(ds)
        ours_dec = decompose(ours["_gt_event"], ours["_pred_group"],
                             ours["annotated_lines"])
        row = {"dataset": ds.name, "ours": ours, "dec": ours_dec, "drain": drain}
        if drain:
            row["drain_dec"] = decompose(drain["_gt_event"],
                                          drain["_pred_group"],
                                          drain["annotated_lines"])
        rows.append(row)
        print("  %-16s 本项目 GA %6.2f%%  已跑" % (ds.name, ours["GA"]), flush=True)

    if not rows:
        print("没有匹配的数据集")
        return 2

    print("\n" + "=" * 108)
    print("GA 失分分解（口径：Loghub PA/GA 协议，Content 输入）")
    print("=" * 108)
    print("%-14s %8s | %-38s | %s"
          % ("数据集", "GA", "本项目失分构成（行）", "过合并占比"))
    print("-" * 108)
    agg = Counter()
    agg_den = 0
    for r in rows:
        d = r["dec"]
        agg.update(d)
        agg_den += d["lost"] or 1
        print("%-14s %7.2f%% | 正确 %-9d 过合并 %-9d 过分裂 %-9d 未解析 %-7d | %5.1f%%"
              % (r["dataset"], r["ours"]["GA"], d["correct"], d["over_merge"],
                 d["over_split"], d["unparsed"],
                 _pct(d["over_merge"], d["lost"])))
    print("-" * 108)

    print("\n全量合计（失分 %d 行里）" % agg["lost"])
    for key, label in (("over_merge", "过合并（条件 a 失败）"),
                       ("over_split", "过分裂（条件 b 失败）"),
                       ("unparsed", "未解析成条目")):
        print("  %-26s %10d 行  %5.1f%%"
              % (label, agg[key], _pct(agg[key], agg["lost"])))

    if any("drain_dec" in r for r in rows):
        print("\n与 Drain3 同口径对照（同样把失分拆开）")
        print("-" * 108)
        print("%-14s %8s %10s | %8s %10s %10s"
              % ("数据集", "我方 GA", "Drain3 GA", "我方过合并", "Drain过合并",
                 "Drain过分裂"))
        for r in rows:
            if "drain_dec" not in r:
                continue
            dd = r["drain_dec"]
            print("%-14s %7.2f%% %9.2f%% | %8.1f%% %9.1f%% %9.1f%%"
                  % (r["dataset"], r["ours"]["GA"], r["drain"]["GA"],
                     _pct(r["dec"]["over_merge"], r["dec"]["lost"]),
                     _pct(dd["over_merge"], dd["lost"]),
                     _pct(dd["over_split"], dd["lost"])))

    if args.csv:
        flat = _csv_rows(rows)
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(CSV_COLS), extrasaction="ignore")
            w.writeheader()
            w.writerows(flat)
        print("\n已写出 %s（%d 行）" % (args.csv, len(flat)))

    print("\n读法：**这个表不告诉你该调什么阈值。**")
    print("它只回答「GA 的分丢在哪一摊」。两摊的方向相反（过合并要更严、"
          "过分裂要更宽），")
    print("所以必须先看构成，再决定那是不是一个**结构性缺陷** —— 而不是"
          "拿它去反推参数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
