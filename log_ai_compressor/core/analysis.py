# -*- coding: utf-8 -*-
"""智能辅助分析：错误因果关联、统计异常检测、优先级排序、堆栈精简降噪。

算法设计
--------
1. **根因判定（因果图）**——保守强证据建边 + 加权评分：
   a. Caused-by 链：带 "Caused by:" 堆栈的错误指向其紧邻的前置错误；
   b. 消息互引用：模板词高包含且含稀有词的后发错误（优化缺陷R76）；
   c. 时间连锁：突发时间窗口内最先出现且含根因特征关键词的错误；
   图源头（出度>0 入度=0）判根因；关键词按 IDF 加权（罕见词证据
   权重大，优化缺陷R76）；入度>0 或含被动失败关键词的簇标记为
   疑似连锁衍生。
2. **统计异常检测（优化缺陷R75 强化）**：
   - 集中爆发（burst）双通道：全局错误直方图中超过 均值+3σ 的桶，
     簇峰值时间落入爆发区间；或簇自持基线（与自身中位数+MAD 比）
     峰值超限 —— 全局平稳但单簇陡增亦可抓；
   - 周期发作（periodic）：实例间隔变异系数 ≤0.10（定时任务指纹）；
   - 新型错误（novel）：罕见且与既有簇模板 Jaccard <0.5；
   - 罕见异常（rare）：总数可观而仅出现 1 次的老错误变体。
3. **优先级综合评分**：级别权重 40% + 频次（对数归一）30% +
   根因 20% + 异常 10%；五级别分档钳制（ERROR 保 P0 / FAIL 钳
   P1 / WARN 钳 P2 / INFO 钳 P3 / DEBUG 封顶 P4，修复缺陷R40）。
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
# 优化缺陷R75：异常检测强化参数（自持基线爆发 / 周期发作 / 新型错误）
OWN_BASELINE_MIN_BUCKETS = 3   # 自持基线：簇内直方图最少桶数
OWN_BASELINE_MIN_PEAK = 5      # 自持基线：峰值桶最小计数（防小样本虚报）
PERIODIC_MIN_SAMPLES = 4       # 周期发作：最少时间戳样本数
PERIODIC_MAX_CV = 0.10         # 周期发作：间隔变异系数上限（越小越规律）
NOVEL_JACCARD_MAX = 0.5        # 新型错误：与既有簇模板相似度上限

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
        _compute_priorities(clusters, result.stats)
        _sort_clusters(clusters)
    result.stats.analysis_cost = time.perf_counter() - t0
    return result.stats.analysis_cost


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------
def _mark_anomalies(clusters: List[ErrorCluster], global_hist, stats: RunStats) -> None:
    """集中爆发 / 周期发作 / 新型错误 / 罕见异常 标注（优先级递降）。

    优化缺陷R75：异常检测强化 ——
    - burst 双通道：全局爆发窗口命中【或】簇自持基线爆发（与自身
      历史比，全局平稳但单簇陡增不再漏报）；
    - 新增 periodic：实例间隔变异系数 ≤0.10（定时任务/心跳失败的
      指纹，此前完全不可见）；
    - 新增 novel：罕见且与既有所有簇模板 Jaccard <0.5（从没见过
      的错，比"老错误的偶发尾巴"含金量高）。
    """
    bursts = global_hist.burst_buckets(k=BURST_SIGMA)
    burst_ranges = [(t, t + global_hist.width) for t, _ in bursts]
    total = stats.error_entries
    token_sets = {id(c): _tokens(c.message_template or c.summary)
                  for c in clusters}

    for c in clusters:
        peak_t: Optional[float] = None
        if c.hist.total:
            series = c.hist.series()
            peak_t = max(series, key=lambda x: x[1])[0]
        global_burst = (peak_t is not None
                        and any(a <= peak_t < b for a, b in burst_ranges))
        if global_burst or _own_baseline_burst(c):
            c.anomaly = "burst"
        elif _is_periodic(c):
            c.anomaly = "periodic"
        elif total >= RARE_MIN_TOTAL and c.count <= 1:
            # 罕见再细分：与既有簇不相似 = 新型错误，相似 = 普通罕见
            c.anomaly = ("novel" if _is_novel(c, clusters, token_sets)
                         else "rare")


def _own_baseline_burst(c: ErrorCluster) -> bool:
    """簇自持基线爆发：峰值桶超过自身 中位数+3×MAD 且 ≥2 倍基线。

    优化缺陷R75：与全局检测互补 —— 全局直方图被大量其他错误稀释
    时，单簇自身从 2 次/桶陡增到 20 次/桶 的局部爆发照样可抓。
    MAD=0（基线完全平稳）时阈值退化为 2×中位数 + 峰值下限。
    """
    import statistics

    series = c.hist.series()
    if len(series) < OWN_BASELINE_MIN_BUCKETS:
        return False
    counts = [cnt for _, cnt in series]
    peak = max(counts)
    if peak < OWN_BASELINE_MIN_PEAK:
        return False
    median = statistics.median(counts)
    mad = statistics.median(abs(x - median) for x in counts)
    threshold = max(median + 3.0 * 1.4826 * mad, 2.0 * median)
    return peak > threshold


def _is_periodic(c: ErrorCluster) -> bool:
    """周期发作：实例间隔变异系数（标准差/均值）≤ 阈值。

    优化缺陷R75：定时任务/心跳/看门狗失败的指纹特征 —— 每隔固定
    间隔准时报错；间隔样本取实例时间戳（内存有界：详实实例 +
    元数据实例均带时间戳，与出现总次数无关）。
    """
    ts = sorted(i.timestamp for i in c.instances
                if i.timestamp is not None)
    if len(ts) < PERIODIC_MIN_SAMPLES:
        return False
    deltas = [b - a for a, b in zip(ts, ts[1:]) if b - a > 1e-3]
    if len(deltas) < PERIODIC_MIN_SAMPLES - 1:
        return False
    mean = sum(deltas) / len(deltas)
    var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    return math.sqrt(var) / mean <= PERIODIC_MAX_CV


_TOKEN_RE = re.compile(r"[a-zA-Z_]{3,}|[一-鿿]{2,}")


def _tokens(text: str) -> set:
    """模板词集（Jaccard 相似度用；英文/数字词 ≥3 字符，中文 ≥2 字）。"""
    return set(_TOKEN_RE.findall((text or "").lower()))


def _is_novel(c: ErrorCluster, clusters: List[ErrorCluster],
              token_sets: dict) -> bool:
    """新型错误：与既有所有簇的模板词集 Jaccard 相似度 < 上限。

    优化缺陷R75：Datadog Content Anomaly 同款思路（内容级相异而
    非量级异常）；仅对罕见簇调用（数量有界，O(罕见簇×总簇)）。
    """
    mine = token_sets.get(id(c)) or set()
    if not mine:
        return False
    for o in clusters:
        if o is c:
            continue
        other = token_sets.get(id(o)) or set()
        if not other:
            continue
        jaccard = len(mine & other) / max(1, len(mine | other))
        if jaccard >= NOVEL_JACCARD_MAX:
            return False
    return True


# ---------------------------------------------------------------------------
# 根因判定
# ---------------------------------------------------------------------------
def _keyword_weights(clusters: List[ErrorCluster]) -> dict:
    """根因/连锁关键词 IDF 权重表（log2 归一）。

    优化缺陷R76：关键词不再一人一票 —— 罕见词（如 deadlock 只在一
    个簇出现）证据权重高于常见词（如 timeout 遍布各簇）：
    w = log2(1 + C/df)，单簇单命中权重恰为 1.0（与旧计数制兼容，
    强关键词阈值 3 语义不变），簇越多、命中越稀有关键词权重越大。
    """
    n = max(1, len(clusters))
    all_kw = tuple(ROOT_CAUSE_KEYWORDS) + tuple(CASCADE_KEYWORDS)
    df = {kw: 0 for kw in all_kw}
    for c in clusters:
        text = f"{c.summary} {c.template}".lower()
        for kw in all_kw:
            if kw in text:
                df[kw] += 1
    # df=0（全语料未出现）权重置 0 —— 该词不参与任何簇的评分
    return {kw: (math.log2(1.0 + n / cnt) if cnt else 0.0)
            for kw, cnt in df.items()}


def _keyword_score(cluster: ErrorCluster, weights: dict) -> float:
    """根因关键词加权得分（根因词权重和 - 连锁词权重和）。"""
    text = f"{cluster.summary} {cluster.template}".lower()
    score = sum(weights.get(kw, 1.0) for kw in ROOT_CAUSE_KEYWORDS
                if kw in text)
    penalty = sum(weights.get(kw, 1.0) for kw in CASCADE_KEYWORDS
                  if kw in text)
    return score - penalty


def _has_cascade_keyword(cluster: ErrorCluster) -> bool:
    text = f"{cluster.summary} {cluster.template}".lower()
    return any(kw in text for kw in CASCADE_KEYWORDS)


def _cluster_sort_key(c: ErrorCluster) -> Tuple[int, float]:
    """排序键：有时间戳者按时间，无时间戳者按行号。"""
    if c.first_seen is not None:
        return (0, c.first_seen)
    return (1, float(c.first_line))


def _add_cross_reference_edges(ordered: List[ErrorCluster],
                               add_edge) -> None:
    """消息互引用边：A 的模板词 60% 出现在后发的 B 中且含稀有词 → A→B。

    优化缺陷R76：衍生错误常在消息中复述上游签名（"auth failed"
    衍生出 "request aborted because auth failed"）；稀有词（簇间
    文档频率 ≤2）防 "error/failed" 等泛词造成的全连接假边。
    O(C²)，C 为簇数（有界），仅做集合运算，开销可忽略。
    """
    token_sets = {id(c): _tokens(c.message_template or c.summary)
                  for c in ordered}
    df: dict = {}
    for ts in token_sets.values():
        for t in ts:
            df[t] = df.get(t, 0) + 1
    for i, a in enumerate(ordered):
        ta = token_sets[id(a)]
        if len(ta) < 2:
            continue
        for b in ordered[i + 1:]:
            tb = token_sets[id(b)]
            if not tb:
                continue
            inter = ta & tb
            if (len(inter) / len(ta) >= 0.6
                    and any(df.get(t, 0) <= 2 for t in inter)):
                add_edge(id(a), id(b))


def _mark_root_causes(clusters: List[ErrorCluster]) -> None:
    """因果图根因判定（优化缺陷R76：从关键词投票升级为因果 DAG）。

    建边（均为保守强证据，宁缺毋错）：
    a. Caused-by 链：带 "Caused by:" 堆栈的错误 → 其紧邻前置错误；
    b. 消息互引用：模板词高包含 + 稀有词的后发错误；
    判定：
    1. 图源头（出度>0 且入度=0）→ 根因（Caused-by 源优先注明）；
    2. 时间连锁：60s 窗口首发 + IDF 加权根因分 >0 → 根因；
    3. 强关键词：加权分 ≥ 阈值 → 根因；
    4. 入度>0 或含连锁关键词 → 疑似连锁衍生（修上游，别修它）。
    """
    ordered = sorted(clusters, key=_cluster_sort_key)
    weights = _keyword_weights(clusters)
    by_id = {id(c): c for c in ordered}
    out_edges: dict = {}
    in_edges: dict = {}
    caused_by_src: set = set()

    def _add(src: int, dst: int) -> None:
        if src == dst:
            return
        out_edges.setdefault(src, set()).add(dst)
        in_edges.setdefault(dst, set()).add(src)

    # a) Caused-by 因果链
    for c in ordered:
        stack = c.sample.entry.stack if c.sample else []
        if any(_CAUSED_BY_RE.match(line) for line in stack):
            prior = _nearest_prior(ordered, c)
            if prior is not None:
                _add(id(prior), id(c))
                caused_by_src.add(id(prior))

    # b) 消息互引用边
    _add_cross_reference_edges(ordered, _add)

    # 1) 图源头 → 根因
    for c in ordered:
        outs = out_edges.get(id(c), set())
        if outs and not in_edges.get(id(c)):
            c.is_root_cause = True
            if id(c) in caused_by_src:
                c.root_cause_reason = "被 Caused-by 因果链指向"
            else:
                c.root_cause_reason = (
                    f"因果链源头（{len(outs)} 个错误由其衍生）")

    # 2) 时间连锁：突发窗口内首发 + 加权根因分 >0
    windows = {}
    for c in ordered:
        if c.first_seen is None:
            continue
        key = int(c.first_seen // BURST_WINDOW_SEC)
        windows.setdefault(key, []).append(c)
    for group in windows.values():
        earliest = group[0]  # ordered 已按时间排序，组内首个即窗口内首发
        if (not earliest.is_root_cause
                and _keyword_score(earliest, weights) > 0):
            earliest.is_root_cause = True
            earliest.root_cause_reason = (
                "时间连锁源头（窗口内首发且含根因特征）")

    # 3) 强关键词 / 4) 连锁衍生标记
    for c in ordered:
        if (not c.is_root_cause
                and _keyword_score(c, weights) >= STRONG_KEYWORD_SCORE):
            c.is_root_cause = True
            c.root_cause_reason = "高频根因特征关键词"
        elif not c.is_root_cause and not c.root_cause_reason:
            ins = in_edges.get(id(c))
            if ins:
                src = by_id[next(iter(ins))]
                c.root_cause_reason = (
                    f"疑似连锁衍生（上游：{src.summary[:40]}）")
            elif _has_cascade_keyword(c):
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
# 修复缺陷R40：五级别优先级分档表（下界, 上界）—— 级别决定档位
# 区间（P0 错误 / P1 失败 / P2 警告 / P3 信息 / P4 调试），频次/
# 根因/异常加分只在档内拉开差距，不再跨档（原纯公式下 FAIL 高频
# 可冲 P0、INFO 低频掉 P4，级别与档位脱钩）
_LEVEL_BANDS = {
    "ERROR": (80.0, None),     # 保底 P0（≥75）
    "FAIL": (55.0, 75.0),      # 钳 P1
    "WARN": (35.0, 55.0),      # 钳 P2
    "INFO": (15.0, 35.0),      # 钳 P3
    "DEBUG": (None, 15.0),     # 封顶 P4
    "TRACE": (None, 15.0),     # 封顶 P4
}


# 优化缺陷R77：持续性/新生度判定参数
ONGOING_WINDOW_SEC = 60.0      # 末见距日志末尾 ≤60s 视为「持续发生」
NEW_PATTERN_TAIL_RATIO = 0.75  # 首现于日志后 25% 时段视为「新生模式」


def _compute_priorities(clusters: List[ErrorCluster],
                        stats: RunStats) -> None:
    """优先级综合评分（优化缺陷R77：加持续性/新生度，评分构成落库）。

    权重：级别 35% + 频次（对数归一）25% + 根因 20% + 异常 10% +
    持续 5%（末见贴日志末尾，此刻还在炸）+ 新生 5%（后段才出现的
    新模式）；每项贡献与档位钳制说明写入 c.priority_detail，
    供详情面板展示"这分是怎么算的"。
    """
    max_count = max((c.count for c in clusters), default=0)
    denom = math.log10(max_count + 1) if max_count > 1 else 1.0
    duration: Optional[float] = None
    if stats.time_start is not None and stats.time_end is not None:
        duration = stats.time_end - stats.time_start
    for c in clusters:
        level_w = LEVEL_WEIGHT.get(c.level, 0.5)
        freq = math.log10(c.count + 1) / denom if max_count > 1 else 1.0
        root = 1.0 if c.is_root_cause else 0.0
        anomaly = 1.0 if c.anomaly else 0.0
        ongoing = 0.0
        if (stats.time_end is not None and c.last_seen is not None
                and 0 <= stats.time_end - c.last_seen
                <= ONGOING_WINDOW_SEC):
            ongoing = 1.0
        new_pat = 0.0
        if (duration and duration > 0 and c.first_seen is not None
                and c.first_seen >= stats.time_start
                + NEW_PATTERN_TAIL_RATIO * duration):
            new_pat = 1.0
        parts = (("级别", 35.0 * level_w), ("频次", 25.0 * freq),
                 ("根因", 20.0 * root), ("异常", 10.0 * anomaly),
                 ("持续", 5.0 * ongoing), ("新生", 5.0 * new_pat))
        score = sum(v for _, v in parts)
        # 修复缺陷R40：按级别分档钳制（ERROR 保底 80 确保 P0 前置；
        # 原 FATAL 强制 90 随 FATAL 删除移除）
        lo, hi = _LEVEL_BANDS.get(c.level, (None, None))
        clamp_note = ""
        if lo is not None and score < lo:
            score = lo
            clamp_note = f"（{c.level} 档保底 {lo:.0f}）"
        elif hi is not None and score >= hi:
            score = hi - 0.1
            clamp_note = f"（{c.level} 档封顶 {hi - 0.1:.0f}）"
        c.priority = round(score, 1)
        segs = [f"{k}{v:.0f}" for k, v in parts if v > 0.05]
        c.priority_detail = "+".join(segs) + clamp_note


def _sort_clusters(clusters: List[ErrorCluster]) -> None:
    """排序：ERROR 置顶 -> 优先级降序 -> 次数降序（修复缺陷R40）。"""
    clusters.sort(
        key=lambda c: (c.level == "ERROR", c.priority, c.count),
        reverse=True,
    )
