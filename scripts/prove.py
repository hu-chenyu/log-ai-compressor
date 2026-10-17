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
    return claim("证据可复核：每条结论带行号出处且核对通过",
                 report["ok"] and covered == total,
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
    lines = 0
    for p in files:
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if s and not s.startswith("#"):
                lines += 1
    return {"modules": len(files), "lines": lines}


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

    return claim(
        "README 工程指标与仓库一致", metrics_ok and counts_ok,
        f"README 写 {claimed_lines} 行 / {claimed_modules} 个模块；"
        f"实测 {m['lines']} 行 / {m['modules']} 个模块"
        f"（口径：log_ai_compressor/**/*.py 非空非注释行）；"
        f"README 写自证主张 {found or '未找到'} 条，本文件实际登记 {expected} 条")


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
    """
    import ast
    layer_roots = ("cli", "web", "mcp", "ai")
    banned_prefix = ("log_ai_compressor.core", "log_ai_compressor.export")
    pkg = ROOT / "log_ai_compressor"

    def _layer_of(p: Path) -> str:
        """该文件属于哪一层。

        `cli.py` 直接躺在包根，`relative_to(pkg).parts[0]` 是 ``"cli.py"``
        而不是 ``"cli"`` —— 漏掉它就等于漏掉 cli.py，所以顶层文件取 stem，
        子目录才取首段目录名。
        """
        parts = p.relative_to(pkg).parts
        return parts[0][:-3] if len(parts) == 1 and parts[0].endswith(".py") \
            else parts[0]

    targets = sorted(p for p in pkg.rglob("*.py") if _layer_of(p) in layer_roots)
    violations = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mod = node.module
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith(banned_prefix):
                        violations.append(f"{rel}: import {a.name}")
            if mod and mod.startswith(banned_prefix):
                violations.append(f"{rel}:{node.lineno} from {mod} import …")
    # 扫到 0 个文件还判「通过」，本身就是失能 —— 门禁必须对此敏感。
    scanned_empty = not targets
    per_layer = {}
    for p in targets:
        k = _layer_of(p)
        per_layer[k] = per_layer.get(k, 0) + 1
    detail = ("；".join(sorted(set(violations))[:6]) if violations
              else "实际扫描 %d 个文件（%s）AST 核验通过"
                   % (len(targets), " ".join("%s=%d" % kv
                                             for kv in sorted(per_layer.items()))))
    if scanned_empty:
        detail = "**扫描到 0 个接入层文件** —— 门禁失能，不能判通过"
    return claim("分层：接入层只经 service 触达 core/export",
                 not violations and not scanned_empty, detail)


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
