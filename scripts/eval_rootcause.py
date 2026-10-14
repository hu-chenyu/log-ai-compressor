#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根因判定评测：CONFIRMED 档的精确率与召回率。

为什么需要这个评测
------------------
本项目对外的核心主张是「拿不出因果链就说证据不足」。但 Loghub-2.0 标注的
是**日志模板**、不是**故障根因**，所以上一轮基准**完全没有验证这个主张**。
本文补上这个缺口。

地面真相是客观的
----------------
`core/analysis.py:411` 写得很清楚：CONFIRMED 的**唯一**来源是
「该簇被 Caused-by 因果链指向」。因此可以**独立于本项目**、直接扫原始
日志的 `Caused by:` 链来构造地面真相，再和工具输出对账：

  精确率（不乱说）  = 被标 CONFIRMED 且原始行确有 Caused-by 直连的簇 / 全部 CONFIRMED 簇
  召回率（该说时说）= 被标 CONFIRMED 且对得上的链数 / 原始日志中真实存在的链数
  **自信错误率**     = 1 - 精确率   ← 这是最该压到 0 的那个数

链的方向（Java 语义）
--------------------
    java.lang.RuntimeException: A      <- 外层：症状
    Caused by: java.io.IOException: B   <- 内层：真正的因
    Caused by: java.io.OutOfMemory: C  <- 最内层：根因

工具判定的是「被 Caused-by 指向的簇」，对应链上**最内层**那个异常。

用法：python scripts/eval_rootcause.py [日志路径 ...]
      python scripts/eval_rootcause.py --repro      # 复现语料（自建场景）

复现语料（--repro）是什么、不是什么
----------------------------------
外部真实数据集（Loghub Android_v1 / Hadoop 全量，含真实 Java 异常栈）托管在
zenodo，当前网络环境反复下载失败 —— **真实数据的召回率至今仍无数字，此处
不编**。`--repro` 提供的是：自建故障场景 + 真实 Java 异常类/栈帧/日志格式
的**功能基准**，用来证明「精确率/召回率/自信错误率」这三个指标的机制能
抓住回归（例如历史上那个把 INFO 行判成 CONFIRMED 的 bug）。它的数字
**不能**替代真实数据集结论，报告里分开列、分开标注。
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from log_ai_compressor import service                      # noqa: E402
from log_ai_compressor.core.analysis import (              # noqa: E402
    CONF_CONFIRMED,
)

# 异常摘要行：`java.lang.RuntimeException: xxx`。类名允许 `$` 内部类/匿名类
# （真实日志里 `com.zaxxer.hikari.pool.HikariPool$1: ...` 这类异常很常见），
# 不再要求以 Exception/Error/Throwable 结尾 —— 否则整条链会被漏成噪音，
# 地面真相自己先失真，评测就白做了。
_CLS = r"(?:[\w$]+\.)*[\w$]+"
_EXC = re.compile(rf"^{_CLS}(?::\s*(.*))?$")
_CAUSED = re.compile(rf"^Caused by:\s*({_CLS})(?::\s*(.*))?$")
# 纯单词行（"Timeout" / "Done"）不是异常类，需带包名点或类名后缀才认
_SUFFIXES = ("Exception", "Error", "Throwable")
# 修复评测器缺陷#1：真实日志的异常摘要常**内嵌在带时间戳的日志行里**
# （Hadoop `2015-10-19 17:41:30,296 INFO [...] Task: Communication exception:
# java.io.IOException: ...`），旧行为只认「裸异常摘要行」开头的链，把这类
# 链整条漏扫 —— 于是工具正确的 Caused-by 判定反被对账判成"自信错误"。
# 用时间戳行作为**条目边界**重建扫描（与工具解析器同口径），内嵌异常的链
# 不再漏。地面真相一旦失真，评测数字全是错的，所以这一条必须修。
_TS_LINE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}")
# 堆栈续行：帧 / 折叠计数 / 被抑制异常 / 空白（不构成条目边界）
_CONT = re.compile(r"^(?:at |\.\.\. |\s*Suppressed:)", re.IGNORECASE)


def _is_cont(s: str) -> bool:
    """堆栈续行（保持当前链继续累积）。"""
    return (not s) or bool(_CONT.match(s))


def _exc_line(s: str):
    """行首异常摘要：返回 (类名, 消息) 或 None（纯单词行拒绝）。"""
    m = _EXC.match(s)
    if not m:
        return None
    cls = s.split(":", 1)[0].strip()
    if "." not in cls and not cls.endswith(_SUFFIXES):
        return None
    rest = s[len(cls):].lstrip()[1:] if ":" in s[len(cls):] else ""
    return cls, rest.strip()


@dataclass
class Chain:
    """原始日志里的一条因果链（独立于本项目解析器构造）。"""
    first_line: int          # 外层异常（症状）所在行
    deepest_line: int        # 最内层 Caused by 指向的异常所在行
    depth: int               # Caused by 层数（0 = 无因果链）
    deepest_sig: str         # 最内层异常的「类名: 消息」归一化签名


@dataclass
class Outcome:
    name: str = ""
    rows: int = 0
    chains: int = 0
    chains_matched: int = 0
    confirmed: int = 0
    confirmed_ok: int = 0
    by_conf: dict = field(default_factory=lambda: defaultdict(int))
    misses: list = field(default_factory=list)
    false_conf: list = field(default_factory=list)


def _sig(cls: str, msg: str | None) -> str:
    """异常签名：类名 + 消息前若干字符（数字折叠，避免参数差异）。"""
    m = re.sub(r"\d+", "N", (msg or "").strip())[:60]
    return f"{cls}:{m}".strip(":").lower()


# ---------------------------------------------------------------------------
# 复现语料（--repro）：自建故障场景 + 真实 Java 异常类/栈帧/日志格式。
# 地面真相仍由 scan_chains 独立扫描产生，不手工标注。
# ---------------------------------------------------------------------------
REPRO_CORPUS: dict[str, str] = {
    # 场景 1：双链 —— 一条 3 层（连接池→连接→拒绝），一条 2 层（缓存预热）
    "order_service.log": """\
2026-10-14 09:12:30,101 INFO  [http-nio-8080-exec-7] c.e.order.OrderService - 开始处理订单 order=88213
2026-10-14 09:12:33,456 ERROR [http-nio-8080-exec-7] c.e.order.OrderService - 订单创建失败 order=88213
java.lang.RuntimeException: 订单创建失败
\tat com.example.order.OrderService.create(OrderService.java:42)
\tat com.example.order.OrderController.submit(OrderController.java:88)
\tat jdk.internal.reflect.DirectMethodHandleAccessor.invoke(DirectMethodHandleAccessor.java:103)
\t... 32 more
Caused by: com.zaxxer.hikari.pool.HikariPool$1: Connection is not available, request timed out after 30000ms.
\tat com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)
\tat com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:181)
\t... 41 more
Caused by: java.net.ConnectException: Connection refused (Connection refused)
\tat java.base/java.net.PlainSocketImpl.socketConnect(Native Method)
\tat java.base/java.net.AbstractPlainSocketImpl.doConnect(AbstractPlainSocketImpl.java:412)
\t... 12 more
2026-10-14 09:12:33,470 WARN  [http-nio-8080-exec-7] c.e.order.OrderService - 订单 88213 已回滚
2026-10-14 09:13:02,880 ERROR [scheduler-1] c.e.cache.CacheWarmup - 缓存预热失败 key=hot:sku
java.lang.IllegalStateException: 缓存预热失败
\tat com.example.cache.CacheWarmup.warm(CacheWarmup.java:57)
\t... 9 more
Caused by: redis.clients.jedis.exceptions.JedisConnectionException: Unexpected end of stream.
\tat redis.clients.jedis.Connection.readProtocolWithCheckingBroken(Connection.java:313)
\t... 11 more
2026-10-14 09:13:05,000 INFO  [scheduler-1] c.e.cache.CacheWarmup - 预热任务结束
""",
    # 场景 2：单链 3 层 + 厚噪音（OOM 在最内层），验证深链能被召回
    "batch_job.log": """\
2026-10-14 02:00:01,000 INFO  [batch-1] c.e.batch.JobRunner - 批处理开始 job=daily-settle
2026-10-14 02:00:05,231 INFO  [batch-1] c.e.batch.JobRunner - 已读取分片 shard=1/12
2026-10-14 02:00:09,876 INFO  [batch-1] c.e.batch.JobRunner - 已读取分片 shard=2/12
2026-10-14 02:03:41,554 ERROR [batch-1] c.e.batch.JobRunner - 批处理中断 job=daily-settle
java.lang.RuntimeException: 批处理中断
\tat com.example.batch.JobRunner.run(JobRunner.java:133)
\tat java.base/java.lang.Thread.run(Thread.java:1583)
Caused by: java.lang.IllegalStateException: 结果集物化失败
\tat com.example.batch.SettleAggregator.materialize(SettleAggregator.java:201)
\t... 4 more
Caused by: java.lang.OutOfMemoryError: Java heap space
\tat com.example.batch.SettleAggregator.<init>(SettleAggregator.java:44)
\t... 7 more
2026-10-14 02:03:41,560 WARN  [batch-1] c.e.batch.JobRunner - 开始清理临时表
2026-10-14 02:03:42,111 INFO  [batch-1] c.e.batch.JobRunner - 清理完成
""",
    # 场景 3：对照组 —— 关键词堆砌但**无任何因果链**（0 条链，必须 0 CONFIRMED）
    "no_chain_control.log": """\
2026-10-14 08:00:01,000 WARN  [worker-2] c.e.task.TaskExecutor - connection refused to db-replica:5432, 重试中
2026-10-14 08:00:02,000 WARN  [worker-2] c.e.task.TaskExecutor - connection refused to db-replica:5432, 重试中
2026-10-14 08:00:03,000 ERROR [worker-2] c.e.task.TaskExecutor - out of memory 告警：堆使用率 97%，GC 时间占比 31%
2026-10-14 08:00:04,000 ERROR [worker-2] c.e.task.TaskExecutor - timeout: 下游 payment 调用 30s 无响应
2026-10-14 08:00:05,000 ERROR [worker-2] c.e.task.TaskExecutor - task failed after 3 retries: 无法连接到主库 10.0.0.11:3306
2026-10-14 08:00:06,000 WARN  [worker-2] c.e.task.TaskExecutor - 降级为兜底逻辑
""",
    # 场景 4：混排 —— 2 条链 + 无链错误 + INFO 噪音，精确率召回率同时受压
    "mixed.log": """\
2026-10-14 15:30:00,120 INFO  [http-nio-8080-exec-1] c.e.api.Gateway - 请求进入 GET /api/v2/report
2026-10-14 15:30:00,333 ERROR [http-nio-8080-exec-1] c.e.api.Gateway - 报表生成失败
java.lang.RuntimeException: 报表生成失败
\tat com.example.api.ReportService.generate(ReportService.java:61)
\t... 24 more
Caused by: java.io.FileNotFoundException: /data/export/template.xlsx (No such file or directory)
\tat java.base/java.io.FileInputStream.open0(Native Method)
\tat com.example.api.ReportService.loadTemplate(ReportService.java:72)
\t... 26 more
2026-10-14 15:30:01,400 ERROR [http-nio-8080-exec-2] c.e.api.Gateway - 上游鉴权服务返回 502
2026-10-14 15:30:02,001 WARN  [http-nio-8080-exec-2] c.e.api.Gateway - 请求已降级处理 GET /api/v2/report
2026-10-14 15:30:07,665 ERROR [kafka-consumer-3] c.e.msg.OrderConsumer - 消息消费失败 topic=order-events
java.lang.IllegalStateException: 消息消费失败
\tat com.example.msg.OrderConsumer.onMessage(OrderConsumer.java:39)
\t... 6 more
Caused by: org.apache.kafka.common.errors.SerializationException: 未知的消息版本 v7
\tat com.example.msg.OrderDecoder.decode(OrderDecoder.java:28)
\t... 8 more
2026-10-14 15:30:08,000 INFO  [kafka-consumer-3] c.e.msg.OrderConsumer - 位点已回滚
""",
}


def scan_chains(path: Path) -> list[Chain]:
    """独立扫描原始日志，抽出所有 Caused-by 因果链。

    修复评测器缺陷#1：条目边界用**时间戳行或裸异常行**判定（而非只认
    裸异常行）。真实日志把异常摘要内嵌在带时间戳的日志行里，链的真正
    信号是堆栈块里的 `Caused by:` —— 只要在一条目内出现 ≥1 个
    `Caused by:` 就是一条链，最内层那个即根因。
    """
    chains: list[Chain] = []
    cur: dict | None = None

    def flush() -> None:
        nonlocal cur
        if cur and cur["depth"] > 0:
            chains.append(Chain(cur["first_line"], cur["deepest_line"],
                                cur["depth"], cur["deepest_sig"]))
        cur = None

    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            m = _CAUSED.match(s)
            if m:
                # Caused by 是链的直接信号：即便前面没有裸异常行（内嵌
                # 异常），也在此开启/续接链
                if cur is None:
                    cur = {"first_line": i, "deepest_line": i, "depth": 0,
                           "deepest_sig": ""}
                cur["depth"] += 1
                cur["deepest_line"] = i
                cur["deepest_sig"] = _sig(m.group(1), m.group(2))
                continue
            # 条目边界：时间戳行（新条目，异常可能内嵌其内）或裸异常摘要行
            if _TS_LINE.match(s) or _exc_line(s):
                flush()
                cur = {"first_line": i, "deepest_line": i, "depth": 0,
                       "deepest_sig": ""}
                continue
            if _is_cont(s):
                continue                      # 帧/折叠/被抑制/空白：续行
            # 其它行：突发的消息分隔（无时间戳），结束当前链，防两条链误并
            flush()
    flush()
    return chains


def evaluate(path: Path, name: str = "") -> Outcome:
    o = Outcome(name=name or (path.parent.name or path.stem))
    chains = scan_chains(path)
    o.chains = len(chains)

    result = service.analyze(
        paths=[str(path)],
        params={"levels": ["INFO", "WARN", "ERROR"], "top_n": 0,
                "context_lines": 5},
    )
    o.rows = result.stats.total_lines

    confirmed = [c for c in result.clusters
                 if c.root_cause_confidence == CONF_CONFIRMED]
    o.confirmed = len(confirmed)

    # --- 精确率：每个 CONFIRMED 簇，其引用行范围内是否真的有 Caused by ---
    deep_lines = {ch.deepest_line for ch in chains}
    for c in confirmed:
        hit = any(c.first_line <= ln <= c.last_line for ln in deep_lines)
        if hit:
            o.confirmed_ok += 1
        else:
            o.false_conf.append(c)

    # --- 召回率：每条真实因果链，是否有 CONFIRMED 簇的行区间覆盖其最内层异常。
    #     与精确率用同一把尺子（行区间含链最内层行 = 认领该链）。不再做异常
    #     签名匹配：簇文本只有消息模板、不含栈，拿签名对账会把真召回判成
    #     漏报 —— 首跑复现语料时召回率因此整体假 0%，评测器自己的 bug。
    for ch in chains:
        hit = any(c.root_cause_confidence == CONF_CONFIRMED
                  and c.first_line <= ch.deepest_line <= c.last_line
                  for c in result.clusters)
        if hit:
            o.chains_matched += 1
        else:
            o.misses.append(ch)

    for c in result.clusters:
        o.by_conf[c.root_cause_confidence or "(空)"] += 1
    o.by_conf["_根因簇数"] = sum(1 for c in result.clusters if c.is_root_cause)
    return o


def main() -> int:
    args = sys.argv[1:]
    repro = args == ["--repro"]
    named: list[tuple[Path, str]] = []
    if repro:
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="rootcause_repro_"))
        for name, text in REPRO_CORPUS.items():
            p = tmp / name
            p.write_text(text, encoding="utf-8")
            named.append((p, name))
    elif args:
        named = [(Path(a), "") for a in args]
    else:
        named = [(p, "") for p in sorted(
            p for p in (ROOT / "data" / "loghub").rglob("*_full.log")
            if scan_chains(p))]            # 只评有真实因果链的数据集
    if not named:
        print("没有找到含 Caused-by 因果链的日志。")
        print("真实数据：下载 Loghub Android_v1 / Hadoop 全量（zenodo，含真实 Java 异常栈）")
        print("功能基准：python scripts/eval_rootcause.py --repro（复现语料，非生产日志）")
        return 1

    print("=" * 78)
    print("  根因判定评测 —— CONFIRMED 档的精确率 / 召回率")
    print("  地面真相：独立扫描原始日志的 Caused by 链（不依赖本项目解析器）")
    if repro:
        print("  [口径] 复现语料：自建故障场景 + 真实 Java 异常/栈帧/日志格式，")
        print("         **非生产日志** —— 验证指标机制与回归，不替代真实数据集结论。")
    print("=" * 78)

    tot = Outcome()
    for p, name in named:
        o = evaluate(p, name)
        print(f"\n  【{o.name}】{o.rows:,} 行")
        print(f"    真实因果链        : {o.chains}")
        print(f"    判为 CONFIRMED    : {o.confirmed}")
        print(f"    精确率（不乱说）  : "
              f"{'—' if not o.confirmed else f'{o.confirmed_ok / o.confirmed * 100:.1f}%'}"
              f"   ({o.confirmed_ok}/{o.confirmed})")
        print(f"    自信错误率        : "
              f"{'—' if not o.confirmed else f'{(1 - o.confirmed_ok / o.confirmed) * 100:.1f}%'}")
        print(f"    召回率（该说时说）: "
              f"{'—' if not o.chains else f'{o.chains_matched / o.chains * 100:.1f}%'}"
              f"   ({o.chains_matched}/{o.chains})")
        dist = {k: v for k, v in o.by_conf.items() if not k.startswith("_")}
        dist["其中根因簇"] = o.by_conf["_根因簇数"]
        print(f"    置信分布          : {dist}")
        for c in o.false_conf[:3]:
            print(f"    ✗ 自信错误        : [{c.cluster_id}] {c.summary[:60]}")
        for ch in o.misses[:3]:
            print(f"    · 漏报            : L{ch.deepest_line} {ch.deepest_sig[:60]}")
        tot.chains += o.chains
        tot.chains_matched += o.chains_matched
        tot.confirmed += o.confirmed
        tot.confirmed_ok += o.confirmed_ok

    print("\n" + "=" * 78)
    print("  合计")
    print(f"    真实因果链 {tot.chains}，CONFIRMED {tot.confirmed}")
    print(f"    精确率 {tot.confirmed_ok / tot.confirmed * 100:.1f}%"
          if tot.confirmed else "    精确率 —")
    print(f"    自信错误率 {(1 - tot.confirmed_ok / tot.confirmed) * 100:.1f}%"
          if tot.confirmed else "    自信错误率 —")
    print(f"    召回率 {tot.chains_matched / tot.chains * 100:.1f}%"
          if tot.chains else "    召回率 —")
    return 0


if __name__ == "__main__":
    sys.exit(main())