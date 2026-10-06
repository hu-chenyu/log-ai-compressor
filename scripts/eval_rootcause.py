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

# 异常摘要行：`java.io.IOException: xxx` / `java.lang.OutOfMemoryError: xxx`
_EXC = re.compile(r"^(?:[\w.$]+\.)?([\w$]*(?:Exception|Error|Throwable))(?::\s*(.*))?$")
# Caused by: 的因果边（链上非首行）
_CAUSED = re.compile(r"^Caused by:\s*(?:[\w.$]+\.)?([\w$]*(?:Exception|Error|Throwable))(?::\s*(.*))?$")


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


def scan_chains(path: Path) -> list[Chain]:
    """独立扫描原始日志，抽出所有 Caused-by 因果链。"""
    chains: list[Chain] = []
    cur: dict | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            m = _CAUSED.match(s)
            if m and cur is not None:
                cur["depth"] += 1
                cur["deepest_line"] = i
                cur["deepest_sig"] = _sig(m.group(1), m.group(2))
                continue
            m = _EXC.match(s)
            if m:
                if cur and cur["depth"] > 0:
                    chains.append(Chain(cur["first_line"], cur["deepest_line"],
                                        cur["depth"], cur["deepest_sig"]))
                cur = {"first_line": i, "deepest_line": i, "depth": 0,
                       "deepest_sig": _sig(m.group(1), m.group(2))}
            elif s == "" or s.startswith("at ") or s.startswith("... "):
                continue
            else:
                if cur and cur["depth"] > 0:
                    chains.append(Chain(cur["first_line"], cur["deepest_line"],
                                        cur["depth"], cur["deepest_sig"]))
                cur = None
    if cur and cur["depth"] > 0:
        chains.append(Chain(cur["first_line"], cur["deepest_line"],
                            cur["depth"], cur["deepest_sig"]))
    return chains


def _cluster_sigs(c) -> set[str]:
    """簇自身及其实例里出现过的异常签名。"""
    sigs = set()
    texts = [c.message_template or "", c.summary or ""]
    for inst in getattr(c, "instances", []) or []:
        texts.append(getattr(inst, "summary", "") or "")
    for t in texts:
        for m in re.finditer(r"([\w$]*(?:Exception|Error|Throwable)):\s*([^\n]{0,80})", t or ""):
            sigs.add(_sig(m.group(1), m.group(2)))
    return sigs


def evaluate(path: Path) -> Outcome:
    o = Outcome(name=path.parent.name or path.stem)
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

    # --- 召回率：每条真实因果链，是否有 CONFIRMED 簇认领它 ---
    all_sigs = [(_cluster_sigs(c), c) for c in result.clusters]
    for ch in chains:
        matched = False
        for sigs, c in all_sigs:
            if not (c.first_line <= ch.deepest_line <= c.last_line):
                continue
            if any(ch.deepest_sig in s or s in ch.deepest_sig for s in sigs):
                matched = c.root_cause_confidence == CONF_CONFIRMED
                break
        if matched:
            o.chains_matched += 1
        else:
            o.misses.append(ch)

    for c in result.clusters:
        o.by_conf[c.root_cause_confidence or "(空)"] += 1
    o.by_conf["_根因簇数"] = sum(1 for c in result.clusters if c.is_root_cause)
    return o


def main() -> int:
    args = sys.argv[1:]
    if args:
        paths = [Path(a) for a in args]
    else:
        paths = sorted(
            p for p in (ROOT / "data" / "loghub").rglob("*_full.log")
            if scan_chains(p))            # 只评有真实因果链的数据集
    if not paths:
        print("没有找到含 Caused-by 因果链的日志。")
        print("请先下载 Loghub 1.0 的 Android_v1（含真实 Java 异常堆栈）。")
        return 1

    print("=" * 78)
    print("  根因判定评测 —— CONFIRMED 档的精确率 / 召回率")
    print("  地面真相：独立扫描原始日志的 Caused by 链（不依赖本项目解析器）")
    print("=" * 78)

    tot = Outcome()
    for p in paths:
        o = evaluate(p)
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