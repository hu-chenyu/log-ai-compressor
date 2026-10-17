#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一量：有没有任何相似度量能把「该合并」和「不该合并」分开？

为什么要有这个脚本
------------------
HPC 是同预算档唯一的负例，病根是**过度合并**：`boot (command N)` 与
`risBoot (command N)` 在 41/44 字符的样板上 ratio 0.965，被并成一簇。

前两版修法都是「猜一个规则」：

* v2.9.8 加「字面词元集合相同才准合并」-> HPC 覆盖率 85.1% 涨到 93.2%，
  但报告从 76 个簇炸成 2,690 个、交付口径从 23.0% 崩到 8.1% -> **撤回**。
* v2.9.8 的结论于是写成「一条全局规则做不到两件事都对，要按数据集自适应粒度」。

v2.10.0 不再猜了，改为**先量**：把 `_find_similar` 的每一次合并决定都记下来，
用 Loghub 的人工标注判定对错，然后去找能分开两类的特征。量完的结论是
**没有这样的特征**（见输出与 `tests/test_cluster_token_guard.py`）。

用法
----
    python scripts/diagnose_merge_decisions.py            # HPC + Proxifier
    python scripts/diagnose_merge_decisions.py --datasets HPC Mac

判据与口径
----------
「该不该合并」= 探针消息与被并入簇的样例消息，Loghub 标注的 EventId 是否相同。
注意这是**按 Loghub 的事件定义**判的，而它与「对用户好不好」不是同一个问题 ——
脚本末尾会把这一点连同覆盖率一起报出来，别拿它单独下结论。

数据不在仓库里（`data/` 被 gitignore），先跑 `python scripts/fetch_datasets.py --all`。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_ai_compressor.core import clustering as C                  # noqa: E402
from log_ai_compressor.core.parser import LogParser                 # noqa: E402
from log_ai_compressor.rules.engine import load_ruleset              # noqa: E402

DATA = ROOT / "data" / "loghub"
_PLACE = re.compile(r"\b0xH\b|\b[USHPN]\b")


def literal_tokens(template: str) -> frozenset:
    """与 clustering.literal_tokens 同口径，但走本脚本自己的正则 ——
    免得脚本在聚类器被改动后悄悄跟着变，量出来的还是同一个东西。"""
    return frozenset(_PLACE.sub(" ", template).split())


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 1.0
    return len(a & b) / len(a | b)


def line_events(name: str) -> Dict[int, str]:
    """LineId(1-based) -> EventId，来自 Loghub 的结构化标注 CSV。"""
    hits = list(DATA.glob(f"{name}/{name}/*_full.log_structured.csv"))
    if not hits:
        raise FileNotFoundError(f"{name}: 缺 *_full.log_structured.csv")
    out: Dict[int, str] = {}
    with hits[0].open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                lid = int(row["LineId"])
            except (KeyError, TypeError, ValueError):
                continue
            eid = (row.get("EventId") or "").strip()
            if eid:
                out[lid] = eid
    return out


def parse_all(name: str):
    path = list(DATA.glob(f"{name}/{name}/*_full.log"))
    if not path:
        raise FileNotFoundError(f"{name}: 缺 *_full.log")
    parser = LogParser(load_ruleset("generic"))
    entries = []
    for i, line in enumerate(
            path[0].read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        e = parser.feed(line, i)
        if e is not None:
            entries.append(e)
    if hasattr(parser, "flush"):
        tail = parser.flush()
        if tail is not None:
            entries.append(tail)
    return entries


def diagnose(name: str) -> Tuple[int, List[dict]]:
    """跑一遍聚类，返回 (簇数, 每次相似度回退的合并决定)。"""
    lev = line_events(name)
    entries = parse_all(name)

    decisions: List[dict] = []
    original = C.ErrorClusterer._find_similar

    def spy(self, msg_tp, level):                       # noqa: ANN001
        probe_line = getattr(self, "_probe_line", None)
        got = original(self, msg_tp, level)
        if got is not None and got.sample and got.sample.entry:
            decisions.append({
                "probe": msg_tp,
                "match": got.message_template,
                "line": probe_line,
                "match_line": got.sample.entry.line_no,
            })
        return got

    C.ErrorClusterer._find_similar = spy
    try:
        clusterer = C.ErrorClusterer()
        for e in entries:
            clusterer._probe_line = e.line_no
            clusterer.add(e)
    finally:
        C.ErrorClusterer._find_similar = original

    total_events = len(set(lev.values()))
    samples = {c.sample.entry.line_no for c in clusterer.clusters
               if c.sample and c.sample.entry}
    covered = {lev[ln] for ln in samples if ln in lev}
    print(f"  条目 {len(entries)}   簇 {len(clusterer.clusters)}   "
          f"标注事件 {total_events}   按簇样例覆盖 {len(covered)}/{total_events}"
          f" = {len(covered) / total_events:.1%}")

    for d in decisions:
        a, b = lev.get(d["line"]), lev.get(d["match_line"])
        d["a"], d["b"] = a, b
        d["verdict"] = ("标注不全" if a is None or b is None
                        else ("该合并" if a == b else "不该合并"))
        d["ratio"] = SequenceMatcher(None, d["probe"], d["match"]).ratio()
        d["jaccard"] = jaccard(literal_tokens(d["probe"]),
                               literal_tokens(d["match"]))
    return len(clusterer.clusters), decisions


def summarise(name: str, decisions: List[dict]) -> None:
    print()
    print(f"  {'类别':<10}{'次数':>7}{'字符相似度 min/中位/max':>30}"
          f"{'词元 Jaccard min/中位/max':>32}")
    print("  " + "-" * 80)
    buckets = {}
    for verdict in ("该合并", "不该合并", "标注不全"):
        sub = [d for d in decisions if d["verdict"] == verdict]
        if not sub:
            continue
        r = sorted(d["ratio"] for d in sub)
        j = sorted(d["jaccard"] for d in sub)
        mid = len(sub) // 2
        buckets[verdict] = (len(sub), r[0], r[mid], r[-1], j[0], j[mid], j[-1])
        print(f"  {verdict:<10}{len(sub):>7}"
              f"{r[0]:>12.3f}{r[mid]:>10.3f}{r[-1]:>8.3f}"
              f"{j[0]:>15.3f}{j[mid]:>10.3f}{j[-1]:>8.3f}")

    right, wrong = buckets.get("该合并"), buckets.get("不该合并")
    if not right or not wrong:
        print("  两类不全，本数据集给不出结论")
        return

    print()
    for label, lo, hi, unit in (("字符相似度", right[1], wrong[3], ""),
                                ("词元 Jaccard", right[4], wrong[6], "")):
        if lo <= hi:
            print(f"  {label}: 「该合并」最低 {lo:.3f} <= 「不该合并」最高 {hi:.3f}"
                  f"  -> 区间重叠，**任何阈值都分不开**")
        else:
            print(f"  {label}: 「该合并」最低 {lo:.3f} > 「不该合并」最高 {hi:.3f}"
                  f"  -> 区间分离，判据出现了，值得重新评估加守卫")

    print()
    print("  「不该合并」样例：")
    for d in [x for x in decisions if x["verdict"] == "不该合并"][:5]:
        print(f"    {d['a']} != {d['b']}  ratio={d['ratio']:.3f} "
              f"J={d['jaccard']:.3f}")
        print(f"      探针 {d['probe'][:86]!r}")
        print(f"      被并 {d['match'][:86]!r}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="合并决策的可分性诊断")
    ap.add_argument("--datasets", default="HPC,Proxifier",
                    help="逗号分隔的数据集名")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.datasets.split(",") if n.strip()]
    if not DATA.is_dir():
        print(f"缺 {DATA}：先跑 python scripts/fetch_datasets.py --all")
        return 1

    print("合并决策可分性诊断（HPC + Proxifier 为 v2.10.0 实测口径）")
    print("判据：Loghub 标注的 EventId 相同 = 该合并")
    for name in names:
        print()
        print("=" * 80)
        print(f"  {name}")
        print("=" * 80)
        try:
            _clusters, decisions = diagnose(name)
        except FileNotFoundError as exc:
            print(f"  跳过：{exc}")
            continue
        print(f"  相似度回退决定 {len(decisions)} 次")
        summarise(name, decisions)

    print()
    print("提醒：上面判的是「按 Loghub 的事件定义对不对」，不是「对用户好不好」。")
    print("Proxifier 大量合并被 Loghub 判错，覆盖率却仍是 100% —— "
          "两个问题必须分开看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
