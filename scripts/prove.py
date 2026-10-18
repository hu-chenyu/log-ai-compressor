#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自证：把本项目对外的每一条主张变成一条命令能跑出来的数字。

为什么有这个脚本
----------------
README 里写了压缩比、证据保留率、置信档位、确定性、评分卡…… 但一份
没人复现过的数字等于营销。这个脚本的定位是：**评审者不必相信作者，
自己跑一条命令就能看到全部主张当前是否成立**，以及**哪一条还没数据**。

刻意把「尚无数据」也打印出来。本项目最容易被诟病的一点就是拿覆盖率、
跑分当成绩单，而真正难测的东西（召回率）恰恰缺真实数据 —— 把这个缺口
写在最显眼的地方，比藏起来可信。

用法：
    python scripts/prove.py              # 人读摘要
    python scripts/prove.py --json       # 机器可读
    python scripts/prove.py --log <路径>  # 追加一个自定义日志的实测
"""
from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path

# Windows 控制台默认非 UTF-8，打印中文会抛 UnicodeEncodeError，让 CI 门禁
# 在「输出阶段」变红 —— 看起来像功能坏了，实际只是编码问题。门禁必须只在
# 主张不成立时失败。先切到 UTF-8 并对无法编码字符降级。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from log_ai_compressor import service as S                      # noqa: E402
from log_ai_compressor.core.evidence import result_fingerprint  # noqa: E402
from log_ai_compressor.core.pipeline import analyze_files       # noqa: E402
from log_ai_compressor.export.reporters import estimate_tokens  # noqa: E402

SAMPLE = ROOT / "examples" / "sample_system.log"
_GREEN, _RED, _YELLOW, _DIM, _OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _ok(flag: bool) -> str:
    return f"{_GREEN}PASS{_OFF}" if flag else f"{_RED}FAIL{_OFF}"


def claim(name: str, holds: bool, detail: str) -> dict:
    return {"claim": name, "holds": bool(holds), "detail": detail}


# ---------------------------------------------------------------------------
# 各项主张
# ---------------------------------------------------------------------------
def claim_determinism(path: Path, runs: int = 3) -> dict:
    """同一份输入跑 N 次，结论指纹必须唯一。"""
    fps = set()
    for _ in range(runs):
        r = analyze_files([path], context_lines=2)
        fps.add(result_fingerprint(r))
    return claim("确定性：同输入同结论",
                 len(fps) == 1,
                 f"{runs} 次运行产生 {len(fps)} 个指纹 "
                 f"（{'一致' if len(fps) == 1 else '被破坏'}）")


def claim_evidence(path: Path) -> dict:
    """每条结论都要能重读原文复核。"""
    r = analyze_files([path], context_lines=2)
    report = S.verify_result(r)
    covered = sum(1 for c in r.clusters if c.evidence)
    total = len(r.clusters)
    # v2.11.1 修 0/0 空转：`covered == total` 在两个都是 0 时恒真，于是
    # 一个**什么都不产出**的产品能拿到这条主张的 PASS —— 实测把
    # pipeline 的 clusters 清空后，11 条主张里 6 条空转，只有对抗性评测
    # 拦下。`claim_layering` 早就为同类问题加了 `scanned_empty`（扫了 0 个
    # 文件不许报通过），这条漏了；同一份列表里同一种病，两种修法。
    # `total > 0` 是显式的那一半，不依赖 `report["ok"]` 恰好也覆盖到。
    return claim("证据可复核：每条结论带行号出处且核对通过",
                 report["ok"] and total > 0 and covered == total,
                 f"{covered}/{total} 个簇带出处；自校验 {report['verdict']}，"
                 f"匹配 {report['matched']}/{report['checked']}")


def claim_confidence(path: Path) -> dict:
    """CONFIRMED 只能来自 Caused-by 链；日志里没有因果链就必须零。

    **v2.9.9 修的恒真**：原来是
    `consistent = (not confirmed) if not has_chain else True`
    —— 样例日志 `examples/sample_system.log` **恰好含 Caused-by 链**，
    于是永远走 `else True`。这条主张声称守「无因果链时零 CONFIRMED」，
    而唯一被跑的样本恰好是有链的，**它永远不会红**。10 条主张里这 1 条空转，
    而它守的正是「置信档位不乱说」这个核心卖点。

    现在拆成**两个都要真判**的分支：
      · 无链 -> 必须零 CONFIRMED（拿程序自己合成的无链日志去判）
      · 有链 -> CONFIRMED 数不得超过链数，且每个 CONFIRMED 都得真的带链证据
    两边都判，恒真分支就不存在了。
    """
    r = analyze_files([path], context_lines=2)
    confirmed = [c for c in r.clusters
                 if c.root_cause_confidence == "CONFIRMED"]
    verdict = (r.evidence or {}).get("verdict")
    text = path.read_text(encoding="utf-8", errors="replace")
    has_chain = "Caused by:" in text
    n_chain = text.count("Caused by:")

    if not has_chain:
        consistent = not confirmed
        why = f"日志无 Caused-by 链，必须零 CONFIRMED，实得 {len(confirmed)}"
    else:
        # 有链时不再是「无条件 True」：CONFIRMED 不能多于链，且每个都得带
        # 链证据（变体 / 证据引用 / 因果计数三者至少有一个非空）。
        chain_backed = all(
            (getattr(c, "root_cause_variants", None)
             or getattr(c, "evidence", None)
             or getattr(c, "cause_counts", None))
            for c in confirmed)
        consistent = len(confirmed) <= n_chain and chain_backed
        why = (f"日志含 {n_chain} 条 Caused-by；判 CONFIRMED {len(confirmed)} 个"
               f"（不得超过链数，且每个须有链证据）")
    return claim("置信档位：无因果链时零确定性断言", consistent,
                 f"{why}；verdict={verdict}")


def claim_compression(path: Path) -> dict:
    """压缩口径必须如实报，不能把完整报告说成几百 token。

    这里刻意**不**要求「完整 md 报告一定比原文小」：52 行的小日志上，
    带样例与上下文的完整报告必然比原文大（实测 934 → 9,204 tok）。
    那是事实，藏起来才是不诚实。所以这条主张只验一件事：
    **投喂用的 summary 口径必须远小于原文**，并且 md 的真实体积一并
    报出来，让人自己判断该用哪个口径。
    """
    r = analyze_files([path], context_lines=50)
    brief = S.export_text(r, "summary")
    md = S.export_text(r, "md")
    raw_tok = estimate_tokens(path.read_text(encoding="utf-8", errors="replace"))
    brief_tok, md_tok = estimate_tokens(brief), estimate_tokens(md)
    holds = brief_tok < raw_tok
    note = ""
    n_lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    if md_tok > raw_tok:
        note = (f"；注意：本日志只有 {n_lines} 行，完整 md 报告比原文**大** "
                f"{md_tok / raw_tok:.1f} 倍 —— 小文件上带样例与上下文的完整报告"
                f"必然比原文长，这是事实不是缺陷；大数据集上的口径见 "
                f"docs/BENCHMARK.md")
    return claim("压缩：投喂口径远小于原文，且完整报告体积如实报出",
                 holds,
                 f"原始 ≈{raw_tok:,} tok → summary ≈{brief_tok:,} tok "
                 f"（{raw_tok / brief_tok:.1f}x）→ md ≈{md_tok:,} tok{note}")


def claim_scorecard() -> dict:
    """对抗性评测：故意出错时会不会闭嘴。"""
    try:
        # 直接把 scripts/ 放进 sys.path 后按模块名导入。用
        # spec_from_file_location 的写法会绕过 sys.modules 注册，而
        # adversarial_eval.py 里的 @dataclass 需要 cls.__module__ 可查，
        # 于是报 'NoneType' has no attribute '__dict__'。
        scripts_dir = str(ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import adversarial_eval            # noqa: PLC0415
        summary = adversarial_eval.scorecard(adversarial_eval.run_all())
        return claim("对抗性评测：该弃权时确实弃权",
                     summary["ok"],
                     f"{summary['cases']} 场景 {summary['passed']}/{summary['checks']} "
                     f"通过；正确弃权率 {summary['correct_abstain_rate']}%；"
                     f"自信错误率 {summary['confident_wrong_rate']}%")
    except Exception as exc:                      # 评测本身跑不起来 = 不通过
        return claim("对抗性评测：该弃权时确实弃权", False,
                     f"评测执行失败：{type(exc).__name__}: {exc}")


def claim_structured() -> dict:
    """结构化日志要零配置可用，且不得误吞普通文本。"""
    from log_ai_compressor.core.structured import parse_line
    json_ok = parse_line('{"level":"ERROR","service.name":"api",'
                         '"message":"db down","trace_id":"abc123def"}')
    plain_ok = parse_line("connect failed after retries=3 timeout=30") is None
    return claim("结构化日志：JSON 认领 / 普通文本不误判",
                 json_ok is not None and bool(json_ok.trace_id) and plain_ok,
                 f"JSON 认领 trace_id={getattr(json_ok, 'trace_id', '')!r}；"
                 f"普通文本误判={not plain_ok}")


def _code_metrics() -> dict:
    """包内代码规模（口径：log_ai_compressor/**/*.py 的非空非注释行）。

    口径必须写死在这里，因为 README 与 prove.py 要用**同一个**定义 ——
    「多少行代码」换一个算法能从 7,486 变成 9,538，中间还隔着注释与空行。
    两边各算一次必然漂移。
    """
    files = sorted((ROOT / "log_ai_compressor").rglob("*.py"))
    # 行数走共用函数，而不是在这里再写一遍算法：**同一个口径写两遍，
    # 换一个算法就漂了**，而漂掉的恰好是一个对外数字。
    return {"modules": len(files),
            "lines": _count_lines_in("log_ai_compressor")}


# 会被 `claim_readme_metrics` 逐个扫「对外工程数字」的文档。
# 每加一份文档，就多一处能悄悄漂移的地方 —— 与基准表那份白名单同一个道理。
_METRIC_DOCS = ("README.md", "docs/FAQ.md", "CONTRIBUTING.md",
                # docs/EVALUATION.md 里粘了一段 prove.py 的输出，其中「测试/
                # 主代码行数比实测 1.57」是**关于本仓库的对外数字**。
                # tests/test_prove_claims_gate.py 的看门狗要求凡是写了行数比
                # 的文档都得进这份名单 —— 与其把粘贴的输出重写掉，不如让它
                # 一起被对账：比值一变，这份快照就该跟着更新。
                "docs/EVALUATION.md")
# 写了这几个字但**刻意不校**的文档。没有理由的豁免等于给自己开后门。
_METRIC_DOCS_EXEMPT = {
    "CHANGELOG.md": "历史快照，AGENTS.md §2.4 明说刻意不重写 —— "
                    "它必然记着若干个已经作废的旧比值",
    "AUDIT.md": "缺陷形态记录。形态二那张表要引述**当时**的实测值"
                "（FAQ ≈1.1 / 当年 1.37），那些数早作废了；"
                "AGENTS.md §2.4 明说历史引用刻意不重写，抹掉等于篡改历史。"
                "本文档自己那两个条数（主张数、变异数）另有门禁校",
}


def _count_lines_of(path) -> int:
    """单个文件的非空非注释行 —— 口径与 `_count_lines_in` 完全一致。"""
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            n += 1
    return n


def _count_lines_in(root_name: str) -> int:
    """与 `_code_metrics` **同一个口径**数某个目录下的非空非注释行。

    口径必须共用一个函数：两个地方各写一遍，换个算法就漂了 ——
    而漂了的恰好是一个对外数字。
    """
    return sum(_count_lines_of(p) for p in (ROOT / root_name).rglob("*.py"))


def _coverage_denominator() -> tuple[int, int, list]:
    """(分母, 被 omit 掉的行数, omit 名单)。

    `pyproject.toml` 的 `[tool.coverage.run] omit` 决定分母，而这份名单本身
    由 `tests/test_coverage_omit_watchdog.py` 钉住。名单直接从 pyproject 读，
    不在这里另抄一份 —— 抄的那份会漂，而漂掉的又是一个对外数字。
    """
    import tomllib
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    omit = list(cfg.get("tool", {}).get("coverage", {}).get("run", {})
                .get("omit", []) or [])
    omitted = sum(_count_lines_of(ROOT / o) for o in omit if (ROOT / o).is_file())
    return _code_metrics()["lines"] - omitted, omitted, omit


def _count_ratio() -> float:
    """测试 / 主代码行数比（README 与 FAQ 都对外写着它）。"""
    prod = _code_metrics()["lines"]
    return round(_count_lines_in("tests") / prod, 2) if prod else 0.0


# 「≈X」里的 X 必须是实测值的**诚实取整**：按 X 自己的小数位去四舍五入实测值，
# 应当正好得到 X。
#
# v2.11.0 那版用固定容差 0.01，于是实测 1.3807 写成「≈1.37」能过 ——
# 差 0.0099 < 0.01。而**那恰恰是本次修复的起因**：AGENTS.md §2.1 禁止把
# 「大概差不多」写成实测值，而一次专门修「没人看守的对外数字」的提交，
# 自己刚写进去的数字就不是实测值，自己刚加的门禁还正好把它放了过去。
#
# 「1.4 对 1.3807」是诚实的取整，「1.37 对 1.3807」不是。判据必须能区分这两者。
#
# 中间那个 `(?:实测\s*)?` 不是可选的装饰。docs/EVALUATION.md 里粘的是
# **prove.py 自己的输出**，那句写的是「测试/主代码行数比**实测** 1.57」——
# 要求数字紧跟「行数比」的正则，一个都匹配不上，于是那份文档被列进
# `_METRIC_DOCS` 之后**扫了 0 处却照样报通过**。
#
# 教训与本仓库记过的另两次同形：分层门禁 `for pkg in (pkg/"ai", ...)`
# 里 `cli` 是文件不是目录，`rglob` 静默返回空；改门禁的 `_is_full_run`
# 永远在 skip。**报「检查通过」但其实什么都没扫的那道门，比没有门禁更糟** ——
# 没有门禁，你知道那里没检查。
_RATIO_RE = re.compile(r"行数比\s*(?:实测\s*)?[≈~]?\s*(\d+(?:\.\d+)?)")


def _is_honest_rounding(claimed: str, real: float) -> bool:
    places = len(claimed.split(".")[1]) if "." in claimed else 0
    return round(real, places) == float(claimed)


def claim_readme_metrics() -> dict:
    """README 里的工程指标必须与仓库实际一致（v2.8.0 新增门禁）。

    为什么单列一条：README 曾长期写着「生产代码 8,906 行（28 个模块）」，
    而实测是 7,486 行 / 32 个 .py —— 差了一整个版本周期的改动量，
    **六道自证门禁没有一项拦它**。一个专门讲「不必相信作者，自己跑」
    的项目，README 里的数字却没有对应的可执行检查，这本身就是矛盾。

    v2.9.1 追加校「README 说自证脚本有 N 条主张」：这同样是 README 里的
    一个数字，也同样一度是错的 —— v2.9.0 加了「接入层只经 service 触达
    core/export」那条之后，README 两处仍写 9 条，而**上面这个门禁照样绿**，
    因为它只校行数与模块数。

    **同一个数字在 README 里出现几次，就校几处** —— 只校第一处，等于把
    v2.8.1 那条「聚合值守住了、明细漏了」的教训换个方向再犯一遍。
    """
    import re
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = _code_metrics()
    row = next((ln for ln in readme.splitlines()
                if ln.strip().startswith("| 生产代码")), "")
    got = re.search(r"([\d,]+)\s*行（(\d+)\s*个模块）", row)
    if not got:
        return claim("README 工程指标与仓库一致", False,
                     f"README 里找不到「生产代码」那一行：{row!r}")
    claimed_lines = int(got.group(1).replace(",", ""))
    claimed_modules = int(got.group(2))
    metrics_ok = claimed_lines == m["lines"] and claimed_modules == m["modules"]

    # 「自证脚本……N 条主张」与本文件实际登记的条数对账。
    # 前向引用 _CLAIM_FNS：它在下方 gather() 之前定义，而调用发生在
    # 模块加载完成之后，所以运行期一定拿得到。
    expected = len(_CLAIM_FNS)
    found = [int(x) for x in re.findall(r"\*\*(\d+)\s*条\*\*\s*主张", readme)]
    counts_ok = bool(found) and all(n == expected for n in found)

    # 「测试/主代码行数比 ≈X」—— v2.11.0 的复核量到它当时是 1.37，而
    # docs/FAQ.md 写的是「≈1.1」。差得不多，但那是**错的**，而且
    # **没有任何门禁校它**。与本文件开头那条病同形：对外数字漂了没人叫。
    # 与其把它列进「尚无数据的主张」，不如接进这条已经算好行数的门禁。
    #
    # 判据是「诚实取整」而不是固定容差 —— 见 `_is_honest_rounding` 的注释。
    ratio_real = _count_ratio()
    ratio_problems = []
    for doc in _METRIC_DOCS:
        path = ROOT / doc
        if not path.is_file() or doc in _METRIC_DOCS_EXEMPT:
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            for claimed in _RATIO_RE.findall(line):
                if not _is_honest_rounding(claimed, ratio_real):
                    ratio_problems.append(
                        f"{doc}:{lineno} 写 {claimed}，实测 {ratio_real}，"
                        f"按 {len(claimed.split('.')[-1]) if '.' in claimed else 0} "
                        f"位取整得不到它")
    ratio_ok = not ratio_problems

    # 「96% 是在 N 行上算的」—— 覆盖率分母。
    #
    # 这个数此前**根本没有门禁**：本 claim 只看「生产代码」那一行，而分母
    # 写在下面的引用块里。于是批 1 给 server.py 加了 46 行之后，分母从
    # 7,144 变成 7,224，而 prove.py 全绿。
    #
    # 形状与 v2.11.0 那条「行数比 ≈X」完全同源：**渲染出来的数字与算出来的
    # 数字，永远不会在文本里相遇**，除非有人显式把它们连起来。
    denom_real, omitted_lines, omit_names = _coverage_denominator()
    got_denom = re.search(r"是在\s*([\d,]+)\s*行上算的", readme)
    if not got_denom:
        denom_ok = False
        denom_note = "README 里找不到「96% 是在 N 行上算的」那句"
    else:
        denom_claimed = int(got_denom.group(1).replace(",", ""))
        denom_ok = denom_claimed == denom_real
        denom_note = (f"覆盖率分母写 {denom_claimed}，实测 {denom_real}"
                      f"（{m['lines']} − {omitted_lines}，omit={omit_names}）")

    detail = (f"README 写 {claimed_lines} 行 / {claimed_modules} 个模块；"
              f"实测 {m['lines']} 行 / {m['modules']} 个模块"
              f"（口径：log_ai_compressor/**/*.py 非空非注释行）；"
              f"README 写自证主张 {found or '未找到'} 条，"
              f"本文件实际登记 {expected} 条；"
              f"测试/主代码行数比实测 {ratio_real}；{denom_note}")
    if ratio_problems:
        detail += "；" + "；".join(ratio_problems[:3])
    return claim("README 工程指标与仓库一致",
                 metrics_ok and counts_ok and ratio_ok and denom_ok, detail)


# 「本项目」自己的口径列。它们的存在本身就说明这是一张**证据保留率**表。
_BENCH_OWN = (("交付", "delivery"), ("同预算", "budget32k"), ("全量", "full"))
# 对照臂列，单独分组：docs/BENCHMARK.md 里还有两张以它们为主角的
# **token 压缩比**表，语义与保留率毫无关系，见 _bench_columns。
_BENCH_ARMS = (("Drain3", "drain3"), ("LogsLop", "logslop"))
_BENCH_COLUMNS = _BENCH_OWN + _BENCH_ARMS
_BENCH_SKIP_CELLS = ("交付上限", "达成度")   # 公式列，不是实测值

# 胜负场次单独校：它是 README 第一屏就被引用的那句话（「同预算档 8 胜 1 平
# 1 负」），而且**从来没有被任何东西校过**。docs/BENCHMARK.md 的「三个
# 口径」那张表里，交付口径写的是「1 胜 9 负」—— 多算一场胜、把 Proxifier
# 那场平（72.7% vs 72.7%）整个漏掉，与同仓库 README、docs/FAQ.md 写的
# 「0 胜 1 平 9 负」直接矛盾，而三份文档都在门禁射程之内。
#
# 小数点漂移和这个不是一回事：**多报一个不存在的胜**，比错一个百分点严重
# 得多 —— 它是这类项目最容易被追问、也最经不起追问的一句话。
_VERDICT_RE = re.compile(
    r"([\d,]+)\s*胜"
    r"(?:\s*[、，,/／]?\s*([\d,]+)\s*平)?"
    r"\s*[、，,/／]?\s*"
    r"([\d,]+)\s*负")
_BENCH_VERDICTS = (("交付", "verdict_delivery"),
                   ("同预算", "verdict_budget"),
                   ("全量", "verdict_full"))

# 胜负是**对某个对照组**说的，这句话没有对照组就是残句。
#
# v2.11.0 那版判据根本不知道对照组是谁：`line.find(口径标签)` 之后才
# 开始找场次，于是「同预算档对 **LogsLop**：10 胜 0 平 0 负」被当成
# 「对 Drain3 的 8 胜 1 平 1 负」而报假漂移 —— **一条会误报的门禁比
# 没有门禁更糟**，它把真漂移淹没在噪音里。
#
# 基线里对 LogsLop **只有同预算一个口径**的实测（`verdict_budget_logslop`
# = 10 胜），所以「对 LogsLop 的交付/全量战绩」是**无法对账**的断言：
# 拿 Drain3 的数字去比是拿错尺子量，默默放过是漏。单列成 `unverifiable`
# 报进 detail，让「这条门禁管不到哪些」是**可见的**，而不是猜的。
_VERDICT_COUNTERPARTS = {"Drain3": "", "LogsLop": "_logslop"}
_DEFAULT_COUNTERPART = "Drain3"


def _bench_verdict_counts(datasets: dict, counterpart: str = "Drain3") -> dict:
    """从基线里每个数据集的 `verdict_*` 重算三个口径对某对照组的场次。"""
    suffix = _VERDICT_COUNTERPARTS[counterpart]
    out = {}
    for label, key in _BENCH_VERDICTS:
        tally = {"胜": 0, "平": 0, "负": 0}
        for rec in datasets.values():
            v = rec.get(key + suffix)
            if v in tally:
                tally[v] += 1
        out[label] = tally
    return out


def _verdict_has_data(datasets: dict, label: str, counterpart: str) -> bool:
    """基线里到底有没有这个「口径 × 对照组」的**完整**实测。

    **「全是 0」和「没测过」是两件事。** 缺 `verdict_full_logslop` 时重算
    出来是 0 胜 0 平 0 负，拿它去比文档里的「10 胜 0 平 0 负」会得到一条
    **永远判红**的门禁；反过来把它当成真的 0，又会永远判绿。

    这里用 `all()` 而不是 `any()`：只要**有一个**数据集缺这个字段，场次就
    是不完整的，拿它算出来的数会**假红**而且怪错了对象 ——
    「只有一个数据集缺 verdict_delivery」实测会算出 0 胜 1 平 8 负，
    然后反过来指责文档写错了。

    同时要求取值落在「胜/平/负」之内：基线里出现 `"tie"` 之类时，
    同样属于「量不出来」，不是「量出来是 0」。
    """
    key = dict(_BENCH_VERDICTS)[label] + _VERDICT_COUNTERPARTS[counterpart]
    values = [rec.get(key) for rec in datasets.values()]
    if not values or any(v is None for v in values):
        return False
    return all(v in ("胜", "平", "负") for v in values)


def _num(s: str) -> int:
    """`1,210` 与 `1210` 是同一个数 —— 千分位不能让它读成 `210`。"""
    return int(str(s).replace(",", ""))


def _verdict_claims(line: str):
    """把一行拆成 `[(口径标签或 None, (胜,平,负), 对照组或 None), ...]`。

    口径与场次必须**同行**，但口径**不一定写在前面** ——
    「对 Drain3 的战绩：0 胜 1 平 9 负（交付口径）。」把口径放在句尾括号里，
    而 v2.11.0 那版是 `line.find(标签)` 之后才开始找场次，于是这句话
    **一处都没校到**（实测 `checked=0`）：一句对外的战绩被判成没有断言。
    同一份代码里「对 LogsLop 误报」和「标签在后的漏校」是**同一个
    位置假设的两面**，所以两边一起改。

    改法：先把场次全找出来，每个场次归给**距离最近**的那个口径标签 /
    对照组名，距离相同时取左边的那个（中文里限定词总是先引出被限定的数）。
    没有标签也没有对照组的（「历史叙述里的 7 胜 2 负」）原样交出去，
    由调用方按「无法判定口径」处理 —— 不猜。

    **这条判据的已知边界**（实测出来的，记在这里而不是假装没有）：
    「最近」是**位置启发式**，它分不清「这一行的场次属于前面那个口径」
    和「这一行后半段夹了一句历史数字」。把 `docs/BENCHMARK.md` 289–297
    段（第一轮的「3 胜 2 负」）合并成一行时，那个 3/0/2 会被判给同段的
    「交付口径」而报假红。四份受校文档现状误报为 0（22 处全对），
    所以这是**潜伏**的；但它确实存在，写法一改就可能撞上。

    口径与场次**跨行**时完全不管 —— 只进 `unlabeled` 计数。四份受校文档
    现在的 5 处 unlabeled 全是历史叙述（「撤回的严格档」「第一轮 3 胜 2 负」
    「CI 全绿 5 胜 0 负」「9 个 7 胜 2 负」），本就不该判红。
    这是为躲开「交付上限」「全级别」而故意选的：跨行窗口匹配会在这类词上乱抓，
    而**一条会误报的门禁比没有门禁更糟** —— 它把真漂移淹没在噪音里。
    """
    matches = list(_VERDICT_RE.finditer(line))
    if not matches:
        return []
    # 口径标签的**可用出现位置**。判据与 `_bench_columns` 对表头用的是同一套
    # 跳过词：「交付上限」「达成度」这些是**公式列**，不是实测口径 ——
    # 表格侧早就躲掉了，胜负侧当时没跟上，于是
    # `| HPC | 27.0% | 8 胜 1 平 1 负 | 交付上限 27.0% |` 会把场次判给「交付」而报假红。
    labels = sorted((line.find(lb), lb) for lb, _ in _BENCH_VERDICTS
                    if line.find(lb) >= 0
                    and not any(bad in line[line.find(lb):line.find(lb) + 6]
                                for bad in _BENCH_SKIP_CELLS))
    cps = sorted((line.find(cp), cp) for cp in _VERDICT_COUNTERPARTS
                 if cp in line)

    def _nearest(table, pos: int):
        # `pos` 必须显式传进来：写成闭包去抓推导式的 `m` 会拿到
        # NameError —— 推导式有自己的作用域，嵌套 lambda 看不见它。
        if not table:
            return None
        return min(table, key=lambda p: (abs(p[0] - pos),
                                         p[0] > pos))[1]

    return [(_nearest(labels, m.start()), _verdict_triple(m),
             _nearest(cps, m.start()))
            for m in matches]


def _bench_verdict_problems(doc_name: str, doc_text: str, datasets: dict,
                            counts: dict | None = None):
    """把文档里「N 胜 N 平 N 负」的断言逐处与重算结果比对。

    返回 (问题列表, {口径: [行号]}, 校过的处数, 备注)。

    口径标签与场次**必须同一行**：靠固定字符窗口跨行匹配，会在
    「交付上限」「全级别」这些含「交付」「全量」的词上乱抓 ——
    一条会误报的门禁比没有门禁更糟，它把真漂移淹没在噪音里。

    同一行里同一口径可能出现多次（同预算 + 全量都是 8/1/1），
    但归到该口径的**每一个**场次都要对得上。v2.11.0 那版是
    「只要有一个对得上就算过」—— 那是为了容忍合法重复表述，
    但它同时放过了一行里写错的那一个：**一行两个数、一个对一个错，
    门禁判绿**。多报一个不存在的胜比错一个百分点严重得多，所以这里
    逐个校，「一个对得上就放过」是这条门禁最危险的简化。

    `counts` 省略时按默认对照组（Drain3）现算。
    """
    problems: list = []
    seen: dict = {}
    checked = 0
    unverifiable: list = []
    unlabeled = 0
    default_counts = counts if counts is not None \
        else _bench_verdict_counts(datasets)
    for lineno, line in enumerate(doc_text.splitlines(), 1):
        for label, triple, cp in _verdict_claims(line):
            if label is None:
                # 历史叙述里的「7 胜 2 负」不带口径，无法判定该拿哪把尺子量。
                # 计入统计并报进 detail，而不是猜一个 —— 猜错就是假红。
                unlabeled += 1
                continue
            cp = cp or _DEFAULT_COUNTERPART
            if not _verdict_has_data(datasets, label, cp):
                unverifiable.append(f"{doc_name}:{lineno} {label} vs {cp}")
                continue
            want = (default_counts if cp == _DEFAULT_COUNTERPART
                    else _bench_verdict_counts(datasets, cp))[label]
            expect = (want["胜"], want["平"], want["负"])
            checked += 1
            seen.setdefault(label, []).append(lineno)
            if triple != expect:
                problems.append(
                    f"{doc_name}:{lineno} {label} vs {cp} 胜负：文档 "
                    f"{triple[0]} 胜 {triple[1]} 平 {triple[2]} 负 vs 实测 "
                    f"{expect[0]} 胜 {expect[1]} 平 {expect[2]} 负")
    return problems, seen, checked, {
        "unverifiable": unverifiable, "unlabeled": unlabeled}


def _verdict_triple(m) -> tuple:
    """`1 胜 9 负` 是合法的写法：没写平就是零平。"""
    return (_num(m.group(1)), _num(m.group(2)) if m.group(2) else 0,
            _num(m.group(3)))

# 每一处**当前**结论的副本都要对账。v2.11.0 才发现第三份：docs/FAQ.md 的
# Q2 有一张同口径的表，v2.10.0 那条只认 README 的门禁照样放过它。
_BENCH_DOCS = ("README.md", "docs/BENCHMARK.md", "docs/FAQ.md")
# 含对外战绩、但**没有**基准表的文档。
#
# v2.11.0 的纳管判据是 `_bench_tables(...)` ——「这份文档里认得出基准表」。
# docs/POSITIONING.md 认不出表，于是成了法外地带：它重复着 README 的三个
# 口径战绩（交付 1 处 / 同预算 2 处 / 全量 1 处），看门狗却既不纳管、
# 也不点名。**「没被禁」不等于「被想过」** —— 上一轮刚在 rules.engine 上
# 记过一次同款教训，这里是同一族的第二次。
#
# 所以纳管判据要按**受校的内容**分，而不是按「有没有表」：
# 表格口径对 `_BENCH_DOCS` 生效，胜负口径对这两个集合的**并集**生效。
_BENCH_VERDICT_DOCS = _BENCH_DOCS + ("docs/POSITIONING.md",)
# 刻意排除的文档，附理由。没有理由的豁免等于给自己开后门。
_BENCH_DOC_EXEMPT = {"CHANGELOG.md": "历史快照，AGENTS.md §2.4 明说刻意不重写"}


def _bench_columns(header: str) -> dict:
    """按**表头**给每个指标列定列号，返回 {基线字段: 列号}；不是基准表返回 {}。

    为什么不能写死「第几列」：README 的表有 5 个百分数列，
    docs/BENCHMARK.md 的同名表多出 `tail @32k` / `grep @32k` 两列；
    而 BENCHMARK.md 里还有七八张以「| 数据集 |」开头但列义完全不同的表
    （行数/token 数、达成度、词元 Jaccard…）。按位置取会把它们一起吃进来，
    然后报一堆根本没漂移的「漂移」—— 比漏报更糟，因为它会让真漂移被淹没。

    识别判据为什么是「有事件数列 **或** 有两个以上本项目口径列」
    --------------------------------------------------------
    只认「标注事件」那一列会放过 `docs/FAQ.md` 的第三份副本 ——
    它那张表是同口径的，只是没列事件数。**而「只堵了半边」正是这条门禁
    自己诞生的原因**：v2.10.0 补 README 时没看 BENCHMARK.md，
    v2.11.0 补 BENCHMARK.md 时差点又漏掉 FAQ.md。

    反过来也不能放宽成「出现 Drain3 就算」：BENCHMARK.md 里有两张以 Drain3
    为主角的 **token 压缩比**表，吃进来只会报出一堆根本不存在的漂移，
    而真漂移会被淹没。判据要卡在「本项目口径」这一侧。
    """
    cells = [c.strip().replace("**", "").strip()
             for c in header.strip().strip("|").split("|")]
    has_events = "标注事件" in cells
    own = [key for token, key in _BENCH_OWN
           if any(token in c for c in cells)]
    if not has_events and len(own) < 2:
        return {}
    out: dict = {}
    if has_events:
        out["events"] = cells.index("标注事件")
    for idx, cell in enumerate(cells):
        if any(bad in cell for bad in _BENCH_SKIP_CELLS):
            continue
        for token, key in _BENCH_COLUMNS:
            if token in cell:
                out.setdefault(key, idx)
                break
    return out


# Markdown 表格的表头下一行必须长这样：只由 | - : 空格组成。
# 少了这条判据，「含有『数据集』三个字的一行」会被当成表头 ——
# 而 README「我们不能证明」那张表的**数据行**里恰好同时出现了
# 「同预算档」「交付口径」「全量口径」，于是整张表被当成基准表校一遍，
# 报出「表里有、基线里没有的数据集：在所有日志形态上都优于 Drain3」。
_MD_SEP_RE = re.compile(r"^[|\s:-]+$")


def _bench_tables(doc_text: str):
    """找出文档里所有基准表，返回 [(列号表, 数据行, 表头单元格), ...]。

    v2.11.1：多带一个**表头单元格**。聚合行（「合计」那一行）要对账的
    「交付上限」「达成度」两列被 `_BENCH_SKIP_CELLS` 跳过、拿不到列号，
    而表头是解析出来的东西 —— 让调用方再解析一遍就等于把口径写两遍，
    换个算法就漂，而漂掉的恰好是一个对外数字。
    """
    lines = doc_text.splitlines()
    out = []
    i = 0
    while i < len(lines) - 1:
        line = lines[i]
        if (line.lstrip().startswith("|") and "数据集" in line
                and _MD_SEP_RE.match(lines[i + 1])):
            cols = _bench_columns(line)
            body, j = [], i + 2          # i+1 是 |---| 分隔行
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                body.append(lines[j])
                j += 1
            if cols:
                header = [c.strip().replace("**", "").strip()
                          for c in line.strip().strip("|").split("|")]
                out.append((cols, body, header))
            i = j
        else:
            i += 1
    return out


# 「交付上限」的物理上限要拿产品默认给几个簇去除 —— 必须与
# `constants.DEFAULT_TOP_N` 同值，否则这里算出来的上限是另一个口径的。
# 有测试钉住这个同步（tests/test_benchmark_table_gate.py），
# 加不着一份测试就等于把这个常量放任漂下去。
_BENCH_N_DEL = 20


def _agg_row_problems(doc_name: str, idx: int, name: str, cells: list,
                      cols: dict, header: list, datasets: dict,
                      eps_pp: float = 0.05):
    """对账「合计」行里**能从基线重算**的那几个数。

    为什么只做「合计」而不做「平均/小计/总计」：那几样口径各不相同，
    猜一个等于造数字。这里只认「合计」这一个明确的口径。

    可重算的三个量（口径全部取自 `scripts/benchmark_loghub.py`）：

        交付实测合计 = Σ 保留率
        交付上限合计 = Σ min(n_del, events) / events
        达成度合计   = Σ(保留率×events) / Σ min(n_del, events)

    v2.11.1 实测：引入「合计」行的那笔提交（d299aeb，标题是「把『交付
    口径已达天花板』从断言变成脚本能复现的数字」）把交付合计写成 282.4%，
    实测是 270.6%，差 11.8pp —— **它想治的病在原地复发了**：脚本产出的
    那部分对了，手算的这部分错了，而这一行此前列在跳过名单里没人叫。
    """
    if "合计" not in name:
        return []
    recs = [r for r in datasets.values()
            if r.get("events") and r.get("delivery") is not None]
    if not recs:
        return []
    cap_n_total = sum(min(_BENCH_N_DEL, r["events"]) for r in recs)
    if not cap_n_total:
        return []
    cov_total = sum(r["delivery"] * r["events"] for r in recs)

    expected = {
        "交付实测": sum(r["delivery"] for r in recs),
        "交付上限": sum(min(_BENCH_N_DEL, r["events"]) / r["events"] for r in recs),
        "达成度": cov_total / cap_n_total,
    }
    out = []
    for i, head in enumerate(header):
        if i >= len(cells) or i == 0:
            continue
        label = next((k for k in expected if k in head), None)
        if label is None:
            continue
        raw = cells[i].strip()
        m = re.search(r"(-?[\d.]+)\s*%", raw)
        if m is None:
            out.append(f"{doc_name} {name} {label}：写不成百分数（{raw!r}）")
            continue
        claimed = float(m.group(1)) / 100
        actual = expected[label]
        # 用**诚实取整**而不是固定容差：合计行常写成整数（达成度 82%），
        # 而重算值是 82.2% —— 按 0 位取整正好是 82，诚实的。
        # 固定 0.05pp 容差会把这种正确写法判红，而**一条会误报的门禁比
        # 没有门禁更糟**（它把真漂移淹没在噪音里）。这与 `_is_honest_rounding`
        # 那条判据治的是同一个病，只是换了个字段。
        if not _is_honest_rounding(m.group(1), actual * 100):
            out.append(
                f"{doc_name} {name} {label}：文档 {claimed * 100:.1f}% vs "
                f"基线重算 {actual * 100:.1f}%")
    return out


def _pct(cell: str):
    """把 `**100.0%**` 解析成 1.0；不是百分数就返回 None。"""
    m = re.search(r"(-?[\d.]+)\s*%", cell)
    return float(m.group(1)) / 100 if m else None


def _bench_doc_problems(doc_name: str, doc_text: str, datasets: dict,
                         eps_pp: float = 0.05, check_tables: bool = True):
    """对**一份**文档里的所有基准表逐格对账，外加口径胜负场次。

    返回 (表内行数, 校验过的百分数格数, 校验过的胜负处数, 问题列表, 备注)。
    三档计数都单独报出来，是因为「扫了 0 个却报通过」比报错更危险 ——
    摘要里必须看得出这一轮到底校了多少东西。

    单独抽出来是为了能测：门禁本尊 `claim_benchmark_table()` 从磁盘读文件，
    而测试要能喂一段"故意改坏的表格"进去 —— 这是本仓库的硬规矩，
    **一条从不曾变红过的门禁等于没有门禁**。

    `check_tables=False` 用于**只带战绩、不带表**的文档
    （docs/POSITIONING.md）：对它找表是「找不到」，而「找不到」已经是
    另一个意思（那份文档本来就该没表），混在一起报就是假红。

    注意 `claim_benchmark_table` **不能**加参数：`prove.py::_run_claim` 见到
    任何带参数的 claim 就会把命令行 path 塞进去当成"从别的目录读基线"。
    """
    problems: list = []
    rows_total = 0
    checked = 0
    cells_unmeasured: list = []
    cells_misnamed: list = []
    seen_datasets: set = set()
    tables = _bench_tables(doc_text) if check_tables else []
    if check_tables and not tables:
        problems.append(
            f"{doc_name} 里找不到基准表（表头需含「标注事件」或两个以上本项目口径列）")
    for idx, (cols, body, header) in enumerate(tables, 1):
        # **列名被改一个字，整列就静默脱管。**
        #
        # v2.11.0 实测：把 README 表头的「交付」改成「投递」，那 10 个交付格
        # 全部不再对账（`cols` 里没有 delivery 键），而 docs/BENCHMARK.md 的
        # 同一份副本仍写着 72.7% —— 仓库自相矛盾、门禁报「全部一致」，
        # `cells_unmeasured` 里也一个字不提。这是 AGENTS.md 记了四次的
        # 「扫了 N 个却报通过」，只是这次 N 从 0 变成了 50。
        #
        # 判据不是「这张表该有哪些列」（各表本就可以只报一部分），而是
        # **own 三列同量纲**：一旦认出 ≥2 个保留率口径，它就是一张保留率表，
        # 三个口径必须报全。少一个要么是列名拼错、要么是漏写，两种都判红。
        # 实测四份受校文档共 4 张基准表，全部含交付列，满足「own≥2」的
        # 有 3 张且三个口径齐全，所以这条判据当前不产生假红。
        own_present = [k for _, k in _BENCH_OWN if k in cols]
        if len(own_present) >= 2:
            missing_own = [k for _, k in _BENCH_OWN if k not in cols]
            if missing_own:
                problems.append(
                    f"{doc_name} 第 {idx} 张基准表认出保留率口径 "
                    f"{'/'.join(own_present)}，缺 {'/'.join(missing_own)}"
                    f"—— 列名改一个字就会整列不再对账（对账格数会悄悄变少）")
                cells_misnamed.append(
                    f"{doc_name} 表{idx} 缺口径列 {'/'.join(missing_own)}")

        # 表头还在、数据行全没了：`_bench_tables` 照样把它认成一张表
        # （判据看的是表头 + 分隔行），而下面 `for raw in body` 一行都不跑，
        # 于是 rows/checked 全是 0、problems 全空 —— **v2.11.0 实测判通过**。
        # 「表在、行没了」比「表没了」更难发现：表还在，肉眼看着好好的。
        if not body:
            problems.append(
                f"{doc_name} 第 {idx} 张基准表只剩表头、一行数据都没有")
            continue
        for raw in body:
            cells = [c.strip().replace("**", "") for c in raw.strip().strip("|").split("|")]
            name = cells[0].strip()
            # 合计/平均这类聚合行不是数据集，拿它跟基线逐格比毫无意义。
            # 但**跳过不等于不管**：「合计」行里的数完全可以从基线重算，
            # 而 v2.11.1 实测发现它长期没人校、已经错了 11.8pp ——
            # 引入它的那笔提交标题恰恰是「把『交付口径已达天花板』从断言
            # 变成脚本能复现的数字」，也就是**它想治的病在原地复发了**：
            # 脚本产出的部分对了，手算的那部分错了，而没人叫。
            if any(agg in name for agg in ("合计", "总计", "小计", "平均")):
                problems.extend(
                    _agg_row_problems(doc_name, idx, name, cells, cols,
                                      header, datasets, eps_pp))
                continue
            rec = datasets.get(name)
            if rec is None:
                problems.append(f"{doc_name} 表里有、基线里没有的数据集：{name}")
                continue
            rows_total += 1
            seen_datasets.add(name)
            if any(idx >= len(cells) for idx in cols.values()):
                problems.append(f"{doc_name} {name} 那行列数不足")
                continue
            # 缺哪列就不查哪列：BENCHMARK.md 里那张「达成度」表也带「标注事件」，
            # 但只有交付一列，按名字硬取会直接 KeyError —— 一条门禁崩掉虽然也是红的，
            # 却红得没有信息。
            for label, key in (("交付", "delivery"), ("同预算", "budget32k"),
                               ("全量", "full"), ("Drain3", "drain3"),
                               ("LogsLop", "logslop")):
                if key not in cols:
                    continue
                claimed = _pct(cells[cols[key]])
                actual = rec.get(key)
                if claimed is None or actual is None:
                    # 对照臂是 None（那一次没跑 LogsLop）时**静默 continue**，
                    # 于是 `checked` 悄悄少几格而 detail 只字不提 —— 160 格
                    # 和 157 格在界面上长得一样。记下来，报给人看。
                    if claimed is not None:
                        cells_unmeasured.append(f"{doc_name} {name} {label}")
                    continue
                checked += 1
                # 在**百分点**上比，并给浮点表示留一丝余量。
                # 写成 `abs(a - b) > 0.0005` 会在边界上误判：OpenStack 的
                # LogsLop 实测是 0.1875、文档写 18.8%，而
                # `0.188 - 0.1875` 在二进制下是 0.0005000000000000004，
                # 刚好「大于」0.0005 —— 一条完全正确的表格被判成漂移。
                if abs(claimed - actual) * 100 > eps_pp + 1e-9:
                    problems.append(
                        f"{doc_name} {name} {label}：文档 {claimed * 100:.1f}% vs "
                        f"实测 {actual * 100:.1f}%")
            if "events" not in cols:
                continue                    # FAQ 那张表没列事件数，不查这一格
            got = cells[cols["events"]].strip().replace(",", "")
            # `rec["events"]` 是硬索引：基线里任一数据集缺这个字段就 KeyError，
            # 而 `gather()` 没有兜底 —— 一条门禁因为数据残缺**崩掉**，带
            # traceback 打死整个 prove.py。同一个循环上面几行已经为指标列
            # 用了 `rec.get(key)` 并注明「崩掉虽然也是红的，却红得没有信息」，
            # 这里当时没跟上。
            actual_events = rec.get("events")
            if actual_events is None:
                cells_unmeasured.append(f"{doc_name} {name} 标注事件")
                continue
            if got.isdigit() and int(got) != actual_events:
                problems.append(
                    f"{doc_name} {name} 标注事件：文档 {got} vs 实测 {actual_events}")
    # 基线里有、本文档一张表里却一行都没有的数据集。
    #
    # 只判「表还在不在」漏掉了**更常见**的那一半：删掉一行（哪怕只删
    # HPC 那一行），表还是齐的、每一格也都对得上，于是门禁照样绿 ——
    # 而「少了一个负例」的报告比「数字错了一个百分点」的报告危险得多，
    # 因为读者会以为那一行不存在。v2.11.0 实测：删掉 1 行 problems=[]。
    #
    # 必须与表格检查同进同退：`check_tables=False` 的文档（POSITIONING.md）
    # 本来就没有表，这里照样报一遍就是十条凭空捏造的假红 —— 而一条会
    # 误报的门禁比没有门禁更糟。
    if check_tables:
        for name in sorted(set(datasets) - seen_datasets):
            problems.append(
                f"{doc_name} 缺数据集 {name} 的行（基线里有，本文档没有）")
    v_problems, _seen, v_checked, notes = _bench_verdict_problems(
        doc_name, doc_text, datasets)
    problems.extend(v_problems)
    notes["cells_unmeasured"] = cells_unmeasured
    notes["cells_misnamed"] = cells_misnamed
    return rows_total, checked, v_checked, problems, notes


_TOKEN_TABLE_MARK = "原始 tokens（旧口径）"
#: 会引用那张 token 表里的**派生比值**的文档。
#: POSITIONING.md 那一行原本不点数据集名 —— 那等于比值无从判定来源，
#: 现在那一行自己写了来源，才进得了对账射程。
_TOKEN_RATIO_DOCS = ("README.md", "docs/FAQ.md", "docs/POSITIONING.md",
                     "docs/BENCHMARK.md")


def _token_table() -> dict:
    """`docs/BENCHMARK.md` 那张 token 压缩表：数据集 -> (原始, 本项目, Drain3)。

    与保留率表**不是同一张**，而且它此前**既没有实测对账、也没有任何内部
    一致性检查** —— `docs/benchmark_baseline.json` 里根本没有这三个数
    （基线只存百分数与胜负）。于是散落在 README / FAQ / POSITIONING 里的
    「压缩比 N 倍」是从这张表除出来的，却没人校过：表是
    15,299,983 → 746，而三处散文写的不是同一个数。
    """
    path = ROOT / "docs" / "BENCHMARK.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # 缺表时返回空，让 `_token_ratio_problems` 报一句人能读懂的
        # 「找不到那张 token 压缩表」，而不是抛一个 FileNotFoundError ——
        # 与本 claim 处理「缺 baseline 文件」的方式保持一致。
        return {}
    rows: dict = {}
    in_table = False
    for ln in text.splitlines():
        if _TOKEN_TABLE_MARK in ln:
            in_table = True
            continue
        if not in_table:
            continue
        s = ln.strip()
        if not s.startswith("|"):
            break
        cells = [c.strip().replace("**", "") for c in s.strip("|").split("|")]
        if len(cells) != 4 or cells[0] in ("数据集", ""):
            continue
        try:
            rows[cells[0]] = tuple(int(c.replace(",", "")) for c in cells[1:4])
        except ValueError:
            continue
    return rows


def _token_ratio_problems() -> tuple:
    """表 → 散文那一步的对账。返回 (问题列表, 说明)。"""
    import re
    rows = _token_table()
    if not rows:
        return ["docs/BENCHMARK.md 里找不到那张 token 压缩表"
                "（文件缺失，或表头「原始 tokens（旧口径）」被改了）"], "找不到表"

    problems: list = []
    checked = 0
    for ds, (raw, ours, _d3) in rows.items():
        if ours <= 0:
            continue
        expect = raw // ours
        for doc in _TOKEN_RATIO_DOCS:
            path = ROOT / doc
            if not path.is_file():
                continue
            for lineno, ln in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                # 必须**同时**出现数据集名与「压缩」二字才算数。
                # 第一版只认数据集名，于是把「HealthApp 上慢 48 倍」这种
                # **耗时**比也当成了压缩比来校 —— 三处假红。
                # 假红比假绿好，但门禁一旦开始报不成立的东西，人就不会再读它。
                if ds not in ln or "压缩" not in ln:
                    continue
                for raw_n in re.findall(r"([\d,]+)\s*倍", ln):
                    checked += 1
                    claimed = int(raw_n.replace(",", ""))
                    if claimed != expect:
                        problems.append(
                            f"{doc}:{lineno} 写 {ds} 压缩比 {claimed} 倍，"
                            f"按表算是 {expect} 倍（{raw:,} ÷ {ours}）")
    d3 = [v[2] for v in rows.values() if v[2] > 0]
    if len(d3) >= 2:
        span = max(d3) // min(d3)
        bench = (ROOT / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
        for raw_n in re.findall(r"跨度\s*([\d,]+)\s*倍", bench):
            checked += 1
            claimed = int(raw_n.replace(",", ""))
            if claimed != span:
                problems.append(
                    f"docs/BENCHMARK.md 写 Drain3 跨度 {claimed} 倍，"
                    f"按表算是 {span} 倍（{max(d3):,} ÷ {min(d3):,}）")
    os_row = rows.get("OpenStack", (0, 0, 0))
    note = (f"token 表 {len(rows)} 行，散文里 {checked} 处派生比值已对账"
            f"（OpenStack {os_row[0]:,} ÷ {os_row[1]} = {os_row[0] // os_row[1]:,}）"
            if os_row[1] else "token 表已解析")
    return problems, note


def claim_benchmark_table() -> dict:
    """**每一处**公开的基准表都必须与最近一次全量实测逐格相等。

    为什么单列一条
    --------------
    `claim_readme_metrics` 校的是**工程指标**（行数 / 模块数 / 主张条数），
    文档里另一张更显眼的表 —— 十行数据集 × 六个百分数 —— **从来没有被
    任何东西校过**。后果不是理论上的：v2.9.9 修了 `mask_text` 的两处误伤
    （`read/write`、`input/output` 被误判成路径），那次改动是正确的，
    但它**改变了聚类结果**，于是

        HPC          76 簇 / 85.1%   ->  79 簇 / 81.1%
        Zookeeper    83 簇 / 92.1%   ->  83 簇 / 91.0%
        Proxifier   117 簇 / 81.8%   ->  115 簇 / 72.7%

    全部门禁绿着，而 README、docs/BENCHMARK.md 与「唯一负例只差 2 个事件」
    一起原地不动。

    v2.11.0 补上第二半
    ------------------
    v2.10.0 加这条门禁时**只对账 README**，把同一张表在 `docs/BENCHMARK.md`
    里的那份副本放过了 —— 而那份副本上**至今仍挂着被撤回的 85.1%**，
    并且正文还主动写着「下面是修正口径后的重测」。

    这不是"补一个洞"，是**同一个洞只堵了半边**：事故驱动的门禁，
    只堵住了自己第一眼看见的那个文件。所以这里对**所有**含基准表的文档
    一视同仁 —— 少一份副本，就少一个能悄悄漂移的地方。

    怎么修才不会复发
    ----------------
    全量基准要跑两小时，进不了 CI。所以这里不重跑，而是对账：

        docs/benchmark_baseline.json             ← --write-baseline 生成的实测快照
        README.md 与 docs/BENCHMARK.md 的表       ← 人类维护的展示层

    改了任何影响聚类的代码，两者就不一致，这条主张变红。重新实测的办法
    写在 detail 里，不需要猜：

        python scripts/benchmark_loghub.py --budgets 32000 --write-baseline

    容差取 0.05pp：报告只有一位小数，0.05 以内的差与显示舍入分不开 ——
    与 `verdict()` 用的判平容差是同一个数，口径一致。
    """
    baseline_path = ROOT / "docs" / "benchmark_baseline.json"
    if not baseline_path.is_file():
        return claim("基准表与实测一致", False,
                     f"缺 {baseline_path.name}：没有实测基线就无法对账。"
                     "跑 python scripts/benchmark_loghub.py --budgets 32000 "
                     "--write-baseline 生成")
    try:
        base = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return claim("基准表与实测一致", False,
                     f"{baseline_path.name} 解析失败：{exc}")

    datasets = base.get("datasets", {})
    problems: list = []
    rows_total = 0
    checked = 0
    verdicts = 0
    unverifiable: list = []
    unlabeled = 0
    cells_unmeasured: list = []
    cells_misnamed: list = []
    seen_docs = []
    for doc_name in _BENCH_VERDICT_DOCS:
        doc_path = ROOT / doc_name
        if not doc_path.is_file():
            problems.append(f"{doc_name} 不存在")
            continue
        text = doc_path.read_text(encoding="utf-8")
        check_tables = doc_name in _BENCH_DOCS
        n_tables = len(_bench_tables(text)) if check_tables else 0
        r, c, v, probs, notes = _bench_doc_problems(
            doc_name, text, datasets, check_tables=check_tables)
        rows_total += r
        checked += c
        verdicts += v
        unverifiable.extend(notes["unverifiable"])
        unlabeled += notes["unlabeled"]
        cells_unmeasured.extend(notes["cells_unmeasured"])
        cells_misnamed.extend(notes["cells_misnamed"])
        seen_docs.append(f"{doc_name}×{n_tables}" if check_tables
                         else f"{doc_name}(无表)")
        problems.extend(probs)

    ratio_problems, ratio_note = _token_ratio_problems()
    problems.extend(ratio_problems)

    detail = (f"对账 {'、'.join(seen_docs) or '无'} 共 {rows_total} 行："
              f"{checked} 格百分数 + {verdicts} 处胜负场次"
              f"（容差 0.05pp）；{ratio_note}；"
              + ("；".join(problems[:6]) if problems else "全部一致")
              + ("；…" if len(problems) > 6 else ""))
    # 覆盖不到的地方必须**说出来**。一条只报「校了 N 处全对」的门禁，
    # 读的人无从知道剩下的断言是「校过且没问题」还是「压根没进射程」——
    # 而这两种在界面上长得一模一样。
    if unverifiable:
        detail += (f"｜另有 {len(unverifiable)} 处无实测可对账"
                   f"（如 {'、'.join(unverifiable[:2])}）")
    if unlabeled:
        detail += f"；{unlabeled} 处胜负未带口径，无从判定对账对象，仅统计不判红"
    # 「没对账」有两种**原因不同**的说法，混成一句就是错误解释：
    # 列名没对上 = 文档写错了；基线里没实测 = 那一次没跑。
    if cells_misnamed:
        detail += (f"；另有 {len(cells_misnamed)} 张表列名没对上，"
                   f"整列未参与对账（如 {'、'.join(cells_misnamed[:2])}）")
    if cells_unmeasured:
        detail += (f"；{len(cells_unmeasured)} 格基线里没有实测值，"
                   f"未参与对账（如 {'、'.join(cells_unmeasured[:2])}）")
    return claim("基准表与实测一致", not problems,
                 detail + "｜重跑：python scripts/benchmark_loghub.py "
                          "--budgets 32000 --write-baseline")


def claim_readme_module_tree() -> dict:
    """架构树必须列全包内模块（v2.8.1 新增门禁）。

    为什么单列一条：`core/causal.py` 在 v2.7.0 新增，架构树一直
    没补 —— 而上一条「行数/模块数一致」的门禁**照样绿**，因为它只数总量、
    不看名单。一棵树少画一个模块，读者会以为那部分逻辑没有独立文件。

    这正是「数字对得上但内容错了」的典型：**聚合值守住了，明细漏了**。
    所以总数与名单要分两条门禁，各守各的。

    v2.9.4：树随 README 瘦身搬到了 `docs/ARCHITECTURE.md`（README 从 853 行
    压到 190 行）。**门禁跟着搬，但它守的还是同一件事** —— 架构树列全了没有。
    一条门禁因为文档搬家而失效，比搬家本身更糟。
    """
    import re
    doc = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"[├└]──\s+(?:[\w/]+/)?([a-z_]+)\.py", doc))
    mods = {p.stem for p in (ROOT / "log_ai_compressor").rglob("*.py")
            if p.stem != "__init__"}
    missing = sorted(mods - listed)
    return claim("架构树列全包内模块", not missing,
                 f"docs/ARCHITECTURE.md 的架构树缺 {missing}" if missing
                 else f"{len(mods)} 个模块全部在架构树里")


# 接入层 = 包内「库层与门面之外」的一级模块或目录。
#
# v2.11.0 那版把名单写死成 `("cli", "web", "mcp", "ai")`，于是**新增第 5 个
# 接入层会被完全跳过**：判据是 `_layer_of(p) in layer_roots`，扫的是一个
# 子集，子集之外的越层 import 一条都看不见；而 `scanned_empty` 只管
# 「全部扫到 0 个」，扫到 3 个它就满意了。
# **这已经是同一种病第三次了**：守的是「已经看见的那一半」。
#
# 改成自动发现全部一级层（排除库层与门面），再与声明式名单比对：
# 新增接入层 → 两个集合不再相等 → 判红，detail 直接点名是哪一层。
_LAYER_LIBRARY = frozenset({"core", "rules", "export"})
_LAYER_FACADE = frozenset({"service", "constants", "__init__", "__main__"})
# 声明式名单：它不是「要扫哪些」，而是「应该存在哪些」，两者必须相等。
_LAYER_ROOTS = ("cli", "web", "mcp", "ai")


def _layer_of(p: Path, pkg: Path) -> str:
    """该文件属于哪一层。

    `cli.py` 直接躺在包根，`relative_to(pkg).parts[0]` 是 ``"cli.py"``
    而不是 ``"cli"`` —— 漏掉它就等于漏掉 cli.py，所以顶层文件取 stem，
    子目录才取首段目录名。
    """
    parts = p.relative_to(pkg).parts
    return parts[0][:-3] if len(parts) == 1 and parts[0].endswith(".py") \
        else parts[0]


def _discover_layer_roots(pkg: Path) -> set:
    """包内实际存在的接入层（一级模块或目录，排除库层与门面）。"""
    skip = _LAYER_LIBRARY | _LAYER_FACADE
    return {_layer_of(p, pkg) for p in pkg.rglob("*.py")
            if _layer_of(p, pkg) not in skip}


def claim_layering() -> dict:
    """接入层不得直接 import core / export —— service 是唯一转换点（v2.8.1）。

    这条 README 早就写着，**但它一直是假的**：`cli.py` 与 `mcp/server.py`
    都在函数体里 `from log_ai_compressor.core.evidence import ...`，绕过
    service 自己拿取证 API。写文档不等于守纪律，于是某天就会有人照着
    那两处再抄一份序列化逻辑 —— 这正是本项目吃过亏的场景（CHANGELOG 里
    「证据摘要精度两条出口不一致」就是这么来的）。

    真正的风险不是「多一层间接」，而是**同一份逻辑会长出第二个实现**。
    所以这里用 AST 核验，不靠人自觉。

    **v2.9.9 修的一处「门禁自己失能」**：原来按 `(pkg / layer).rglob("*.py")`
    逐层扫，而 `cli` 是**文件不是目录** —— `Path.rglob` 对不存在的路径**静默
    返回空**，于是 `cli.py` 一次都没被扫到，却照样报「4 个接入层 AST 核验
    通过」（那 4 是 `len(layer_roots)`，即**名字个数**，不是实际扫了几个文件）。

    这比没有门禁更糟：没有门禁你知道那里没检查；这个门禁**明确告诉你检查过
    了**。而它的 docstring 恰恰就是**为了修 `cli.py` 的越层 import 才写的**。

    两处一起改：
      1. 遍历 `pkg/**/*.py` 再按路径前缀筛接入层 —— 不再假设某层是目录；
      2. detail 报**实际扫描的文件数**，且扫描数为 0 时直接判失败。
         「扫了 0 个文件还通过」必须是红的，不能是绿的。

    **v2.11.0 修的两处同类盲区**（同一个家族：门禁在自己声称修好的
    那一类写法上，又漏掉了另一种写法）：
      3. **相对导入被整条丢掉**。原来写的是
         `isinstance(node, ast.ImportFrom) and node.module and not node.level`
         —— `not node.level` 把 `from ..core.evidence import …` 全部丢弃。
         而它在 `mcp/server.py` 里运行时完全合法、门禁却报「核验通过」。
         现在按文件的包路径解析 level：`from ..core.evidence` 在
         `log_ai_compressor.mcp` 里解析成 `log_ai_compressor.core.evidence`。
      4. **`from log_ai_compressor import core` 看不到越层**。`node.module`
         只是 `log_ai_compressor`，不以禁用前缀开头，于是整条放行 ——
         而它拿到的 `core` 正是 core 层。现在判定导入的**基名是不是包**：
         是包就把每个被 import 的名字当成可能的子模块一起校。

    **v2.11.1 修的一处**（第四次同族：又只守住了已经看见的那一半）：
      5. `layer_roots` 是字面量 `("cli","web","mcp","ai")`，判据又是
         `_layer_of(p) in layer_roots` —— **新增第 5 个接入层会被完全跳过**，
         而 `scanned_empty` 只管「全部扫到 0 个」，扫到 3 个就满意。
         现在 `_discover_layer_roots()` 自动发现全部一级层（排除库层与门面），
         并要求发现结果与 `_LAYER_ROOTS` **相等**：加一层不登记就判红，
         从名单里删一层也判红。
    """
    import ast
    banned_prefix = ("log_ai_compressor.core", "log_ai_compressor.export")
    pkg = ROOT / "log_ai_compressor"
    # 自动发现 ≠ 声明即免检：两个集合必须**相等**，不相等就是有人加了一层
    # 却没告诉门禁，或者有人从名单里删了一层却没删文件。两种都判红。
    discovered = _discover_layer_roots(pkg)
    drift = sorted(discovered ^ set(_LAYER_ROOTS))
    layer_roots = tuple(sorted(discovered))

    def _package_of(p: Path) -> str:
        """该文件**所在的包** —— 相对导入的基准点。

        `log_ai_compressor/mcp/server.py` 的包是 `log_ai_compressor.mcp`
        （不是 `log_ai_compressor.mcp.server`），`log_ai_compressor/cli.py`
        的包就是 `log_ai_compressor`。

        这一格算错，`from ..core.x import …` 就会解析到别处去，
        而错的方向恰好是「解析不出越层」—— 也就是门禁失能。
        我第一版把模块自己的名字也算进了包里，于是 level 多退一级，
        相对导入一条都抓不到：**门禁仍然绿着，却什么都没校**。
        """
        parts = list(p.relative_to(pkg).parts)
        if parts[-1] != "__init__.py":
            parts = parts[:-1]          # 模块文件的包是它所在的那个目录
        return ".".join(["log_ai_compressor", *parts])

    def _is_package(dotted: str) -> bool:
        """``dotted`` 是不是一个包（有 ``__init__.py`` 的目录）。"""
        rel = Path(*dotted.split(".")) if dotted else None
        return bool(rel) and (ROOT / rel / "__init__.py").is_file()

    def _banned(mod: str) -> bool:
        # 必须按**包边界**比：`log_ai_compressor.corefoo` 不是 core 层。
        # 用裸 startswith 会把同前缀的无关模块一起禁掉。
        return any(mod == p or mod.startswith(p + ".") for p in banned_prefix)

    def _resolve(node, package: str) -> list:
        """列出这个 import 节点引入的**全限定模块名**。"""
        if isinstance(node, ast.Import):
            return [a.name for a in node.names]
        if node.level == 0:
            base = node.module or ""
        else:
            parts = package.split(".")
            keep = len(parts) - (node.level - 1)   # level=1 是「当前包」
            if keep < 1:
                return []                          # 相对导入越出包根，无法解析
            base = ".".join(parts[:keep])
            if node.module:
                base = f"{base}.{node.module}"
        if not node.module:
            return [f"{base}.{a.name}" if base else a.name for a in node.names]
        out = [base]
        if _is_package(base):
            # `from log_ai_compressor import core` / `from .. import core`：
            # 被 import 的名字本身可能就是越层的子模块。
            out.extend(f"{base}.{a.name}" for a in node.names)
        return out

    targets = sorted(p for p in pkg.rglob("*.py")
                     if _layer_of(p, pkg) in layer_roots)
    violations = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        package = _package_of(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for mod in _resolve(node, package):
                if _banned(mod):
                    if isinstance(node, ast.ImportFrom):
                        # 按**原文写法**报，而不是按解析后的全限定名 ——
                        # `.. import core` 报成 `..` 等于什么都没说
                        shown = ("." * node.level + (node.module or "")
                                 + " import " + ",".join(a.name for a in node.names))
                    else:
                        shown = mod
                    violations.append(f"{rel}:{node.lineno} {shown}")
    # 扫到 0 个文件还判「通过」，本身就是失能 —— 门禁必须对此敏感。
    scanned_empty = not targets
    per_layer = {}
    for p in targets:
        k = _layer_of(p, pkg)
        per_layer[k] = per_layer.get(k, 0) + 1
    detail = ("；".join(sorted(set(violations))[:6]) if violations
              else "实际扫描 %d 个文件（%s）AST 核验通过"
                   % (len(targets), " ".join("%s=%d" % kv
                                             for kv in sorted(per_layer.items()))))
    if scanned_empty:
        detail = "**扫描到 0 个接入层文件** —— 门禁失能，不能判通过"
    if drift:
        detail = ("**发现结果与 _LAYER_ROOTS 不一致：" + "、".join(drift)
                  + "**（新增接入层请同步 `_LAYER_ROOTS`；"
                    "不在名单里就不会被扫，等于没有门禁）" + detail)
    return claim("分层：接入层只经 service 触达 core/export",
                 not violations and not scanned_empty and not drift, detail)


def claim_optdeps() -> dict:
    """硬依赖必须只有 PyYAML —— 轻量是 PyPI 生态的硬通货。"""
    try:
        import tomllib
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        deps = meta["project"].get("dependencies", [])
        heavy = [d for d in deps if any(k in d.lower() for k in
                                        ("fastapi", "uvicorn", "mcp", "httpx"))]
        return claim("轻量安装：硬依赖不含 Web/MCP/AI", not heavy,
                     f"硬依赖 {deps}" if deps else "未声明硬依赖")
    except Exception as exc:
        return claim("轻量安装：硬依赖不含 Web/MCP/AI", False, str(exc))


# ---------------------------------------------------------------------------
# 已知缺口（刻意打印，不藏）
# ---------------------------------------------------------------------------
KNOWN_GAPS = [
    ("根因判定的真实数据集覆盖度",
     "复现语料（自建故障场景 + 真实 Java 异常/栈帧/日志格式，**非生产日志**）"
     "5 条因果链，精确率 100%、召回率 100%、自信错误率 0%，无链对照组 "
     "0 CONFIRMED（eval_rootcause.py --repro）。"
     "**一个真实数据集**："
     "Loghub Android_v1 **全量** 1,555,005 行 / 131 条链 —— 精确率 100%、"
     "自信错误率 0%、报告口径召回率 100%（这一项最初是 **0%**，"
     "logcat 无解析模式 + 单行 Caused by 被行首锚点漏扫，修后补上）；"
     "该数据集可从 Loghub 1.0 的 Zenodo 记录 8196385 完整下载复现，"
     "v2.4.0 已补跑全量（原先测的是切片）。"
     "v2.7.0 起对外只认**报告口径**：CONFIRMED 簇对外报告的根因文本"
     "（含全部内层因变体）必须点名该链的根因类名 —— 旧口径查的是簇的"
     "样例原文，而 v2.6.0 的「一簇只报一因」缺陷在旧口径下照样满分，"
     "指标看不见自己该管的缺陷。"
     "**v2.7.0 已撤回一组数字**：Loghub Hadoop 61 个含链文件 / 5,921 条链、"
     "精确率 100%、自信错误率 0%、召回率 91.4%。撤回理由：该数据集的来源"
     "已确认不可得 —— 两个 Zenodo 记录（8275861 / 8196385）、logpai/loghub "
     "仓库根目录、本项目 data/ 下全部真实日志都查过，均无 YARN / "
     "`application_*/container_*.log`；且本地全部 Loghub 日志逐文件实测 "
     "Caused by 出现次数为 0，只有 Android_v1 有链。"
     "一个跑不出来的数字留着只会在被追问时塌台，撤。"
     "本项目现在只有两类主张：能逐条复现的，和明说给不出的。"
     "此外尚未覆盖的日志形态未知，结论未必能外推。"),
    ("与云平台根因准确率的对比",
     "不可比且不该比。RCA100 的结论是 62.5% 的根因案例至少需要两种模态，"
     "本工具只有日志一种。硬比只会输，且输了也不说明什么。"),
]


# 主张清单是**唯一口径源**：README 里「自证脚本断言 N 条主张」的那个 N、
# gather() 实际跑几条、CI 看到的条数，全部由它决定。
#
# 为什么提成模块级常量：以前 gather() 里是一份字面量列表，README 另写一个
# 数字 —— **两处各写各的**。于是 v2.9.0 加了第 10 条之后 README 两处仍写
# 9 条，而全部门禁绿着。这和 v2.8.1 那次是同一个形状：一个数字有不止一处
# 来源，却只有一处被校。
_CLAIM_FNS = (
    claim_determinism,
    claim_evidence,
    claim_confidence,
    claim_compression,
    claim_scorecard,
    claim_structured,
    claim_optdeps,
    claim_layering,
    claim_readme_metrics,
    claim_readme_module_tree,
    claim_benchmark_table,
)


def _run_claim(fn, path: Path) -> dict:
    """带日志路径参数的主张要传 path，其余直接调。

    用签名判断而不是另维护一张「谁要 path」的名单 —— 那张名单自己就会变成
    第二个会漂移的地方，正是上面那条门禁要治的病。
    """
    return fn(path) if inspect.signature(fn).parameters else fn()


def gather(path: Path) -> dict:
    claims = [_run_claim(fn, path) for fn in _CLAIM_FNS]
    return {
        "subject": str(path.name),
        "claims": claims,
        "all_hold": all(c["holds"] for c in claims),
        "known_gaps": [{"item": k, "why": v} for k, v in KNOWN_GAPS],
    }


def render(res: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print("=" * 74)
    print(f"自证：{res['subject']} —— 不必相信作者，自己跑")
    print("=" * 74)
    for c in res["claims"]:
        print(f"  {_ok(c['holds'])}  {c['claim']}")
        print(f"        {_DIM}{c['detail']}{_OFF}")
    print()
    print(f"{_YELLOW}尚无数据的主张（刻意列出，不藏）{_OFF}")
    for g in res["known_gaps"]:
        print(f"  ?  {g['item']}")
        print(f"        {_DIM}{g['why']}{_OFF}")
    print()
    print(f"总判定：{'全部主张成立' if res['all_hold'] else '存在不成立的主张'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把对外主张变成可复现的数字")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--log", default=None, help="改用指定日志做压缩/确定性实测")
    args = ap.parse_args(argv)
    path = Path(args.log) if args.log else SAMPLE
    if not path.is_file():
        print(f"错误：日志不存在 {path}", file=sys.stderr)
        return 2
    res = gather(path)
    render(res, args.json)
    return 0 if res["all_hold"] else 1


if __name__ == "__main__":
    sys.exit(main())
