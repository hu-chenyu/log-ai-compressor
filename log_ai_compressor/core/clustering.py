# -*- coding: utf-8 -*-
"""错误智能聚类去重：模糊指纹 + 编辑距离相似度。

算法设计（两级匹配，兼顾准确性与性能）
------------------------------------
1. **指纹归一化（mask）**：将消息与堆栈中的可变部分替换为占位符——
   十六进制地址→0xH、UUID→U、引号字符串→S、路径→P、数字→N。
   行号/参数/地址差异被抹平后，同类错误模板完全一致，走 O(1) 字典精确命中
   （主路径，覆盖绝大多数重复错误）；
2. **相似度回退**：精确未命中时，在**整个同级别桶**内与既有模板做
   difflib 编辑距离比值（≥ 阈值判同簇），并将新模板注册进精确字典，
   后续变体可继续 O(1) 命中；桶长超过比较上限时按模板字典序确定性截断
   （**不是「最近 N 个」**，那会让早期簇永远比不到，见 `_find_similar`）；
3. **典型样例**：每簇仅保留 1 份完整样例；首份样例若缺少堆栈而后续
   出现带堆栈的同类错误，则替换为带堆栈版本（更利于定位根因）。

内存控制：簇内存放「模板 + 计数 + 样例 + 有界直方图」，
与错误出现次数无关，与日志总行数无关。
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from log_ai_compressor.constants import (
    CLUSTER_SIMILARITY_THRESHOLD,
    MAX_CLUSTER_INSTANCES_DETAILED,
    MAX_CLUSTER_INSTANCES_META,
    MAX_SIMILARITY_COMPARE,
    MAX_TOTAL_INSTANCES,
)
from log_ai_compressor.core.models import (
    ClusterInstance,
    ClusterSample,
    ErrorCluster,
    LogEntry,
)

# ---------------------------------------------------------------------------
# 指纹归一化（顺序敏感：UUID -> 十六进制令牌 -> 0x地址 -> 引号串 -> 路径 -> 数字）
# ---------------------------------------------------------------------------
_MASK_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# 无 0x 前缀的十六进制令牌（会话 ID / 请求 ID / 短哈希）：
# 要求 6~16 位、**至少含一位数字**且**至少含一个十六进制字母**。
#
# 「至少一个字母」是 v2.9.9 加的。原来只要求含数字，于是**纯数字串**
# （10 位用户 ID、13 位会话号）被判成 H，而 _VAR_KIND_NAMES 把 H 标成
# 「标识符」—— 数字 ID 被报成标识符，变量分布面板的语义就是错的。
# 纯数字交给 _MASK_NUM 当「数值」，含字母的才是十六进制令牌。
# 「至少一个数字」那条让 deadbeef（纯字母英文色）不被误伤。
_MASK_HEX_TOKEN = re.compile(
    r"\b(?=[0-9a-fA-F]{6,16}\b)(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])"
    r"[0-9a-fA-F]{6,16}\b"
)
_MASK_HEX = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_MASK_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# 路径识别。**两段以上、且末段带扩展名**，或以路径根起头，才算路径。
#
# 原来是 `[\w.\-]+[\\/]` —— **任何含斜杠的词都算路径**，于是日志里极常见的
# `read/write` 与 `input/output` 被掩成**完全相同**的模板（实测
# similarity = 1.000），两类本质不同的事件被并成一簇。`TCP/IP`、`and/or`
# 同理。
#
# 现在的判据是「**看起来像文件**：末段带扩展名」，于是：
#   src/db/pool.c    两段 + 末段有 .c   -> 掩（保住原有行为）
#   read/write       两段 + 末段无扩展名 -> 留
#   input/output     两段 + 末段无扩展名 -> 留
#   TCP/IP           两段 + 末段无扩展名 -> 留
#   /var/log/x       以 / 起头          -> 掩
#   ./conf/a.yaml    以 ./ 起头         -> 掩
#
# 漏掩方向是「多几个簇」，误掩方向是「并错事件」—— 按本项目一贯的
# 「宁漏勿误」，这个不对称是对的。
_MASK_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]"
    r"|(?<![\w.\-])(?:\.{1,2})?[\\/][\w.\-/\\]*"
    r"|(?<![\w.\-])[\w\-]+(?:[\\/][\w\-]+)*[\\/][\w\-]+\.[A-Za-z0-9]{1,8}"
    r"(?![\w.\-]))"
)
_MASK_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS = re.compile(r"\s+")

# 指纹参与长度上限（超长消息截断，避免拼接与比较开销失控）
_FINGERPRINT_MAX_LEN = 200


def mask_text(text: str, max_len: int = _FINGERPRINT_MAX_LEN) -> str:
    """归一化文本：抹平可变参数，得到错误指纹模板。"""
    if len(text) > max_len:
        text = text[:max_len]
    text = _MASK_UUID.sub("U", text)
    text = _MASK_HEX_TOKEN.sub("H", text)
    text = _MASK_HEX.sub("0xH", text)
    text = _MASK_QUOTED.sub("S", text)
    text = _MASK_PATH.sub("P", text)
    text = _MASK_NUM.sub("N", text)
    return _WS.sub(" ", text).strip()


def message_template(entry: LogEntry) -> str:
    """消息指纹模板（不含级别与堆栈，用于跨堆栈差异的同类合并）。"""
    return mask_text(entry.full_message)


def fingerprint(entry: LogEntry) -> str:
    """构建条目指纹：级别 + 核心报错信息 + 堆栈前 3 行特征。"""
    parts = [entry.level, mask_text(entry.full_message)]
    if entry.stack:
        parts.extend(mask_text(frame, 120) for frame in entry.stack[:3])
    return " | ".join(p for p in parts if p)


def similarity(a: str, b: str) -> float:
    """编辑距离相似度比值（0~1）。"""
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# 字面词元守卫：编辑距离对「长样板 + 短区分词」失明 —— **实测代价大于收益，本版不做**
# ---------------------------------------------------------------------------
# 诊断成立。Loghub HPC 默认档只覆盖 63 / 74 种（85.1%），并簇实例：
#
#   E57  N node-N action start N N boot (command N)      41 字符
#   E49  N node-N action start N N risBoot (command N)   44 字符
#   ratio = 2 x 41 / (41 + 44) = 0.965  >= 0.85  ->  判为同一类
#
# 38 个字符的样板前缀把 3 个字符的区分词淹没了，字符级相似度看不见那 3 个
# 字符恰恰是唯一的区别。这是真的漏判。
#
# **但直接加「字面词元集合相同才准合并」这条守卫，代价远大于收益**（v2.9.8 实测）：
#
#   数据集      守卫前(簇/交付口径)   守卫后(簇/交付口径)
#   HPC          76 / 23.0%        →  2690 / 8.1%
#   Proxifier   117 / 81.8%        →  1834 / 45.5%
#   Zookeeper    83 / 22.5%        →   107 / 22.5%
#
# 覆盖率的账面收益是实的（HPC 同预算 85.1% → 93.2%），但**产品交付口径
# 崩掉一半**：用户拿到手的那 20 个簇从「23% 的事件」变成「8% 的事件」，
# 报告从 76 行变成 2,690 行 —— 那不是更好的工具，是坏掉的工具。**已撤回。**
#
# ---------------------------------------------------------------------------
# v2.10.0：把「按数据集自适应粒度」这条路走到底，结论是**它不可实现**
# ---------------------------------------------------------------------------
# v2.9.8 的结论写的是「一条全局规则做不到两件事都对，需要按数据集自适应粒度」。
# v2.10.0 真的去试了，方法是**不猜阈值，而是先量**：把 `_find_similar` 的每一次
# 合并决定都记下来，用 Loghub 的人工标注判定「这次合并对不对」，再去找能分开
# 两类的特征。HPC + Proxifier 共 4,142 次决定，量到：
#
#                        次数   字符相似度(中位)   词元 Jaccard(中位)
#   HPC    该合并        2193       0.923              0.750
#   HPC    不该合并        67       0.899              0.429
#   Prox.  该合并        1390       0.899              0.846
#   Prox.  不该合并       492       0.899              0.750
#
# **两个特征都完全重叠，没有任何一个相似度量能把两类分开。**
#
#   · 字符相似度：HPC「不该合并」最高 0.971，「该合并」最低 0.851 —— 交叠。
#   · 词元 Jaccard：HPC「该合并」最低 0.273，「不该合并」中位 0.429 —— 连
#     「该合并」里相似度最低的那些，都比「不该合并」里的一半还低。
#   · Proxifier 有 Jaccard = **1.000**（字面词元集合完全相同）却被 Loghub 判为
#     不同事件的合并：E6/E7/E8/E9 的区别只是「哪一个 bytes 字段带 (N KB)」。
#
# 也就是说：**Loghub 的事件定义不是模板相似度的函数**，任何「提高/降低阈值」
# 或「加一道词元闸门」的做法都不可能同时对两个数据集成立 —— 它不是调参问题，
# 是判据本身不存在。
#
# 顺带**更正 v2.9.8 注释里的一处错误结论**：原文写「主机名本身在某些数据集里
# 就是事件身份，补掩码等于把不同事件合并，方向就是错的」。这是错的 ——
# Loghub 对 Proxifier 的标注里，主机名与进程名都被写成 `<*>`：
#
#   E9  <*> close, <*> bytes sent, <*> bytes received, lifetime <*>
#
# 主机**不是**身份。所以「补主机名掩码」这条路的失败原因是**过度掩码**
#（第一版把 `chrome.exe` 也当主机名），而不是方向反了。这条更正重要，
# 因为它把「补掩码」从「已被证伪」重新放回「值得再试」的候选里。
#
# 还有一件事必须说清楚，否则下一个人会得出错误的结论：
# **「按 Loghub 判对错」与「对用户好不好」不是同一个问题。**
# Proxifier 1882 次合并里有 492 次按 Loghub 标准是错的，而它的同预算覆盖率
# 仍是 100%。HPC 的问题不只是「合错了」，还包括 79 个簇里只有 60 个不同事件
# 身份 —— 拆开会同时改善两者吗？没有证据。**所以本版不假装做了。**
#
# 要真的攻下它，需要的是**不是相似度**的东西：可学习的消息表示、事件抽取的
# 弱监督、或按数据集配置的粒度参数（而最后这个本质上是拿评测集调参，
# 本项目的纪律明令禁止）。在那之前，诚实的做法是把缺口量化写在这里，
# 而不是改一个阈值然后宣称修好了。
#
# 保留 `literal_tokens()` 与 `tests/test_cluster_token_guard.py`：它们是这个
# 诊断的**可执行证据**，「为什么不做」比「悄悄跳过」更值得留在仓库里。
_PLACEHOLDER_TOK_RE = re.compile(r"\b0xH\b|\b[USHPN]\b")


def literal_tokens(template: str) -> frozenset:
    """模板里未被掩码的字面词元集合（占位符 U/H/S/P/N 剔除）。

    只看**词**不看字符：`boot` 与 `risBoot` 在字符层面只差 3 个字符，
    在词层面却是两个不同的词 —— 而后者才是「是不是同一件事」的判据。
    """
    return frozenset(_PLACEHOLDER_TOK_RE.sub(" ", template).split())


# ---------------------------------------------------------------------------
# 优化缺陷R101：变量分布分析（模板槽位取值 Top K，详情面板【变量分布】）
# ---------------------------------------------------------------------------
# 占位符均为独立令牌（掩码整体替换词元），\b 边界防止误切字面单词
# 中的同名大写字母（如 "SEVERE" 里的 S 不是占位符）
_PLACEHOLDER_RE = re.compile(r"\b0xH\b|\b[USHPN]\b")
_VAR_KIND_NAMES = {"U": "标识符", "H": "标识符", "0xH": "地址",
                   "S": "字符串", "P": "路径", "N": "数值"}


def extract_variable_distribution(
        template: str, messages: List[str],
        max_slots: int = 4, top_k: int = 3
) -> Tuple[int, List[Tuple[str, List[Tuple[str, int]]]]]:
    """从消息模板 + 实例消息中提取各变量槽位的取值分布 Top K。

    原理：模板字面量转义为正则锚点，占位符变捕获组；实例消息
    逐条匹配，捕获值即变量实际取值，Counter 统计频次。

    参数：
        template: 簇的 message_template（含 U/H/0xH/S/P/N 占位符）；
        messages: 实例消息列表（可为截断摘要，匹配失败自动跳过）；
        max_slots: 最多展示的槽位数（防模板变量过多刷屏）；
        top_k: 每槽位最多展示的取值数。
    返回：
        (匹配成功的消息条数, [(槽位名, [(取值, 次数), ...]), ...])；
        无占位符或无匹配时返回 (0, [])。
    """
    if not template or not messages:
        return 0, []
    literals = _PLACEHOLDER_RE.split(template)
    holders = _PLACEHOLDER_RE.findall(template)
    if not holders:
        return 0, []
    # 模板被 200 字符截断时尾部字面量可能不完整 —— 不做尾部锚定，
    # 只要求从头匹配（摘要 160 截断 / 省略号结尾均可对齐）
    pattern = "".join(
        re.escape(lit) + ("(.+?)" if i < len(holders) else "")
        for i, lit in enumerate(literals))
    try:
        rx = re.compile(pattern)
    except re.error:
        return 0, []
    counters: List[Counter] = [Counter() for _ in holders]
    matched = 0
    for msg in messages[:200]:                # 上限保护（实例有界，双保险）
        m = rx.match((msg or "").strip())
        if not m:
            continue
        matched += 1
        for gi, val in enumerate(m.groups()):
            v = val.strip()
            if v:
                counters[gi][v] += 1
    if not matched:
        return 0, []
    slots: List[Tuple[str, List[Tuple[str, int]]]] = []
    kind_seen: Dict[str, int] = {}
    for idx, counter in enumerate(counters):
        if len(slots) >= max_slots:
            break
        if not counter:
            continue
        kind = _VAR_KIND_NAMES.get(holders[idx], "变量")
        kind_seen[kind] = kind_seen.get(kind, 0) + 1
        name = kind if kind_seen[kind] == 1 else f"{kind}#{kind_seen[kind]}"
        slots.append((name, counter.most_common(top_k)))
    return matched, slots


# ---------------------------------------------------------------------------
# 聚类器
# ---------------------------------------------------------------------------
class ErrorClusterer:
    """错误聚类器：流式 add()，逐条聚合。"""

    def __init__(self,
                 similarity_threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
                 max_compare: int = MAX_SIMILARITY_COMPARE):
        self._threshold = similarity_threshold
        self._max_compare = max_compare
        self._exact: Dict[str, ErrorCluster] = {}       # 完整指纹 -> 簇（精确命中表）
        self._msg_index: Dict[Tuple[str, str], ErrorCluster] = {}  # (级别,消息模板) -> 簇
        self._by_level: Dict[str, List[ErrorCluster]] = {}  # 级别桶（相似度回退）
        self._clusters: List[ErrorCluster] = []
        self._next_id = 0
        # 修复缺陷R4：全局实例记录计数（内存有界）
        self._instance_total = 0

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def add(self, entry: LogEntry,
            before_lines: Optional[List[str]] = None
            ) -> Tuple[ErrorCluster, bool, Optional[ClusterInstance]]:
        """错误条目入簇。

        参数：
            entry: 通过过滤的错误条目
            before_lines: 该条目出现时刻的前上下文快照（管线维护，用于样例）

        返回：
            (簇, 是否新建簇或更换了样例, 本次记录的实例) —— 更换样例时
            管线需要重新捕获后上下文；实例非 None 且含完整条目时管线
            为其捕获后上下文（修复缺陷R44）；实例未记录（超上限）时为
            None。

        三级匹配策略：
        1. 完整指纹（级别+消息+堆栈前3行）精确命中；
        2. (级别, 消息模板) 精确命中 —— 同消息、堆栈有无/截断差异的合并
           （错误首次出现无堆栈、后续带完整堆栈的典型场景）；
        3. 消息模板编辑距离相似度回退（限同级别桶、设比较上限）。

        **第 3 级对样板雷同、用词不同的消息会误合并**（已诊断、本版不修，
        证据见 `literal_tokens` 上方注释与 tests/test_cluster_token_guard.py）：
        38 个字符的样板前缀能把 3 个字符的区分词淹没，字符相似度看不见后者。
        """
        fp = fingerprint(entry)
        cluster = self._exact.get(fp)
        if cluster is not None:
            replaced, inst = self._update(cluster, entry, before_lines)
            return cluster, replaced, inst

        msg_tp = message_template(entry)
        cluster = self._msg_index.get((entry.level, msg_tp))
        if cluster is not None:
            self._exact[fp] = cluster
            replaced, inst = self._update(cluster, entry, before_lines)
            return cluster, replaced, inst

        cluster = self._find_similar(msg_tp, entry.level)
        if cluster is not None:
            self._exact[fp] = cluster           # 变体注册进精确表，后续 O(1)
            self._msg_index[(entry.level, msg_tp)] = cluster
            replaced, inst = self._update(cluster, entry, before_lines)
            return cluster, replaced, inst

        cluster, inst = self._create(fp, msg_tp, entry, before_lines)
        return cluster, True, inst

    @property
    def clusters(self) -> List[ErrorCluster]:
        """簇列表（创建顺序）。"""
        return self._clusters

    def __len__(self) -> int:
        return len(self._clusters)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _find_similar(self, msg_tp: str, level: str) -> Optional[ErrorCluster]:
        """在**整个**级别桶内对消息模板做编辑距离相似度匹配。

        这里曾经写 ``bucket[-self._max_compare:]``，也就是只比级别桶里最近的
        256 个簇。于是「A、B 两条相似消息之间隔了多少行」变成了决定它们合不
        合并的变量：

            序列 [300 个填充, A, B]   -> A/B 合并     （A 还在窗口里）
            序列 [A, 300 个填充, B]   -> A/B 不合并   （A 已被挤出窗口）

        两串输入是**同一多重集合**，只换了排列，分区却不同。而
        ``result_fingerprint`` 的承诺是「同输入必须逐字节相同」。
        258 是个性能上限，本该只管「比多少个」，不该顺带决定「谁没被比过」。

        **上限已经整个去掉，候选集就是整个桶**（实测见下面的数据）。
        曾经试过「超上限时按模板字典序取前 N 个」以保留一个上限，结果**更糟**：
        字典序前 256 是固定的 256 个模板，滑动窗口换成它之后，
        Loghub Mac 的 INFO 桶从 604 个簇涨到 1604 个 —— 字母靠后的模板永远
        找不到同类，各自成簇。日志 Mac 的同预算覆盖率从 85.8% 掉到 **64.4%**。
        **任何「固定取前 N 个」的截断都是系统性偏袒，只是换个方向偏。**

        代价用**精确的长度上界剪枝**补回来：

            ratio(a, b) <= 2 * min(|a|,|b|) / (|a| + |b|)

        这是 difflib 的数学上界（匹配上的字符不可能超过较短串的全文，
        而分母是两边长度之和）。上界达不到阈值，就必然达不到阈值 ——
        连 ``SequenceMatcher`` 都不用建，两次长度运算就否掉。

        同分也要按内容定胜负：``score >= best_score`` 就覆盖会让胜出者取决于
        桶内的到达顺序，同分这一种情况仍会漏出行序依赖。这里改成同分取模板
        字典序小的那个 —— 模板在**同一级别桶内唯一**（`_msg_index`` 以
        `(level, msg_tp)` 为键，同模板不会再建第二个簇），所以它是内容派生的
        全序，与到达顺序无关。

        实测（Loghub 10 个数据集，同预算 32k 主指标）：

            候选集策略                 Mac INFO 桶  全量簇  同预算覆盖  分析耗时
            最近 256 个（旧，有偏袒）        604     766     85.8%     23.5s
            模板字典序前 256（更糟）        1604    1766     64.4%     25.3s
            整桶（现在，无偏袒）            547     709     90.4%     32.7s

        去掉偏袒之后覆盖率不降反涨 4.6pp —— 「窗口」当初省下的不是必需的算力，
        而是用结果正确性换来的。
        """
        bucket = self._by_level.get(level)
        if not bucket or self._max_compare <= 0:
            return None
        best: Optional[ErrorCluster] = None
        best_score = self._threshold
        n = len(msg_tp)
        for cluster in bucket:
            other = cluster.message_template
            m = len(other)
            # 精确长度上界剪枝（见 docstring）。best_score 单调不减，
            # 所以用当前值当门槛只会越剪越紧，不会漏掉任何能胜出的候选。
            shorter = m if m < n else n
            if 2 * shorter / (n + m) < best_score:
                continue
            # quick_ratio 是 ratio 的廉价上界，先行剪枝。
            matcher = SequenceMatcher(None, msg_tp, other)
            if matcher.quick_ratio() < best_score:
                continue
            score = matcher.ratio()
            if score < best_score:
                continue
            if (best is None or score > best_score
                    or other < best.message_template):
                best, best_score = cluster, score
        return best

    def _create(self, fp: str, msg_tp: str, entry: LogEntry,
                before_lines: Optional[List[str]]
                ) -> Tuple[ErrorCluster, Optional[ClusterInstance]]:
        cluster = ErrorCluster(
            cluster_id=self._next_id,
            template=fp,
            message_template=msg_tp,
            level=entry.level,
            module=entry.module,
            summary=self._summarize(entry),
            count=0,
            first_line=entry.line_no,
            last_line=entry.last_line_no or entry.line_no,
            first_seen=entry.timestamp,
            last_seen=entry.timestamp,
            sample=ClusterSample(entry=entry, before=list(before_lines or [])),
        )
        self._next_id += 1
        self._clusters.append(cluster)
        self._exact[fp] = cluster
        self._msg_index[(entry.level, msg_tp)] = cluster
        self._by_level.setdefault(entry.level, []).append(cluster)
        _, inst = self._update(cluster, entry, before_lines)
        return cluster, inst

    def _update(self, cluster: ErrorCluster, entry: LogEntry,
                before_lines: Optional[List[str]]
                ) -> Tuple[bool, Optional[ClusterInstance]]:
        """更新簇计数/时间/样例；返回 (是否更换了样例, 本次记录的实例)。"""
        cluster.count += 1
        cluster.last_line = entry.last_line_no or entry.line_no
        # 修复：无时间戳的条目**不得**抹掉簇已有的时间信息。此前无条件
        # 赋值 last_seen = entry.timestamp，混合日志（部分行带戳、部分行
        # 不带）里只要最后一条不带戳，整个簇的 last_seen 就变 None ——
        # 报告的「首末时间」变空、优先级里的「持续发生」判定随之失效。
        if entry.timestamp is not None:
            cluster.last_seen = entry.timestamp
            cluster.hist.add(entry.timestamp)
        # 修复缺陷R4：记录实例（前 N 个含完整条目+上下文，其余仅元数据）
        inst = self._record_instance(cluster, entry, before_lines)
        replaced = False
        # 样例升级：已存样例无堆栈而新条目带堆栈 -> 替换（更利于根因定位）
        if entry.has_stack and not (cluster.sample and cluster.sample.entry.has_stack):
            cluster.sample = ClusterSample(
                entry=entry, before=list(before_lines or []))
            replaced = True
        if not cluster.module and entry.module:
            cluster.module = entry.module
        return replaced, inst

    def _record_instance(self, cluster: ErrorCluster, entry: LogEntry,
                         before_lines: Optional[List[str]]
                         ) -> Optional[ClusterInstance]:
        """记录簇内单个实例（修复缺陷R4，全屏簇展开数据源）。

        返回新建的实例（未记录时返回 None，供管线决定是否为其
        捕获后上下文 —— 修复缺陷R44）。

        三层上限（内存有界）：
        1. 全局总数超限 -> 不再记录（instances_truncated 标记）；
        2. 每簇超 MAX_CLUSTER_INSTANCES_META -> 不再记录；
        3. 每簇超 MAX_CLUSTER_INSTANCES_DETAILED -> 仅记元数据
           （entry/before 为空，详情面板退化为摘要视图）。
        """
        if self._instance_total >= MAX_TOTAL_INSTANCES:
            cluster.instances_truncated = True
            return None
        insts = cluster.instances
        if len(insts) >= MAX_CLUSTER_INSTANCES_META:
            cluster.instances_truncated = True
            return None
        self._instance_total += 1
        detailed = len(insts) < MAX_CLUSTER_INSTANCES_DETAILED
        inst = ClusterInstance(
            timestamp=entry.timestamp,
            line_no=entry.line_no,
            last_line_no=entry.last_line_no or entry.line_no,
            summary=self._summarize(entry),
            entry=entry if detailed else None,
            before=list(before_lines or []) if detailed else [],
        )
        insts.append(inst)
        return inst

    @staticmethod
    def _summarize(entry: LogEntry) -> str:
        """展示用错误摘要（完整消息，截断 160 字符）。"""
        text = entry.full_message or (entry.stack[0] if entry.stack else "")
        return text[:160]
