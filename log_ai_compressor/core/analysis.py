# -*- coding: utf-8 -*-
"""智能辅助分析：错误因果关联、统计异常检测、优先级排序、堆栈精简降噪。

算法设计
--------
1. **根因判定（因果关联）**——三路证据融合：
   a. Caused-by 链：带 "Caused by:" 堆栈的错误指向其紧邻的前置错误；
   b. 时间连锁：突发时间窗口内最先出现且含根因特征关键词的错误；
   c. 强关键词：根因特征词（timeout/refused/permission...）高频命中。
   未命中根因但含被动失败关键词（retry/after/downstream...）的簇
   标记为疑似连锁衍生。
2. **统计异常检测**：
   - 集中爆发（burst）：全局错误直方图中超过 均值+3σ 的桶，
     簇峰值时间落入爆发区间则标注；
   - 罕见异常（rare）：总数可观而仅出现 1 次的错误。
3. **优先级综合评分**：级别权重 40% + 频次（对数归一）30% +
   根因 20% + 异常 10%；FATAL 级强制 P0 前置。
4. **堆栈降噪**：折叠系统库/第三方框架帧，保留业务栈帧与
   关键因果行（Caused by / Traceback / 异常摘要）。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from log_ai_compressor.constants import (
    CASCADE_KEYWORDS,
    LEVEL_WEIGHT,
    ROOT_CAUSE_KEYWORDS,
    is_noise_stack_frame,
)
from log_ai_compressor.core.models import AnalysisResult, ErrorCluster, RunStats

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
BURST_WINDOW_SEC = 60.0        # 时间连锁判定窗口（秒）
BURST_SIGMA = 3.0              # 集中爆发判定阈值（均值 + N 倍标准差）
RARE_MIN_TOTAL = 10            # 触发罕见异常判定的最小错误总量
STRONG_KEYWORD_SCORE = 3       # 强根因关键词命中数阈值

# 关键因果行（降噪时永不折叠）
_CAUSED_BY_RE = re.compile(r"^\s*Caused by\s*[:：]", re.IGNORECASE)
_TRACEBACK_RE = re.compile(r"^Traceback \(|Backtrace:", re.IGNORECASE)
_EXCEPTION_RE = re.compile(r"^[A-Za-z_][\w.$]*(?:Exception|Error|Fault|Interrupt)\s*[:({]")
_RAISE_RE = re.compile(r"^\s*raise\s+\w")


def _is_key_frame(line: str) -> bool:
    """关键因果行：Caused by / Traceback 头 / 异常摘要 / raise，永不折叠。"""
    return bool(
        _CAUSED_BY_RE.match(line) or _TRACEBACK_RE.match(line)
        or _EXCEPTION_RE.match(line) or _RAISE_RE.match(line)
    )


# ---------------------------------------------------------------------------
# 堆栈精简降噪
# ---------------------------------------------------------------------------
@dataclass
class SimplifiedStack:
    """降噪后的堆栈：展示行（含折叠注释）+ 业务/噪声帧计数。"""
    lines: List[str] = field(default_factory=list)
    business_count: int = 0
    noise_count: int = 0

    @property
    def has_business_frames(self) -> bool:
        return self.business_count > 0


def simplify_stack(stack: Sequence[str]) -> SimplifiedStack:
    """堆栈降噪：系统库/第三方框架帧折叠为注释，业务帧与关键因果行保留。"""
    out: List[str] = []
    noise_total = 0
    run_noise = 0

    def flush_noise() -> None:
        nonlocal run_noise
        if run_noise:
            out.append(f"    ...... 已折叠 {run_noise} 行系统库/第三方栈帧 ......")
            run_noise = 0

    for line in stack:
        if not _is_key_frame(line) and is_noise_stack_frame(line):
            noise_total += 1
            run_noise += 1
        else:
            flush_noise()
            out.append(line)
    flush_noise()
    return SimplifiedStack(out, len(stack) - noise_total, noise_total)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def analyze_clusters(result: AnalysisResult) -> float:
    """对管线结果执行智能分析（就地填充字段并排序），返回耗时（秒）。"""
    t0 = time.perf_counter()
    clusters = result.clusters
    if clusters:
        _mark_anomalies(clusters, result.global_hist, result.stats)
        _mark_root_causes(clusters)
        _compute_priorities(clusters)
        _sort_clusters(clusters)
    result.stats.analysis_cost = time.perf_counter() - t0
    return result.stats.analysis_cost


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------
def _mark_anomalies(clusters: List[ErrorCluster], global_hist, stats: RunStats) -> None:
    """集中爆发（burst）与罕见异常（rare）标注。"""
    bursts = global_hist.burst_buckets(k=BURST_SIGMA)
    burst_ranges = [(t, t + global_hist.width) for t, _ in bursts]
    total = stats.error_entries

    for c in clusters:
        peak_t: Optional[float] = None
        if c.hist.total:
            series = c.hist.series()
            peak_t = max(series, key=lambda x: x[1])[0]
        if peak_t is not None and any(a <= peak_t < b for a, b in burst_ranges):
            c.anomaly = "burst"
        elif total >= RARE_MIN_TOTAL and c.count <= 1:
            c.anomaly = "rare"


# ---------------------------------------------------------------------------
# 根因判定
# ---------------------------------------------------------------------------
def _keyword_score(cluster: ErrorCluster) -> int:
    """根因关键词得分（命中数 - 连锁关键词命中数）。"""
    text = f"{cluster.summary} {cluster.template}".lower()
    score = sum(1 for kw in ROOT_CAUSE_KEYWORDS if kw in text)
    penalty = sum(1 for kw in CASCADE_KEYWORDS if kw in text)
    return score - penalty


def _has_cascade_keyword(cluster: ErrorCluster) -> bool:
    text = f"{cluster.summary} {cluster.template}".lower()
    return any(kw in text for kw in CASCADE_KEYWORDS)


def _cluster_sort_key(c: ErrorCluster) -> Tuple[int, float]:
    """排序键：有时间戳者按时间，无时间戳者按行号。"""
    if c.first_seen is not None:
        return (0, c.first_seen)
    return (1, float(c.first_line))


def _mark_root_causes(clusters: List[ErrorCluster]) -> None:
    ordered = sorted(clusters, key=_cluster_sort_key)

    # 1) Caused-by 因果链：链尾错误指向其前置错误为根因
    for c in ordered:
        stack = c.sample.entry.stack if c.sample else []
        if any(_CAUSED_BY_RE.match(line) for line in stack):
            prior = _nearest_prior(ordered, c)
            if prior is not None and not prior.is_root_cause:
                prior.is_root_cause = True
                prior.root_cause_reason = "被 Caused-by 因果链指向"

    # 2) 时间连锁：突发窗口内首发 + 根因关键词
    windows = {}
    for c in ordered:
        if c.first_seen is None:
            continue
        key = int(c.first_seen // BURST_WINDOW_SEC)
        windows.setdefault(key, []).append(c)
    for group in windows.values():
        earliest = group[0]  # ordered 已按时间排序，组内首个即窗口内首发
        if not earliest.is_root_cause and _keyword_score(earliest) > 0:
            earliest.is_root_cause = True
            earliest.root_cause_reason = "时间连锁源头（窗口内首发且含根因特征）"

    # 3) 强关键词 / 连锁衍生标记
    for c in ordered:
        if not c.is_root_cause and _keyword_score(c) >= STRONG_KEYWORD_SCORE:
            c.is_root_cause = True
            c.root_cause_reason = "高频根因特征关键词"
        elif not c.is_root_cause and not c.root_cause_reason and _has_cascade_keyword(c):
            c.root_cause_reason = "疑似连锁衍生错误（被动失败特征）"


def _nearest_prior(ordered: Sequence[ErrorCluster],
                   target: ErrorCluster) -> Optional[ErrorCluster]:
    """按行号寻找 target 之前最近出现的其他簇。"""
    best: Optional[ErrorCluster] = None
    best_gap = None
    for c in ordered:
        if c is target:
            continue
        if c.first_line < target.first_line:
            gap = target.first_line - c.last_line
            if best_gap is None or gap < best_gap:
                best, best_gap = c, gap
    return best


# ---------------------------------------------------------------------------
# 优先级计算
# ---------------------------------------------------------------------------
def _compute_priorities(clusters: List[ErrorCluster]) -> None:
    max_count = max((c.count for c in clusters), default=0)
    denom = math.log10(max_count + 1) if max_count > 1 else 1.0
    for c in clusters:
        level_w = LEVEL_WEIGHT.get(c.level, 0.5)
        freq = math.log10(c.count + 1) / denom if max_count > 1 else 1.0
        root = 1.0 if c.is_root_cause else 0.0
        anomaly = 1.0 if c.anomaly else 0.0
        # 权重：级别 40% + 频次 30% + 根因 20% + 异常 10%
        score = 100.0 * (0.40 * level_w + 0.30 * freq + 0.20 * root + 0.10 * anomaly)
        if c.level == "FATAL":
            score = max(score, 90.0)   # 致命错误保证 P0 前置
        c.priority = round(score, 1)


def _sort_clusters(clusters: List[ErrorCluster]) -> None:
    """排序：FATAL 置顶 -> 优先级降序 -> 次数降序。"""
    clusters.sort(
        key=lambda c: (c.level == "FATAL", c.priority, c.count),
        reverse=True,
    )
