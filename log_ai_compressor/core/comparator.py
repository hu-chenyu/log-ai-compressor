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
    }


def _cluster_key(c: ErrorCluster) -> Tuple[str, str]:
    """对比键：（级别, 消息指纹模板）。"""
    return (c.level, c.message_template or c.template)


def compare_results(base: AnalysisResult, other: AnalysisResult) -> CompareResult:
    """对比两份管线结果。"""
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

    for key, bc in b_map.items():
        ac = a_map.get(key)
        if ac is None:
            result.new_items.append(CompareItem(
                template=bc.template, level=bc.level, module=bc.module,
                summary=bc.summary, count_a=0, count_b=bc.count))
        else:
            change = (None if ac.count == 0
                      else (bc.count - ac.count) / ac.count * 100.0)
            result.common_items.append(CompareItem(
                template=bc.template, level=bc.level, module=bc.module,
                summary=bc.summary, count_a=ac.count, count_b=bc.count,
                change_rate=change))

    for key, ac in a_map.items():
        if key not in b_map:
            result.gone_items.append(CompareItem(
                template=ac.template, level=ac.level, module=ac.module,
                summary=ac.summary, count_a=ac.count, count_b=0))

    # 排序：新增按 B 次数降序；消失按 A 次数降序；共同按变化幅度降序
    result.new_items.sort(key=lambda i: i.count_b, reverse=True)
    result.gone_items.sort(key=lambda i: i.count_a, reverse=True)
    result.common_items.sort(
        key=lambda i: abs(i.change_rate or 0), reverse=True)
    return result


def compare_files(paths, *, levels=None, include=None, exclude=None,
                  top_n=None, context_lines=None, rule=None
                  ) -> List[CompareResult]:
    """多文件对比便捷入口：第一个文件为基准，与其余文件两两对比。"""
    paths = [Path(p) for p in paths]
    if len(paths) < 2:
        raise ValueError("对比分析至少需要 2 个日志文件")
    results = [
        analyze_file(p, levels=levels, include=include, exclude=exclude,
                     top_n=top_n, context_lines=context_lines, rule=rule,
                     analyze=False)
        for p in paths
    ]
    base = results[0]
    return [compare_results(base, other) for other in results[1:]]
