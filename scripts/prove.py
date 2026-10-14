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
    """CONFIRMED 只能来自 Caused-by 链；日志里没有因果链就必须零。"""
    r = analyze_files([path], context_lines=2)
    confirmed = [c for c in r.clusters
                 if c.root_cause_confidence == "CONFIRMED"]
    verdict = (r.evidence or {}).get("verdict")
    has_chain = "Caused by:" in path.read_text(encoding="utf-8", errors="replace")
    consistent = (not confirmed) if not has_chain else True
    return claim("置信档位：无因果链时零确定性断言",
                 consistent,
                 f"日志含 Caused-by：{has_chain}；"
                 f"判 CONFIRMED {len(confirmed)} 个；verdict={verdict}")


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
     "**两个真实数据集**：Loghub Hadoop 全量 61 个含链文件 / 5,921 条链 —— "
     "精确率 100%、自信错误率 0%、严格口径召回率 91.4%（宽松口径 99.9%，"
     "只要求行区间盖住、不问簇说的是不是这一条，两者差 8.6 个点，"
     "某文件 2,643 条链只有 12 个 CONFIRMED 断言却'认领'了 5,836 次；"
     "**对外只认严格口径**）。Loghub Android_v1 切片 228,710 行 / 131 条链 —— "
     "精确率 100%、自信错误率 0%、严格召回 100%（这一项最初是 **0%**，"
     "logcat 无解析模式 + 单行 Caused by 被行首锚点漏扫，修后补上）。"
     "**但样本仍偏**：Hadoop 的 5,838/5,921 条链来自 5 个巨型文件，"
     "Android 是切片（链密度高 6.8 倍），且 Android 全量因聚类慢约 30 倍未跑。"
     "尚未覆盖的日志形态未知，结论未必能外推。"),
    ("与云平台根因准确率的对比",
     "不可比且不该比。RCA100 的结论是 62.5% 的根因案例至少需要两种模态，"
     "本工具只有日志一种。硬比只会输，且输了也不说明什么。"),
]


def gather(path: Path) -> dict:
    claims = [
        claim_determinism(path),
        claim_evidence(path),
        claim_confidence(path),
        claim_compression(path),
        claim_scorecard(),
        claim_structured(),
        claim_optdeps(),
    ]
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
