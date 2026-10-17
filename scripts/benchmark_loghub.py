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
import json
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
from log_ai_compressor.constants import DEFAULT_TOP_N, LEVEL_ORDER    # noqa: E402
from log_ai_compressor.export.reporters import (             # noqa: E402
    estimate_raw_tokens,
    estimate_tokens,
)

DATA_DIR = ROOT / "data" / "loghub"
# 两个根目录都要扫。`fetch_datasets.py` 解到 data/loghub_full/，
# 而历史上那批数据集在 data/loghub/ —— 两者一直并存，但 benchmark 过去
# 只认后者，于是**照 README 跑一遍必然撞上「没有可用数据集」**：
# 复现门槛又长在「知道怎么做」上，而不是长在「做不做得成」上。
# eval_rootcause.py 早就同时扫这两个根，这里跟它对齐。
DATA_DIRS = (DATA_DIR, ROOT / "data" / "loghub_full")

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
    """扫描 data/{loghub,loghub_full}/<Name>/<Name>/<Name>_full.log。

    两个根都扫、同名只取先命中的那个（见 DATA_DIRS 处的说明）。
    只有**真正收进来**的才记进 seen —— 否则一个坏副本会把另一个根里的
    好数据挡掉，而报错信息只会说「标注事件类型为 0」，看不出是被遮蔽了。
    """
    roots = [d for d in DATA_DIRS if d.is_dir()]
    if not roots:
        raise SystemExit(
            f"找不到数据集目录：{' 或 '.join(str(d) for d in DATA_DIRS)}\n"
            f"请先跑 python scripts/fetch_datasets.py --all")
    out: List[Dataset] = []
    seen: Set[str] = set()
    for root in roots:
        for log_path in sorted(root.glob("*/*/*_full.log")):
            name = log_path.name[: -len("_full.log")]
            if name in seen:
                continue
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
            seen.add(name)
            out.append(ds)
    return sorted(out, key=lambda d: d.name)


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


# LogsLop 跳过标记形如 "  ...2...3"（两空格 + 省略号 + 两组行号）
_SKIP_MARK = re.compile(r"^\s*\.\.\.\d+\.\.\.\d+\s*$")


def method_logslop(ds: Dataset, budget_tokens: int) -> Tuple[str, Set[str]]:
    """AMD LogsLop（2026-07 ROCm 博客开源，pip install logslop）：逐行 Jaccard
    去重，只保留每个簇的首见行，其余折叠成 ``...N...M`` 跳过标记。

    喂**原始日志行**而不是剥离过的 Content 字段 —— 这是它自己文档与博客里的
    标准用法（Drain 要剥离结构化头是因为它的模板抽取需要，对照时给各自应得的
    输入才叫公平）。

    与 Drain3 的关键差别：LogsLop 保留的是**原始行**，所以能按 LineId 精确回查，
    映射口径与 tail / grep / 本项目一致，不存在模板相似度误判。
    """
    try:
        import logslop
    except ImportError:
        return "", set()
    # 生成器按原顺序吐出「首见行」与「跳过标记」，跳过标记不占原始行号，
    # 因此只能靠「数到第几条被保留的行」反推它是原文第几行（LineId 为 1-based）。
    kept: List[Tuple[str, int]] = []
    src = 0
    for out in logslop.process(ds.raw_lines):
        if _SKIP_MARK.match(out):
            continue
        src += 1
        kept.append((out, src))

    rows: List[str] = []
    covered: Set[str] = set()
    spent = 0
    for text, lid in kept:
        cost = estimate_tokens(text) + 1
        if spent + cost > budget_tokens:
            break
        spent += cost
        rows.append(text)
        eid = ds.event_of_line_no(lid)
        if eid:
            covered.add(eid)
    return "\n".join(rows), covered


def sweep_logslop(ds: Dataset, budgets: List[int]) -> List[Tuple[int, int, int]]:
    out = []
    for b in budgets:
        text, cov = method_logslop(ds, b)
        out.append((b, estimate_tokens(text), len(cov)))
    return out


def _clusters_for_budget(result, budget_tokens: int, n_eff: int) -> int:
    """在 token 预算内最多能放多少个簇。

    做法：用已经测出的 (top_n=n_eff, 实测 tokens) 反推「每簇边际成本」与
    「固定开销」，再解出预算内的最大簇数。**不重新分析** —— 分析是贵的，
    导出是便宜的。

    为什么需要它：产品默认 `top_n=20` 对复杂日志是**明显的浪费** ——
    Loghub 上 32k 预算里我们只用掉 682~1,318 token，剩下的全扔了，而
    Drain3 在同一预算下用掉 10,051（HealthApp 甚至 566,802）。
    README 宣称「同样 token 预算下」对比，而代码从来没按预算分配过。
    """
    total_clusters = len(result.clusters)
    if total_clusters <= n_eff:
        return total_clusters
    sample = service.export_text(result, "summary", top_n=n_eff)
    cost = max(estimate_tokens(sample), 1)
    # 每多一个簇的边际成本：整份报告除以簇数会低估（固定开销摊薄），用
    # 两个点做差分更准。退化时用「总额 / 簇数」兜底。
    half = max(n_eff // 2, 1)
    cost_half = estimate_tokens(service.export_text(result, "summary", top_n=half))
    marginal = max((cost - cost_half) // max(n_eff - half, 1), 1)
    fixed = max(cost - marginal * n_eff, 0)
    room = max(budget_tokens - fixed, 0)
    return max(min(total_clusters, room // marginal), n_eff)


def method_ours(ds: Dataset, budget_tokens: int,
                levels: Optional[str] = None,
                similarity: str = "standard",
                top_n: Optional[int] = None) -> Tuple[str, Set[str], int]:
    """本项目：压缩摘要报告 + 证据卡片。

    证据保留口径 = **簇样例保留的原始行**对应的标注事件。
    样例行是未改动的原始日志（LogEntry.raw），可用 line_no 精确回查，
    因此不会因为我们的占位符重写而虚高或虚低。

    **返回的覆盖率与返回的文本必须是同一个对象** —— 这条是本函数存在的理由。
    之前这里有两个错叠在一起：

    1. 调用方传 `top_n: 0`，而 pipeline 的 `top_n if top_n else DEFAULT`
       把 **0 当成 falsy** 回落到 20。也就是说「不限簇数」根本没生效，
       报告里始终只有前 20 个簇。
    2. 覆盖率却按 `result.clusters` **全部**统计。

    结果是**指标在数用户根本没收到的东西**：严格档在 OpenSSH 上切出 1,831 个
    簇、覆盖率因此报到 100%，可报告里只有 20 个 —— 809 个 token 装不下
    1,831 行。那不是保住证据，那是把证据堆在内存里。

    所以这里显式分开两个口径，都返回：
      · 交付口径（top_n 取产品默认）—— **主指标**，用户真拿到的那 N 个簇
      · 全量口径（top_n=None 落到默认 / 传大数）—— 聚类质量上界
    两者不可混说，报告里也必须分别标注。

    返回 (文本, 覆盖事件集合, 实际生效的簇数上限)。
    """
    level_list = (levels.split(",") if levels else None)
    n_eff = top_n if top_n else DEFAULT_TOP_N
    # 分析一次就够（分析贵、导出便宜）：用 top_n=10^9 把所有簇都建出来，
    # 之后想按哪个口径导出/统计都只是切片。
    result = service.analyze(
        paths=[str(ds.log_path)],
        params={"levels": level_list, "top_n": 10 ** 9, "context_lines": 0,
                "similarity": similarity},
    )
    n_use = _clusters_for_budget(result, budget_tokens, n_eff) \
        if top_n is None else min(n_eff, len(result.clusters))
    shown = result.clusters[:n_use]
    text = service.export_text(result, "summary", top_n=n_use)
    covered: Set[str] = set()
    for c in shown:
        sample = getattr(c, "sample", None)
        if sample is None or sample.entry is None:
            continue
        eid = ds.event_of_line_no(sample.entry.line_no)
        if eid:
            covered.add(eid)
    return text, covered, n_use


# ---------------------------------------------------------------------------
# 全级别集合：把工具从「错误分诊」模式切到「全量摘要」模式。
# 两者都要报 —— 只报对自己有利的那一个是不诚实的。
#
# **必须从 constants.LEVEL_ORDER 派生，不能手写。** 曾经手写过一份：
#   ["TRACE","DEBUG","INFO","WARN","WARNING","ERROR","FATAL","CRITICAL"]
# 它有两处错，而且都静默生效：
#
#   1. **漏了 FAIL** —— 而 FAIL 是规范级别里真实的一档
#      （constants.LEVEL_ORDER = ERROR/FAIL/WARN/INFO/DEBUG/TRACE）。
#      `Failed password for root ...` 这类行按 level_hints 被判成 FAIL，
#      于是被过滤掉 —— 结果是**「全级别」比默认的 ERROR/FAIL 还窄**，
#      传全级别拿到的簇数少于什么都不传。
#   2. 收了 WARNING / FATAL / CRITICAL 三个**别名**。它们经 LEVEL_ALIASES
#      归一后是 WARN / ERROR / ERROR，永远不会作为 entry.level 出现 ——
#      纯死配置，白占三个格子还让人以为覆盖了它们。
#
# 实测代价：Loghub OpenSSH 上约 43 万行（占 68%）被判 FAIL 或无级别而
# 整批丢掉，该数据集的保留率被严重低估。本脚本曾据此把结论写成
# 「OpenSSH 输给 Drain3」—— 那是**测量工具的错，不是产品的错**。
#
# CLI 侧早就有同样的防护：cli._parse_levels 用 canonical_level 归一，
# 所以 `--level WARNING` 能正常工作。漂移只发生在这份手写列表上。
ALL_LEVELS = list(LEVEL_ORDER)

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


def delivery_ceiling(n_shown: int, total_events: int) -> float:
    """交付口径的**物理上限**（百分比）。

    产品默认只给 `n_shown` 个簇，而人工标注事件有 `total_events` 种，
    于是证据保留率最多 `min(n_shown, total_events) / total_events`。

    **这个上限与聚类算法好坏无关** —— 它只反映「报告里放得下几个簇」。
    Mac 有 626 种事件、交付口径给 20 个簇，天花板就是 3.2%，实测也是 3.2%：
    那一栏低是天花板低，不是「没做到位」。

    报达成度（覆盖率 / 上限）是为了让读者自己看出这件事；不给上限的话，
    「1 胜 9 负」会被读成「算法输了 9 个」，而它不是这个意思。
    """
    if not total_events:
        return 0.0
    return min(n_shown, total_events) / total_events * 100


def verdict(our_n: int, their_n: int, total: int) -> str:
    """按**计数**判胜负，不拿四舍五入后的百分数比大小。

    为什么必须这样：v2.9.6 之前比的是 `92.105... > 92.1`，把一个
    **0.005 个百分点的舍入差判成了「胜」**。OpenSSH 两边其实都是
    35/38，是平手。

    报告保留 1 位小数，于是**小于半个末位（0.05pp）的差无法与舍入误差
    区分** —— 落在容差里的一律平手。只要比较落在计数上，歧义就不存在。
    """
    eps = total * 0.0005          # 0.05pp 换算成计数
    if our_n > their_n + eps:
        return "胜"
    if our_n < their_n - eps:
        return "负"
    return "平"


def run_one(ds: Dataset, budgets: List[int], levels: Optional[str],
             use_drain: bool, use_logslop: bool = True) -> Dict[str, object]:
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
    lop_text, lop_cov = method_logslop(ds, 10 ** 9) if use_logslop else ("", set())
    t_lop = time.time() - t0

    lv_all = ",".join(ALL_LEVELS) if not levels else levels
    # 同预算档与 tail/grep @32000tok 对齐：同一个数字，谁也别占便宜
    BUDGET = max(DEFAULT_BUDGETS)

    t0 = time.time()
    our_def_text, our_def_cov, n_def = method_ours(
        ds, BUDGET, levels, top_n=DEFAULT_TOP_N)
    t_our_def = time.time() - t0

    t0 = time.time()
    our_del_text, our_del_cov, n_del = method_ours(
        ds, BUDGET, lv_all, top_n=DEFAULT_TOP_N)
    t_our_del = time.time() - t0

    t0 = time.time()
    our_bud_text, our_bud_cov, n_bud = method_ours(ds, BUDGET, lv_all)
    t_our_bud = time.time() - t0

    t0 = time.time()
    our_full_text, our_full_cov, n_full = method_ours(
        ds, BUDGET, lv_all, top_n=10 ** 9)
    t_our_full = time.time() - t0

    # 每行必须用**自己那一份** text/cov —— 曾经这里四个口径全指向同一对变量，
    # 于是「交付口径」和「全量口径」打印出同一个数字，看着像两行其实一行。
    rows: List[Tuple[str, str, str, str, str]] = []
    # 下面这张表要拿覆盖率跟 Drain3/LogsLop 逐档比，所以必须先确认**每一行
    # 的覆盖率都没有超过这一行自己展示的数量**。两个基线的 n_shown 就是它们
    # 覆盖数本身（报告里逐事件列模板），断言对它们恒真；ours 的 n_shown 是真正
    # 生效的簇数，断言对它有意义 —— 将来谁把变量接错行，这里当场响。
    for label, text, cov, secs, n_shown in (
            (f"log-ai-compressor 默认级别(排障, top{n_del})",
             our_def_text, our_def_cov, t_our_def, n_def),
            (f"log-ai-compressor 全级别(交付口径 top{n_del})",
             our_del_text, our_del_cov, t_our_del, n_del),
            (f"log-ai-compressor 全级别(同预算 {BUDGET}tok, top{n_bud}) ←主指标",
             our_bud_text, our_bud_cov, t_our_bud, n_bud),
            (f"log-ai-compressor 全级别(全量口径·聚类上界 top{n_full})",
             our_full_text, our_full_cov, t_our_full, n_full),
            ("Drain3（ICWS'17, 821★）", drain_text, drain_cov, t_drain,
             len(drain_cov)),
            ("LogsLop（AMD 2026-07）", lop_text, lop_cov, t_lop, len(lop_cov)),
    ):
        if not text and not cov:
            continue
        n_tok = estimate_tokens(text)
        assert len(cov) <= n_shown, (
            f"{label}：覆盖率报了 {len(cov)} 种事件，"
            f"超过这一行展示的 {n_shown} 个 —— 指标在数报告里没出现的东西")
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

    # ---- 对照判定：三个口径各比一次，输赢按事件种数算 ----
    # 不比四舍五入后的百分数。报告只有 1 位小数，0.05pp 以内的差和舍入误差
    # 分不开，拿它判胜负等于让显示精度参与裁决（v2.9.5 就这么把平手判成了胜）。
    scopes = (("交付口径 top%d" % n_del, our_del_cov),
              ("同预算 %dtok" % BUDGET, our_bud_cov),
              ("全量口径 top%d" % n_full, our_full_cov))
    print()
    print("  对照判定（按事件种数比；差 <0.05pp 记平 —— 落在报告舍入精度以内）")
    print(f"  {'本项目口径':<22}{'vs Drain3':>12}{'vs LogsLop':>14}")
    print("  " + "-" * 48)
    for scope, cov in scopes:
        vd = (verdict(len(cov), len(drain_cov), total_events)
              if use_drain else "—")
        vl = (verdict(len(cov), len(lop_cov), total_events)
              if use_logslop else "—")
        detail = (f"  ({len(cov)} vs {len(drain_cov)}/{len(lop_cov)} 种)"
                  if use_drain and use_logslop else "")
        print(f"  {scope:<22}{vd:>12}{vl:>14}{detail}")

    # ---- 交付口径的物理上限与达成度 ----
    # 为什么要有这段：交付口径那一栏对 Drain3 是「1 胜 9 负」，但那个数字
    # **极易被误读成「算法输了 9 个」**。它其实量的是「产品给多少个簇」：
    # 报告只装 n_del 个簇，而标注事件有 total 种，于是覆盖率的上限就是
    # min(n_del, total)/total —— **这个上限跟聚类算法好坏无关**。
    #
    # Drain3 没有这个旋钮（它把有多少模板就吐多少），所以两栏根本不是
    # 同一条起跑线 —— 这也是为什么主指标是「同预算 32k」而不是交付口径。
    #
    # 报「达成度」= 覆盖率 / 上限，是为了让读者一眼看出：这一栏低是因为
    # 天花板低，不是因为我们没做到位。Zookeeper / Hadoop / Mac 三个是
    # 顶格 100% —— 一个簇不多给就已经摸到上限。
    cap_n = min(n_del, total_events)
    ceiling = delivery_ceiling(n_del, total_events)
    got = len(our_del_cov) / total_events * 100 if total_events else 0.0
    print()
    print(f"  交付口径的物理上限：min({n_del}, {total_events})/{total_events} "
          f"= {ceiling:.1f}%   （产品只给 {n_del} 个簇，与算法无关）")
    print(f"  实测 {got:.1f}%  ->  达成度 "
          f"{(len(our_del_cov) / cap_n * 100) if cap_n else 0:.0f}%")

    print()
    print(f"  朴素基线在同预算下的保留率曲线（事件类型 {total_events} 种）")
    print(f"  {'预算(tokens)':>14}{'tail 保留':>12}{'grep 保留':>12}"
          f"{'LogsLop 保留':>14}{'本项目(全级别)':>16}")
    print("  " + "-" * 70)
    st = dict((b, c) for b, _, c in sweep_tail(ds, budgets))
    sg = dict((b, c) for b, _, c in sweep_grep(ds, budgets))
    sl = dict((b, c) for b, _, c in sweep_logslop(ds, budgets)) if use_logslop else {}
    for b in budgets:
        # 本项目**不在这条曲线上**：它是固定一次分析 + 按预算导出，不是每个
        # 预算重新分析一次。之前这里在每一行都抄同一个数，读者横着一看会以为
        # 「500 tok 时我们也有 X%」—— 交付口径本身就要 682~1,318 token。
        # 所以只在预算真正等于 BUDGET 的那一行给数，其余行给 —。
        ours_at = pct(len(our_bud_cov), total_events) if b == BUDGET else "—"
        print(f"  {b:>14,}{pct(st[b], total_events):>12}"
              f"{pct(sg[b], total_events):>12}"
              f"{(pct(sl[b], total_events) if use_logslop else '—'):>14}{ours_at:>16}")

    # 返回结构化结果，供 main() 汇总进基线文件（--write-baseline）。
    # 百分比一律存**比率**而不是百分数：0.811 和 81.1 在 JSON 里都是合法
    # 浮点，但只有前者能拿来做容差比较 —— README 写的是一位小数，
    # 对账时必须允许"同一数字的不同写法"通过，否则门禁自己就成了噪声源。
    return {
        "events": total_events,
        "delivery": len(our_del_cov) / total_events if total_events else 0.0,
        "budget32k": len(our_bud_cov) / total_events if total_events else 0.0,
        "full": len(our_full_cov) / total_events if total_events else 0.0,
        "clusters": n_full,
        "n_delivery": n_del,
        "n_budget": n_bud,
        "drain3": (len(drain_cov) / total_events
                   if (use_drain and drain_cov and total_events) else None),
        "logslop": (len(lop_cov) / total_events
                    if (use_logslop and lop_cov and total_events) else None),
        "verdict_budget": (verdict(len(our_bud_cov), len(drain_cov), total_events)
                           if use_drain else None),
        "verdict_delivery": (verdict(len(our_del_cov), len(drain_cov), total_events)
                             if use_drain else None),
        "verdict_full": (verdict(len(our_full_cov), len(drain_cov), total_events)
                         if use_drain else None),
        "verdict_budget_logslop": (verdict(len(our_bud_cov), len(lop_cov),
                                           total_events) if use_logslop else None),
        "verdict_delivery_logslop": (verdict(len(our_del_cov), len(lop_cov),
                                             total_events) if use_logslop else None),
        "verdict_full_logslop": (verdict(len(our_full_cov), len(lop_cov),
                                         total_events) if use_logslop else None),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Loghub 2.0 公开基准")
    ap.add_argument("--datasets", default="",
                    help="逗号分隔的数据集名，留空=全部")
    ap.add_argument("--budgets", default="",
                    help="逗号分隔的 token 预算，留空用默认档位")
    ap.add_argument("--levels", default="",
                    help="传给本项目的级别过滤，逗号分隔；留空=工具默认")
    ap.add_argument("--no-drain3", action="store_true", help="跳过 Drain3 对照")
    ap.add_argument("--no-logslop", action="store_true",
                    help="跳过 AMD LogsLop 对照（未装 logslop 时可显式关掉）")
    ap.add_argument("--write-baseline", action="store_true",
                    help="把本次实测结果写进 docs/benchmark_baseline.json")
    args = ap.parse_args()

    # 写基线的前置条件**先查**，别跑完两小时才发现白跑。
    #
    # 为什么这么严：基线一旦不完整，`claim_benchmark_table` 就会拿残缺的
    # 基线去判 README —— 没跑到的数据集要么被当成没这回事，要么反过来
    # 把「这次没测」固化成「产品就是这样」。两种都是把缺失误写成事实。
    if args.write_baseline:
        if args.no_drain3 or args.no_logslop:
            print("拒绝写基线：基线要含 Drain3 / LogsLop 对照臂，"
                  "缺了就没法对账胜负。去掉 --no-drain3 / --no-logslop 再跑。")
            return 1
        if args.datasets.strip():
            print("拒绝写基线：只跑了子集。基线必须覆盖全部 10 个数据集，"
                  "否则没测到的会被当成测过的。去掉 --datasets 再跑。")
            return 1

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

    records: Dict[str, object] = {}
    for ds in datasets:
        try:
            rec = run_one(ds, budgets, args.levels or None, not args.no_drain3,
                          not args.no_logslop)
        except Exception as exc:                     # 单个数据集失败不中断全局
            print(f"\n  {ds.name} 基准失败：{type(exc).__name__}: {exc}")
            continue
        if rec is not None:
            records[ds.name] = rec

    if args.write_baseline:
        missing = [d.name for d in datasets if d.name not in records]
        if missing:
            print(f"\n  拒绝写基线：{missing} 没跑成功。"
                  "基线必须覆盖全部数据集，否则没测到的会被当成测过的。")
            return 1
        out = ROOT / "docs" / "benchmark_baseline.json"
        out.write_text(json.dumps({
            "_comment": ("由 scripts/benchmark_loghub.py --write-baseline 生成；"
                         "scripts/prove.py 的 claim_benchmark_table 用它对账 "
                         "README 的基准表。改过任何影响聚类的代码后必须重跑。"),
            "budget": max(budgets),
            "datasets": records,
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\n  基线已写入 {out}")
        print("  接着按新数字回填 README / docs/BENCHMARK.md，"
              "然后跑 python scripts/prove.py 对账。")

    return 0


if __name__ == "__main__":
    sys.exit(main())