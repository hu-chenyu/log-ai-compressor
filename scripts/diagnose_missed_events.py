#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：哪些人工标注事件类型在压缩输出里丢了，丢到哪里去了。

用法：python scripts/diagnose_missed_events.py Zookeeper HealthApp
"""
from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "benchmark_loghub", ROOT / "scripts" / "benchmark_loghub.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["benchmark_loghub"] = bench
_spec.loader.exec_module(bench)

from log_ai_compressor import service  # noqa: E402


def main() -> int:
    names = sys.argv[1:] or ["Zookeeper", "HealthApp"]
    for ds in bench.discover(names):
        print("=" * 78)
        print(f"  {ds.name}   标注事件 {len(ds.events)} 种")
        print("=" * 78)

        result = service.analyze(
            paths=[str(ds.log_path)],
            params={"levels": bench.ALL_LEVELS, "top_n": 0,
                    "context_lines": 0})
        covered = set()
        # 簇 -> 它吸收了哪些标注事件
        cluster_of_event = {}
        for c in result.clusters:
            s = getattr(c, "sample", None)
            if s is None or s.entry is None:
                continue
            eid = ds.event_of_line_no(s.entry.line_no)
            if eid:
                covered.add(eid)
                cluster_of_event[eid] = c

        missed = sorted(ds.events - covered)
        print(f"\n  工具产出 {len(result.clusters)} 个簇，"
              f"覆盖 {len(covered)} / {len(ds.events)} 种事件，"
              f"漏 {len(missed)} 种\n")

        # 每种标注事件本来有多少行（判断「稀有模板」还是「被合并掉」）
        occ = Counter(ds.line_event.values())
        for eid in missed:
            tpl = ds.event_template.get(eid, "")
            # 找出是否有簇的样例消息与它高度相似（= 被并进了别的簇）
            merged_into = None
            for c in result.clusters:
                s = getattr(c, "sample", None)
                if s is None or s.entry is None:
                    continue
                if eid in cluster_of_event.values():
                    break
                msg = s.entry.full_message
                if bench.canon(msg) and bench.canon(tpl):
                    from difflib import SequenceMatcher
                    if SequenceMatcher(
                            None, bench.canon(msg),
                            bench.canon(tpl)).ratio() >= 0.7:
                        merged_into = c
                        break
            print(f"  [{eid}] 出现 {occ[eid]:,} 次")
            print(f"      标注模板: {tpl[:100]}")
            if merged_into is not None:
                print(f"      被并入簇 #{merged_into.cluster_id}: "
                      f"{merged_into.message_template[:90]}")
            else:
                print("      未找到相似簇 —— 该事件类型可能根本没被解析出来")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())