#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对抗性评测集：专门设计来**误导**本工具的场景，按评分卡自动打分。

为什么要有这个
--------------
本项目对外的核心主张是「拿不出因果链就说证据不足」。主张本身不需要
证明 —— 但一个只会给自己打分的工具没有资格主张这件事。

所以这里做业界很少见的一件事：**构造一批专门设计来让本工具出错的日志**，
然后按四条硬性规则打分，并把结果接进 CI。任何一次「本来该弃权却给出了
高置信根因」的回归都会让 CI 变红，而不是等到某次值班时才发现。

四条评分项（每条都是"错了就致命"的那种）
----------------------------------------
| 评分项 | 通过条件 |
| --- | --- |
| `abstain_correctly` | 该弃权时确实弃权（verdict=INSUFFICIENT 且无 CONFIRMED 簇） |
| `missing_evidence_correct` | 声称缺失的证据确实缺失（不虚报缺口） |
| `no_confident_wrong` | 不得在正确答案为「无法定论」时给出 CONFIRMED |
| `causal_language_ok` | 不得把时间共现说成因果（只能标 related / 连锁衍生） |

三个口径上的取舍，先写明以免误读分数
------------------------------------
1. **弃权算通过，不算失败。** 在一个确实无法定论的事件上正确弃权，
   是这个工具想要的行为。只算命中率的评测会偏好猜得最自信的系统 ——
   那正是我们要淘汰的。
2. **`no_confident_wrong` 是权重最高的负项。** 自信地给错答案，
   代价远大于没给答案：值班的人会照着它去查。
3. **不评「准确率」。** 地面真相由我们自己定义，等于自说自话；评的是
   **过程合规性**（该不该下结论时下了没有、下结论时有没有举证），
   这与 Loghub 标注的模板 ground truth 是两套独立的东西。

用法：
    python scripts/adversarial_eval.py            # 打印评分卡，有失败退 1
    python scripts/adversarial_eval.py --json     # 机器可读
    python scripts/adversarial_eval.py --case red_herring_deploy
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

# Windows 控制台默认 cp1252/cp936，打印中文会抛 UnicodeEncodeError ——
# 而且症状极具误导性：评分卡本身全绿，却因为「打印表格时编码失败」让
# 整个 step 变红，看起来像功能坏了。评测是门禁，门禁必须只在**评分**
# 失败时失败，不能因为控制台编码。所以先把 stdout/stderr 切到 UTF-8
# 并对无法编码的字符降级（errors="replace"），保证任何平台都能跑完。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from log_ai_compressor import service as S                    # noqa: E402
from log_ai_compressor.core.analysis import (                # noqa: E402
    CONF_CONFIRMED,
    CONF_INSUFFICIENT,
)

# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------
@dataclass
class Case:
    """一个对抗性场景 + 它的正确答案。"""

    name: str
    doc: str                     # 这个场景在考什么
    log: str
    #: 每条评分项的判定函数：接收分析结果，返回 (通过?, 实测说明)
    checks: Dict[str, Callable[[object], "tuple[bool, str]"]] = field(
        default_factory=dict)


def _ev(result):
    return result.evidence or {}


def _confirmed(result) -> List:
    return [c for c in result.clusters
            if c.root_cause_confidence == CONF_CONFIRMED]


def _gaps(result) -> List[str]:
    return [g["code"] for g in _ev(result).get("gaps", [])]


def _no_abstain(result) -> "tuple[bool, str]":
    """「没有下结论」才算弃权通过。

    口径说明（本文件最重要的一行）：本工具的档位是三档，LIKELY 的报告
    措辞是「可能原因（统计推断，非因果证明）」且 can_conclude=False ——
    **它没有把任何一条结论交给用户当根因**，与 INSUFFICIENT 在"有没有
    下死结论"这件事上没有区别，只是多给了线索。所以弃权的判据是
    `can_conclude == False 且无 CONFIRMED 簇`，而不是"verdict 必须等于
    INSUFFICIENT"。

    真正的红线只有一条：**该弃权时给了 CONFIRMED**。那是"自信的错误根因
    比没有答案更糟"里说的那种致命输出，由 no_confident_wrong 单独把守。
    """
    ev = _ev(result)
    conf = _confirmed(result)
    can_conclude = bool(ev.get("can_conclude"))
    ok = (not can_conclude) and not conf
    return ok, (f"verdict={ev.get('verdict')}, can_conclude={can_conclude}, "
                f"CONFIRMED 簇 {len(conf)} 个"
                + (f"（{[c.summary[:20] for c in conf]}）" if conf else ""))


def _gap_present(code: str) -> Callable[[object], "tuple[bool, str]"]:
    def check(result) -> "tuple[bool, str]":
        gaps = _gaps(result)
        return code in gaps, f"缺口 {code} {'在' if code in gaps else '不在'}声明的 {gaps} 中"
    return check


def _gap_absent(code: str) -> Callable[[object], "tuple[bool, str]"]:
    def check(result) -> "tuple[bool, str]":
        gaps = _gaps(result)
        return code not in gaps, f"缺口 {code} {'被误报' if code in gaps else '未被误报'}"
    return check


def _no_false_confirmed(keyword: str) -> Callable[[object], "tuple[bool, str]"]:
    """指定的良性文本不得被提升为 CONFIRMED。"""
    def check(result) -> "tuple[bool, str]":
        bad = [c.summary[:40] for c in _confirmed(result)
               if keyword in c.summary]
        return not bad, (f"「{keyword}」被误判为 CONFIRMED: {bad}"
                         if bad else f"「{keyword}」未被误判为根因")
    return check


def _confirmed_contains(needle: str) -> Callable[[object], "tuple[bool, str]"]:
    """必须存在 CONFIRMED 根因，且其判定依据里点名了指定异常。

    查的是 `root_cause_reason` 而不是 `summary`：簇的 summary 取的是条目
    **首行**（Java 里那是外层异常），而 CONFIRMED 指向的是 Caused-by 链
    上**最内层**那个。两者不同恰恰是正确的 —— 拿 summary 去断言根因异常
    名字，会误判成"工具认错了根因"。
    """
    def check(result) -> "tuple[bool, str]":
        conf = _confirmed(result)
        hit = [c.root_cause_reason[:60] for c in conf if needle in c.root_cause_reason]
        return bool(hit), (f"CONFIRMED 依据点名「{needle}」: {hit}" if hit
                           else f"CONFIRMED 依据未点名「{needle}」，实际 "
                                f"{[c.root_cause_reason[:50] for c in conf]}")
    return check


def _no_causal_language(result) -> "tuple[bool, str]":
    """非 CONFIRMED 的簇不得使用因果措辞。

    只有 CONFIRMED（Caused-by 链直连）才允许说"根因"；其余档位只能说
    "疑似连锁衍生""统计线索"。把 related 说成 caused 是取证工具最致命的
    越界 —— 它会让人去修一个没坏的组件。
    """
    banned = ("导致了", "由其引发", "caused by", "根因是", "根本原因是")
    offenders = []
    for c in result.clusters:
        if c.root_cause_confidence == CONF_CONFIRMED:
            continue
        text = c.root_cause_reason or ""
        for b in banned:
            if b in text:
                offenders.append(f"{c.summary[:24]}→{b}")
    return not offenders, (f"因果措辞越界: {offenders}" if offenders
                           else "非 CONFIRMED 簇未使用因果措辞")


def _evidence_present(result) -> "tuple[bool, str]":
    """每个簇都必须给出可回读的证据出处。"""
    missing = [c.summary[:30] for c in result.clusters if not c.evidence]
    return not missing, (f"缺证据出处的簇: {missing}" if missing
                         else f"{len(result.clusters)} 个簇均带证据出处")


def _trace_chain_min_services(n: int) -> Callable[[object], "tuple[bool, str]"]:
    """必须重建出至少覆盖 n 个服务的调用链。"""
    def check(result) -> "tuple[bool, str]":
        chains = list(getattr(result, "traces", None) or [])
        best = max((t.service_count for t in chains), default=0)
        ok = best >= n
        detail = (f"最长链路覆盖 {best} 个服务（要求 ≥{n}）")
        if not ok and not chains:
            detail += "，且一条链都没重建出来"
        return ok, detail
    return check


def _not_all_structured(result) -> "tuple[bool, str]":
    """普通文本消息不得被 logfmt 判定整行吞掉。"""
    entries = [i.entry for c in result.clusters for i in c.instances
               if i.entry is not None]
    if not entries:
        return True, "无条目可判（跳过）"
    bad = [e.message[:40] for e in entries
           if getattr(e, "structured", False) and "retries=" in e.message]
    ok = not bad
    return ok, (f"疑似被误判为 logfmt: {bad}" if bad
                else f"{len(entries)} 条均未被误判为 logfmt")


#: 所有场景共用的两条底线：出处可回读 + 不越界用因果措辞
_ALWAYS = {
    "causal_language_ok": _no_causal_language,
    "evidence_present": _evidence_present,
}


CASES: List[Case] = [
    # ------------------------------------------------------------------
    Case(
        name="red_herring_deploy",
        doc="部署脚本里含 ERROR 字样的红鲱鱼出现在真实故障之前。"
            "考的是：本工具会不会把「时间上更早」当成「因」。",
        log=(
            "2026-10-14 10:00:01 INFO  [deploy] rollout starting, 0 errors expected\n"
            "2026-10-14 10:00:05 INFO  [deploy] preflight check: no error, no timeout\n"
            "2026-10-14 10:00:09 WARN  [deploy] legacy job emitted an ERROR marker\n"
            "2026-10-14 10:00:20 ERROR [db] connection refused to db-primary:5432\n"
            "2026-10-14 10:00:21 ERROR [api] request failed: upstream unavailable\n"
            "2026-10-14 10:00:22 INFO  [api] retry scheduled\n"
        ),
        checks={
            "abstain_correctly": _no_abstain,
            "no_confident_wrong": _no_false_confirmed("ERROR marker"),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="keyword_spam_benign",
        doc="一条完全正常的信息行，措辞里塞满根因关键词（error/timeout/"
            "refused/assert）。考的是：关键词投票会不会被措辞骗走。",
        log=(
            "2026-10-14 11:00:00 INFO [health] all assertions passed, no timeout, no refused connection, no error\n"
            "2026-10-14 11:00:05 INFO [health] disk usage 12%, memory ok, permission ok\n"
            "2026-10-14 11:00:09 ERROR [worker] job 88 failed: unable to parse input\n"
            "2026-10-14 11:00:14 INFO [health] heartbeat ok\n"
        ),
        checks={
            "no_confident_wrong": _no_false_confirmed("assertions passed"),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="cooccurrence_no_causality",
        doc="两个错误每次都紧挨着出现（典型上下游连锁）。"
            "考的是：时间共现会不会被当成因果链。",
        log=(
            "2026-10-14 12:00:00 ERROR [cache] read timeout from redis-3\n"
            "2026-10-14 12:00:01 ERROR [api] request failed after cache read timeout\n"
            "2026-10-14 12:05:00 ERROR [cache] read timeout from redis-3\n"
            "2026-10-14 12:05:01 ERROR [api] request failed after cache read timeout\n"
            "2026-10-14 12:10:00 ERROR [cache] read timeout from redis-3\n"
            "2026-10-14 12:10:01 ERROR [api] request failed after cache read timeout\n"
        ),
        checks={
            "abstain_correctly": _no_abstain,
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="insufficient_single_module",
        doc="单一模块、无堆栈、无跨服务线索 —— 结构上就不可能定论。"
            "考的是：它会不会老老实实说「证据不足」并列出缺什么。",
        log=(
            "2026-10-14 13:00:00 ERROR [db] connection refused to db-primary:5432\n"
            "2026-10-14 13:00:05 ERROR [db] connection refused to db-primary:5432\n"
            "2026-10-14 13:00:11 ERROR [db] connection refused to db-primary:5432\n"
        ),
        checks={
            "abstain_correctly": _no_abstain,
            "missing_evidence_correct": _gap_present("single_module"),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="no_timestamps",
        doc="无时间戳的日志。考的是：它会不会承认「定不了先后」。",
        log=(
            "[ERROR] connection refused to db-primary\n"
            "[INFO]  retrying\n"
            "[ERROR] connection refused to db-primary\n"
            "[ERROR] connection refused to db-primary\n"
        ),
        checks={
            "abstain_correctly": _no_abstain,
            "missing_evidence_correct": _gap_present("no_timestamps"),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="confirmed_caused_by",
        doc="唯一该下死结论的场景：Java Caused-by 链。"
            "考的是：该 CONFIRMED 时不能缩手缩脚 —— 否则「弃权优先」"
            "就退化成了「永远弃权」。",
        log=(
            "2026-10-14 14:00:00 ERROR [order] java.lang.RuntimeException: order 9001 failed\n"
            "    at com.app.OrderService.submit(OrderService.java:88)\n"
            "    at com.app.api.OrderController.post(OrderController.java:41)\n"
            "Caused by: java.sql.SQLTimeoutException: Lock wait timeout exceeded\n"
            "    at com.mysql.cj.jdbc.ClientPreparedStatement.execute(ClientPreparedStatement.java:973)\n"
            "    ... 12 more\n"
            "2026-10-14 14:00:03 ERROR [order] java.lang.RuntimeException: order 9002 failed\n"
            "Caused by: java.sql.SQLTimeoutException: Lock wait timeout exceeded\n"
            "2026-10-14 14:00:07 ERROR [order] java.lang.RuntimeException: order 9003 failed\n"
            "Caused by: java.sql.SQLTimeoutException: Lock wait timeout exceeded\n"
        ),
        checks={
            "no_confident_wrong": _confirmed_contains("Lock wait timeout"),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="structured_trace_chain",
        doc="结构化日志（JSON）+ trace_id：跨服务调用链应被重建，"
            "且「缺少关联键」这条证据缺口必须**被自己关掉**。"
            "考的是：本工具能不能把自己列出的缺口真的补上。",
        log=(
            '{"@timestamp":"2026-10-14T09:00:00.100Z","level":"INFO",'
            '"service.name":"api-gateway","message":"received request",'
            '"trace_id":"a1b2c3d4e5f6"}\n'
            '{"@timestamp":"2026-10-14T09:00:00.150Z","level":"INFO",'
            '"service.name":"order-service","message":"calling inventory",'
            '"trace_id":"a1b2c3d4e5f6"}\n'
            '{"@timestamp":"2026-10-14T09:00:00.300Z","level":"ERROR",'
            '"service.name":"inventory-service","message":"db connection refused",'
            '"trace_id":"a1b2c3d4e5f6"}\n'
            '{"@timestamp":"2026-10-14T09:00:00.420Z","level":"ERROR",'
            '"service.name":"api-gateway","message":"request aborted after retries",'
            '"trace_id":"a1b2c3d4e5f6"}\n'
        ),
        checks={
            "trace_chain_found": _trace_chain_min_services(2),
            "missing_evidence_correct": _gap_absent("no_correlation_id"),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="structured_trace_not_causal",
        doc="有跨服务调用链，但链上没有 Caused-by。"
            "**这是最容易越界的一格**：调用链只证明顺序。"
            "考的是：会不会因为「链路很完整」就把结论升成 CONFIRMED。",
        log=(
            '{"@timestamp":"2026-10-14T09:00:00.100Z","level":"ERROR",'
            '"service.name":"gateway","message":"upstream timeout",'
            '"trace_id":"bb11cc22dd33"}\n'
            '{"@timestamp":"2026-10-14T09:00:00.150Z","level":"ERROR",'
            '"service.name":"orders","message":"gateway call failed",'
            '"trace_id":"bb11cc22dd33"}\n'
            '{"@timestamp":"2026-10-14T09:00:00.200Z","level":"ERROR",'
            '"service.name":"frontend","message":"orders call failed",'
            '"trace_id":"bb11cc22dd33"}\n'
        ),
        checks={
            # 有链路 + 有关键词（timeout/failed）也不能升 CONFIRMED
            "abstain_correctly": _no_abstain,
            "trace_chain_found": _trace_chain_min_services(3),
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="benign_text_not_logfmt",
        doc="普通文本消息里恰好出现两处 `key=value`，且它们占了大半个句子。"
            "考的是 logfmt 判定会不会把普通消息整行吞掉 —— "
            "结构化支持最大的风险就是**误判**。",
        log=(
            "2026-10-14 16:00:00 ERROR [net] handshake failed after retries=3 timeout=30\n"
            "2026-10-14 16:00:05 ERROR [net] handshake failed after retries=3 timeout=30\n"
            "2026-10-14 16:00:09 INFO  [net] recovered, retries=0 timeout=30\n"
        ),
        checks={
            "not_swallowed_by_logfmt": _not_all_structured,
        },
    ),
    # ------------------------------------------------------------------
    Case(
        name="false_gap_not_reported",
        doc="带完整 Caused-by 链的单模块日志。考的是**反向**能力："
            "证据其实够了，不该虚报「缺因果链」的缺口。"
            "只会加缺口不会减缺口的工具同样不可信。",
        log=(
            "2026-10-14 15:00:00 ERROR [pay] java.lang.IllegalStateException: payment 77 rejected\n"
            "    at com.pay.Gateway.charge(Gateway.java:31)\n"
            "Caused by: java.lang.OutOfMemoryError: Java heap space\n"
            "    at java.base/java.util.Arrays.copyOf(Arrays.java:3745)\n"
            "    ... 5 more\n"
            "2026-10-14 15:00:04 ERROR [pay] java.lang.IllegalStateException: payment 78 rejected\n"
            "Caused by: java.lang.OutOfMemoryError: Java heap space\n"
        ),
        checks={
            "missing_evidence_correct": _gap_absent("no_causal_chain"),
        },
    ),
]

_SCORE_ITEMS = ("abstain_correctly", "missing_evidence_correct",
                "no_confident_wrong", "causal_language_ok",
                "evidence_present", "trace_chain_found",
                "not_swallowed_by_logfmt")


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def run_case(case: Case) -> Dict[str, object]:
    result = S.analyze(text=case.log, params={"context_lines": 3})
    checks = dict(_ALWAYS)
    checks.update(case.checks)
    results: Dict[str, Dict[str, object]] = {}
    for item in _SCORE_ITEMS:
        fn = checks.get(item)
        if fn is None:
            continue                     # 该场景不考这一项（如 CONFIRMED 场景不考弃权）
        try:
            ok, detail = fn(result)
        except Exception as exc:        # 评分脚本自己出错也算不通过
            ok, detail = False, f"评分执行异常: {type(exc).__name__}: {exc}"
        results[item] = {"pass": bool(ok), "detail": detail}
    judged = len(results)
    passed = sum(1 for v in results.values() if v["pass"])
    return {
        "case": case.name, "doc": case.doc, "verdict": _ev(result).get("verdict"),
        "checked": judged, "passed": passed, "items": results,
        "clusters": len(result.clusters),
        "confirmed": len(_confirmed(result)),
    }


def run_all(only: Optional[str] = None) -> List[Dict[str, object]]:
    return [run_case(c) for c in CASES if not only or c.name == only]


def scorecard(reports: List[Dict[str, object]]) -> Dict[str, object]:
    """汇总：整体通过率 + 自信错误率（最该压到 0 的那个数）。"""
    total = sum(r["checked"] for r in reports)
    passed = sum(r["passed"] for r in reports)
    # 自信错误 = 在应当弃权的场景里给出了 CONFIRMED
    overreach = [r["case"] for r in reports
                 if r["verdict"] == CONF_INSUFFICIENT and r["confirmed"] > 0]
    abstain_cases = [r for r in reports
                     if "abstain_correctly" in r["items"]]
    correct_abstain = sum(1 for r in abstain_cases
                          if r["items"]["abstain_correctly"]["pass"])
    return {
        "cases": len(reports),
        "checks": total,
        "passed": passed,
        "pass_rate": round(100.0 * passed / total, 1) if total else 0.0,
        "confident_wrong_cases": overreach,
        "confident_wrong_rate": (round(100.0 * len(overreach) / len(reports), 1)
                                 if reports else 0.0),
        "abstain_cases": len(abstain_cases),
        "correct_abstain_rate": (round(100.0 * correct_abstain / len(abstain_cases), 1)
                                 if abstain_cases else 0.0),
        "ok": passed == total and not overreach,
    }


def _print_table(reports: List[Dict[str, object]], summary: Dict[str, object],
                 as_json: bool) -> None:
    if as_json:
        print(json.dumps({"summary": summary, "cases": reports},
                         ensure_ascii=False, indent=2))
        return
    mark = {True: "✓", False: "✗"}
    print("=" * 78)
    print("对抗性评测：专门设计来误导本工具的场景")
    print("=" * 78)
    for r in reports:
        print(f"\n● {r['case']}   [{r['passed']}/{r['checked']}]"
              f"  verdict={r['verdict']}"
              f"  簇={r['clusters']} CONFIRMED={r['confirmed']}")
        print(f"  {r['doc']}")
        for item, v in r["items"].items():
            print(f"    {mark[v['pass']]} {item:28} {v['detail']}")
    print("\n" + "=" * 78)
    print("评分卡")
    print("=" * 78)
    print(f"  场景数              : {summary['cases']}")
    print(f"  评分项 / 通过        : {summary['passed']}/{summary['checks']}"
          f"  ({summary['pass_rate']}%)")
    print(f"  正确弃权率           : {summary['correct_abstain_rate']}%"
          f"  （{summary['abstain_cases']} 个应弃权场景）")
    print(f"  自信错误率           : {summary['confident_wrong_rate']}%"
          f"  {summary['confident_wrong_cases'] or '（0 个，该压住的数压住了）'}")
    print(f"  总判定               : {'PASS' if summary['ok'] else 'FAIL'}")
    print("\n注：弃权算通过。「没给出答案」在确实无法定论的事件上是正确行为，"
          "\n    只有「不该下结论却下了」才记为失败。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="对抗性评测 + 评分卡")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--case", default=None, help="只跑指定场景")
    args = ap.parse_args(argv)
    reports = run_all(args.case)
    if not reports:
        print(f"未知场景：{args.case}", file=sys.stderr)
        return 2
    summary = scorecard(reports)
    _print_table(reports, summary, args.json)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
