# -*- coding: utf-8 -*-
"""多文件对比分析（2~3 个日志文件）：新增 / 消失 / 共同错误与数量变化率。

设计说明
--------
- 对比键 =（级别, 消息指纹模板）：与堆栈无关，规避「同一错误一处带
  堆栈、一处无堆栈」造成的假差异；
- 多文件（3 个）时以第一个文件为基准，分别与其余文件两两对比；
- 典型场景：版本对比、修复前后对比 —— 快速回答「改了什么、
  修好了什么、又引入了什么」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from log_ai_compressor.core.models import AnalysisResult, ErrorCluster
from log_ai_compressor.core.pipeline import analyze_file


@dataclass
class CompareItem:
    """一对簇的对比结果。"""
    template: str
    level: str = ""
    module: str = ""
    summary: str = ""
    count_a: int = 0
    count_b: int = 0
    change_rate: Optional[float] = None   # (B-A)/A*100；A=0（新增）时为 None
    # 本项是怎么对上的：'template'（消息模板相同）/ 'trace'（同一个
    # trace_id，措辞不同）/ ''（新增或消失，没有对应项）。
    #
    # 为什么要有这个字段：同一个 trace 在两个文件里措辞不同（上游报
    # "connection refused"、下游报 "upstream unavailable"）时，只按
    # 消息模板匹配会把**同一次事故**算成「1 个新增 + 1 个消失」——
    # 报告会让人以为出了新问题、同时又修好了老问题。看报告的人
    # 分不清这两项其实是同一件事。标出对齐依据，人才判断得了。
    matched_by: str = ""
    # 按 trace 对齐时共同命中的关联键（给人看一眼是哪条 trace）
    trace_ids: List[str] = field(default_factory=list)

    @property
    def change_text(self) -> str:
        if self.change_rate is None:
            return "新增"
        rate = self.change_rate
        if rate > 0:
            return f"+{rate:.1f}%"
        if rate < 0:
            return f"{rate:.1f}%"
        return "持平"


@dataclass
class CompareResult:
    """一次两两对比的完整结果。"""
    base_name: str = ""
    other_name: str = ""
    new_items: List[CompareItem] = field(default_factory=list)     # B 新增
    gone_items: List[CompareItem] = field(default_factory=list)    # B 中消失
    common_items: List[CompareItem] = field(default_factory=list)

    @property
    def total_common(self) -> int:
        return sum(i.count_b for i in self.common_items)

    def as_dict(self) -> dict:
        return {
            "base": self.base_name,
            "other": self.other_name,
            "new": [_item_dict(i) for i in self.new_items],
            "gone": [_item_dict(i) for i in self.gone_items],
            "common": [_item_dict(i) for i in self.common_items],
        }


def _item_dict(i: CompareItem) -> dict:
    return {
        "level": i.level, "module": i.module, "summary": i.summary,
        "count_a": i.count_a, "count_b": i.count_b,
        "change_rate": i.change_rate,
        "matched_by": i.matched_by,
        "trace_ids": list(i.trace_ids),
    }


def _cluster_key(c: ErrorCluster) -> Tuple[str, str]:
    """对比键：（级别, 消息指纹模板）。"""
    return (c.level, c.message_template or c.template)


def _trace_index(clusters) -> Dict[str, List[ErrorCluster]]:
    """关联键 → 出现过它的簇（一个 trace 可能横跨多个簇）。"""
    idx: Dict[str, List[ErrorCluster]] = {}
    for c in clusters:
        for tid in (getattr(c, "trace_ids", None) or ()):
            idx.setdefault(tid, []).append(c)
    return idx


def compare_results(base: AnalysisResult, other: AnalysisResult) -> CompareResult:
    """对比两份管线结果。

    对齐分两级（2026-10-21 第九轮外部复核发现缺第二级）：

    1. **消息模板**相同 → 直接配上（原有行为）；
    2. **同一个 trace_id** → 兜底配上。

    为什么需要第二级：同一次请求横跨多个服务时，各服务的措辞往往不同
    （上游 "connection refused"、下游 "upstream unavailable"）。只按
    模板匹配时，这一次事故会被算成「新增 1 种 + 消失 1 种」—— 报告读
    起来像「出了新问题、同时修好了老问题」，而实际上是**同一件事**。
    对一位拿着两份日志做版本对比的工程师，这个假象会直接把人带偏。

    兜底的边界：只有**两边都没被模板匹配上**的簇才走 trace 兜底，
    且一个簇只配一次（先到先得），避免把 trace 当万能钥匙乱连。
    """
    a_map: Dict[Tuple[str, str], ErrorCluster] = {}
    for c in base.clusters:
        a_map.setdefault(_cluster_key(c), c)
    b_map: Dict[Tuple[str, str], ErrorCluster] = {}
    for c in other.clusters:
        b_map.setdefault(_cluster_key(c), c)

    result = CompareResult(
        base_name=Path(base.stats.source).name,
        other_name=Path(other.stats.source).name,
    )

    # 已配上的簇（用 id 记，因为同一簇可能有多条消息模板）
    matched_a: set = set()
    matched_b: set = set()

    # ---- 第一级：消息模板相同 ------------------------------------------
    for key, bc in b_map.items():
        ac = a_map.get(key)
        if ac is None:
            continue
        change = (None if ac.count == 0
                  else (bc.count - ac.count) / ac.count * 100.0)
        result.common_items.append(CompareItem(
            template=bc.template, level=bc.level, module=bc.module,
            summary=bc.summary, count_a=ac.count, count_b=bc.count,
            change_rate=change, matched_by="template"))
        matched_a.add(id(ac))
        matched_b.add(id(bc))

    # ---- 第二级：同一个 trace_id 兜底 ----------------------------------
    # 只处理**两边都没被模板匹配上**的簇。
    a_traces = _trace_index(base.clusters)
    b_traces = _trace_index(other.clusters)
    for tid, b_clusters in sorted(b_traces.items()):
        a_clusters = a_traces.get(tid)
        if not a_clusters:
            continue
        for bc in b_clusters:
            if id(bc) in matched_b:
                continue
            for ac in a_clusters:
                if id(ac) in matched_a:
                    continue
                change = (None if ac.count == 0
                          else (bc.count - ac.count) / ac.count * 100.0)
                result.common_items.append(CompareItem(
                    template=bc.template, level=bc.level,
                    module=bc.module, summary=bc.summary,
                    count_a=ac.count, count_b=bc.count,
                    change_rate=change, matched_by="trace",
                    trace_ids=[tid]))
                matched_a.add(id(ac))
                matched_b.add(id(bc))
                break

    # ---- 剩下的才是真新增 / 真消失 ------------------------------------
    for key, ac in a_map.items():
        if id(ac) in matched_a:
            continue
        result.gone_items.append(CompareItem(
            template=ac.template, level=ac.level, module=ac.module,
            summary=ac.summary, count_a=ac.count, count_b=0,
            matched_by=""))
    for key, bc in b_map.items():
        if id(bc) in matched_b:
            continue
        result.new_items.append(CompareItem(
            template=bc.template, level=bc.level, module=bc.module,
            summary=bc.summary, count_a=0, count_b=bc.count,
            matched_by=""))

    # 排序：新增按 B 次数降序；消失按 A 次数降序；共同按变化幅度降序
    result.new_items.sort(key=lambda i: i.count_b, reverse=True)
    result.gone_items.sort(key=lambda i: i.count_a, reverse=True)
    result.common_items.sort(
        key=lambda i: abs(i.change_rate or 0), reverse=True)
    return result


def compare_files(paths, *, levels=None, include=None, exclude=None,
                  top_n=None, context_lines=None, rule=None,
                  use_regex: bool = False, encoding=None
                  ) -> List[CompareResult]:
    """多文件对比便捷入口：第一个文件为基准，与其余文件两两对比。"""
    paths = [Path(p) for p in paths]
    if len(paths) < 2:
        raise ValueError("对比分析至少需要 2 个日志文件")
    results = [
        analyze_file(p, levels=levels, include=include, exclude=exclude,
                     top_n=top_n, context_lines=context_lines, rule=rule,
                     analyze=False, use_regex=use_regex, encoding=encoding)
        for p in paths
    ]
    base = results[0]
    return [compare_results(base, other) for other in results[1:]]
