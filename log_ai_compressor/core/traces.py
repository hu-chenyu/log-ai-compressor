# -*- coding: utf-8 -*-
"""调用链重建：从日志里的 trace_id / request_id 还原跨服务链路。

这是本工具**唯一能自己补上的证据缺口**
----------------------------------------
`analysis.assess_evidence` 长期把「跨服务 Trace 或 request_id 关联」列为
缺失证据（权重 80，仅次于缺少因果链）。但工具只是"指出缺什么"，从不去
把缺的补上 —— 这让「诚实」停在嘴皮子上。

本模块就是去补它：结构化日志（JSON / logfmt）里本来就带着 trace_id /
request_id，把同一个 ID 下的所有条目按时间排开，一条调用链就出来了。
**不需要装 agent、不需要接拓扑、不需要第二模态** —— 这正是本地文件
相对云平台的结构性优势：他们要看链路得先把你埋点接进他们的机房。

严格的语义边界（不可越界）
--------------------------
链路只证明**顺序**，不证明**因果**。所以：

  ✓ 可以：把某簇标为「在这条调用链上」，把链路作为证据列出，
          在证据缺口评估里**关掉**"没有调用链"这一项
  ✗ 不可以：因此把任何一档提升到 CONFIRMED

工具里 CONFIRMED 的唯一来源仍然是条目内 Caused-by 链。这条线一旦松动，
整个「拿不出因果链就说不知道」的立场就作废了 —— 一个自信的错误根因
比没有根因更糟，而自信恰恰是这里最容易犯的错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# 关联键长度的合理区间：太短是序号/ID 噪声，太长是误当关联键
_MIN_ID_LEN = 6
_MAX_ID_LEN = 64


@dataclass
class TraceStep:
    """调用链上的一步。"""

    line_no: int
    level: str
    service: str
    message: str
    timestamp: Optional[float] = None
    cluster_id: Optional[int] = None      # 该行归属的错误簇（若有）
    is_error: bool = False


@dataclass
class TraceChain:
    """一条由同一个关联键串起来的调用链。"""

    correlation_id: str
    key_kind: str                          # 'request_id' | 'trace_id'
    steps: List[TraceStep] = field(default_factory=list)

    @property
    def services(self) -> List[str]:
        """按出现顺序去重的服务列表（空服务名不计入）。"""
        out: List[str] = []
        for s in self.steps:
            if s.service and s.service not in out:
                out.append(s.service)
        return out

    @property
    def cross_service(self) -> bool:
        """跨服务：链上出现了一个以上不同的服务名。"""
        return len(self.services) > 1

    @property
    def first_error(self) -> Optional[TraceStep]:
        for s in self.steps:
            if s.is_error:
                return s
        return None

    @property
    def service_count(self) -> int:
        return len(self.services)

    def narrative(self, limit: int = 8) -> str:
        """一行式链路叙事：a → b → c（首个失败点标出）。"""
        head = self.steps[:limit]
        parts = []
        for s in head:
            label = f"{s.service or '-'}:{s.message[:26]}"
            parts.append(f"[{label}]" if s.is_error else label)
        tail = "" if len(self.steps) <= limit else f" …共 {len(self.steps)} 步"
        first = self.first_error
        mark = ""
        if first is not None:
            mark = f"；首个失败点 L{first.line_no}"
        return " → ".join(parts) + tail + mark

    def as_dict(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "key_kind": self.key_kind,
            "steps": len(self.steps),
            "services": self.services,
            "service_count": self.service_count,
            "cross_service": self.cross_service,
            "first_error_line": (self.first_error.line_no
                                 if self.first_error else None),
            "narrative": self.narrative(),
        }


def _plausible_id(v: str) -> bool:
    """过滤掉明显不是关联键的值（纯数字短 ID、UUID 以外的长随机串等）。

    日志里 `id=42` 这种到处都是，用它建链只会得到一堆无意义的"链路"。
    关联键的共同特征：足够长，且不是纯序号。
    """
    if not (_MIN_ID_LEN <= len(v) <= _MAX_ID_LEN):
        return False
    if v.isdigit():                        # 纯数字：多是自增主键
        return False
    return True


def build_traces(entries: Iterable[Any],
                 cluster_of: Optional[Dict[int, int]] = None) -> List[TraceChain]:
    """从条目里重建调用链。

    Args:
        entries: LogEntry 迭代（通常取自簇的实例与样例）
        cluster_of: line_no -> cluster_id，用来把步骤挂到错误簇上

    Returns:
        只保留「有诊断价值」的链路：跨服务，或链上出现错误。单条无错的
        同服务日志不值得展示 —— 它不构成任何证据。
    """
    buckets: Dict[str, List[TraceStep]] = {}
    kinds: Dict[str, str] = {}
    for e in entries:
        cid = e.correlation_id if hasattr(e, "correlation_id") else ""
        if not cid or not _plausible_id(cid):
            continue
        kind = ("request_id" if getattr(e, "request_id", "")
                else "trace_id")
        buckets.setdefault(cid, []).append(TraceStep(
            line_no=e.line_no,
            level=getattr(e, "level", "") or "",
            service=getattr(e, "module", "") or "",
            message=getattr(e, "message", "") or "",
            timestamp=getattr(e, "timestamp", None),
            cluster_id=(cluster_of or {}).get(e.line_no),
            is_error=getattr(e, "level", "") in ("ERROR", "FAIL"),
        ))
        kinds[cid] = kind

    chains: List[TraceChain] = []
    for cid, steps in buckets.items():
        # 顺序：有时间戳按时间，否则按行号（无序日志的兜底口径）
        if all(s.timestamp is not None for s in steps):
            steps.sort(key=lambda s: (s.timestamp, s.line_no))
        else:
            steps.sort(key=lambda s: s.line_no)
        chain = TraceChain(correlation_id=cid, key_kind=kinds[cid],
                           steps=steps)
        if not chain.cross_service and chain.first_error is None:
            continue                    # 同服务无错误：不构成证据
        chains.append(chain)
    chains.sort(key=lambda c: (not c.cross_service, -len(c.steps)))
    return chains

# 说明：此处原有 trace_summary(chains)，docstring 写着「写进证据评估的
# inputs_used 与报告」—— 但它从来没被调用过，而它算的那三个数在
# analysis.assess_evidence 里早就**内联**算好了（链数 / 跨服务链数）。
# 同一个口径存在两份实现，其中一份还是死的，正是「派生值有两个来源」这类
# 隐患的温床。已删；要用就调 analysis 里那一份。
