# -*- coding: utf-8 -*-
"""证据引用与自校验：让每条结论都能被独立复核。

为什么需要这个模块
------------------
本项目对外的核心主张是「拿不出因果链就说证据不足」。但主张本身不等于
证据 —— 一个说「根因是 X」的工具，如果无法回答「X 是在第几行看到的、
那几行现在还是不是这样」，那它的结论和 LLM 随口编的一句没有本质区别。

所以这里做三件事：

1. **证据引用（EvidenceRef）**：把结论绑定到日志原文的**具体行区间**，
   并对那段原文算摘要（sha256）。摘要不是装饰 —— 它让"结论来自这段
   原文"这句话变成可验证的断言。
2. **自校验（verify_refs）**：重新读取原始日志，重算摘要，逐条比对。
   文件被改过、截断过、结论与原文对不上，立刻暴露。
   **这是本工具唯一能证明自己在说实话的机制**：它不需要相信我们，
   调用方自己就能验。
3. **确定性指纹（result_fingerprint）**：同一份输入两次分析，
   结论部分的 sha256 必须逐字节相同。行业里几乎没人报这个数，
   而它恰恰是"结论可写进事后复盘"的前提。

设计上刻意保持本模块只依赖同层原语（不 import models / service）：
证据是比模型更底层的概念，模型层反过来引用它。唯一引入的是
core/encoding 的解码原语 —— 修复缺陷#1：自校验必须与摄入走**同一条**
解码路径（编码探测 + .gz/.zip 透明解压），否则非 UTF-8 日志解析正常、
一复核就误报 TAMPERED，直接击穿「可复核」这条核心主张。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from log_ai_compressor.core.encoding import detect_encoding, open_text_stream

# 证据类型（决定报告与 UI 怎么呈现）
# 说明：此前这里还有一条 KIND_INSTANCE = "instance"，定义后全项目零使用
# （find_unused.py 实测），已删。留着它等于宣称「我们支持按实例取证」，
# 而实际上没有任何代码产出这种引用 —— 一个说谎的常量比没有更糟。
KIND_SAMPLE = "sample"        # 簇的典型样例原文
KIND_CAUSED_BY = "caused_by"  # 驱动 CONFIRMED 判定的 Caused by 行

_ID_LEN = 12                  # evidence_id 长度：够短好引用，够长不撞
_DIGEST_LEN = 16              # 摘要展示长度（完整 sha256 存全文）


def digest_lines(lines: Sequence[str]) -> str:
    """一组原始行的内容摘要（sha256 十六进制）。

    归一化只做一件事：剥掉行尾换行符。**不做** strip()、不折叠空白 ——
    摘要要能发现"日志被人改过"，任何内容改动都必须体现为摘要变化。
    """
    payload = "\n".join(line.rstrip("\r\n") for line in lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceRef:
    """一条可复核的证据引用。"""

    evidence_id: str
    source: str                 # 日志文件路径（粘贴文本为 "<粘贴文本>"）
    line_start: int
    line_end: int
    kind: str = KIND_SAMPLE
    digest: str = ""            # 该行区间的 sha256（全文）
    # 修复缺陷#1：摄入时使用的文本编码（含 .gz/.zip 透明解压约定）。
    # verify 必须用**同一个**解码器回读才能重算出同样的摘要 —— 否则
    # GBK/UTF-16 日志必然误报 TAMPERED。空串 = 回读时自动探测（兼容
    # 旧版报告），非空 = 以此为准（用户强制编码时自动探测会猜错）。
    encoding: str = ""
    note: str = ""              # 人话说明（例：驱动 CONFIRMED 的那一行）

    @property
    def short_digest(self) -> str:
        return self.digest[:_DIGEST_LEN]

    def as_dict(self) -> Dict[str, Any]:
        """对外表示。

        `digest` 一律给**完整** sha256。之前这里截断到 16 位、短摘要只进
        报告正文，导致同一份证据经 Web API 与 JSON 导出两条路拿到不同精度
        的摘要 —— 第三方接入方拿 Web API 的结果去校验必然失配，而
        `verify --report` 只吃完整摘要。这类不一致极难排查（两边字段名
        都叫 digest，值格式却不同），所以序列化层不做截断，截断只发生在
        人读的展示路径（见 short_digest）。
        """
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "kind": self.kind,
            "digest": self.digest,
            "encoding": self.encoding,
            "note": self.note,
        }

    def citation(self) -> str:
        """一行式引用串，可直接贴进复盘 / 工单。"""
        loc = (f"{self.source}:L{self.line_start}"
               if self.line_start == self.line_end
               else f"{self.source}:L{self.line_start}-L{self.line_end}")
        note = f" ({self.note})" if self.note else ""
        return f"{loc} [{self.evidence_id}]{note}"


def make_ref(source: str, line_start: int, line_end: int,
             lines: Sequence[str], kind: str = KIND_SAMPLE,
             note: str = "", encoding: str = "") -> EvidenceRef:
    """构造证据引用。

    evidence_id 是**确定性**的：同样的（文件, 起止行, 原文）永远得到同样
    的 ID。不含时间戳、不含随机数、不含对象 id —— 这样同一份日志两次
    分析的证据 ID 可直接 diff，也才能写进回归测试的断言。

    encoding 记录摄入时的解码方式，verify 回读时原样复现（修复缺陷#1）。
    它刻意**不进** evidence_id：ID 标识的是「哪一段原文」，解码方式是
    回读提示，换了探测器也不该改变证据身份。
    """
    line_end = max(line_end, line_start)
    digest = digest_lines(lines)
    raw = f"{source}|{line_start}|{line_end}|{kind}|{digest}".encode("utf-8")
    eid = hashlib.sha256(raw).hexdigest()[:_ID_LEN]
    return EvidenceRef(evidence_id=eid, source=source,
                       line_start=line_start, line_end=line_end,
                       kind=kind, digest=digest, encoding=encoding,
                       note=note)


def derive_ref(base: "EvidenceRef", kind: str, note: str = "") -> "EvidenceRef":
    """从已有引用派生一个**同一段原文**、不同用途的引用。

    用于「同一条原文支撑了不同层级的结论」：比如典型样例行同时也是
    CONFIRMED 判定的 Caused-by 链出处。派生引用共享行区间与摘要（所以
    校验时指向同一段原文），但 evidence_id 因 kind 不同而不同 ——
    这样报告里能区分「因为看到了这段」和「因为这段里的因果链」。

    摘要不重算：重算需要原文，而这里刻意只靠已有引用派生，避免为了
    拿摘要再读一次文件。
    """
    raw = (f"{base.source}|{base.line_start}|{base.line_end}|"
           f"{kind}|{base.digest}").encode("utf-8")
    return EvidenceRef(evidence_id=hashlib.sha256(raw).hexdigest()[:_ID_LEN],
                       source=base.source, line_start=base.line_start,
                       line_end=base.line_end, kind=kind,
                       digest=base.digest, encoding=base.encoding,
                       note=note)


# ---------------------------------------------------------------------------
# 自校验
# ---------------------------------------------------------------------------
@dataclass
class VerifyReport:
    """一次自校验的完整结果。"""

    checked: int = 0
    matched: int = 0
    mismatched: int = 0
    missing_source: int = 0        # 源文件已不存在（被删/换机器）
    details: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """全部对得上才算 ok。

        部分匹配**不算** ok：一份掺了假的证据报告，不能因为"大部分是真的"
        就放行 —— 那正是取证工具最不能犯的错。
        """
        return self.mismatched == 0 and self.missing_source == 0

    @property
    def verdict(self) -> str:
        if not self.checked:
            return "NO_EVIDENCE"
        return "VERIFIED" if self.ok else "TAMPERED"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ok": self.ok,
            "checked": self.checked,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "missing_source": self.missing_source,
            "details": self.details,
        }


def _read_range(path: Path, line_start: int, line_end: int,
                encoding: str = "") -> Optional[List[str]]:
    """按 1-based 行号读取区间；越界或读不到返回 None。

    修复缺陷#1：回读必须与摄入同一条解码路径 —— 优先用证据记录的
    encoding（用户强制编码时自动探测会猜错），未记录（旧版报告）则
    自动探测；open_text_stream 同时覆盖 .gz/.zip 透明解压。此前硬编码
    utf-8，GBK/UTF-16/压缩日志一复核就全军覆没式误报 TAMPERED。
    """
    try:
        enc = encoding or detect_encoding(path)
        with open_text_stream(path, enc) as fh:
            out: List[str] = []
            for i, line in enumerate(fh, 1):
                if i < line_start:
                    continue
                if i > line_end:
                    break
                out.append(line)
            return out if len(out) == line_end - line_start + 1 else None
    except OSError:
        return None


def verify_refs(refs: Iterable[EvidenceRef]) -> VerifyReport:
    """重读原始日志，逐条核对证据摘要。

    这一步是本工具的信任来源：结论不是靠"相信算法"成立的，
    而是靠调用方随时能自己复核。日志被改过 → 摘要对不上 → 立刻报
    TAMPERED，而不是继续拿旧结论误导排查。
    """
    report = VerifyReport()
    seen_files: Dict[str, Optional[List[str]]] = {}
    for ref in refs:
        report.checked += 1
        path = Path(ref.source)
        # 粘贴文本没有可回读的源文件，直接跳过（而不是谎称校验通过）
        if not ref.source or ref.source.startswith("<"):
            report.missing_source += 1
            report.details.append({"evidence_id": ref.evidence_id,
                                   "status": "NO_SOURCE",
                                   "source": ref.source})
            continue
        if ref.source not in seen_files:
            seen_files[ref.source] = None if not path.is_file() else path
        real = seen_files[ref.source]
        if real is None:
            report.missing_source += 1
            report.details.append({"evidence_id": ref.evidence_id,
                                   "status": "MISSING_SOURCE",
                                   "source": ref.source})
            continue
        lines = _read_range(real, ref.line_start, ref.line_end, ref.encoding)
        if lines is None:
            report.mismatched += 1
            report.details.append({"evidence_id": ref.evidence_id,
                                   "status": "OUT_OF_RANGE",
                                   "source": ref.source,
                                   "line_start": ref.line_start,
                                   "line_end": ref.line_end})
            continue
        actual = digest_lines(lines)
        if actual == ref.digest:
            report.matched += 1
            report.details.append({"evidence_id": ref.evidence_id,
                                   "status": "MATCH", "source": ref.source})
        else:
            report.mismatched += 1
            report.details.append({"evidence_id": ref.evidence_id,
                                   "status": "MISMATCH", "source": ref.source,
                                   "expected": ref.digest[:_DIGEST_LEN],
                                   "actual": actual[:_DIGEST_LEN]})
    return report


# ---------------------------------------------------------------------------
# 确定性指纹
# ---------------------------------------------------------------------------
# 不参与指纹的字段：它们每次运行都不同，但不影响任何结论
_VOLATILE_STATS = frozenset({
    "duration", "lines_per_second", "analysis_cost",
    "duration_sec", "lines_per_second", "analysis_cost_sec",
    "determinism_fingerprint",        # 指纹自身不能参与自己的计算
})


def result_fingerprint(result) -> str:
    """一次分析的"结论指纹"：同输入必须逐字节相同。

    只覆盖**会影响判断**的部分：级别、摘要、次数、优先级、根因判定与
    置信档位、证据引用、时间戳。耗时与速率这类每次都不同的数字被排除。

    这个数可以直接公开（"确定性指纹一致性 100%"）：把同一份日志跑两遍，
    比对指纹是否相等 —— 一个不需要相信厂商就能做的验证。

    注意这里**不能**经由 service.result_to_dict 取统计（那条路上挂着
    本函数，会无限递归）。统计口径在此处就地重建，且刻意只取与结论
    相关的字段。
    """
    s = result.stats
    stats = {
        "source": s.source, "rule": s.rule_name, "total_lines": s.total_lines,
        "entry_lines": s.entry_lines, "error_lines": s.error_lines,
        "error_entries": s.error_entries, "ts_entries": s.ts_entries,
        "level_counts": dict(sorted(s.level_counts.items())),
        "time_start": round(s.time_start, 3) if s.time_start is not None else None,
        "time_end": round(s.time_end, 3) if s.time_end is not None else None,
        "truncated": s.truncated, "limit_hit": s.limit_hit,
    }
    payload: List[Tuple[Any, ...]] = []
    for c in result.clusters:
        payload.append((
            c.cluster_id, c.level, c.module, c.summary, c.count,
            round(c.priority, 1), c.priority_label, c.is_root_cause,
            c.root_cause_confidence, c.root_cause_reason, c.anomaly,
            # 变更时间窗关联是结论的一部分，必须进指纹（确定性口径）
            c.change_context,
            round(c.first_seen, 3) if c.first_seen is not None else None,
            round(c.last_seen, 3) if c.last_seen is not None else None,
            tuple(e.evidence_id for e in c.evidence),
        ))
    blob = repr((stats, payload))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
