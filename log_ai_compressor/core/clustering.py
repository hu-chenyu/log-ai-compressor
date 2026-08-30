# -*- coding: utf-8 -*-
"""错误智能聚类去重：模糊指纹 + 编辑距离相似度。

算法设计（两级匹配，兼顾准确性与性能）
------------------------------------
1. **指纹归一化（mask）**：将消息与堆栈中的可变部分替换为占位符——
   十六进制地址→0xH、UUID→U、引号字符串→S、路径→P、数字→N。
   行号/参数/地址差异被抹平后，同类错误模板完全一致，走 O(1) 字典精确命中
   （主路径，覆盖绝大多数重复错误）；
2. **相似度回退**：精确未命中时，仅在「同级别桶」内与既有模板做
   difflib 编辑距离比值（≥ 阈值判同簇），并将新模板注册进精确字典，
   后续变体可继续 O(1) 命中；比较数量设上限，防止极端退化；
3. **典型样例**：每簇仅保留 1 份完整样例；首份样例若缺少堆栈而后续
   出现带堆栈的同类错误，则替换为带堆栈版本（更利于定位根因）。

内存控制：簇内存放「模板 + 计数 + 样例 + 有界直方图」，
与错误出现次数无关，与日志总行数无关。
"""
from __future__ import annotations

import re
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
# 要求 6~16 位且至少含一位数字，规避 "beaded" 等纯字母英文单词误伤
_MASK_HEX_TOKEN = re.compile(
    r"\b(?=[0-9a-fA-F]{6,16}\b)(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,16}\b"
)
_MASK_HEX = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_MASK_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_MASK_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|[\w.\-]+[\\/])[\w.\-/\\]*")
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
            ) -> Tuple[ErrorCluster, bool]:
        """错误条目入簇。

        参数：
            entry: 通过过滤的错误条目
            before_lines: 该条目出现时刻的前上下文快照（管线维护，用于样例）

        返回：
            (簇, 是否新建簇或更换了样例) —— 更换样例时管线需要重新
            捕获后上下文。

        三级匹配策略：
        1. 完整指纹（级别+消息+堆栈前3行）精确命中；
        2. (级别, 消息模板) 精确命中 —— 同消息、堆栈有无/截断差异的合并
           （错误首次出现无堆栈、后续带完整堆栈的典型场景）；
        3. 消息模板编辑距离相似度回退（限同级别桶、设比较上限）。
        """
        fp = fingerprint(entry)
        cluster = self._exact.get(fp)
        if cluster is not None:
            replaced = self._update(cluster, entry, before_lines)
            return cluster, replaced

        msg_tp = message_template(entry)
        cluster = self._msg_index.get((entry.level, msg_tp))
        if cluster is not None:
            self._exact[fp] = cluster
            replaced = self._update(cluster, entry, before_lines)
            return cluster, replaced

        cluster = self._find_similar(msg_tp, entry.level)
        if cluster is not None:
            self._exact[fp] = cluster           # 变体注册进精确表，后续 O(1)
            self._msg_index[(entry.level, msg_tp)] = cluster
            replaced = self._update(cluster, entry, before_lines)
            return cluster, replaced

        cluster = self._create(fp, msg_tp, entry, before_lines)
        return cluster, True

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
        """在级别桶内对消息模板做编辑距离相似度匹配。"""
        bucket = self._by_level.get(level)
        if not bucket:
            return None
        best: Optional[ErrorCluster] = None
        best_score = self._threshold
        for cluster in bucket[-self._max_compare:]:
            # quick_ratio 是 ratio 的廉价上界，先行剪枝
            matcher = SequenceMatcher(None, msg_tp, cluster.message_template)
            if matcher.quick_ratio() < best_score:
                continue
            score = matcher.ratio()
            if score >= best_score:
                best, best_score = cluster, score
        return best

    def _create(self, fp: str, msg_tp: str, entry: LogEntry,
                before_lines: Optional[List[str]]) -> ErrorCluster:
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
        self._update(cluster, entry, before_lines)
        return cluster

    def _update(self, cluster: ErrorCluster, entry: LogEntry,
                before_lines: Optional[List[str]]) -> bool:
        """更新簇计数/时间/样例；返回是否更换了样例。"""
        cluster.count += 1
        cluster.last_line = entry.last_line_no or entry.line_no
        cluster.last_seen = entry.timestamp
        if entry.timestamp is not None:
            cluster.hist.add(entry.timestamp)
        # 修复缺陷R4：记录实例（前 N 个含完整条目+上下文，其余仅元数据）
        self._record_instance(cluster, entry, before_lines)
        replaced = False
        # 样例升级：已存样例无堆栈而新条目带堆栈 -> 替换（更利于根因定位）
        if entry.has_stack and not (cluster.sample and cluster.sample.entry.has_stack):
            cluster.sample = ClusterSample(
                entry=entry, before=list(before_lines or []))
            replaced = True
        if not cluster.module and entry.module:
            cluster.module = entry.module
        return replaced

    def _record_instance(self, cluster: ErrorCluster, entry: LogEntry,
                         before_lines: Optional[List[str]]) -> None:
        """记录簇内单个实例（修复缺陷R4，全屏簇展开数据源）。

        三层上限（内存有界）：
        1. 全局总数超限 -> 不再记录（instances_truncated 标记）；
        2. 每簇超 MAX_CLUSTER_INSTANCES_META -> 不再记录；
        3. 每簇超 MAX_CLUSTER_INSTANCES_DETAILED -> 仅记元数据
           （entry/before 为空，详情面板退化为摘要视图）。
        """
        if self._instance_total >= MAX_TOTAL_INSTANCES:
            cluster.instances_truncated = True
            return
        insts = cluster.instances
        if len(insts) >= MAX_CLUSTER_INSTANCES_META:
            cluster.instances_truncated = True
            return
        self._instance_total += 1
        detailed = len(insts) < MAX_CLUSTER_INSTANCES_DETAILED
        insts.append(ClusterInstance(
            timestamp=entry.timestamp,
            line_no=entry.line_no,
            last_line_no=entry.last_line_no or entry.line_no,
            summary=self._summarize(entry),
            entry=entry if detailed else None,
            before=list(before_lines or []) if detailed else [],
        ))

    @staticmethod
    def _summarize(entry: LogEntry) -> str:
        """展示用错误摘要（完整消息，截断 160 字符）。"""
        text = entry.full_message or (entry.stack[0] if entry.stack else "")
        return text[:160]
