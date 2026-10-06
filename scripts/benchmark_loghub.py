#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Loghub 2.0 公开基准：压缩比 vs 证据保留率。

要回答的问题只有一个：

    给定一份真实系统日志，在**同样的 token 预算**下，
    谁留住的「事件类型」最多？

Loghub-2.0（ISSTA'24）的 event template 是**人工标注**的
（论文里就是拿它当 ground truth 评估 15 个解析器），
所以「证据保留率」有真实标注依据，不是自说自话。

四组对照
--------
1. ``tail -N``      —— 最朴素：只截最后 N 行
2. ``grep``         —— 关键词过滤 error/exception/fail
3. ``Drain3``       —— 学术标准日志模板抽取（ICWS'17，821★）
4. ``log-ai-compressor`` —— 本项目

度量
----
* **压缩比** = 原始日志 token / 输出 token（越大越省上下文）
* **证据保留率** = 输出中仍可识别的人工标注事件类型数 / 事件类型总数

事件类型映射口径（公平性关键）
------------------------------
* tail / grep / 本项目 输出都含**原始行**，结构化 CSV 的 ``LineId``
  与原始日志行号一一对应，直接查表精确映射，零误差；
* Drain3 输出的是**模板**（非原始行），只能与标注模板做
  归一化模糊匹配，阈值 0.8 —— 这一项对 Drain3 略偏宽松。

用法
----
    python scripts/benchmark_loghub.py
    python scripts/benchmark_loghub.py --datasets Proxifier,Apache
    python scripts/benchmark_loghub.py --no-drain3
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_ai_compressor import service                       # noqa: E402
from log_ai_compressor.export.reporters import (             # noqa: E402
    estimate_raw_tokens,
    estimate_tokens,
)

DATA_DIR = ROOT / "data" / "loghub"

# grep 基线的关键词 —— 这是人工排障时最常见的第一手动作
GREP_PATTERN = re.compile(
    r"error|exception|fail|fatal|critical|abort|refused|timeout|"
    r"denied|panic|warn", re.IGNORECASE)

# 模板归一化：把可变值统一折叠成 <P>
_CANON_PARAM = re.compile(r"<\*>|\{\*\}|<\[PARAM\]>")
_CANON_NUM = re.compile(r"\b\d+(?:[.:/-]\d+)*\b")
_CANON_WS = re.compile(r"\s+")


def canon(text: str) -> str:
    """模板 / 行文本归一化，抹平可变值后用于跨方法比较。"""
    t = _CANON_PARAM.sub(" <P> ", text)
    t = _CANON_NUM.sub(" <P> ", t)
    t = _CANON_WS.sub(" ", t).strip().lower()
    return t


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------
class Dataset:
    """一个 Loghub 2.0 数据集：原始日志 + 人工标注的事件类型。"""

    def __init__(self, name: str, log_path: Path, csv_path: Path) -> None:
        self.name = name
        self.log_path = log_path
        with csv_path.open(encoding="utf-8", errors="replace", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.raw_lines: List[str] = log_path.read_text(
            encoding="utf-8", errors="replace").splitlines()
        # LineId(1-based) -> EventId；标注只覆盖部分行，未覆盖的记为 None
        self.line_event: Dict[int, str] = {}
        self.event_template: Dict[str, str] = {}
        # LineId -> 剥离了时间戳/主机/级别后的正文。Loghub 自己就是这么标注的，
        # 也是 Drain 官方文档要求喂给它的输入（"extract structured headers
        # like timestamp, hostname, severity before passing to Drain"）。
        # 给对照组喂它应得的输入，才叫公平对照。
        self.line_content: Dict[int, str] = {}
        for r in rows:
            try:
                lid = int(r["LineId"])
            except (KeyError, TypeError, ValueError):
                continue
            content = (r.get("Content") or "").strip()
            if content:
                self.line_content[lid] = content
            eid = (r.get("EventId") or "").strip()
            if not eid:
                continue
            self.line_event[lid] = eid
            self.event_template.setdefault(eid, (r.get("EventTemplate") or "").strip())

        # 反查：原始行文本 -> EventId（用于精确映射，tail/grep/本项目都走这条）
        self.text_event: Dict[str, str] = {}
        for lid, eid in self.line_event.items():
            if 1 <= lid <= len(self.raw_lines):
                self.text_event.setdefault(self.raw_lines[lid - 1], eid)

        self.events: Set[str] = set(self.event_template)
        self.event_canon: Dict[str, str] = {
            e: canon(t) for e, t in self.event_template.items() if t
        }

    @property
    def raw_chars(self) -> int:
        return sum(len(l) for l in self.raw_lines)

    @property
    def raw_tokens(self) -> int:
        return estimate_raw_tokens(self.raw_chars)

    def event_of_line_no(self, line_no: int) -> Optional[str]:
        """本项目的簇样例保留的是 line_no，按行号查表即得标注事件。"""
        return self.line_event.get(line_no)

    def event_of_text(self, text: str) -> Optional[str]:
        return self.text_event.get(text)

    def match_template(self, template: str) -> Optional[str]:
        """把一个（可能来自 Drain3 的）模板映射到标注事件。

        Drain3 输出的是模板而非原始行，没有 LineId 可查，只能归一化后
        与标注模板做相似度匹配。阈值 0.8，偏低以免冤枉对照组。
        """
        c = canon(template)
        if not c:
            return None
        best, best_r = None, 0.8
        for eid, ec in self.event_canon.items():
            r = SequenceMatcher(None, c, ec).ratio()
            if r >= best_r:
                best, best_r = eid, r
        return best


def discover(names: Optional[List[str]] = None) -> List[Dataset]:
    """扫描 data/loghub/<Name>/<Name>/<Name>_full.log。"""
    if not DATA_DIR.is_dir():
        raise SystemExit(
            f"找不到数据集目录：{DATA_DIR}\n"
            f"请先按 README 下载 Loghub 2.0 并解压到该目录。")
    out: List[Dataset] = []
    for log_path in sorted(DATA_DIR.glob("*/*/*_full.log")):
        name = log_path.name[: -len("_full.log")]
        if names and name not in names:
            continue
        csv_path = log_path.with_name(log_path.name.replace(
            "_full.log", "_full.log_structured.csv"))
        if not csv_path.is_file():
            print(f"  跳过 {name}：缺少结构化标注 {csv_path.name}")
            continue
        ds = Dataset(name, log_path, csv_path)
        if not ds.events:
            print(f"  跳过 {name}：标注事件类型为 0")
            continue
        out.append(ds)
    return out


# ---------------------------------------------------------------------------
# 四组方法：都返回 (输出文本, 覆盖到的标注事件集合)
# ---------------------------------------------------------------------------
def method_tail(ds: Dataset, budget_tokens: int) -> Tuple[str, Set[str]]:
    """朴素基线：从文件末尾往前塞，直到撞到 token 预算。

    这正是大多数人排障时第一反应：日志太大，看最后 N 行。
    """
    kept: List[str] = []
    covered: Set[str] = set()
    spent = 0
    for line in reversed(ds.raw_lines):
        cost = estimate_tokens(line) + 1
        if spent + cost > budget_tokens:
            break
        spent += cost
        kept.append(line)
        eid = ds.event_of_text(line)
        if eid:
            covered.add(eid)
    return "\n".join(kept), covered


def method_grep(ds: Dataset, budget_tokens: int) -> Tuple[str, Set[str]]:
    """关键词基线：grep error|exception|fail ...，按时间顺序塞到预算为止。"""
    kept: List[str] = []
    covered: Set[str] = set()
    spent = 0
    for line in ds.raw_lines:
        if not GREP_PATTERN.search(line):
            continue
        cost = estimate_tokens(line) + 1
        if spent + cost > budget_tokens:
            break
        spent += cost
        kept.append(line)
        eid = ds.event_of_text(line)
        if eid:
            covered.add(eid)
    return "\n".join(kept), covered


def method_drain3(ds: Dataset, budget_tokens: int) -> Tuple[str, Set[str]]:
    """Drain3 模板抽取（学术标准）。输出模板 + 出现次数。

    喂给它 Loghub 自己标注的 ``Content`` 字段（已剥离时间戳/主机/级别），
    这是 Drain 官方文档明确要求的输入形态 —— 用应得的输入做对照，
    否则测的是「谁被错误使用」，不是「谁更好」。
    """
    try:
        from drain3.drain import Drain
    except ImportError:
        return "", set()
    drain = Drain()
    for lid in sorted(ds.line_content):
        try:
            drain.add_log_message(ds.line_content[lid])
        except Exception:
            pass
    rows: List[str] = []
    covered: Set[str] = set()
    spent = 0
    for cl in drain.clusters:
        tpl = cl.get_template()
        row = f"{tpl}  x{cl.size}"
        cost = estimate_tokens(row) + 1
        if spent + cost > budget_tokens:
            break
        spent += cost
        rows.append(row)
        eid = ds.match_template(tpl)
        if eid:
            covered.add(eid)
    return "\n".join(rows), covered


def method_ours(ds: Dataset, budget_tokens: int,
                levels: Optional[str] = None) -> Tuple[str, Set[str]]:
    """本项目：压缩摘要报告 + 证据卡片。

    证据保留口径 = **簇样例保留的原始行**对应的标注事件。
    样例行是未改动的原始日志（LogEntry.raw），可用 line_no 精确回查，
    因此不会因为我们的占位符改写而虚高或虚低。
    """
    level_list = (levels.split(",") if levels else None)
    result = service.analyze(
        paths=[str(ds.log_path)],
        params={"levels": level_list, "top_n": 0, "context_lines": 0},
    )
    text = service.export_text(result, "summary")
    covered: Set[str] = set()
    for c in result.clusters:
        sample = getattr(c, "sample", None)
        if sample is None or sample.entry is None:
            continue
        eid = ds.event_of_line_no(sample.entry.line_no)
        if eid:
            covered.add(eid)
    return text, covered


# ---------------------------------------------------------------------------
# 全级别集合：把工具从「错误分诊」模式切到「全量摘要」模式。
# 两者都要报 —— 只报对自己有利的那一个是不诚实的。
ALL_LEVELS = ["TRACE", "DEBUG", "INFO", "WARN", "WARNING",
              "ERROR", "FATAL", "CRITICAL"]

# 朴素基线的 token 预算档位（用于画保留率曲线）
DEFAULT_BUDGETS = [500, 1000, 2000, 4000, 8000, 16000, 32000]


def sweep_tail(ds: Dataset, budgets: List[int]) -> List[Tuple[int, int, int]]:
    out = []
    for b in budgets:
        text, cov = method_tail(ds, b)
        out.append((b, estimate_tokens(text), len(cov)))
    return out


def sweep_grep(ds: Dataset, budgets: List[int]) -> List[Tuple[int, int, int]]:
    out = []
    for b in budgets:
        text, cov = method_grep(ds, b)
        out.append((b, estimate_tokens(text), len(cov)))
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def pct(num: int, den: int) -> str:
    return "—" if not den else f"{num / den * 100:.1f}%"


def run_one(ds: Dataset, budgets: List[int], levels: Optional[str],
             use_drain: bool) -> None:
    total_events = len(ds.events)
    print()
    print("=" * 78)
    print(f"  {ds.name}   {len(ds.raw_lines):,} 行   "
          f"{ds.raw_tokens:,} tokens   人工标注事件类型 {total_events} 种")
    print("=" * 78)

    t0 = time.time()
    drain_text, drain_cov = method_drain3(ds, 10 ** 9) if use_drain else ("", set())
    t_drain = time.time() - t0

    t0 = time.time()
    our_def_text, our_def_cov = method_ours(ds, 10 ** 9, levels)
    t_our_def = time.time() - t0

    t0 = time.time()
    our_all_text, our_all_cov = method_ours(
        ds, 10 ** 9, ",".join(ALL_LEVELS) if not levels else levels)
    t_our_all = time.time() - t0

    rows: List[Tuple[str, str, str, str, str]] = []
    for label, text, cov, secs in (
            ("log-ai-compressor 默认级别(排障)", our_def_text, our_def_cov, t_our_def),
            ("log-ai-compressor 全级别(摘要)", our_all_text, our_all_cov, t_our_all),
            ("Drain3（ICWS'17, 821★）", drain_text, drain_cov, t_drain),
    ):
        if not text and not cov:
            continue
        n_tok = estimate_tokens(text)
        rows.append((label, f"{n_tok:,}", f"{ds.raw_tokens / max(n_tok, 1):,.0f}x",
                     pct(len(cov), total_events), f"{secs:.2f}s"))

    for label, fn in (("tail -N（截最后 N 行）", method_tail),
                      ("grep 关键词", method_grep)):
        text, cov = fn(ds, budgets[-1])
        n_tok = estimate_tokens(text)
        rows.append((f"{label} @ {budgets[-1]}tok", f"{n_tok:,}",
                     f"{ds.raw_tokens / max(n_tok, 1):,.0f}x",
                     pct(len(cov), total_events), "—"))

    print(f"  {'方法':<34}{'输出tokens':>12}{'压缩比':>10}{'证据保留率':>12}{'耗时':>8}")
    print("  " + "-" * 78)
    for r in rows:
        print(f"  {r[0]:<34}{r[1]:>12}{r[2]:>10}{r[3]:>12}{r[4]:>8}")

    print()
    print(f"  朴素基线在同预算下的保留率曲线（事件类型 {total_events} 种）")
    print(f"  {'预算(tokens)':>14}{'tail 保留':>12}{'grep 保留':>12}"
          f"{'本项目(全级别)':>16}")
    print("  " + "-" * 54)
    st = dict((b, c) for b, _, c in sweep_tail(ds, budgets))
    sg = dict((b, c) for b, _, c in sweep_grep(ds, budgets))
    for b in budgets:
        ours_at = pct(len(our_all_cov), total_events)
        print(f"  {b:>14,}{pct(st[b], total_events):>12}"
              f"{pct(sg[b], total_events):>12}{ours_at:>16}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Loghub 2.0 公开基准")
    ap.add_argument("--datasets", default="",
                    help="逗号分隔的数据集名，留空=全部")
    ap.add_argument("--budgets", default="",
                    help="逗号分隔的 token 预算，留空用默认档位")
    ap.add_argument("--levels", default="",
                    help="传给本项目的级别过滤，逗号分隔；留空=工具默认")
    ap.add_argument("--no-drain3", action="store_true", help="跳过 Drain3 对照")
    args = ap.parse_args()

    names = [n.strip() for n in args.datasets.split(",") if n.strip()] or None
    budgets = ([int(b) for b in args.budgets.split(",") if b.strip()]
               or DEFAULT_BUDGETS)

    datasets = discover(names)
    if not datasets:
        print("没有可用数据集。请先下载 Loghub 2.0 到 data/loghub/。")
        return 1

    print(f"Loghub 2.0 基准：{len(datasets)} 个数据集")
    print("数据来源：https://zenodo.org/record/8275861")
    print("标注依据：Loghub-2.0 (ISSTA'24) 人工标注 event template")

    for ds in datasets:
        try:
            run_one(ds, budgets, args.levels or None, not args.no_drain3)
        except Exception as exc:                     # 单个数据集失败不中断全局
            print(f"\n  {ds.name} 基准失败：{type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())