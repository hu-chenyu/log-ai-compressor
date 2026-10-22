# -*- coding: utf-8 -*-
"""核心数据模型：日志条目、错误簇、自适应时间直方图、运行统计与结果。

设计说明
--------
- 数据模型与处理逻辑分离：parser 产出 LogEntry，clustering 聚合为 ErrorCluster，
  analysis 填充智能分析字段，export/GUI 只读模型渲染，层间单向依赖；
- TimeHistogram 是内存控制的核心：桶数上限固定、超出自动扩宽桶宽合并旧桶，
  内存占用 O(max_buckets)，与日志总行数无关。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from log_ai_compressor.constants import (
    GLOBAL_HIST_MAX_BUCKETS,
    HIST_MAX_BUCKETS,
    LEVEL_WEIGHT,
)
from log_ai_compressor.core.evidence import EvidenceRef


def format_timestamp(t: Optional[float]) -> str:
    """时间戳展示格式化：epoch 秒 -> 日期时间；相对秒 -> 秒计数。

    **按 UTC 展示**（2026-10-22 第十轮修复）。

    为什么：`datetime.fromtimestamp(t)` 不带 tz 时按**本机本地时区**渲染，
    于是同一份日志在 UTC 机器与 UTC+8 机器上会显示**不同的钟点** ——
    报告里「时间范围 08:00:00 ~ 08:00:25」在别人的机器上变成
    「00:00:00 ~ 00:00:25」。日志里的裸时间戳没有时区信息，本工具统一
    按 UTC 解释它（见 `core/parser.py::_to_epoch`），展示口径就必须与
    解释口径一致，否则「同一份日志的结论」在不同机器上对不上。

    这也是 JSON 导出（`export/reporters.py::_iso_ts`）一直以来的口径 ——
    这次是把人读的那几条路径补齐到同一套规则上。
    """
    if t is None:
        return "-"
    if t < 1e9:                      # 相对计时（嵌入式秒计数等）
        return f"{t:.3f}s"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 自适应时间直方图
# ---------------------------------------------------------------------------
class TimeHistogram:
    """时间分布直方图（自适应分桶，内存 O(max_buckets)）。

    - 首个样本确定起点，桶宽从 1 秒起步；
    - 桶数超过上限时桶宽 ×8 并合并旧桶（向前兼容历史数据）；
    - 时间回跳（无序日志）时向前平移起点。
    """

    __slots__ = ("_max", "_width", "_start", "_counts")

    def __init__(self, max_buckets: int = HIST_MAX_BUCKETS,
                 initial_width: float = 1.0):
        self._max = max_buckets
        self._width = initial_width
        self._start: Optional[float] = None
        self._counts: Dict[int, int] = {}

    # -- 写入 ------------------------------------------------------------
    def add(self, t: float) -> None:
        """记录一个时间点。"""
        if self._start is None:
            self._start = t
            self._counts = {0: 1}
            return
        if t < self._start:
            # 时间回跳：向前平移起点并整体重定位旧桶
            shift = int((self._start - t) / self._width) + 1
            self._start -= shift * self._width
            self._counts = {k + shift: v for k, v in self._counts.items()}
        idx = int((t - self._start) // self._width)
        while idx >= self._max:
            self._widen()
            idx = int((t - self._start) // self._width)
        self._counts[idx] = self._counts.get(idx, 0) + 1

    def _widen(self) -> None:
        """桶宽 ×8，旧桶按 8:1 合并。"""
        merged: Dict[int, int] = {}
        for idx, cnt in self._counts.items():
            merged[idx // 8] = merged.get(idx // 8, 0) + cnt
        self._counts = merged
        self._width *= 8

    # -- 读取 ------------------------------------------------------------
    @property
    def width(self) -> float:
        """当前桶宽（秒）。"""
        return self._width

    @property
    def total(self) -> int:
        """样本总数。"""
        return sum(self._counts.values())

    @property
    def start(self) -> Optional[float]:
        return self._start

    def series(self) -> List[Tuple[float, int]]:
        """返回 (桶起始时间, 计数) 升序序列，用于趋势图。"""
        if self._start is None:
            return []
        return [
            (self._start + i * self._width, c)
            for i, c in sorted(self._counts.items())
        ]

    def burst_buckets(self, k: float = 3.0) -> List[Tuple[float, int]]:
        """识别集中爆发桶。

        稳健统计：中位数 + k×MAD×1.4826（抗尖峰膨胀：普通 mean+σ 会被
        尖峰自身推高阈值导致漏检）；同时要求至少 2 倍于基线中位数。
        """
        import statistics

        if not self._counts or self._start is None:
            return []
        items = sorted(self._counts.items())
        counts = [c for _, c in items]
        if len(counts) < 2:
            return []
        median = statistics.median(counts)
        mad = statistics.median(abs(c - median) for c in counts)
        robust_threshold = median + k * 1.4826 * mad
        threshold = max(robust_threshold, 2.0 * median)
        return [
            (self._start + i * self._width, c)
            for i, c in items
            if c > threshold
        ]


# ---------------------------------------------------------------------------
# 日志条目
# ---------------------------------------------------------------------------
@dataclass
class LogEntry:
    """单条解析完成的日志条目（含消息折行与堆栈聚合）。"""

    line_no: int                        # 起始行号（1-based）
    raw: str = ""                       # 首行原始文本
    timestamp: Optional[float] = None   # epoch 秒或相对秒
    level: str = "INFO"                 # 规范化级别
    module: str = ""                    # 模块名（可为空）
    message: str = ""                   # 日志内容首行
    message_extra: List[str] = field(default_factory=list)  # 折行续接内容
    stack: List[str] = field(default_factory=list)          # 堆栈/回溯行
    last_line_no: int = 0               # 条目结束行号（含堆栈与续行）
    # **文件内**行号（1-based）—— 与 `line_no` 的差别见下。
    #
    # `line_no` 是**全局**计数：轮转合并（run_files）时它是「这批行里的第几行」，
    # 用于内部逻辑（滚动窗口、后上下文待补、进度）。
    # `file_line_no` 才是**这个来源文件里的第几行** —— 报告里给人看的行号、
    # 证据引用要指向的都是它。
    #
    # 两者在单文件、非尾部窗口时相等；一旦不等（`--tail` 的尾部窗口、轮转合并），
    # 用错的那套就会让「报告里的行号」指向错的地方 ——
    # 而「行号能让人复核回去」正是本工具的核心承诺。
    # 2026-10-22 实测踩过：`--tail 10` 的簇区间报成 92~101，而窗口首行是 91。
    file_line_no: int = 0
    # 同上：条目结束时的**文件内**行号。
    file_last_line_no: int = 0
    # 轮转合并分析时这一行来自哪个文件（单文件时等于该文件路径）。
    # 证据引用要精确到文件，否则 app.log.1 / app.log.2 合并后
    # "第 42 行"指向两个不同的地方，引用就没法复核了。
    source_file: str = ""
    # 结构化日志（JSON Lines / logfmt）的关联键 —— 跨服务调用链的原料。
    # 纯文本日志里这些键可能藏在 message 中，这里只装结构化解析认领到的
    # 值；没抽到就是空串，不猜测。
    trace_id: str = ""
    request_id: str = ""
    span_id: str = ""
    # 本条是否由结构化解析产出（报告里据此说明字段来自原生字段而非正则）
    structured: bool = False
    fields: Dict[str, Any] = field(default_factory=dict)   # 未认领字段原样保留

    @property
    def correlation_id(self) -> str:
        """跨服务关联键：优先 request_id（调用链语义），其次 trace_id。"""
        return self.request_id or self.trace_id

    @property
    def has_stack(self) -> bool:
        return bool(self.stack)

    @property
    def full_message(self) -> str:
        """含折行的完整消息。"""
        return " ".join([self.message] + self.message_extra).strip()


# ---------------------------------------------------------------------------
# 错误簇（聚类结果）
# ---------------------------------------------------------------------------
@dataclass
class ClusterSample:
    """错误簇的典型样例：完整条目 + 前后上下文原始行。"""
    entry: LogEntry
    before: List[str] = field(default_factory=list)   # 前上下文（原始行）
    after: List[str] = field(default_factory=list)    # 后上下文（原始行）


@dataclass
class ChangeEvent:
    """部署/变更事件（服务启动、发版、重启、停机……）。

    跨服务/时间窗/部署事件关联的一环：错误首发前若刚发生过变更，
    是极有价值的排查线索（"上线后开始报错"）。与 traces 同一语义
    边界 —— **只作统计线索，绝不提升置信档位**：时间窗里的共现不
    证明是变更引起的，可能是巧合，也可能是变更与错误同源于另一次
    故障。捕获必须发生在级别过滤之前（发版行多为 INFO 级）。
    """
    ts: Optional[float] = None
    module: str = ""
    text: str = ""                       # 事件原文（截断展示）
    line_no: int = 0
    source: str = ""

    def as_dict(self) -> dict:
        return {"ts": self.ts, "module": self.module, "text": self.text,
                "line_no": self.line_no, "source": self.source}


@dataclass
class ClusterInstance:
    """簇内单个错误实例（修复缺陷R4：全屏簇展开查看全部实例）。

    内存有界设计：
    - 前 MAX_CLUSTER_INSTANCES_DETAILED 个实例保留完整条目与
      前后上下文（可查看原始日志、堆栈与上下文）；
    - 后续实例仅记录时间戳/行号/摘要（元数据）；
    - 超出全局上限后不再记录（instances_truncated 标记）。
    """
    timestamp: Optional[float] = None
    line_no: int = 0
    last_line_no: int = 0
    summary: str = ""                        # 展示用摘要（截断 160）
    entry: Optional[LogEntry] = None         # 完整条目（详情用，可为 None）
    before: List[str] = field(default_factory=list)   # 前上下文（详情用）
    # 修复缺陷R44：实例后上下文（此前仅典型样例收集，点击实例
    # 时详情面板无后上下文可显示）
    after: List[str] = field(default_factory=list)    # 后上下文（详情用）


@dataclass(frozen=True)
class RootCauseVariant:
    """簇内**一种** Caused-by 内层因（链上最内层的那个异常）。

    为什么需要它：簇是按外层消息+堆栈指纹聚的，**同一个簇里可以出现多个
    不同的内层因**。v2.6.0 只报样例那一个，其余全丢 —— 一个真实的漏报缺陷。
    现在按出现次数降序全部列出，主因仍写进 `root_cause_reason`（保持旧口径
    与旧调用方可用），变体清单另存本字段。

    Attributes:
        reason: 内层因原文（异常类名 + 消息）
        count: 携带该内层因的条目数（覆盖**全量**输入，不受实例保留上限影响）
        evidence: 该内层因第一处出现的原文引用；拿不到时为 None ——
            宁可缺一条引用，也不给一条指向别处的假引用。
    """
    reason: str
    count: int
    evidence: Optional[EvidenceRef] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reason": self.reason,
            "count": self.count,
            "evidence_id": self.evidence.evidence_id if self.evidence else "",
            "citation": self.evidence.citation() if self.evidence else "",
        }


@dataclass
class ErrorCluster:
    """同类错误簇：模糊指纹聚合后的错误类别。"""

    cluster_id: int
    template: str                        # 归一化完整指纹模板（级别+消息+堆栈特征）
    message_template: str = ""           # 消息指纹模板（跨堆栈差异的同类合并键）
    level: str = "ERROR"
    module: str = ""
    summary: str = ""                    # 首条消息（展示用）
    count: int = 0
    first_line: int = 0
    last_line: int = 0
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    sample: Optional[ClusterSample] = None
    hist: TimeHistogram = field(default_factory=TimeHistogram)
    # 修复缺陷R4：全部实例记录（展开查看；count 与 len(instances)
    # 在超出保留上限时不一致，以 count 为准）
    instances: List[ClusterInstance] = field(default_factory=list)
    instances_truncated: bool = False    # 实例记录超出上限（未全量保留）

    # ---- 智能分析结果（analysis 模块填充）----
    is_root_cause: bool = False
    root_cause_reason: str = ""
    # ---- 簇内全部 Caused-by 内层因（v2.7.0 修复「一簇只报一因」漏报）----
    # 摄入期**流式**累计：每条带 Caused-by 链的条目入簇时就记一次，
    # 因此计数覆盖全量输入，不受 MAX_CLUSTER_INSTANCES_DETAILED 的截断影响
    # （靠实例详情反推只能得到有偏样本 —— 那正是 v2.6.0 缺陷的成因）。
    cause_counts: Dict[str, int] = field(default_factory=dict)
    # 超出 MAX_CAUSES_PER_CLUSTER 后未展开的实例数（如实记账，不静默丢弃）
    cause_overflow: int = 0
    # 分析期按次数降序展开的变体（含各自第一处原文引用）
    root_cause_variants: List[RootCauseVariant] = field(default_factory=list)
    # 摄入期为每种内层因造的第一处原文引用（内层因文本 → 引用）。
    # 单独存映射而不是直接塞进 evidence：塞进去就得靠 note 文本反查是哪条
    # 内层因，note 一改就断；映射也便于分析期决定要不要并入对外证据列表。
    cause_evidence: Dict[str, EvidenceRef] = field(default_factory=dict)
    # 置信档位（v2 新增）：把"是/不是根因"的二元判断升级为三档，
    # 让工具在证据不足时**敢于不下结论** —— 见 analysis.assess_evidence。
    #   CONFIRMED   确定性证据链直连（Caused-by 链 / 强关键词 ≥3）
    #   LIKELY      有指向性但非直连（时间连锁首发 / 关键词 1~2）
    #   INSUFFICIENT 只有时间共现或频率，没有因果链 —— 不应作为根因输出
    root_cause_confidence: str = ""
    anomaly: str = ""                    # 'burst'/'periodic'/'novel'/'rare'/''
    priority: float = 0.0
    # 优化缺陷R77：优先级评分构成（详情面板判断依据，
    # 形如 "级别35+频次25+根因20（ERROR 档保底 80）"）
    priority_detail: str = ""
    # 优化缺陷R78：相似簇关联（模板 Jaccard ≥0.8 的簇 id 列表）
    related_clusters: List[int] = field(default_factory=list)
    # 优化缺陷R78：根因时间线叙事（因果图传播链文本，仅根因簇有值）
    root_timeline: str = ""
    # 该簇出现过的调用链关联键（analysis 填充）。**不提升置信档位** ——
    # 链路只证明同一次请求的先后顺序，不证明因果。
    traces: List[str] = field(default_factory=list)
    # 该簇出现过的**关联键**（request_id / trace_id），**摄入期**流式收集。
    #
    # 为什么要有它（与 `traces` 区分）：`traces` 由 analysis 层填充，而
    # 对比分析走 `analyze=False`（不做智能标记，只要差异）—— 于是 compare
    # 此前**拿不到任何关联键**，同一个 trace 跨两个文件时只能按消息文本
    # 匹配，同一次请求在两边措辞不同就会被算成「新增 + 消失」两份。
    # 这个字段在聚类期就记好，与分析层无关，compare 因此能按 trace 对齐。
    #
    # 上限：每簇最多记 MAX_CLUSTER_TRACE_IDS 个（内存有界；超出只说明
    # 「这个簇关联了很多次请求」，不影响对齐判定 —— 对齐只需要**有没有**）。
    trace_ids: List[str] = field(default_factory=list)
    # 变更事件时间窗关联（analysis._attach_change_events 填充）："错误
    # 首发在「发版/重启」后 Ns" 的一行叙事。与 traces 同一语义边界 ——
    # **只作统计线索，绝不提升置信档位**（时间共现不证明因果）。
    change_context: str = ""
    # 证据引用（analysis 填充）：把簇的存在与判定绑定到日志原文的具体行。
    # 没有它，「根因是 X」只是一句话；有了它，每条结论都能被调用方
    # 重新读原文核对 —— 见 core/evidence.py。
    evidence: List[EvidenceRef] = field(default_factory=list)

    @property
    def primary_evidence(self) -> Optional[EvidenceRef]:
        """主证据引用（取第一条；无证据时返回 None）。"""
        return self.evidence[0] if self.evidence else None

    @property
    def level_weight(self) -> float:
        return LEVEL_WEIGHT.get(self.level, 0.0)

    @property
    def priority_label(self) -> str:
        """优先级档位标签（P0 错误 / P1 失败 / P2 警告 / P3 信息 /
        P4 调试；修复缺陷R40：FATAL 删除后五档重划）。"""
        if self.priority >= 75:
            return "P0"
        if self.priority >= 55:
            return "P1"
        if self.priority >= 35:
            return "P2"
        if self.priority >= 15:
            return "P3"
        return "P4"


# ---------------------------------------------------------------------------
# 运行统计与分析结果
# ---------------------------------------------------------------------------
@dataclass
class RunStats:
    """一次管线运行的元统计。"""
    source: str = ""                     # 文件路径或 '<粘贴文本>'
    encoding: str = "utf-8"
    rule_name: str = "generic"
    total_lines: int = 0                 # 总行数
    # 优化缺陷R99：原始摄入字符数（Token 估算 + 压缩率展示的原始侧
    # 分母；流式逐行累计，不额外占内存）
    raw_chars: int = 0
    entry_lines: int = 0                 # 结构化条目数
    error_lines: int = 0                 # 错误级行数（FATAL/ERROR/FAIL）
    error_entries: int = 0               # 通过过滤并参与聚类的错误条目数
    # 优化缺陷R71：带时间戳的条目数（规则体检的时间戳识别率分子）
    ts_entries: int = 0
    level_counts: Dict[str, int] = field(default_factory=dict)
    # 因果线索被级别过滤掉的条目数（entry 里带 Caused-by 链但级别不是
    # ERROR/FAIL）。这个数不为 0 时，根因**就在被丢掉的那批行里** ——
    # 过滤近乎全灭的 notice 必须点破这一点，否则用户只知道「过滤很狠」，
    # 不知道「狠到把根因一起扔了」。
    causal_dropped: int = 0
    # 混合编码：按**另一种**编码才解出来的行数（这份文件混着两种编码）
    mixed_encoding_lines: int = 0
    # 解不出来的行数（多半不是文本日志）+ 含 NUL 字节的行数
    undecodable_lines: int = 0
    nul_lines: int = 0
    duration: float = 0.0                # 处理耗时（秒）
    lines_per_second: float = 0.0
    time_start: Optional[float] = None   # 日志时间范围
    time_end: Optional[float] = None
    truncated: bool = False              # 是否因取消中断
    # 优化缺陷R85：行数上限采样命中标记（与取消区分：非中断而是
    # 达上限收束 —— 状态栏显示「已达行数上限」而非「已取消」）
    limit_hit: bool = False
    analysis_cost: float = 0.0           # 智能分析耗时（秒，用于开销核算）

    def as_dict(self) -> dict:
        """导出 JSON 友好的字典。

        limit_hit 必须在这里：JSON 导出是给脚本二次处理的，脚本据此
        才能知道「这份结果只覆盖了前 N 行」。此前 JSON meta 里只有
        truncated（用户取消），行数上限采样完全不落盘 —— 一份 5 万行
        日志前 1000 行的报告与完整报告在机器眼里完全一样。
        """
        return {
            "source": self.source, "encoding": self.encoding,
            "rule": self.rule_name, "total_lines": self.total_lines,
            "raw_chars": self.raw_chars,
            "entry_lines": self.entry_lines, "error_lines": self.error_lines,
            "error_entries": self.error_entries,
            "ts_entries": self.ts_entries,
            "level_counts": dict(self.level_counts),
            # 混合编码 / 非文本：JSON 侧据此判断「这份输入解得干不干净」——
            # 一份二进制文件被当成日志分析过，机器侧必须能自己看出来。
            "mixed_encoding_lines": self.mixed_encoding_lines,
            "undecodable_lines": self.undecodable_lines,
            "nul_lines": self.nul_lines,
            # 因果线索被级别过滤掉的条数：JSON 侧据此判断「根因可能被扔了」，
            # 不落盘的话 Web/API 消费者就看不到这个诊断（缺陷#6 的同类：
            # 关键状态只存在于内存里，机器读不到）。
            "causal_dropped": self.causal_dropped,
            "duration_sec": round(self.duration, 3),
            "lines_per_second": round(self.lines_per_second, 1),
            "time_start": self.time_start, "time_end": self.time_end,
            "truncated": self.truncated,
            "limit_hit": self.limit_hit,
            "analysis_cost_sec": round(self.analysis_cost, 3),
        }


@dataclass
class AnalysisResult:
    """管线完整输出：错误簇（按优先级降序）+ 运行统计 + 全局错误直方图。"""

    stats: RunStats
    clusters: List[ErrorCluster] = field(default_factory=list)
    global_hist: TimeHistogram = field(
        default_factory=lambda: TimeHistogram(max_buckets=GLOBAL_HIST_MAX_BUCKETS)
    )
    keywords: List[str] = field(default_factory=list)  # 高亮关键字
    # 级别过滤几乎滤掉全部条目时的解释（pipeline._build_filter_notice 生成）。
    # 只解释、不改变行为：自动放宽过滤会让用户拿到他没要的东西。
    notice: Optional[str] = None
    # v2：证据充分性评估（analysis.assess_evidence 填充）。
    # 存 dict 而非 dataclass，避免 models ↔ analysis 循环 import；
    # 结构见 analysis.EvidenceAssessment.to_dict。
    evidence: Optional[Dict[str, Any]] = None
    # 重建出的调用链（core/traces.TraceChain）。只在结构化日志里存在
    # 关联键时非空 —— 纯文本日志没有关联键，这里就诚实地是空的。
    traces: List[Any] = field(default_factory=list)
    # 捕获到的部署/变更事件（本模块 ChangeEvent）。**必须在级别过滤
    # 之前捕获**（pipeline._handle_entry）—— 发版/重启行多为 INFO 级，
    # 等过滤后捕获会整条丢掉，"上线后开始报错"这类线索将不可见。
    change_events: List[Any] = field(default_factory=list)

    @property
    def top_clusters(self) -> List[ErrorCluster]:
        """默认 Top N（DEFAULT_TOP_N）簇。"""
        from log_ai_compressor.constants import DEFAULT_TOP_N
        return self.clusters[:DEFAULT_TOP_N]

    def clusters_of_module(self, module: str) -> List[ErrorCluster]:
        return [c for c in self.clusters if c.module == module]

    def clusters_of_level(self, level: str) -> List[ErrorCluster]:
        return [c for c in self.clusters if c.level == level]
