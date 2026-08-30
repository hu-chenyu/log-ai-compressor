# -*- coding: utf-8 -*-
"""导出层：结构化报告输出（Markdown / JSON / 纯文本）。

Markdown 格式专为「投喂大模型」优化：
- 一、概览统计 —— 让模型快速建立全局认知；
- 二、Top 错误清单 —— 表格化去重后的错误全集；
- 三、典型样例详情 —— 每簇一份样例 + 前后上下文 + 降噪堆栈，
  避免原始日志的重复内容浪费上下文窗口。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from log_ai_compressor import __version__
from log_ai_compressor.constants import DEFAULT_TOP_N
from log_ai_compressor.core.analysis import simplify_stack
from log_ai_compressor.core.comparator import CompareResult
from log_ai_compressor.core.models import (
    AnalysisResult,
    ErrorCluster,
    format_timestamp,
)

_ANOMALY_LABELS = {"burst": "集中爆发", "rare": "罕见异常", "": ""}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _md_escape(text: str) -> str:
    """Markdown 表格单元格转义。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("`", "'")


def _ts_range(result: AnalysisResult) -> str:
    s = result.stats
    if s.time_start is None:
        return "-"
    return f"{format_timestamp(s.time_start)} ~ {format_timestamp(s.time_end)}"


def _root_summary(result: AnalysisResult) -> str:
    roots = [c for c in result.clusters if c.is_root_cause]
    if not roots:
        return "未发现明确根因（无时间连锁 / Caused-by 链 / 强根因特征）"
    top = roots[0]
    others = f"（另有 {len(roots) - 1} 个根因候选）" if len(roots) > 1 else ""
    return f"{top.summary[:120]}{others}"


def _anomaly_label(c: ErrorCluster) -> str:
    return _ANOMALY_LABELS.get(c.anomaly, c.anomaly)


def _rate_text(lps: float) -> str:
    if lps >= 10000:
        return f"{lps / 10000:.1f} 万行/秒"
    return f"{lps:.0f} 行/秒"


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------
def to_markdown(result: AnalysisResult, top_n: Optional[int] = None,
                title: Optional[str] = None) -> str:
    """生成适配大模型输入的 Markdown 结构化报告。"""
    n = top_n or DEFAULT_TOP_N
    clusters = result.clusters[:n]
    s = result.stats
    title = title or f"日志AI压缩报告：{s.source}"

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> log-ai-compressor v{__version__} | "
                 f"处理 {s.total_lines} 行 | 耗时 {s.duration:.2f}s | "
                 f"{_rate_text(s.lines_per_second)} | 规则 {s.rule_name}")
    lines.append("")
    lines.append(f"**初步定位根因**：{_md_escape(_root_summary(result))}")
    lines.append("")

    # 一、概览统计
    lines.append("## 一、概览统计")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 日志来源 | {_md_escape(s.source)} |")
    lines.append(f"| 编码 | {s.encoding} |")
    lines.append(f"| 总行数 | {s.total_lines} |")
    lines.append(f"| 错误行数（FATAL/ERROR/FAIL） | {s.error_lines} |")
    lines.append(f"| 错误种类数（去重后） | {len(result.clusters)} |")
    lines.append(f"| 错误总次数（过滤后） | {s.error_entries} |")
    lines.append(f"| 日志时间范围 | {_ts_range(result)} |")
    if s.truncated:
        lines.append(f"| 处理状态 | 用户取消，已处理前 {s.total_lines} 行 |")
    lines.append("")
    level_parts = [f"{k}={v}" for k, v in sorted(s.level_counts.items())]
    lines.append(f"级别分布：{', '.join(level_parts) if level_parts else '-'}")
    lines.append("")

    # 二、Top 错误清单
    lines.append(f"## 二、Top {len(clusters)} 错误清单（按优先级排序）")
    lines.append("")
    if not clusters:
        lines.append("未发现符合条件的错误。")
        return "\n".join(lines) + "\n"
    lines.append("| # | 优先级 | 级别 | 次数 | 模块 | 根因 | 异常 | 错误摘要 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, c in enumerate(clusters, 1):
        root = f"✔ {_md_escape(c.root_cause_reason[:16])}" if c.is_root_cause else "—"
        lines.append(
            f"| {i} | {c.priority_label} | {c.level} | {c.count} | "
            f"{_md_escape(c.module) or '-'} | {root} | "
            f"{_anomaly_label(c) or '—'} | {_md_escape(c.summary)} |")
    lines.append("")

    # 三、典型样例详情
    lines.append("## 三、典型样例详情（每错误一份，含上下文与降噪堆栈）")
    lines.append("")
    for i, c in enumerate(clusters, 1):
        lines.extend(_cluster_detail_md(i, c))
    return "\n".join(lines) + "\n"


def _cluster_detail_md(index: int, c: ErrorCluster) -> List[str]:
    """单个错误簇的详情段落。"""
    out: List[str] = []
    out.append(f"### {index}. [{c.priority_label}][{c.level}] "
               f"{_md_escape(c.summary)}")
    out.append("")
    meta = [f"出现 {c.count} 次", f"行 {c.first_line}~{c.last_line}"]
    if c.first_seen is not None:
        meta.append(f"首末时间 {format_timestamp(c.first_seen)}"
                    f" ~ {format_timestamp(c.last_seen)}")
    if c.module:
        meta.append(f"模块 {c.module}")
    out.append(f"- {' | '.join(meta)}")
    notes = []
    if c.is_root_cause:
        notes.append(f"根因：{c.root_cause_reason}")
    elif c.root_cause_reason:
        notes.append(c.root_cause_reason)
    if c.anomaly:
        notes.append(f"异常：{_anomaly_label(c)}")
    if notes:
        out.append(f"- 智能分析：{'；'.join(notes)}")
    out.append("")

    sample = c.sample
    if sample is None:
        out.append("（无典型样例）")
        out.append("")
        return out

    entry = sample.entry
    if sample.before:
        out.append("**前上下文**")
        out.append("```")
        out.extend(sample.before)
        out.append("```")
        out.append("")

    out.append("**典型样例**")
    out.append("```")
    out.append(entry.raw)
    if entry.message_extra:
        out.extend(entry.message_extra)
    out.append("```")
    out.append("")

    if entry.stack:
        simplified = simplify_stack(entry.stack)
        out.append(f"**堆栈（已降噪：业务帧 {simplified.business_count} 行，"
                   f"折叠系统/第三方帧 {simplified.noise_count} 行）**")
        out.append("```")
        out.extend(simplified.lines)
        out.append("```")
        out.append("")

    if sample.after:
        out.append("**后上下文**")
        out.append("```")
        out.extend(sample.after)
        out.append("```")
        out.append("")
    return out


# ---------------------------------------------------------------------------
# 简要摘要（一键复制投喂 AI）
# ---------------------------------------------------------------------------
def brief_summary(result: AnalysisResult, top_n: Optional[int] = None) -> str:
    """生成精简文本摘要（适合直接粘贴给 AI 助手）。"""
    n = top_n or min(DEFAULT_TOP_N, 10)
    s = result.stats
    out: List[str] = []
    out.append(f"【日志分析摘要】{s.source}")
    out.append(f"总行数 {s.total_lines}，错误 {s.error_lines} 行，"
               f"去重后 {len(result.clusters)} 种，时间范围 {_ts_range(result)}。")
    out.append(f"初步根因：{_root_summary(result)}")
    out.append("")
    for i, c in enumerate(result.clusters[:n], 1):
        tags = [c.priority_label, c.level, f"×{c.count}"]
        if c.is_root_cause:
            tags.append("根因")
        if c.anomaly:
            tags.append(_anomaly_label(c))
        out.append(f"{i}. [{' | '.join(tags)}] {c.summary}")
        if c.is_root_cause:
            out.append(f"   -> {c.root_cause_reason}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# JSON 导出
# ---------------------------------------------------------------------------
def _cluster_dict(c: ErrorCluster, with_sample: bool = True) -> dict:
    d = {
        "id": c.cluster_id,
        "level": c.level,
        "module": c.module,
        "summary": c.summary,
        "count": c.count,
        "priority": c.priority,
        "priority_label": c.priority_label,
        "root_cause": c.is_root_cause,
        "root_cause_reason": c.root_cause_reason,
        "anomaly": c.anomaly,
        "first_line": c.first_line,
        "last_line": c.last_line,
        "first_seen": c.first_seen,
        "last_seen": c.last_seen,
    }
    if with_sample and c.sample is not None:
        entry = c.sample.entry
        simplified = simplify_stack(entry.stack) if entry.stack else None
        d["sample"] = {
            "line_no": entry.line_no,
            "raw": entry.raw,
            "message": entry.full_message,
            "level": entry.level,
            "module": entry.module,
            "timestamp": entry.timestamp,
            "stack": entry.stack,
            "stack_simplified": simplified.lines if simplified else [],
            "stack_noise_folded": simplified.noise_count if simplified else 0,
            "context_before": c.sample.before,
            "context_after": c.sample.after,
        }
    return d


def to_json(result: AnalysisResult, top_n: Optional[int] = None) -> str:
    """导出 JSON 格式（结构化，适配脚本二次处理）。"""
    n = top_n or DEFAULT_TOP_N
    payload = {
        "generator": f"log-ai-compressor v{__version__}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": result.stats.as_dict(),
        "root_cause_summary": _root_summary(result),
        "error_kinds": len(result.clusters),
        "clusters": [_cluster_dict(c) for c in result.clusters[:n]],
        "time_series": [[t, c] for t, c in result.global_hist.series()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 纯文本导出
# ---------------------------------------------------------------------------
def to_text(result: AnalysisResult, top_n: Optional[int] = None) -> str:
    """导出纯文本报告（无 Markdown 标记，适配任意阅读环境）。"""
    n = top_n or DEFAULT_TOP_N
    s = result.stats
    out: List[str] = []
    out.append("=" * 60)
    out.append(f"日志AI压缩报告：{s.source}")
    out.append("=" * 60)
    out.append(f"总行数: {s.total_lines}  错误行: {s.error_lines}  "
               f"错误种类: {len(result.clusters)}  耗时: {s.duration:.2f}s")
    out.append(f"时间范围: {_ts_range(result)}")
    out.append(f"初步根因: {_root_summary(result)}")
    out.append("-" * 60)
    for i, c in enumerate(result.clusters[:n], 1):
        out.append(f"[{i}] {c.priority_label} {c.level} ×{c.count}  "
                   f"{c.summary}  (行 {c.first_line}~{c.last_line})")
        if c.is_root_cause:
            out.append(f"     根因: {c.root_cause_reason}")
        if c.anomaly:
            out.append(f"     异常: {_anomaly_label(c)}")
        if c.sample is not None and c.sample.entry.stack:
            simplified = simplify_stack(c.sample.entry.stack)
            for line in simplified.lines[:8]:
                out.append(f"     {line}")
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 对比报告
# ---------------------------------------------------------------------------
def compare_to_markdown(results: List[CompareResult]) -> str:
    """多文件对比差异报告（Markdown）。"""
    out: List[str] = []
    out.append("# 日志对比分析报告")
    out.append("")
    for cmp in results:
        out.append(f"## 基准 `{cmp.base_name}` vs `{cmp.other_name}`")
        out.append("")
        out.append(f"- 新增错误：**{len(cmp.new_items)}** 种"
                   f"（共 {sum(i.count_b for i in cmp.new_items)} 次）")
        out.append(f"- 消失错误：**{len(cmp.gone_items)}** 种"
                   f"（基准中共 {sum(i.count_a for i in cmp.gone_items)} 次）")
        out.append(f"- 共同错误：**{len(cmp.common_items)}** 种"
                   f"（共 {cmp.total_common} 次）")
        out.append("")

        for name, items, count_col in (("新增错误（基准中不存在）", cmp.new_items, "count_b"),
                                        ("消失错误（对比文件中已不存在）", cmp.gone_items, "count_a")):
            if items:
                out.append(f"### {name}")
                out.append("")
                out.append("| 级别 | 次数 | 模块 | 错误摘要 |")
                out.append("| --- | --- | --- | --- |")
                for i in items:
                    count = i.count_b if count_col == "count_b" else i.count_a
                    out.append(f"| {i.level} | {count} | "
                               f"{_md_escape(i.module) or '-'} | "
                               f"{_md_escape(i.summary)} |")
                out.append("")

        if cmp.common_items:
            out.append("### 共同错误（数量变化）")
            out.append("")
            out.append("| 级别 | 基准次数 | 对比次数 | 变化率 | 模块 | 错误摘要 |")
            out.append("| --- | --- | --- | --- | --- | --- |")
            for i in cmp.common_items:
                out.append(f"| {i.level} | {i.count_a} | {i.count_b} | "
                           f"{i.change_text} | {_md_escape(i.module) or '-'} | "
                           f"{_md_escape(i.summary)} |")
            out.append("")
    return "\n".join(out) + "\n"
