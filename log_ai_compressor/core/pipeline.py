# -*- coding: utf-8 -*-
"""流式处理管线：逐行读取 -> 解析 -> 过滤 -> 聚类 -> 智能分析 -> 结果。

性能与内存设计（核心架构约束）
------------------------------
1. **纯流式逐行读取**：文件以文本流迭代器逐行消费，内存占用仅与
   错误簇数量相关，与日志总行数无关（理论无行数上限）；
2. **典型样例上下文捕获**：
   - 前上下文：滚动窗口（带行号的 deque，容量 context+64）按行号
     区间截取，条目自身堆栈行不会混入前上下文；
   - 后上下文：对「新建簇 / 更换样例」的典型样例与每个含完整
     详情的实例开启待补队列，收集其后 context 行物理原始行，
     收满即关闭 —— 内存严格有界；
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
    CHANGE_EVENT_RE,
    CLUSTER_SIMILARITY_THRESHOLD,
    DEFAULT_SELECTED_LEVELS,
    ERROR_LEVELS,
    MAX_CAUSES_PER_CLUSTER,
    MAX_CHANGE_EVENTS,
    PROGRESS_EVERY_LINES,
    SIMILARITY_PRESETS,
)
from log_ai_compressor.core.analysis import (
    ANALYSIS_MODE_DEEP,
    _causal_lines,
    _has_caused_by,
    analyze_clusters,
)
from log_ai_compressor.core.causal import deepest_caused_by
from log_ai_compressor.core.clustering import ErrorClusterer
from log_ai_compressor.core.encoding import (
    LogDecoder,
    detect_encoding,
    is_compressed,
    open_text_stream,
    read_last_lines,
)
from log_ai_compressor.core.evidence import (
    KIND_CAUSED_BY,
    KIND_SAMPLE,
    make_ref,
)
from log_ai_compressor.core.filters import EntryFilter, FilterConfig
from log_ai_compressor.core.models import AnalysisResult, ChangeEvent, RunStats
from log_ai_compressor.core.parser import LogParser
from log_ai_compressor.rules.engine import (
    AUTO_RULE,
    RuleSet,
    detect_rule,
    load_ruleset,
    stratified_sample,
)

# 进度回调签名：cb({"lines", "lps", "clusters", "elapsed", "phase"})
ProgressCallback = Callable[[dict], None]


class ProcessingCancelled(Exception):
    """用户主动取消处理（run 仍会返回增量结果前抛出/或内部捕获）。"""


def _in_tod_window(tod: float, start: Optional[float],
                   end: Optional[float]) -> bool:
    """每日时段区间判定（秒），**支持跨零点**。

    修复：此前是两个独立的 `tod < start` / `tod > end` 早退，于是
    tod_start > tod_end 的区间（22:00~06:00 这种夜间窗口，日志分析的
    常规需求）会把一天里**所有**条目都判为区间外 —— 用户设了窗口却
    拿到一份空报告，且没有任何提示。现在 start > end 解释为跨零点。
    """
    lo = 0.0 if start is None else start
    hi = 86400.0 if end is None else end
    if lo <= hi:
        return lo <= tod <= hi
    return tod >= lo or tod <= hi          # 跨零点：22:00~06:00


def _range_refs(entry, recent: deque, kind: str, note: str,
                encoding: str = "") -> List:
    """把条目的行区间换算成可回读的原始行引用列表（可能为空）。

    抽出来是因为「样例原文」与「簇内某一内层因的第一处出现」要造的引用
    结构完全一样，只有 kind / note 不同。
    """
    start = entry.line_no
    end = entry.last_line_no or entry.line_no
    # 修复缺陷#2：轮转合并（run_files）按全局行号计数，而引用指向的是
    # 单个来源文件 —— 必须换算成**文件内**行号，否则第二个文件起的证据
    # 全部行号错位，verify 回读必然 OUT_OF_RANGE。条目跨文件边界（堆栈
    # 续进下一个轮转文件）时按（来源, 文件内连续行）拆成多条引用，
    # 每条都能单独回读复核；绝不给一条指向错误行号的引用。
    seg = [(src, fno, raw) for (no, fno, src, _text, raw) in recent
           if start <= no <= end]
    if len(seg) != end - start + 1:
        return []
    runs: List[list] = []       # [source, 文件内起行, 文件内止行, [原始行...]]
    for src, fno, raw in seg:
        if runs and runs[-1][0] == src and fno == runs[-1][2] + 1:
            runs[-1][2] = fno
            runs[-1][3].append(raw)
        else:
            runs.append([src, fno, fno, [raw]])
    return [make_ref(src or "<粘贴文本>", first, last, raws,
                     kind=kind, note=note, encoding=encoding)
            for src, first, last, raws in runs]


def _attach_evidence(cluster, entry, recent: deque, replace: bool = False,
                     encoding: str = "") -> None:
    """给簇挂上证据引用（典型样例所在的原文行区间）。

    为什么挂在聚类这一步而不是分析层：证据要的是**原始行**，
    而原始行只有摄入时还在窗口里 —— 分析层再来取就只剩剥过 ANSI 的
    展示文本，摘要对不上原文。

    行区间超出滚动窗口（超长堆栈）时**不**硬造证据：宁可少一条引用，
    也不能给一条验不过的引用 —— 一个会失配的证据引用比没有更糟。
    """
    if cluster.evidence and not replace:
        return
    refs = _range_refs(entry, recent, KIND_SAMPLE, "典型样例原文", encoding)
    if replace:
        # 样例被升级成带堆栈的版本时，证据必须跟着换 —— 引用指向的行
        # 和实际展示的样例对不上，比没有引用更容易误导人。
        cluster.evidence = refs
    else:
        cluster.evidence.extend(refs)


def _track_causes(cluster, entry, recent: deque, encoding: str = "") -> None:
    """累计本条目携带的 Caused-by 内层因（v2.7.0 修复「一簇只报一因」）。

    为什么必须在**摄入期**做，而不是分析层回头翻 `cluster.instances`：
    实例详情每簇只留前 200 条（MAX_CLUSTER_INSTANCES_DETAILED），大簇
    绝大多数实例是元数据（entry=None）—— 分析层能看到的只是一个有偏样本。
    真实分布（主因占比）是这个产品声称要给出的东西，不能建立在样本上。

    每种内层因只在**首次出现**时造一条原文引用：那一次原文还在滚动窗口里，
    之后就没了。造不出就留空 —— 缺一条引用可以接受，指错一条不行。
    """
    deepest = deepest_caused_by(_causal_lines(entry))
    if not deepest:
        return
    counts = cluster.cause_counts
    if deepest in counts:
        counts[deepest] += 1
        return
    if len(counts) >= MAX_CAUSES_PER_CLUSTER:
        cluster.cause_overflow += 1
        return
    counts[deepest] = 1
    refs = _range_refs(entry, recent, KIND_CAUSED_BY,
                       f"簇内内层因（第 1 次出现）：{deepest[:60]}", encoding)
    if refs:
        cluster.cause_evidence[deepest] = refs[0]


# 优化缺陷R90：ANSI 转义序列（Jenkins AnsiColor 等彩色控制符，
# 导出/投喂大模型时为乱码字符）—— 行摄入时剥除（含 ESC 才
# 启动正则，无 ANSI 的日志零额外开销）
_ANSI_ESCAPE_RE = None  # 延迟编译（模块导入零成本）


def _strip_ansi(line: str) -> str:
    global _ANSI_ESCAPE_RE
    if "\x1b" not in line:
        return line
    if _ANSI_ESCAPE_RE is None:
        import re as _re
        _ANSI_ESCAPE_RE = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    return _ANSI_ESCAPE_RE.sub("", line)


def _sample_compressed(path: Path, encoding: str,
                       total: int = 150) -> List[str]:
    """压缩包分层采样（优化缺陷R87：gzip/zip 无法按字节 seek 估算
    行偏移 —— 头部 50 + 流式跳读解压后 64KB/256KB 各取 50，
    开销与文件大小无关；日志格式通常整体统一，足敷规则识别）。
    """
    third = total // 3
    out: List[str] = []
    with open_text_stream(path, encoding) as stream:
        for line in stream:                     # 头段
            out.append(line.rstrip("\r\n"))
            if len(out) >= third:
                break
        for skip_bytes in (1 << 16, 1 << 18):   # 中/尾近似段
            skipped = 0
            for line in stream:
                skipped += len(line)
                if skipped >= skip_bytes:
                    break
            seg = 0                             # 每段取 third 行
            for line in stream:
                out.append(line.rstrip("\r\n"))
                seg += 1
                if seg >= third:
                    break
    return out


def _sample_log_file(path: Path, encoding: str,
                     total: int = 150) -> List[str]:
    """文件分层采样（头 50 + 中 50 + 尾 50，优化缺陷R71）。

    中部/尾部按文件大小估算行均长后 seek 读取（不整读大文件）；
    seek 落点可能截断多字节字符，首行残段直接丢弃，解码错误替换。
    小文件（估算行数 ≤total）退化为全量头部读取。
    优化缺陷R87：压缩包（gzip/zip）走流式跳读采样（无法 seek）。
    """
    if is_compressed(path):
        return _sample_compressed(path, encoding, total)
    head: List[str] = []
    with open_text_stream(path, encoding) as stream:
        for i, line in enumerate(stream):
            if i >= total // 3:
                break
            head.append(line.rstrip("\r\n"))
    try:
        size = path.stat().st_size
        avg = max(1, sum(len(l) + 1 for l in head) // max(1, len(head)))
        if size // avg <= total:
            return head
        for frac in (0.5, 1.0):
            offset = int(size * frac) - avg * (total // 3)
            with open(path, "rb") as fh:
                fh.seek(max(0, offset))
                data = fh.read(avg * (total // 3 + 4))
            seg = data.decode(encoding, errors="replace").splitlines()[1:]
            head.extend(seg[:total // 3])
    except (OSError, UnicodeError, LookupError):
        pass
    return head


@dataclass
class PipelineConfig:
    """管线配置：过滤参数 + 解析规则 + 分析开关。"""
    filter_config: FilterConfig = field(default_factory=FilterConfig.defaults)
    rule: Optional[str] = None          # 规则集名（generic/embedded/jenkins）或 YAML 路径
    analyze: bool = True                # 是否执行智能分析（根因/异常/优先级）
    # 优化缺陷R79：智能分析模式 full（完整，默认阈值）/ deep（深度扫描，
    # 降阈宁多报不漏报）/ fast（快速聚类，等效 analyze=False）
    analysis_mode: str = "full"
    # 优化缺陷R84：相似度阈值档位 strict（严格0.95）/ standard（标准
    # 0.85，默认）/ lenient（宽松0.70）—— 控制错误聚类合并激进度
    similarity: str = "standard"
    # 优化缺陷R85：时间范围过滤（第一前置项）—— epoch 精确区间
    # （time_start/time_end，秒）或每日时段区间（tod_start/tod_end，
    # 当日秒数）；None 表示该端不限；无时间戳条目始终保留
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    tod_start: Optional[float] = None
    tod_end: Optional[float] = None
    # 优化缺陷R85：行数上限采样（None 或 ≤0 = 全部；达上限收束并标记
    # stats.limit_hit，与取消中断区分）
    max_lines: Optional[int] = None
    # 优化缺陷R88：编码指定（None=自动探测；老系统日志自动探测失灵
    # 时手动纠正乱码，如 "utf-8" / "gb18030"）
    encoding: Optional[str] = None


# 过滤近乎全灭时提示用户（避免"点了没反应"的静默失败）
_NOTICE_MIN_ENTRIES = 50        # 小于此条目数不提示，避免样本噪声
# 修复缺陷#6：阈值 0.10 → 0.15 —— 实测 OpenStack 默认级别保留率 10.4%，
# 恰好高过旧阈值一丁点，于是那份"几乎空的报告"依旧拿不到任何解释。
# HealthApp 1.9% / Zookeeper 2.2% / OpenStack 10.4% 三个实测坏例全部要
# 被这个阈值兜住。
_NOTICE_MAX_KEEP_RATIO = 0.15   # 保留率低于 15% 即视为"过滤近乎全灭"
_NOTICE_TOP_LEVELS = 5


def _build_filter_notice(stats, clusters) -> Optional[str]:
    """级别过滤几乎滤掉全部条目时，给出一句可操作的解释。

    为什么要有这个：不少系统日志压根没有级别字段（Loghub HealthApp /
    OpenStack 实测），所有行被判为 INFO，于是默认的 ERROR/FAIL 过滤
    只剩 1.9% 的标注事件。用户指着日志点一下，拿到一份几乎空的报告
    且**没有任何解释** —— 这是最坏的失败形态：看不出是"没有错误"
    还是"工具坏了"。

    修复缺陷#6：**报告为空（0 簇）时同样必须解释** —— 此前 `not clusters`
    和 `kept <= 0` 都直接静默返回，恰恰把最坏情形（近空/全空报告）
    排除在解释之外。静默是这个函数要消灭的东西，不能自己制造静默。

    刻意**不自动放宽过滤**：用户要的是错误，静悄悄改成全量输出会给出
    他没要的东西，并且让压缩比之类的数字失去意义。这里只解释、不改行为。
    """
    total = stats.entry_lines
    if total < _NOTICE_MIN_ENTRIES:
        return None
    # 簇内实例数是"通过过滤的条目数"的准确值；但实例有全局上限
    # （MAX_TOTAL_INSTANCES），极端情况下可能为 0，此时退回 error_entries，
    # 免得"恰好没有可记录的实例"被误判成"过滤没生效"而静默。
    kept = sum(len(c.instances) for c in clusters) or stats.error_entries
    if kept / total > _NOTICE_MAX_KEEP_RATIO:
        return None
    levels = sorted(stats.level_counts.items(), key=lambda kv: -kv[1])
    shown = " / ".join(f"{lv} {cnt:,}" for lv, cnt in levels[:_NOTICE_TOP_LEVELS])
    more = " / …" if len(levels) > _NOTICE_TOP_LEVELS else ""
    pct = kept / total * 100
    causal = getattr(stats, "causal_dropped", 0)
    if causal:
        # 有因果链的行被级别过滤掉了 —— 这是最要紧的一句，必须排在最前面。
        head = (
            f"注意：被过滤掉的 {causal:,} 条里带 Caused-by 因果链，"
            f"你的根因很可能就在这些行中，而它们不在选中的级别里。"
        )
    else:
        head = ""
    return (
        f"{head}"
        f"当前级别过滤只保留了 {kept:,} / {total:,} 条（{pct:.1f}%），"
        f"其余条目未进入分析。这份日志识别到的级别分布：{shown}{more}。"
        f"若日志确实没有错误，这属于正常结果；"
        f"若怀疑是级别字段格式特殊导致的漏判，可用 --level INFO 查看全量摘要。"
    )


def _apply_decode_notes(result, stats, decoder) -> None:
    """把「这份文件解得有多干净」**如实**写进统计与提示。

    三种情况都要显式说出来（2026-10-21 第九轮外部复核抓到）：

    * **混合编码**：有 N 行不是主编码 —— 已逐行兜底**修好**，但要说出来；
    * **非文本**：有 N 行含 NUL 字节 —— 这多半不是日志文件，却会被当成
      日志分析一遍并**退出码 0**（实测二进制文件还写了「压缩 95%」）；
    * **空文件**：0 行 —— 要明说「这份文件是空的」，而不是「未发现明确根因」
      （空文件与「有日志但没定论」是两回事，混成一句话就是**报数为 0 与
      报不出数分不开**）。
    """
    stats.mixed_encoding_lines = decoder.repaired
    stats.undecodable_lines = decoder.undecodable
    stats.nul_lines = decoder.nul_lines
    notes = []
    if decoder.repaired:
        notes.append("有 %d 行不是主编码 %s，已逐行按实际编码解码"
                     % (decoder.repaired, decoder.encoding))
    if decoder.nul_lines:
        notes.append("有 %d 行含 NUL 字节 —— 这份文件可能不是文本日志"
                     % decoder.nul_lines)
    if decoder.undecodable:
        notes.append("有 %d 行按任何候选编码都解不出来"
                     "（已按替换符保留，没有丢行）" % decoder.undecodable)
    if not stats.total_lines:
        notes.append("这份文件是空的（0 行）")
    if notes:
        joined = "；".join(notes)
        result.notice = (result.notice + "；" if result.notice else "") + joined


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
        # 规则集：外部注入优先（复用已编译规则），否则按配置名加载；
        # 优化缺陷R71：rule=auto 时延迟到行源处自动识别后再加载
        self._auto_rule = ruleset is None and self._config.rule == AUTO_RULE
        if ruleset is not None:
            self._ruleset = ruleset
        elif self._auto_rule:
            self._ruleset = None
        else:
            self._ruleset = load_ruleset(self._config.rule)
        self._entry_filter = EntryFilter(self._config.filter_config)
        self._ctx = self._config.filter_config.context_lines

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------
    def run_file(self, path) -> AnalysisResult:
        """流式分析日志文件（编码自动探测或配置指定，优化缺陷R88）。"""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"日志文件不存在: {path}")
        encoding = self._config.encoding or detect_encoding(path)
        auto = self._resolve_ruleset(_sample_log_file(path, encoding))
        stats = RunStats(source=str(path), encoding=encoding,
                         rule_name=self._ruleset.name + ("（自动）" if auto else ""))
        decoder = LogDecoder(encoding)
        result = self._process_lines(decoder.iter_lines(path), stats)
        _apply_decode_notes(result, stats, decoder)
        return result

    def run_files(self, paths) -> AnalysisResult:
        """轮转文件合并分析（优化缺陷R89：多文件按序串流，跨轮转
        因果链不断 —— 簇/根因/时间线基于全局时间戳，与文件边界
        无关；调用方负责排序（最旧在前，见 GUI 轮转序号排序））。
        """
        paths = [Path(p) for p in paths]
        for p in paths:
            if not p.is_file():
                raise FileNotFoundError(f"日志文件不存在: {p}")
        # 编码：取首个文件（同系列轮转文件编码一致；指定编码时全用指定值）
        encoding = self._config.encoding or detect_encoding(paths[0])
        # 规则识别采样：取首个文件（同系列格式一致）
        auto = self._resolve_ruleset(_sample_log_file(paths[0], encoding))
        stats = RunStats(
            source=" ␞ ".join(p.name for p in paths) if len(paths) > 1
            else str(paths[0]),
            encoding=encoding,
            rule_name=self._ruleset.name + ("（自动）" if auto else ""))
        # 每行带上来源文件名：轮转合并后「第 42 行」在几个文件里都有，
        # 证据引用必须精确到文件才复核得了（core/evidence.py）

        decoder = LogDecoder(encoding)

        def chained():
            # 共用同一个 decoder 实例 ⇒ 混合编码 / 非文本的计数跨文件累加。
            # BOM 是**逐行**剥离的（不依赖「这是第几行」），所以轮转文件
            # 各自的首行都能正确处理。
            for p in paths:
                for line in decoder.iter_lines(p):
                    yield line, str(p)

        result = self._process_lines(chained(), stats)
        _apply_decode_notes(result, stats, decoder)
        return result

    def run_text(self, text: str, source: str = "<粘贴文本>") -> AnalysisResult:
        """分析粘贴的日志文本。"""
        lines = text.splitlines()
        auto = self._resolve_ruleset(stratified_sample(lines))
        stats = RunStats(source=source, encoding="utf-8",
                         rule_name=self._ruleset.name + ("（自动）" if auto else ""))
        return self._process_lines(lines, stats)

    def run_tail(self, path, tail_lines: int) -> AnalysisResult:
        """只分析文件的**最后 tail_lines 行**（行号仍指向原文件）。

        为什么要它：值班时最常问的是「**刚才**发生了什么」，而日志
        可能有几十 GB —— 全量分析一遍是几十秒到几分钟，等不起。

        关键约束一：行号必须**指向原文件**。尾部窗口的第一行在原文件里
        可能是第 999 万行，报告里写「第 1 行」就等于指错了地方 ——
        证据引用会指向无关的内容，「可复核」当场失效。

        关键约束二：规则识别必须用**整个文件**的采样，不能用尾部窗口。
        尾部 5 行长一个样，规则识别会选错（日志格式千差万别，5 行根本
        不足以认出格式）—— 实测：尾部采样选中 embedded 规则，
        「ERROR line 96」这种通用格式**一条都识别不出来**，
        报告里显示「错误 0 行」。规则识别用全文件采样（与 run_file 一致），
        只有**分析范围**是尾部。
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"日志文件不存在: {path}")
        encoding = self._config.encoding or detect_encoding(path)
        lines, start, total, decoder = read_last_lines(path, tail_lines, encoding)
        # 规则识别用**全文件**的分层采样（与 run_file 同一条路径）
        auto = self._resolve_ruleset(_sample_log_file(path, encoding))
        stats = RunStats(source=str(path), encoding=encoding,
                         rule_name=self._ruleset.name + ("（自动）" if auto else ""))
        result = self._process_lines(lines, stats, line_offset=start)
        # 混合编码 / 非文本的记账也要有（尾部窗口同样可能落在混编文件里）
        _apply_decode_notes(result, stats, decoder)
        # 如实说明「这次只看的是尾部」：报告覆盖的不是全量，用户必须知道。
        note = (f"只看最后 {len(lines)} 行（文件共 {total:,} 行，"
                f"起始行 {start:,}）")
        result.notice = (result.notice + "；" if result.notice else "") + note
        # 注意：**不改** stats.total_lines —— 它的口径是「本次处理了多少行」，
        # 改了会让按它算的压缩率、速率全部失真（拿窗口行数当文件行数）。
        # 文件总行数由 notice 说明（并可从 start + 窗口行数算出）。
        return result

    def _resolve_ruleset(self, sample: List[str]) -> bool:
        """auto 模式：按分层采样打分选规则并加载（返回是否自动识别）。

        优化缺陷R71：rule_name 追加「（自动）」标记，状态栏/报告可见
        实际生效的规则；手动指定规则时直接返回 False（原行为不变）。
        """
        if not self._auto_rule:
            return False
        self._ruleset = load_ruleset(detect_rule(sample))
        return True

    # ------------------------------------------------------------------
    # 核心流式处理
    # ------------------------------------------------------------------
    def _process_lines(self, lines: Iterable[str], stats: RunStats,
                       line_offset: int = 0) -> AnalysisResult:
        """处理日志行。

        `line_offset` = 这批行在**原文件**里的起始行号（1-based）。
        为什么要它：`--tail N` 只喂最后 N 行进来，但那 N 行在原文件里
        是第 9,999,991~10,000,000 行 —— 行号不偏移，证据引用就指向
        错误的行，「可复核」这个核心承诺当场失效。
        """
        t0 = time.perf_counter()
        parser = LogParser(self._ruleset)
        # 优化缺陷R84：相似度档位映射阈值（未知键回退标准，配置兼容）
        clusterer = ErrorClusterer(
            similarity_threshold=SIMILARITY_PRESETS.get(
                self._config.similarity, CLUSTER_SIMILARITY_THRESHOLD))

        from log_ai_compressor.core.models import TimeHistogram
        from log_ai_compressor.constants import GLOBAL_HIST_MAX_BUCKETS
        global_hist = TimeHistogram(max_buckets=GLOBAL_HIST_MAX_BUCKETS)

        # 前上下文滚动窗口：(行号, 处理后文本, 原始文本)，容量
        # context+64 保证常规堆栈长度下仍能取到条目之前的上下文。
        # 保留原始文本是因为证据摘要必须基于**文件原始字节**（证据要能
        # 被 verify_refs 重读原文复核）；用剥过 ANSI 的文本算摘要，
        # 自校验会永远失配。
        window = self._ctx + 64
        recent: deque = deque(maxlen=window)
        # 后上下文待补队列：id(样例/实例) -> {"target", "start_line", "lines"}
        pending: Dict[int, dict] = {}

        # line_no = 这批行在**原文件**里的行号（尾部窗口时从 line_offset
        # 起算）—— 条目与证据的行号都要指向原文件。
        # processed = 本次处理了多少行（与原文件行号无关，用于进度、
        # 行数上限、速率统计）。
        line_no = line_offset
        processed = 0
        cancelled = False
        limit_hit = False     # 优化缺陷R85：行数上限采样命中标记
        cur_source = str(stats.source)
        # 部署/变更事件收集（级别过滤前捕获，见 _handle_entry）
        change_events: List[ChangeEvent] = []
        # 修复缺陷#2：每个来源文件独立计行号（证据引用要指向文件内行号；
        # 全局 line_no 只服务窗口/待补/进度等内部逻辑）。按来源名计数而非
        # 切换即清零，是为了同一文件在合并序列里出现多次/交错时也不串号。
        src_line_no: Dict[str, int] = {}
        if line_offset > 0:
            # 尾部窗口：文件内行号从 line_offset 起算（证据要指回原文件）
            src_line_no[cur_source] = line_offset - 1

        def notify(phase: str) -> None:
            if self._progress_cb:
                elapsed = time.perf_counter() - t0
                self._progress_cb({
                    "lines": processed,
                    "lps": processed / elapsed if elapsed > 0 else 0.0,
                    "clusters": len(clusterer),
                    "elapsed": elapsed,
                    "phase": phase,
                })

        try:
            for item in lines:
                if isinstance(item, tuple):
                    raw_line, cur_source = item
                else:
                    raw_line = item
                line_no += 1
                processed += 1
                file_line_no = src_line_no.get(cur_source, 0) + 1
                src_line_no[cur_source] = file_line_no
                # 优化缺陷R99：原始字符数逐行累计（Token 压缩率原始侧）
                stats.raw_chars += len(raw_line)
                stripped = raw_line.rstrip("\r\n")
                line = _strip_ansi(stripped)

                # 1) 新条目开始 -> 处理上一个完整条目
                # `file_line_no` = 该行在**这个来源文件**里的行号 ——
                # 报告里给人看的行号与证据引用都要用它（`line_no` 是全局
                # 计数，只服务滚动窗口/待补/进度那些内部逻辑）。
                # 两者不等时（`--tail` 的尾部窗口、轮转合并）用错的那套，
                # 报告里的行号就会指向错的地方（2026-10-22 实测：
                # `--tail 10` 的簇区间报成 92~101，而窗口首行是 91）。
                completed = parser.feed(line, line_no, file_line_no)
                if completed is not None:
                    completed.source_file = cur_source
                    self._handle_entry(completed, stats, clusterer,
                                       global_hist, recent, pending,
                                       change_events)

                # 2) 后上下文补录（仅对开启待补的样例）
                if pending:
                    for item_ in list(pending.values()):
                        if (line_no > item_["start_line"]
                                and len(item_["lines"]) < self._ctx):
                            item_["lines"].append(line)
                            if len(item_["lines"]) >= self._ctx:
                                self._close_pending(item_, pending)

                # 3) 前上下文滚动窗口（no=全局行号，fno=文件内行号，
                #    src=来源 —— 证据引用按后者换算，修复缺陷#2）
                recent.append((line_no, file_line_no, cur_source,
                               line, stripped))

                # 4) 取消轮询（独立间隔，开销极低）
                if (self._cancel_event is not None
                        and processed % CANCEL_CHECK_EVERY_LINES == 0
                        and self._cancel_event.is_set()):
                    cancelled = True
                    break

                # 4.5) 行数上限采样（优化缺陷R85）：达上限即收束 ——
                # 标记 limit_hit（与取消中断区分，非用户取消）
                # （max_lines ≤ 0 视为不限，与 service 层「0 = 全部」
                # 的口径一致；此前 0 会让第一行就命中上限，只读 1 行）
                # 用 processed 而不是 line_no：后者带尾部窗口的偏移，
                # 会让「上限 1000 行」在第 1000 行**原文件行号**处命中，
                # 于是实际只处理了 1000-offset 行。
                if (self._config.max_lines is not None
                        and self._config.max_lines > 0
                        and processed >= self._config.max_lines):
                    limit_hit = True
                    break

                # 5) 进度上报（按行数间隔）
                if processed % PROGRESS_EVERY_LINES == 0:
                    notify("parsing")

            # 流结束：产出最后一个条目
            last = parser.flush()
            if last is not None:
                last.source_file = cur_source
                self._handle_entry(last, stats, clusterer, global_hist,
                                   recent, pending, change_events)
        except ProcessingCancelled:
            cancelled = True

        # 关闭所有待补样例的后上下文
        for item in list(pending.values()):
            self._close_pending(item, pending)

        # total_lines = 本次**处理**了多少行。
        # 不能直接用 line_no：尾部窗口（--tail）时 line_no 从 line_offset
        # 起算，直接赋值会得到「偏移 + 窗口行数」这种既不是文件行数
        # 也不是处理行数的怪数字。
        stats.total_lines = processed
        stats.truncated = cancelled
        stats.limit_hit = limit_hit       # 优化缺陷R85：上限采样命中
        stats.duration = time.perf_counter() - t0
        stats.lines_per_second = (processed / stats.duration
                                  if stats.duration > 0 else 0.0)

        result = AnalysisResult(
            stats=stats,
            clusters=clusterer.clusters,
            global_hist=global_hist,
            keywords=self._config.filter_config.normalized_include(),
            notice=_build_filter_notice(stats, clusterer.clusters),
            change_events=change_events,
        )
        if self._config.analyze and result.clusters:
            # 优化缺陷R79：深度扫描模式降阈执行（完整模式默认阈值）
            if self._config.analysis_mode == "deep":
                analyze_clusters(result, **ANALYSIS_MODE_DEEP)
            else:
                analyze_clusters(result)
        notify("done" if not cancelled else "cancelled")
        return result

    # ------------------------------------------------------------------
    # 条目处理
    # ------------------------------------------------------------------
    def _handle_entry(self, entry, stats: RunStats, clusterer: ErrorClusterer,
                      global_hist, recent: deque, pending: Dict[int, dict],
                      change_events: Optional[List[ChangeEvent]] = None) -> None:
        """统计 + 过滤 + 聚类；必要时开启样例后上下文待补。"""
        # 优化缺陷R85：时间范围过滤（第一前置项，先于一切统计）——
        # epoch 精确区间 / 每日时段区间；无时间戳条目无法定位，
        # 始终保留（宁留勿丢，避免 CI 日志大量无戳行被误清）
        ts = entry.timestamp
        if ts is not None:
            if (self._config.time_start is not None
                    and ts < self._config.time_start):
                return
            if (self._config.time_end is not None
                    and ts > self._config.time_end):
                return
            if (self._config.tod_start is not None
                    or self._config.tod_end is not None):
                # **用 gmtime 而非 localtime**（2026-10-22 第十轮修复）：
                # 每日时段窗口必须与时间戳的**解释口径**一致 ——
                # 裸时间戳按 UTC 解释（见 core/parser.py::_to_epoch），
                # 这里就按 UTC 取「一天里的第几秒」。
                # 此前用 localtime，于是同一条 `--tod-start` 参数
                # 在 UTC 机器与 UTC+8 机器上选出**不同的条目** ——
                # 这与 tests/test_perf_gate.py 早就钉住的
                # 「负载不得调用 localtime」是同一个洞的两半。
                lt = time.gmtime(ts)
                tod = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
                if not _in_tod_window(tod, self._config.tod_start,
                                      self._config.tod_end):
                    return
        stats.entry_lines += 1
        stats.level_counts[entry.level] = stats.level_counts.get(entry.level, 0) + 1

        # 日志时间范围（所有带时间戳条目，不限级别）
        if entry.timestamp is not None:
            stats.ts_entries += 1     # 优化缺陷R71：时间戳识别率分子
            if stats.time_start is None or entry.timestamp < stats.time_start:
                stats.time_start = entry.timestamp
            if stats.time_end is None or entry.timestamp > stats.time_end:
                stats.time_end = entry.timestamp

        if entry.level in ERROR_LEVELS:
            stats.error_lines += 1

        # 跨服务/时间窗/部署事件关联：变更事件捕获**必须在级别过滤
        # 之前** —— 发版/重启行多为 INFO 级，默认「只看 ERROR/FAIL」
        # 会把它们整条丢掉，"上线后开始报错"这类时间窗关联线索将不可
        # 见。此处只捕获、不改过滤行为（用户要的是错误，不混入报告）。
        #
        # **但 ERROR/FAIL 级的行一律不算变更事件。** CHANGE_EVENT_RE 的注释
        # 写着「宁漏勿误 —— 把普通错误行当成发版会给出误导性的时间窗叙事」，
        # 而它自己刚犯了这个错：macOS 系统日志里
        #   `netbiosd[31128]: network_reachability_changed : network is not
        #    reachable, netbiosd is shutting down`
        # 整句命中 `shutting down`。那不是停机，是网络不可达导致 netbiosd
        # 拆连接 —— 而且它**本身就是被当成错误报告的那一行**。于是报告里
        # 出现了 406 条「错误首发在〈另一条错误〉后 41s」，还冠以「变更关联」
        # 的标题：既不是变更，也不是新信息，只是把同一条错误换个 PID 复述一遍。
        #
        # 判据：**错误行不是变更事件。** 真正的发版/重启/上线行是 INFO/notice 级；
        # 代价是漏掉「服务崩溃也算停机」这类确有排查价值的信号 —— 按本正则
        # 自己声明的「宁漏勿误」，这个取舍是对的。
        if change_events is not None and len(change_events) < MAX_CHANGE_EVENTS:
            msg = entry.message or entry.raw or ""
            if entry.level not in ERROR_LEVELS and CHANGE_EVENT_RE.search(msg):
                change_events.append(ChangeEvent(
                    ts=entry.timestamp,
                    module=entry.module or "",
                    text=msg[:200],
                    line_no=entry.line_no,
                    source=entry.source_file or "",
                ))

        # 修复缺陷R40：聚类准入由过滤器决定 —— 级别复选框选中
        # WARN/INFO/DEBUG 时同样聚类展示（五级别五色五档）；
        # error_lines/error_entries 统计口径不变（仍只计错误类级别）
        if not self._entry_filter.match(entry):
            # 被级别过滤掉、但**带着因果链**的条目：单独计数。
            # 这是「过滤近乎全灭」notice 里最有价值的一句话的依据 ——
            # 告诉用户根因可能就在他刚丢掉的那批行里，比泛泛说
            # 「若怀疑格式特殊可用 INFO」具体得多。
            # 只在 ERROR/FAIL 之外才可能丢根因，故先判级别省掉正则开销。
            if entry.level not in ERROR_LEVELS and _has_caused_by(
                    _causal_lines(entry)):
                stats.causal_dropped += 1
            return
        if entry.level in ERROR_LEVELS:
            stats.error_entries += 1

        # 前上下文快照：按行号区间截取（排除条目自身的堆栈/折行）
        before: Optional[List[str]] = None
        if self._ctx > 0:
            before = [text for (no, _fno, _src, text, _raw) in recent
                      if entry.line_no - self._ctx <= no < entry.line_no]

        cluster, replaced, instance = clusterer.add(entry, before)
        # 证据引用：把「这个簇是存在的」绑定到日志原文的具体行区间。
        # 摘要取自文件原始文本（recent 里的原始行元），这样 verify 能重读
        # 原文核对 —— 这是结论可被独立复核的前提。
        _attach_evidence(cluster, entry, recent, replace=replaced,
                         encoding=stats.encoding or "")
        # 簇内 Caused-by 内层因的流式累计（v2.7.0 修复「一簇只报一因」）：
        # 与样例证据同一层造，因为原始行同样只在摄入窗口里。
        _track_causes(cluster, entry, recent, encoding=stats.encoding or "")
        if replaced and self._ctx > 0:
            # 新建簇或样例升级 -> 开启样例后上下文待补
            pending[id(cluster.sample)] = {
                "target": cluster.sample,
                "start_line": entry.last_line_no or entry.line_no,
                "lines": [],
            }
        # 修复缺陷R44：详情实例同样开启后上下文待补（此前仅典型
        # 样例收集，点击实例时详情面板无后上下文可显示）；
        # 元数据实例（entry=None，超详情上限）不收集，内存有界
        if (instance is not None and instance.entry is not None
                and self._ctx > 0):
            pending[id(instance)] = {
                "target": instance,
                "start_line": entry.last_line_no or entry.line_no,
                "lines": [],
            }

        # 全局错误时间直方图（过滤后的错误，与结果口径一致）
        if entry.timestamp is not None:
            global_hist.add(entry.timestamp)

    def _close_pending(self, item: dict, pending: Dict[int, dict]) -> None:
        """收束待补目标：写入 after 上下文并移除队列。

        修复缺陷R44：目标泛化为典型样例或详情实例（两者均有
        after 字段），键为 id(target) —— 同簇的样例与多个实例
        可同时待补，互不覆盖。
        """
        target = item["target"]
        target.after = list(item["lines"])
        pending.pop(id(target), None)


# ---------------------------------------------------------------------------
# 便捷 API（GUI / CLI / 测试共用）
# ---------------------------------------------------------------------------
def _build_config(levels, include, exclude, top_n, context_lines, rule,
                  analyze, analysis_mode="full", similarity="standard",
                  time_start=None, time_end=None, tod_start=None,
                  tod_end=None, max_lines=None,
                  encoding=None, use_regex=False) -> PipelineConfig:
    """构造管线配置。

    **所有调用方必须用关键字传参**（下面已全部改成名参）。
    原因：这里有 16 个位置参数，历史上已经因为少传一个 None 而让
    `use_regex` 落到 `tod_end` 上 —— 关键词被当正则解释，报告里的
    错误行数悄悄变成 0，而**没有任何报错**。位置参数在这种长度下
    本身就是缺陷源，靠调用者数参数个数不可能可靠。
    """
    cfg = FilterConfig(
        levels=list(levels) if levels else list(DEFAULT_SELECTED_LEVELS),
        include=list(include or []),
        exclude=list(exclude or []),
        top_n=top_n if top_n else FilterConfig.defaults().top_n,
        context_lines=context_lines if context_lines is not None
        else FilterConfig.defaults().context_lines,
        # 优化缺陷R91：关键词按正则解释（透传 GUI 开关）
        use_regex=use_regex,
    )
    # 优化缺陷R79：fast 快速聚类等效关闭智能分析
    return PipelineConfig(filter_config=cfg, rule=rule,
                          analyze=analyze and analysis_mode != "fast",
                          analysis_mode=analysis_mode,
                          similarity=similarity,
                          time_start=time_start, time_end=time_end,
                          tod_start=tod_start, tod_end=tod_end,
                          max_lines=max_lines, encoding=encoding)


def analyze_file(path, *, levels=None, include=None, exclude=None,
                 top_n=None, context_lines=None, rule=None, analyze=True,
                 analysis_mode: str = "full",
                 similarity: str = "standard",
                 time_start=None, time_end=None, tod_start=None,
                 tod_end=None, max_lines=None, encoding=None,
                 use_regex: bool = False,
                 progress_cb: Optional[ProgressCallback] = None,
                 cancel_event: Optional[Event] = None) -> AnalysisResult:
    """分析单个日志文件（详见 LogPipeline.run_file）。"""
    pipeline = LogPipeline(_build_config(
        levels=levels, include=include, exclude=exclude, top_n=top_n,
        context_lines=context_lines, rule=rule, analyze=analyze,
        analysis_mode=analysis_mode, similarity=similarity,
        time_start=time_start, time_end=time_end, tod_start=tod_start,
        tod_end=tod_end, max_lines=max_lines, encoding=encoding,
        use_regex=use_regex),
        progress_cb=progress_cb, cancel_event=cancel_event)
    return pipeline.run_file(path)


def analyze_files(paths, *, levels=None, include=None, exclude=None,
                  top_n=None, context_lines=None, rule=None, analyze=True,
                  analysis_mode: str = "full",
                  similarity: str = "standard",
                  time_start=None, time_end=None, tod_start=None,
                  tod_end=None, max_lines=None, encoding=None,
                  use_regex: bool = False,
                  progress_cb: Optional[ProgressCallback] = None,
                  cancel_event: Optional[Event] = None) -> AnalysisResult:
    """轮转文件合并分析（优化缺陷R89，详见 LogPipeline.run_files）。

    调用方负责 paths 排序（最旧在前）；单文件时等效 analyze_file。
    """
    pipeline = LogPipeline(_build_config(
        levels=levels, include=include, exclude=exclude, top_n=top_n,
        context_lines=context_lines, rule=rule, analyze=analyze,
        analysis_mode=analysis_mode, similarity=similarity,
        time_start=time_start, time_end=time_end, tod_start=tod_start,
        tod_end=tod_end, max_lines=max_lines, encoding=encoding,
        use_regex=use_regex),
        progress_cb=progress_cb, cancel_event=cancel_event)
    return pipeline.run_files(paths)


def analyze_tail(path, tail_lines: int, *, levels=None, include=None,
                 exclude=None, top_n=None, context_lines=None, rule=None,
                 analyze=True, analysis_mode: str = "full",
                 similarity: str = "standard", encoding=None,
                 use_regex: bool = False, progress_cb=None,
                 cancel_event=None) -> AnalysisResult:
    """只分析文件最后 tail_lines 行（详见 LogPipeline.run_tail）。"""
    # 全部用**关键字**传参：_build_config 有 16 个位置参数，历史上
    # 因为少传一个 None 而让 use_regex 落到 tod_end 上 —— 关键词被当
    # 正则解释，报告里的错误行数悄悄变成 0，而**没有任何报错**。
    pipeline = LogPipeline(_build_config(
        levels=levels, include=include, exclude=exclude, top_n=top_n,
        context_lines=context_lines, rule=rule, analyze=analyze,
        analysis_mode=analysis_mode, similarity=similarity,
        encoding=encoding, use_regex=use_regex),
        progress_cb=progress_cb, cancel_event=cancel_event)
    return pipeline.run_tail(path, tail_lines)


def analyze_text(text: str, *, source: str = "<粘贴文本>", levels=None,
                 include=None, exclude=None, top_n=None, context_lines=None,
                 rule=None, analyze=True, analysis_mode: str = "full",
                 similarity: str = "standard",
                 time_start=None, time_end=None, tod_start=None,
                 tod_end=None, max_lines=None, use_regex: bool = False,
                 progress_cb: Optional[ProgressCallback] = None,
                 cancel_event: Optional[Event] = None) -> AnalysisResult:
    """分析粘贴文本（详见 LogPipeline.run_text）。"""
    pipeline = LogPipeline(_build_config(
        levels=levels, include=include, exclude=exclude, top_n=top_n,
        context_lines=context_lines, rule=rule, analyze=analyze,
        analysis_mode=analysis_mode, similarity=similarity,
        time_start=time_start, time_end=time_end, tod_start=tod_start,
        tod_end=tod_end, max_lines=max_lines, use_regex=use_regex),
        progress_cb=progress_cb, cancel_event=cancel_event)
    return pipeline.run_text(text, source=source)
