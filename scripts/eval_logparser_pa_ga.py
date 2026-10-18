#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 Loghub / LogParser 的标准解析协议（PA / GA）评测本项目的聚类。

**这个脚本为什么存在**
-------------------
v2.11.4 的 `docs/BENCHMARK.md` 里的覆盖率，全部是**本项目自己定义的指标**：
「用户真收到的那 `DEFAULT_TOP_N` 个簇，按簇样例行去查 Loghub 的 EventId」。

它有两个问题，评审一定会问：

1. **口径是自己定的**。「簇样例覆盖了多少种事件」不是任何公认指标的名字，
   换个定义数字就变，所以它不能用来回答「你的聚类好不好」。
2. **从来没被外部指标量过**。「换个不是你设计的指标，你还敢跑吗」——
   在 v2.11.4 之前，答案是**没跑过**。

PA / GA 是 LogParser（Jiang et al., ICSE-SEIP 2019）定的两个指标，
Drain / Drain3 / LILAC / LLMParser / LibreLog 全部用它们，本项目此前是
唯一一个说自己「做日志分析」却没用过它们的。

**口径来源（三份，都实读过，不是转述）**
----------------------------------------
* **发源地**：`logpai/logparser` 的 `logparser/utils/evaluator.py`，
  commit `10e31f3a1ff2bb783508a27efb24eb868b97287d`。它的 `get_accuracy()`
  是这套指标最早的公开实现，本脚本的 `grouping_accuracy()` 就是它的
  纯 stdlib 等价移植（只保留 accuracy，丢掉基于组合数的 P/R/F1）。
* **当前在用的实现**：`zeyang919/LibreLog` 的 `parser/accuracy.py`，
  commit `36c8a45d6e7d1f6486f88767ca02dc3729463fb6`。它的 `get_accuracy()`
  与上面那份**逐行同构**（除了把 P/R/F1 三个占位成 0），并且额外把
  「模板相等」的 PA 拆了出来。两份互相独立地给出同一个公式 ——
  这就是「交叉核实」，而不是「读了一份就信」。
* **公式表述**：LibreLog 论文 arXiv:2408.01585。

**一个必须写下来的命名坑**
------------------------
`logpai/logparser` 的 `evaluate()` 把 `get_accuracy()` 的返回值打印成
`Parsing_Accuracy`（`evaluator.py:46`），而它算的其实是**分组**对不对；
PA 在那一版里根本没实现。所以同一个值在两个仓库里叫两个名字。
本脚本按 LibreLog 的命名（GA = 分组、PA = 模板），**并在这里留下出处**，
免得下一个照着 `logparser` 读代码的人以为这里标错了。

**GA 与 PA 的定义**
------------------
* **GA（Grouping Accuracy）= 被正确归组的行数 / 有标注的总行数**。
  一组「被正确归组」的条件有两个，缺一不可：
  (a) 组内所有行在地真值里属于**同一个** EventId；
  (b) 该组包含这个 EventId 的**全部**出现。
  少了 (b) 就不是「把一个事件聚成一簇」，而是「聚了半截」。
* **PA（Parsing Accuracy）= 模板与地真值模板一致的行数 / 有标注的总行数**。

两个分母都是**有标注的总行数**（EventId 非空的行），所以「漏掉的行」自动
算错，不另外做口径。这也是两份实现的原样：LogParser 先剔掉无标注行再算，
LibreLog 的预测侧 CSV 是按地真值 `Content` 左连接出来的，没解析出的行模板
为空、与地真值不等，自然计入分母。

**协议适配（必须写明，否则数字会被误读）**
----------------------------------------
本项目**不是逐行 parser，是流式聚类器**。要接上逐行的 PA/GA，有三处
本项目特有的适配，全部列在这里，不藏：

| # | Loghub 标准 | 本项目 | 为什么 |
| --- | --- | --- | --- |
| A | 每行产出一个正则模板 | 每行产出**所在簇的静态文本模板** | 本项目不做逐行正则抽取，聚类的产物就是「哪些行属于同一类」 |
| B | 模板形如 `Failed to connect to (.*)` | 掩码占位符 `U/H/S/P/N` 折叠成 `<*>` | Loghub 地真值用 `<*>`，要做字符串相等就必须让两侧同形 |
| C | 预测侧按正则模板清洗（`clean_regex_content`） | **两侧共用同一个** `clean_content` | LibreLog 那套清洗是**非对称**的（正则侧只留非字母段），对不含正则的模板没有意义。共用一个函数是最保守的选择：不可能因为清洗规则偏向哪一侧而抬高某一边的分数 |

C 是最重要的一条：**只要两侧用同一个归一化函数，任何一方都不可能靠
「洗得干净」取得优势**。这也是为什么本脚本不抄 `clean_regex_content`。

**分母里的「漏」不是 bug，是要报出来的事实**
------------------------------------------
本项目的解析器会把折行 / 堆栈并成一个条目，于是这些行**没有自己的模板**。
在 PA/GA 口径下它们就是错的。这里不做任何补偿，只把数量报出来
（`未独立成行` 一列），让读者自己判断这个分数被压了多少。

用法
----
    python scripts/eval_logparser_pa_ga.py                      # 全部数据集
    python scripts/eval_logparser_pa_ga.py --datasets HPC,Apache
    python scripts/eval_logparser_pa_ga.py --csv docs/logparser_pa_ga.csv
    python scripts/eval_logparser_pa_ga.py --dump Apache --dump-n 8
    python scripts/eval_logparser_pa_ga.py --self-check         # 口径自检

数据不在仓库里（`data/` 被 gitignore），先跑 `python scripts/fetch_datasets.py --all`。
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from log_ai_compressor.core.clustering import (            # noqa: E402
    ErrorClusterer, mask_text,
)
from log_ai_compressor.core.parser import LogParser       # noqa: E402
from log_ai_compressor.rules.engine import load_ruleset   # noqa: E402

from benchmark_loghub import discover                    # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 参考实现（改动本脚本的口径时，连同这两条一起改）
LOGPARSER_REF = (
    "https://github.com/logpai/logparser/blob/main/logparser/utils/evaluator.py"
    "@10e31f3a1ff2bb783508a27efb24eb868b97287d"
)
LIBRELOG_REF = (
    "https://github.com/zeyang919/LibreLog/blob/main/parser/accuracy.py"
    "@36c8a45d6e7d1f6486f88767ca02dc3729463fb6"
)


# ---------------------------------------------------------------------------
# 口径：两个函数都必须是 LibreLog 的逐行等价移植，改动前先看上面那行 ref
# ---------------------------------------------------------------------------
def clean_content(content: str) -> str:
    """Loghub 侧的模板归一化 —— LibreLog `clean_content()` 原样移植。

    去 `,` 与 `.`、按空格切、把含 `<*>` 的整段清空、再无分隔符拼回去。
    两侧**共用**这一个函数（见模块 docstring 的适配 C）。
    """
    content = content.replace(",", "").replace(".", "")
    segments = content.split(" ")
    return "".join("" if "<*>" in seg else seg for seg in segments)


def grouping_accuracy(
    gt_event: Dict[int, str],
    pred_group: Dict[int, str],
    total_lines: int,
) -> Tuple[float, int]:
    """GA —— LibreLog `get_accuracy()` 的纯 stdlib 等价移植。

    参数：
        gt_event:    LineId(1-based) -> 地真值 EventId
        pred_group:  LineId(1-based) -> 预测侧分组标签（本项目用 cluster_id）
        total_lines: 标注总行数（两个准确率的分母）

    返回：(GA, 被正确归组的行数)

    注意 `total_lines` 用的是**标注总行数**，不是 `len(gt_event)`：
    LibreLog 那边分母是 `series_groundtruth.size`，即地真值 CSV 的行数。
    """
    if total_lines <= 0:
        return 0.0, 0
    gt_total = Counter(gt_event.values())
    groups: Dict[str, List[int]] = {}
    for line_no, group in pred_group.items():
        groups.setdefault(group, []).append(line_no)

    accurate = 0
    for log_ids in groups.values():
        events = {gt_event[ln] for ln in log_ids if ln in gt_event}
        if len(events) != 1:                       # 条件 (a)：组内必须同一个事件
            continue
        only = next(iter(events))
        if len(log_ids) != gt_total[only]:         # 条件 (b)：且必须是全部出现
            continue
        accurate += len(log_ids)
    return accurate / total_lines, accurate


def parsing_accuracy(
    gt_template: Dict[int, str],
    pred_template: Dict[int, str],
    total_lines: int,
) -> Tuple[float, int]:
    """PA —— LibreLog `evaluate_result()` 里 PA 那一段的纯 stdlib 等价移植。

    两侧都过 `clean_content()` 后逐行字符串相等即算对。
    没有模板的行（`pred_template` 里查不到）自动算错，留在分母里。
    """
    if total_lines <= 0:
        return 0.0, 0
    correct = 0
    for line_no, template in gt_template.items():
        mine = pred_template.get(line_no)
        if mine is None:
            continue
        if clean_content(mine) == clean_content(template):
            correct += 1
    return correct / total_lines, correct


# ---------------------------------------------------------------------------
# 本项目侧：把流式聚类的产物摊成「每行一个模板 / 一个分组」
# ---------------------------------------------------------------------------
# mask_text 的占位符：UUID->U / 十六进制->H / 0x 地址->0xH / 引号串->S /
# 路径->P / 数字->N。Loghub 的地真值用 `<*>`，所以把一段连续的占位符
# （含 IP 那种 N.N.N.N）折叠成**一个** `<*>` —— 地真值也是整个 IP 一个 `<*>`。
_PLACEHOLDER_RUN = re.compile(r"(?:\b0xH\b|\b[NHUPS]\b)(?:\.(?:\b[NHUPS]\b))*")


def entry_template(message: str) -> str:
    """一条消息 → Loghub 形状的静态文本模板（占位符折叠成 `<*>`）。

    用 `message` 而不是 `full_message`：Loghub 的评测单位是**单行**，
    折行部分由 `last_line_no` 另外统计（见 `unparsed_lines`）。
    """
    return _PLACEHOLDER_RUN.sub("<*>", mask_text(message))


def run_drain(ds) -> Optional[dict]:
    """Drain3 对照臂 —— 同一份 Content、同一套 PA/GA、同一份地真值。

    没有对照臂的话，「PA 61.71%」这个数字**无从判断高低**：读者只能拿它
    和论文里别人在自己机器上跑出来的数字比，而那些数字连 Drain 版本都
    不一定一样。同一台机器、同一份输入、同一段口径下的两条曲线才是
    可以比较的。

    三处与本项目臂刻意保持一致：
      * 喂 `Content` 列（raw 那一列只对本项目有意义：对照臂没有「从
        原始日志里剥头部」这回事）；
      * 分母是 EventId 非空的标注行数；
      * 模板取**簇的最终模板**（Drain 边跑边改模板，用最终模板才是各家
        论文报的那个数；本项目臂取的是簇的最终模板，同一口径）。
    """
    try:
        from drain3.drain import Drain
    except ImportError:
        return None

    drain = Drain()
    pred_group: Dict[int, str] = {}
    line_cluster: Dict[int, object] = {}
    for lid in sorted(ds.line_content):
        try:
            cluster, _state = drain.add_log_message(ds.line_content[lid])
        except Exception:                      # noqa: BLE001 —— 与主基准同一口径
            continue
        pred_group[lid] = str(cluster.cluster_id)
        line_cluster[lid] = cluster
    pred_template = {lid: c.get_template() for lid, c in line_cluster.items()}

    gt_event = dict(ds.line_event)
    gt_template = {lid: ds.event_template[eid]
                   for lid, eid in gt_event.items() if eid in ds.event_template}
    total_lines = annotated_lines(ds)
    ga, ga_lines = grouping_accuracy(gt_event, pred_group, total_lines)
    pa, pa_lines = parsing_accuracy(gt_template, pred_template, total_lines)
    return {
        "dataset": ds.name,
        "method": "drain3",
        "source": "content",
        "annotated_lines": total_lines,
        "gt_events": len(ds.events),
        "entries": len(pred_template),
        "lines_unparsed": total_lines - len(pred_template),
        "clusters": len(drain.clusters),
        "PA": round(100.0 * pa, 2),
        "PA_correct": pa_lines,
        "GA": round(100.0 * ga, 2),
        "GA_lines": ga_lines,
    }


def annotated_lines(ds) -> int:
    """**EventId 非空**的标注行数 —— 分母。

    按 `logpai/logparser` 的 `logparser/utils/evaluator.py::evaluate()` 的
    做法：它先 `df_groundtruth[~df_groundtruth["EventId"].isnull()]` 把没有
    标注的行剔掉，**再**算 accuracy，所以分母是「有标注的行」而不是
    「CSV 的行数」。

    实测当前 9 个带标注的数据集里 EventId 空值为 0 行（`Android_2k` 无标注，
    根本不进这个脚本），所以这一条今天不改变任何数字 —— 但它写在代码里，
    是为了下一个带部分标注的数据集不会**悄悄**换分母。

    刻意**不去碰** `benchmark_loghub.py` 的 `Dataset`：`run_one()` 被
    `prove.py` 的十行基准表逐格对账牵着，加字段要连带重跑全量基准。
    """
    for cand in (Path(ds.log_path).parent / f"{ds.name}_full.log_structured.csv",
                 Path(ds.log_path) / f"{ds.name}_full.log_structured.csv"):
        if not cand.is_file():
            continue
        with cand.open(encoding="utf-8", errors="replace", newline="") as fh:
            return sum(1 for row in csv.DictReader(fh)
                       if (row.get("EventId") or "").strip())
    raise FileNotFoundError("%s: 找不到 *_full.log_structured.csv" % ds.name)


def run_dataset(ds, source: str = "raw") -> dict:
    """跑一遍本项目的解析 + 聚类，算出 PA / GA 与相关分解。

    参数：
        source: ``"raw"`` 喂 ``<Name>_full.log`` 的原始行（用户真给我们的东西）；
                ``"content"`` 喂 Loghub 标注里的 ``Content`` 列
                （= 剥掉时间戳/主机/级别后的正文，Drain 官方文档要求喂给它的
                那种输入，也就是 PA/GA 协议里大家真正在比的那个）。
    """
    if source not in ("raw", "content"):
        raise ValueError("source 只能是 raw / content，收到 %r" % (source,))
    parser = LogParser(load_ruleset("generic"))
    clusterer = ErrorClusterer()

    pred_template: Dict[int, str] = {}
    pred_group: Dict[int, str] = {}
    entries = 0
    covered_lines = 0

    def feed(entry) -> None:
        nonlocal entries, covered_lines
        entries += 1
        cluster, _replaced, _inst = clusterer.add(entry)
        pred_template[entry.line_no] = entry_template(entry.message)
        pred_group[entry.line_no] = str(cluster.cluster_id)
        covered_lines += max(1, entry.last_line_no - entry.line_no + 1)

    if source == "raw":
        with ds.log_path.open(encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                entry = parser.feed(line.rstrip("\n"), line_no)
                if entry is not None:
                    feed(entry)
    else:
        for line_no in sorted(ds.line_content):
            entry = parser.feed(ds.line_content[line_no], line_no)
            if entry is not None:
                feed(entry)
    if hasattr(parser, "flush"):
        tail = parser.flush()
        if tail is not None:
            feed(tail)

    gt_event = dict(ds.line_event)
    gt_template = {
        lid: ds.event_template[eid]
        for lid, eid in gt_event.items()
        if eid in ds.event_template
    }
    total_lines = annotated_lines(ds)
    ga, ga_lines = grouping_accuracy(gt_event, pred_group, total_lines)
    pa, pa_lines = parsing_accuracy(gt_template, pred_template, total_lines)

    return {
        "dataset": ds.name,
        "method": "ours",
        "source": source,
        "annotated_lines": total_lines,
        "gt_events": len(ds.events),
        "entries": entries,
        "lines_parsed": len(pred_template),
        "lines_unparsed": total_lines - len(pred_template),
        "clusters": len(clusterer.clusters),
        "PA": round(100.0 * pa, 2),
        "PA_correct": pa_lines,
        "GA": round(100.0 * ga, 2),
        "GA_lines": ga_lines,
        "_gt_event": gt_event,
        "_gt_template": gt_template,
        "_pred_template": pred_template,
        "_pred_group": pred_group,
    }


# ---------------------------------------------------------------------------
# 自检：口径函数必须在一个能手算的样例上给出手算值
# ---------------------------------------------------------------------------
def self_check() -> int:
    ok = True

    def expect(label: str, got, want) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print("  %-52s %-14s %s" % (label, repr(got),
                                    "OK" if good else "期望 " + repr(want)))

    print("clean_content（LibreLog 原样移植）")
    expect("去 , 和 .", clean_content("a,b.c"), "abc")
    expect("含 <*> 的整段清空", clean_content("Failed to <*> now"), "Failedtonow")
    expect("空格不保留", clean_content("a b  c"), "abc")

    print("grouping_accuracy")
    gt = {1: "E1", 2: "E1", 3: "E2", 4: "E2", 5: "E3"}
    # E3 的那一行压根没解析出来：E1、E2 完整命中 4 行，E3 整行丢掉
    expect("E1/E2 完整命中、E3 全漏", grouping_accuracy(
        gt, {1: "a", 2: "a", 3: "b", 4: "b"}, 5)[0], 0.8)
    # E2 只拿到一半 -> 条件 (b) 不成立，整组不计分（E3 整行漏掉，不得分）
    expect("E2 只拿到一半（条件 b 挡下）", grouping_accuracy(
        gt, {1: "a", 2: "a", 3: "b"}, 5)[0], 0.4)
    # 同一组少一行、但漏的是另一个事件：E3 那一组是完整的，照样得分
    expect("E2 缺一半、E3 完整 -> 3/5", grouping_accuracy(
        gt, {1: "a", 2: "a", 3: "b", 5: "c"}, 5)[0], 0.6)
    expect("全部命中", grouping_accuracy(gt, {1: "a", 2: "a", 3: "b", 4: "b", 5: "c"}, 5)[0], 1.0)
    # 混组：一个预测组里塞了两个地真事件 -> 整组不计
    expect("混组不计", grouping_accuracy(gt, {1: "a", 2: "a", 3: "a", 4: "a", 5: "a"}, 5)[0], 0.0)
    # 全空：解析器一个簇都没出
    expect("全漏", grouping_accuracy(gt, {}, 5)[0], 0.0)
    # 分母是标注总行数，不是预测侧行数
    expect("分母=标注总行数", grouping_accuracy(gt, {1: "a", 2: "a", 3: "b", 4: "b"}, 7)[0],
           4 / 7)

    print("parsing_accuracy")
    gt_t = {1: "Failed to connect to <*>", 2: "Failed to connect to <*>"}
    expect("模板全同", parsing_accuracy(gt_t, {1: "Failed to connect to <*>",
                                               2: "Failed to connect to <*>"}, 2)[0], 1.0)
    expect("漏一行", parsing_accuracy(gt_t, {1: "Failed to connect to <*>"}, 2)[0], 0.5)
    expect("清洗后仍相等", parsing_accuracy(
        {1: "a, b <*>"}, {1: "a b <*>"}, 1)[0], 1.0)
    expect("空模板算错", parsing_accuracy({1: "a"}, {1: ""}, 1)[0], 0.0)

    print()
    print("自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def dump(ds, row: dict, n: int) -> None:
    """打印前 n 行的「地真值模板 vs 本项目模板」，用来肉眼确认适配是否合理。"""
    print("\n■ %s（%s）逐行对照（前 %d 行）" % (ds.name, row["source"], n))
    shown = 0
    for lid in sorted(row["_gt_template"]):
        if shown >= n:
            break
        gt = row["_gt_template"][lid]
        mine = row["_pred_template"].get(lid)
        same = mine is not None and clean_content(mine) == clean_content(gt)
        shown += 1
        print("  L%-7d %-6s gt=%r" % (lid, "对" if same else "错", gt[:78]))
        print("  %14s ours=%r" % ("", (mine or "<无模板>")[:78]))


def _print_block(rows: List[dict], title: str) -> None:
    print("\n" + title)
    print("-" * 96)
    print("%-16s %9s %7s %8s %8s %10s %8s %8s"
          % ("数据集", "标注行", "事件", "条目", "簇", "未独立成行", "PA", "GA"))
    for r in rows:
        print("%-16s %9d %7d %8d %8d %10d %7.2f%% %7.2f%%"
              % (r["dataset"], r["annotated_lines"], r["gt_events"], r["entries"],
                 r["clusters"], r["lines_unparsed"], r["PA"], r["GA"]))
    if rows:
        print("%-16s %9s %7s %8s %8s %10s %7.2f%% %7.2f%%"
              % ("算术平均", "", "", "", "", "",
                 sum(r["PA"] for r in rows) / len(rows),
                 sum(r["GA"] for r in rows) / len(rows)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Loghub 标准解析协议（PA/GA）评测")
    ap.add_argument("--datasets", default="", help="逗号分隔；留空=全部")
    ap.add_argument("--csv", default="", help="把结果写到 CSV")
    ap.add_argument("--dump", default="", help="打印该数据集前若干行的模板对照")
    ap.add_argument("--dump-n", type=int, default=8)
    ap.add_argument("--dump-source", default="content", choices=("raw", "content"))
    ap.add_argument("--self-check", action="store_true",
                    help="只跑口径自检，不碰数据")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()

    names = [n.strip() for n in args.datasets.split(",") if n.strip()]
    datasets = discover(names or None)
    if not datasets:
        print("没有可用数据集。先跑 python scripts/fetch_datasets.py --all")
        return 2

    print("=" * 96)
    print("Loghub 标准解析协议（PA / GA）")
    print("口径来源（两份独立实现互相印证）：")
    print("  %s" % LOGPARSER_REF)
    print("  %s" % LIBRELOG_REF)
    print("适配：本项目是流式聚类器，模板取「所在簇的静态文本」，"
          "占位符折叠成 <*>，两侧共用同一个归一化函数")
    print("两种输入都测：raw=原始日志（用户真给的）；"
          "content=Loghub 标注的 Content 列（Drain 协议里大家比的那个）")
    print("=" * 96)

    all_rows: Dict[str, List[dict]] = {}
    for source in ("raw", "content"):
        rows: List[dict] = []
        for ds in datasets:
            print("\n■ %s [本项目/%s] …" % (ds.name, source), flush=True)
            r = run_dataset(ds, source)
            rows.append(r)
            print("   标注 %d 行 / %d 种事件 -> 条目 %d，簇 %d"
                  % (r["annotated_lines"], r["gt_events"], r["entries"],
                     r["clusters"]))
            print("   PA %6.2f%%  (%d / %d)    GA %6.2f%%  (%d 行)"
                  % (r["PA"], r["PA_correct"], r["annotated_lines"],
                     r["GA"], r["GA_lines"]))
        all_rows[source] = rows

    control: List[dict] = []
    for ds in datasets:
        print("\n■ %s [Drain3/content] …" % ds.name, flush=True)
        r = run_drain(ds)
        if r is None:
            print("   drain3 未安装，对照臂跳过（pip install drain3）")
            break
        control.append(r)
        print("   标注 %d 行 / %d 种事件 -> 条目 %d，簇 %d"
              % (r["annotated_lines"], r["gt_events"], r["entries"], r["clusters"]))
        print("   PA %6.2f%%  (%d / %d)    GA %6.2f%%  (%d 行)"
              % (r["PA"], r["PA_correct"], r["annotated_lines"], r["GA"],
                 r["GA_lines"]))

    _print_block(all_rows["raw"], "本项目 · 输入 = 原始日志行（用户真给我们的东西）")
    _print_block(all_rows["content"],
                 "本项目 · 输入 = Loghub 标注的 Content 列（Drain 协议可比口径）")
    if control:
        _print_block(control, "对照臂 · Drain3 0.9.11 · 同一份 Content、同一套口径")

    if args.dump:
        for r in all_rows[args.dump_source]:
            if r["dataset"] == args.dump:
                ds = next(d for d in datasets if d.name == args.dump)
                dump(ds, r, args.dump_n)
                break
        else:
            print("\n--dump %s 不在本次结果里" % args.dump)

    if args.csv:
        flat = all_rows["raw"] + all_rows["content"] + control
        cols = ("dataset", "method", "source", "annotated_lines", "gt_events",
                "entries", "clusters", "lines_unparsed", "PA", "GA")
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(cols), extrasaction="ignore")
            w.writeheader()
            w.writerows(flat)
        print("\n已写出 %s" % args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
