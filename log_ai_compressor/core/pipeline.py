# -*- coding: utf-8 -*-
"""流式处理管线：逐行读取 -> 解析 -> 过滤 -> 聚类 -> 智能分析 -> 结果。

性能与内存设计（核心架构约束）
------------------------------
1. **纯流式逐行读取**：文件以文本流迭代器逐行消费，内存占用仅与
   错误簇数量相关，与日志总行数无关（理论无行数上限）；
2. **典型样例上下文捕获**：
   - 前上下文：滚动窗口（带行号的 deque，容量 context+64）按行号
     区间截取，条目自身堆栈行不会混入前上下文；
   - 后上下文：仅对「新建簇 / 更换样例」开启待补队列，收集其后
     context 行物理原始行，收满即关闭 —— 内存严格有界；
3. **进度与取消**：进度回调按行数间隔触发；取消事件按独立间隔轮询，
   取消后返回已完成的增量结果（truncated 标记）。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Dict, Iterable, List, Optional

from log_ai_compressor.constants import (
    CANCEL_CHECK_EVERY_LINES,
    DEFAULT_SELECTED_LEVELS,
    ERROR_LEVELS,
    PROGRESS_EVERY_LINES,
)
from log_ai_compressor.core.analysis import analyze_clusters
from log_ai_compressor.core.clustering import ErrorClusterer
from log_ai_compressor.core.encoding import detect_encoding, open_text_stream
from log_ai_compressor.core.filters import EntryFilter, FilterConfig
from log_ai_compressor.core.models import AnalysisResult, ErrorCluster, RunStats
from log_ai_compressor.core.parser import LogParser
from log_ai_compressor.rules.engine import RuleSet, load_ruleset

# 进度回调签名：cb({"lines", "lps", "clusters", "elapsed", "phase"})
ProgressCallback = Callable[[dict], None]


class ProcessingCancelled(Exception):
    """用户主动取消处理（run 仍会返回增量结果前抛出/或内部捕获）。"""


@dataclass
class PipelineConfig:
    """管线配置：过滤参数 + 解析规则 + 分析开关。"""
    filter_config: FilterConfig = field(default_factory=FilterConfig.defaults)
    rule: Optional[str] = None          # 规则集名（generic/embedded/jenkins）或 YAML 路径
    analyze: bool = True                # 是否执行智能分析（根因/异常/优先级）


class LogPipeline:
    """单文件 / 单文本的流式分析管线（可复用，线程安全边界由调用方保证）。"""

    def __init__(self,
                 config: Optional[PipelineConfig] = None,
                 progress_cb: Optional[ProgressCallback] = None,
                 cancel_event: Optional[Event] = None,
                 ruleset: Optional[RuleSet] = None):
        self._config = config or PipelineConfig()
        self._progress_cb = progress_cb
        self._cancel_event = cancel_event
        # 规则集：外部注入优先（复用已编译规则），否则按配置名加载
        self._ruleset = ruleset or load_ruleset(self._config.rule)
        self._entry_filter = EntryFilter(self._config.filter_config)
        self._ctx = self._config.filter_config.context_lines

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------
    def run_file(self, path) -> AnalysisResult:
        """流式分析日志文件（编码自动探测）。"""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"日志文件不存在: {path}")
        encoding = detect_encoding(path)
        stats = RunStats(source=str(path), encoding=encoding,
                         rule_name=self._ruleset.name)
        with open_text_stream(path, encoding) as stream:
            return self._process_lines(stream, stats)

    def run_text(self, text: str, source: str = "<粘贴文本>") -> AnalysisResult:
        """分析粘贴的日志文本。"""
        stats = RunStats(source=source, encoding="utf-8",
                         rule_name=self._ruleset.name)
        return self._process_lines(text.splitlines(), stats)

    # ------------------------------------------------------------------
    # 核心流式处理
    # ------------------------------------------------------------------
    def _process_lines(self, lines: Iterable[str], stats: RunStats) -> AnalysisResult:
        t0 = time.perf_counter()
        parser = LogParser(self._ruleset)
        clusterer = ErrorClusterer()

        from log_ai_compressor.core.models import TimeHistogram
        from log_ai_compressor.constants import GLOBAL_HIST_MAX_BUCKETS
        global_hist = TimeHistogram(max_buckets=GLOBAL_HIST_MAX_BUCKETS)

        # 前上下文滚动窗口：(行号, 行文本)，容量 context+64 保证
        # 常规堆栈长度下仍能取到条目之前的上下文
        window = self._ctx + 64
        recent: deque = deque(maxlen=window)
        # 后上下文待补队列：cluster_id -> {"cluster", "start_line", "lines"}
        pending: Dict[int, dict] = {}

        line_no = 0
        cancelled = False

        def notify(phase: str) -> None:
            if self._progress_cb:
                elapsed = time.perf_counter() - t0
                self._progress_cb({
                    "lines": line_no,
                    "lps": line_no / elapsed if elapsed > 0 else 0.0,
                    "clusters": len(clusterer),
                    "elapsed": elapsed,
                    "phase": phase,
                })

        try:
            for raw_line in lines:
                line_no += 1
                line = raw_line.rstrip("\r\n")

                # 1) 新条目开始 -> 处理上一个完整条目
                completed = parser.feed(line, line_no)
                if completed is not None:
                    self._handle_entry(completed, stats, clusterer,
                                       global_hist, recent, pending)

                # 2) 后上下文补录（仅对开启待补的样例）
                if pending:
                    for item in list(pending.values()):
                        if (line_no > item["start_line"]
                                and len(item["lines"]) < self._ctx):
                            item["lines"].append(line)
                            if len(item["lines"]) >= self._ctx:
                                self._close_pending(item, pending)

                # 3) 前上下文滚动窗口
                recent.append((line_no, line))

                # 4) 取消轮询（独立间隔，开销极低）
                if (self._cancel_event is not None
                        and line_no % CANCEL_CHECK_EVERY_LINES == 0
                        and self._cancel_event.is_set()):
                    cancelled = True
                    break

                # 5) 进度上报（按行数间隔）
                if line_no % PROGRESS_EVERY_LINES == 0:
                    notify("parsing")

            # 流结束：产出最后一个条目
            last = parser.flush()
            if last is not None:
                self._handle_entry(last, stats, clusterer, global_hist,
                                   recent, pending)
        except ProcessingCancelled:
            cancelled = True

        # 关闭所有待补样例的后上下文
        for item in list(pending.values()):
            self._close_pending(item, pending)

        stats.total_lines = line_no
        stats.truncated = cancelled
        stats.duration = time.perf_counter() - t0
        stats.lines_per_second = (line_no / stats.duration
                                  if stats.duration > 0 else 0.0)

        result = AnalysisResult(
            stats=stats,
            clusters=clusterer.clusters,
            global_hist=global_hist,
            keywords=self._config.filter_config.normalized_include(),
        )
        if self._config.analyze and result.clusters:
            analyze_clusters(result)
        notify("done" if not cancelled else "cancelled")
        return result

    # ------------------------------------------------------------------
    # 条目处理
    # ------------------------------------------------------------------
    def _handle_entry(self, entry, stats: RunStats, clusterer: ErrorClusterer,
                      global_hist, recent: deque, pending: Dict[int, dict]) -> None:
        """统计 + 过滤 + 聚类；必要时开启样例后上下文待补。"""
        stats.entry_lines += 1
        stats.level_counts[entry.level] = stats.level_counts.get(entry.level, 0) + 1

        # 日志时间范围（所有带时间戳条目，不限级别）
        if entry.timestamp is not None:
            if stats.time_start is None or entry.timestamp < stats.time_start:
                stats.time_start = entry.timestamp
            if stats.time_end is None or entry.timestamp > stats.time_end:
                stats.time_end = entry.timestamp

        if entry.level in ERROR_LEVELS:
            stats.error_lines += 1

        # 修复缺陷R40：聚类准入由过滤器决定 —— 级别复选框选中
        # WARN/INFO/DEBUG 时同样聚类展示（五级别五色五档）；
        # error_lines/error_entries 统计口径不变（仍只计错误类级别）
        if not self._entry_filter.match(entry):
            return
        if entry.level in ERROR_LEVELS:
            stats.error_entries += 1

        # 前上下文快照：按行号区间截取（排除条目自身的堆栈/折行）
        before: Optional[List[str]] = None
        if self._ctx > 0:
            before = [text for (no, text) in recent
                      if entry.line_no - self._ctx <= no < entry.line_no]

        cluster, replaced = clusterer.add(entry, before)
        if replaced and self._ctx > 0:
            # 新建簇或样例升级 -> 开启后上下文待补
            pending[cluster.cluster_id] = {
                "cluster": cluster,
                "start_line": entry.last_line_no or entry.line_no,
                "lines": [],
            }

        # 全局错误时间直方图（过滤后的错误，与结果口径一致）
        if entry.timestamp is not None:
            global_hist.add(entry.timestamp)

    def _close_pending(self, item: dict, pending: Dict[int, dict]) -> None:
        """收束待补样例：写入样例的 after 上下文并移除队列。"""
        cluster: ErrorCluster = item["cluster"]
        if cluster.sample is not None:
            cluster.sample.after = list(item["lines"])
        pending.pop(cluster.cluster_id, None)


# ---------------------------------------------------------------------------
# 便捷 API（GUI / CLI / 测试共用）
# ---------------------------------------------------------------------------
def _build_config(levels, include, exclude, top_n, context_lines, rule,
                  analyze) -> PipelineConfig:
    cfg = FilterConfig(
        levels=list(levels) if levels else list(DEFAULT_SELECTED_LEVELS),
        include=list(include or []),
        exclude=list(exclude or []),
        top_n=top_n if top_n else FilterConfig.defaults().top_n,
        context_lines=context_lines if context_lines is not None
        else FilterConfig.defaults().context_lines,
    )
    return PipelineConfig(filter_config=cfg, rule=rule, analyze=analyze)


def analyze_file(path, *, levels=None, include=None, exclude=None,
                 top_n=None, context_lines=None, rule=None, analyze=True,
                 progress_cb: Optional[ProgressCallback] = None,
                 cancel_event: Optional[Event] = None) -> AnalysisResult:
    """分析单个日志文件（详见 LogPipeline.run_file）。"""
    pipeline = LogPipeline(_build_config(levels, include, exclude, top_n,
                                         context_lines, rule, analyze),
                           progress_cb=progress_cb, cancel_event=cancel_event)
    return pipeline.run_file(path)


def analyze_text(text: str, *, source: str = "<粘贴文本>", levels=None,
                 include=None, exclude=None, top_n=None, context_lines=None,
                 rule=None, analyze=True,
                 progress_cb: Optional[ProgressCallback] = None,
                 cancel_event: Optional[Event] = None) -> AnalysisResult:
    """分析粘贴文本（详见 LogPipeline.run_text）。"""
    pipeline = LogPipeline(_build_config(levels, include, exclude, top_n,
                                         context_lines, rule, analyze),
                           progress_cb=progress_cb, cancel_event=cancel_event)
    return pipeline.run_text(text, source=source)
