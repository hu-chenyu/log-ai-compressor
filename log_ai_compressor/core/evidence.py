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
        """**每一条引用都真的回读核对过，且全对**。

        v2.11.1 修的假通过：原来只有 `mismatched == 0 and missing_source == 0`，
        于是两种「什么都没验成」都报 True：

        - `checked == 0`（一条引用都没有）—— `mcp/server.py` 的契约是
          「`ok=False` 才不转述」，于是 Agent 读到 `ok=true` 就把
          「一条都没复核」当成「每条都已复核通过」；
        - 全部是粘贴文本引用 —— 结构上就回读不了，那不是「通过」。

        取「只有 VERIFIED 才算通过」，宁可让机器接口说不了话，
        也不能让它替用户断言「验过了」。
        """
        return self.verdict == "VERIFIED"

    @property
    def verdict(self) -> str:
        """四档，v2.11.1 新增 UNVERIFIABLE。

        - `NO_EVIDENCE`   一条引用都没有（空报告 / 未产出任何引用）
        - `TAMPERED`      真的回读过、且对不上 —— 日志被改过
        - `UNVERIFIABLE`  有引用，但**一条都回读不了**（粘贴文本没有源文件、
                          源文件已被删、条目超出摄入窗口取不到原文）
        - `VERIFIED`      全部回读通过

        v2.11.1 之前只有三档，而 `missing_source` 被并进 TAMPERED ——
        「粘贴进来的文本没法回读」因此被报成「日志被篡改」，退出码 1，
        屏幕上写着「请勿直接采信」。那是**方向相反的假警报**：不是谎称通过，
        是把「验不了」说成「被抓」。而 CLI 那一层给的却是
        `NO_EVIDENCE` + exit 0 —— 同一个事实，两层两个结论。
        """
        if not self.checked:
            return "NO_EVIDENCE"
        if self.mismatched:
            return "TAMPERED"
        if self.missing_source:
            return "UNVERIFIABLE"
        return "VERIFIED"

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


#: `_read_range` 的三种结果，**必须分开**。
#:
#: 修复缺陷（v2.11.2）：原先三种情况都返回 `None`，而调用方只把 `None`
#: 当成「行数不够」→ 记 `mismatched` → verdict 变 `TAMPERED`。于是
#: **文件正被另一个进程独占**（正在写入 / 正在轮转 / 编辑器开着，
#: 这是日志场景里最常见的一种状态）会被报成「日志确实被改过，
#: 请勿直接采信」，并让退出码变成 1。
#:
#: 实测（Windows，msvcrt 独占锁住前 64 字节，内容一个字没改）：
#:
#:     证据：共 9 条引用，匹配 0 条，失配 9 条，源不可读 0 条
#:     自校验结论：TAMPERED      exit 1
#:     ── 解锁后再验 ──
#:     每条结论都能在原始日志里逐行复核。   exit 0   （内容逐字节相同）
#:
#: 「读不到」被并进「对不上」，是把**没发生过的事**当成证据。
_READ_OK = "ok"
_READ_OUT_OF_RANGE = "out_of_range"
_READ_UNREADABLE = "unreadable"


def _read_range(path: Path, line_start: int, line_end: int,
                encoding: str = ""):
    """按 1-based 行号读取区间，返回 (状态, 行列表)。

    状态是上面三个常量之一：读到了 / 读到了但行数不够 / **压根读不到**。
    最后一类不是篡改证据，只是现在验不了。

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
    except OSError:
        # 独占锁 / 权限 / 临时 IO 错误。这说明**验不了**，不是「对不上」。
        return _READ_UNREADABLE, None
    if len(out) != line_end - line_start + 1:
        # 读到了，但那一段行数不够 —— 文件被截短了，**这才是真的变了**。
        return _READ_OUT_OF_RANGE, None
    return _READ_OK, out


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
        state, lines = _read_range(real, ref.line_start, ref.line_end,
                                   ref.encoding)
        if state == _READ_UNREADABLE:
            # 记 missing_source 而不是 mismatched：一条都回读不了 → verdict
            # 自然是 UNVERIFIABLE（「验不了」），而不是 TAMPERED。
            report.missing_source += 1
            report.details.append({"evidence_id": ref.evidence_id,
                                   "status": "UNREADABLE",
                                   "source": ref.source})
            continue
        if state == _READ_OUT_OF_RANGE:
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
# 不参与指纹的字段（口径记录在这里，**不另设常量**）：耗时与速率这类每次
# 运行都不同的数字，以及指纹自身。此前有一个 `_VOLATILE_STATS` 集合写着
# 这份名单，但实现是就地逐字段重建 stats 字典、从不由那个集合推导 ——
# 于是它变成一个「改了没用」的死常量，而注释会让读代码的人以为改它就能
# 控制指纹口径。名单写在下面 docstring 里，改口径时看得到、也改得动。

# 结论字段白名单：簇上**会进入报告、且影响判断**的字段。
# 新增结论字段时必须同步加进这里，否则指纹会看不见这次改动 —— v2.7.0 就
# 漏了 root_cause_variants，于是「把变体改回只报主因」这种回归指纹纹丝不动，
# 而指纹恰恰是对外公开的「同输入同结论」唯一证明。
_FINGERPRINT_CLUSTER_FIELDS = (
    "cluster_id", "level", "module", "summary", "count", "priority",
    "priority_label", "is_root_cause", "root_cause_confidence",
    "root_cause_reason", "anomaly", "change_context",
    "root_cause_variants", "cause_overflow", "root_timeline",
    "related_clusters", "traces", "first_seen", "last_seen",
    # 是簇字段 `evidence`；它以路径无关身份（起止行+类型+摘要）入指纹，
    # 所以这个名单里不能叫 evidence_ids —— 那会让人以为指纹用的是 ID。
    "evidence",
)


def _fingerprint_ref(ref) -> tuple:
    """证据引用的**路径无关**身份：哪一段原文（起止行 + 类型 + 原文摘要）。

    为什么不用 `evidence_id`：它由 `(source, 起止行, 类型, 摘要)` 派生，
    而 `source` 是**调用方传入的路径字符串**。同一份日志用绝对路径和相对
    路径打开，`evidence_id` 全都不同，于是结论指纹也跟着变 ——
    而指纹对外承诺的是「同一份日志跑两遍逐字节相同」。

    v2.11.0 实测（未改之前）：

        绝对路径 examples/sample_system.log  ->  856b49a807c1bc171…
        相对路径 examples/sample_system.log  ->  2adae7d0a62c5d53…

    两次分析的是**逐字节相同**的一份日志，逐字段 diff 只有路径不同。
    CI 的确定性门禁用**同一个字面路径**连跑三次，结构上就检测不到这一维。

    摘要 `digest` 已经在里面，所以判别力与 `evidence_id` 等价 ——
    唯一丢掉的是路径，而路径不是「结论」。
    """
    return () if ref is None else (ref.line_start, ref.line_end,
                                   ref.kind, ref.digest)


def _fingerprint_variants(cluster) -> tuple:
    """簇内全部内层因的稳定摘要（原因 / 次数 / 各自的首处证据引用）。"""
    return tuple(
        (v.reason, v.count, _fingerprint_ref(v.evidence))
        for v in (getattr(cluster, "root_cause_variants", None) or ())
    )


def result_fingerprint(result) -> str:
    """一次分析的"结论指纹"：同输入必须逐字节相同。

    覆盖**会影响判断**的部分：级别、摘要、次数、优先级、根因判定与置信档位、
    **簇内全部内层因**、根因时间线、关联簇、调用链、证据引用、时间戳。
    耗时与速率这类每次都不同的数字被排除（在下面的 stats 字面量里根本不取）。

    这个数可以直接公开（"确定性指纹一致性 100%"）：把同一份日志跑两遍，
    比对指纹是否相等 —— 一个不需要相信厂商就能做的验证。

    反过来说，**它也是承诺**：凡是会出现在报告里、且影响判断的字段，都必须
    进来。漏掉一个字段，就等于给那种"悄悄改回旧行为"的回归发了一张通行证
    —— v2.7.0 的 `root_cause_variants` 就是这么漏的。

    注意这里**不能**经由 service.result_to_dict 取统计（那条路上挂着
    本函数，会无限递归）。统计口径在此处就地重建，且刻意只取与结论
    相关的字段。

    集合型字段（`related_clusters` / `traces`）按**排序后的元组**入指纹：
    它们语义上是集合，而集合的存储顺序不该让同一份日志的指纹漂移。

    **路径不是结论**（v2.11.0）
    ------------------------
    指纹刻意不取 `stats.source`，证据也刻意不以 `evidence_id` 入指纹 ——
    两者都含调用方传入的路径字符串，于是同一份日志用绝对路径和相对路径
    打开会得到两个不同的指纹，而对外承诺是「同一份日志跑两遍逐字节相同」。
    证据改用**路径无关身份**（起止行 + 类型 + 原文摘要）入指纹，判别力不变。
    这条边界同时写进了 AGENTS.md §4b 的表里 —— 「不写进去的边界，
    下一个人只会当成 bug 或者当成承诺」。
    """
    s = result.stats
    # 刻意**不取** `s.source`：它是调用方传入的路径字符串（绝对/相对/软链接/
    # 大小写都可以），而同一份日志用不同写法打开，分析结果逐字段相同。
    # 取了它，指纹就成了「你怎么写的这个路径」的函数 —— 对外承诺的
    # 「同一份日志跑两遍逐字节相同」当场不成立。
    stats = {
        "rule": s.rule_name, "total_lines": s.total_lines,
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
            # v2.7.0 补：以下五个此前不在指纹里，于是改它们指纹纹丝不动。
            # 每一条都会出现在报告里、且都会改变用户的结论，必须进。
            _fingerprint_variants(c),
            c.cause_overflow,
            c.root_timeline,
            tuple(sorted(c.related_clusters)),
            tuple(sorted(c.traces)),
            round(c.first_seen, 3) if c.first_seen is not None else None,
            round(c.last_seen, 3) if c.last_seen is not None else None,
            # 证据用**路径无关身份**入指纹（见 _fingerprint_ref）
            tuple(_fingerprint_ref(e) for e in c.evidence),
        ))
    blob = repr((stats, payload))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
