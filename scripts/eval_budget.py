#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同预算公平对照：同样的 token 预算下，谁把「根因证据」留在了输出里？

要回答的问题
------------
给定一个**根因埋在厚噪音里**的日志文件和同一个 token 预算，
四种方法各显其能，谁的输出里还看得见因果链最内层的根因异常？

    tail -N       只截最后 N 行（大多数人的第一反应）
    grep          关键词过滤 error/exception/fail ...
    Drain3        学术标准模板抽取（ICWS'17，821★）
    本工具        确定性压缩摘要（根因判定 + 证据引用）

度量：**根因证据留存率** = 输出中仍可见的因果链根因异常数 / 真实链数
（输出按同一预算截断，谁超预算谁被截 —— 同一把尺子）。

语料构造（诚实标注：半合成）
----------------------------
- **真实底座**：Loghub Hadoop 全量日志（真实生产集群日志）作背景噪音；
- **已知故障**：复现语料（eval_rootcause.REPRO_CORPUS）的因果链场景
  均匀埋入噪音中 —— 链的地面真相仍由 scan_chains 独立扫描产生；
- 所以这测的是「真实噪音底座 + 注入已知故障」的**功能基准**，
  不是纯真实故障数据集的结论 —— 后者仍缺（zenodo 不可达），不冒充。

诚实标注：不含 LogSieve / LLM 摘要器
------------------------------------
离线环境拿不到 LogSieve 的实现与 LLM API，同预算对照只覆盖
tail / grep / Drain3 三档。网络恢复后可直接在本框架里补测
（方法接口一致：输入行列表 + 预算，输出文本）。

用法：python scripts/eval_budget.py [--noise-lines N] [--budgets B1,B2,...]
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from log_ai_compressor import service                       # noqa: E402
from log_ai_compressor.export.reporters import estimate_tokens  # noqa: E402
from eval_rootcause import REPRO_CORPUS, scan_chains         # noqa: E402

# grep 基线关键词（人工排障第一手动作）
GREP_PATTERN = re.compile(
    r"error|exception|fail|fatal|critical|abort|refused|timeout|"
    r"denied|panic|warn", re.IGNORECASE)
# 噪声底座（去栈 Loghub Hadoop 全量；缺文件时退化为纯复现语料）
NOISE_FILE = ROOT / "data" / "loghub" / "hadoop_full.log"
_TS_PREFIX = re.compile(
    r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*\s*")


def load_noise(n: int) -> List[str]:
    """从真实底座均匀采样 n 行（均匀采样保住全时段的多样噪音）。"""
    if not NOISE_FILE.is_file() or n <= 0:
        return []
    with NOISE_FILE.open(encoding="utf-8", errors="replace") as fh:
        pool = [ln.rstrip("\n") for ln in fh if ln.strip()]
    if len(pool) <= n:
        return pool
    step = len(pool) / n
    return [pool[int(i * step)] for i in range(n)]


def build_corpus(noise: List[str], path: Path) -> None:
    """把故障场景均匀埋进噪音：每段场景之间垫等量噪音行。"""
    blocks = list(REPRO_CORPUS.values())
    per = max(len(noise) // (len(blocks) + 1), 1) if noise else 0
    out: List[str] = []
    for i, block in enumerate(blocks):
        out.extend(noise[i * per:(i + 1) * per])
        out.append(block.rstrip("\n"))
    out.extend(noise[len(blocks) * per:])
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 四种方法：输入 (行列表, 预算 tokens) -> 输出文本（超预算即截断）
# ---------------------------------------------------------------------------
def _clip(text: str, budget: int) -> str:
    """按估算 token 数截断输出（同一把尺子管所有方法）。"""
    if estimate_tokens(text) <= budget:
        return text
    out, spent = [], 0
    for line in text.splitlines():
        cost = estimate_tokens(line) + 1
        if spent + cost > budget:
            break
        spent += cost
        out.append(line)
    return "\n".join(out)


def method_tail(lines: List[str], budget: int) -> str:
    kept: List[str] = []
    spent = 0
    for line in reversed(lines):
        cost = estimate_tokens(line) + 1
        if spent + cost > budget:
            break
        spent += cost
        kept.append(line)
    return "\n".join(reversed(kept))


def method_grep(lines: List[str], budget: int) -> str:
    kept: List[str] = []
    spent = 0
    for line in lines:
        if not GREP_PATTERN.search(line):
            continue
        cost = estimate_tokens(line) + 1
        if spent + cost > budget:
            break
        spent += cost
        kept.append(line)
    return "\n".join(kept)


def method_drain3(lines: List[str], budget: int) -> str:
    """Drain3 模板 + 计数。时间戳剥掉再喂（Drain 官方要求的输入形态）。"""
    try:
        from drain3.drain import Drain
    except ImportError:
        return ""
    drain = Drain()
    for line in lines:
        try:
            drain.add_log_message(_TS_PREFIX.sub("", line))
        except Exception:
            pass
    rows: List[str] = []
    spent = 0
    for cl in sorted(drain.clusters, key=lambda c: -c.size):
        row = f"{cl.get_template()}  x{cl.size}"
        cost = estimate_tokens(row) + 1
        if spent + cost > budget:
            break
        spent += cost
        rows.append(row)
    return "\n".join(rows)


def method_ours(corpus: Path, budget: int) -> str:
    """本工具：默认级别排障摘要（summary 口径）。"""
    result = service.analyze(
        paths=[str(corpus)],
        params={"levels": ["INFO", "WARN", "ERROR"], "top_n": 0,
                "context_lines": 0},
    )
    return _clip(service.export_text(result, "summary"), budget)


# ---------------------------------------------------------------------------
# 留存判定
# ---------------------------------------------------------------------------
def retained(output: str, deepest_sig: str) -> bool:
    """根因异常是否在输出中仍可见（类名 或 消息前缀命中即算留存）。

    同一条尺子量所有方法：tail/grep 输出原始行，Drain3 输出模板，
    本工具输出根因叙事 —— 只要根因异常的**身份**还能被认出来就算数，
    刻意从宽，不冤枉对照组。
    """
    out = output.lower()
    cls, _, msg = deepest_sig.partition(":")
    if cls and cls.lower() in out:
        return True
    frag = msg.strip()[:20].lower()
    return bool(frag) and frag in out


def main() -> int:
    ap = argparse.ArgumentParser(description="同预算公平对照")
    ap.add_argument("--noise-lines", type=int, default=2000,
                    help="背景噪音行数（真实底座均匀采样）")
    # 默认档位必须与 docs/BENCHMARK.md 里发布的那张表逐列一致 ——
    # 否则"照文档敲命令"复现不出"文档里的数"，等于结论不可复核。
    ap.add_argument("--budgets", default="200,400,800,1600,3200,6400")
    ap.add_argument("--no-drain3", action="store_true")
    args = ap.parse_args()
    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]

    noise = load_noise(args.noise_lines)
    tmp = Path(tempfile.mkdtemp(prefix="budget_eval_"))
    corpus = tmp / "corpus.log"
    build_corpus(noise, corpus)
    lines = corpus.read_text(encoding="utf-8").splitlines()
    chains = scan_chains(corpus)
    if not chains:
        print("语料里没有 Caused-by 因果链，无法度量根因留存。")
        return 1

    methods: List[Tuple[str, Callable[[int], str]]] = [
        ("tail -N（截最后 N 行）", lambda b: method_tail(lines, b)),
        ("grep 关键词", lambda b: method_grep(lines, b)),
    ]
    if not args.no_drain3:
        methods.append(("Drain3（ICWS'17, 821★）",
                        lambda b: method_drain3(lines, b)))
    methods.append(("log-ai-compressor", lambda b: method_ours(corpus, b)))

    print("=" * 78)
    print("  同预算公平对照 —— 谁把根因证据留在了输出里？")
    print(f"  语料：真实底座 {len(noise):,} 行（Loghub Hadoop）+ "
          f"注入故障 {len(chains)} 条因果链（半合成，诚实标注）")
    print("  预算：输出按估算 token 数硬性截断，同一把尺子管所有方法")
    print("  [诚实标注] 不含 LogSieve / LLM 摘要器：离线拿不到其实现与 API，")
    print("             恢复网络后可在本框架内以同一接口补测。")
    print("=" * 78)

    header = f"  {'方法':<28}" + "".join(f"{f'{b}tok':>10}" for b in budgets)
    print("\n" + header)
    print("  " + "-" * (28 + 10 * len(budgets)))
    for name, fn in methods:
        cells = []
        for b in budgets:
            out = fn(b)
            hits = sum(1 for ch in chains if retained(out, ch.deepest_sig))
            cells.append(f"{hits / len(chains) * 100:.0f}%")
        print(f"  {name:<28}" + "".join(f"{c:>10}" for c in cells))

    print(f"\n  根因证据留存率 = 输出中仍可见的链根因异常 / 共 {len(chains)} 条链")
    print("  口径说明：留存判定从宽（类名或消息前缀命中即算），不冤枉对照组；")
    print("  Drain3 输出的是模板，命中的是模板里的异常类名 —— 与原始行同一把尺子。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
