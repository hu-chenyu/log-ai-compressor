# -*- coding: utf-8 -*-
"""CLI 命令行入口：支持脚本 / 流水线自动化调用。

用法示例
--------
    # 分析单个日志并导出 Markdown 报告
    log-ai-compressor run test.log --top 20 --level ERROR,FAIL -o report.md

    # JSON 格式导出
    log-ai-compressor run test.log --format json -o report.json

    # 多文件对比（版本对比 / 修复前后对比）
    log-ai-compressor compare v1.log v2.log -o diff.md

    # 查看可用解析规则模板
    log-ai-compressor rules list

    # 启动 GUI
    log-ai-compressor gui
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from log_ai_compressor import __version__
from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_TOP_N,
    canonical_level,
)
from log_ai_compressor.core.comparator import compare_files
from log_ai_compressor.core.pipeline import analyze_file, analyze_files
from log_ai_compressor.rules.engine import RuleSetError
from log_ai_compressor.export.reporters import (
    brief_summary,
    compare_to_markdown,
    to_json,
    to_markdown,
    to_text,
)

# 导出格式 -> 生成函数
_FORMAT_TABLE = {"md": to_markdown, "markdown": to_markdown,
                 "json": to_json, "txt": to_text, "text": to_text}
_SUFFIX_FORMAT = {".md": "md", ".markdown": "md", ".json": "json",
                  ".txt": "txt", ".text": "txt"}

EXIT_OK, EXIT_ERROR, EXIT_CANCELLED = 0, 1, 130


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _add_filter_options(p: argparse.ArgumentParser) -> None:
    """公共过滤参数（run / compare 共用）。"""
    # 修复缺陷R40：默认级别 ERROR+FAIL（FATAL 已删除归一 ERROR；
    # --level FATAL 仍兼容 —— 归一映射到 ERROR）
    p.add_argument("--level", "-l", default="ERROR,FAIL",
                   help="级别过滤（逗号分隔，默认 ERROR,FAIL；"
                        "可用值 ERROR/FAIL/WARN/INFO/DEBUG/TRACE）")
    p.add_argument("--include", "-k", default="",
                   help="包含关键字（逗号分隔，任一命中保留）")
    p.add_argument("--exclude", default="",
                   help="排除关键字（逗号分隔，任一命中剔除）")
    p.add_argument("--top", "-t", type=int, default=DEFAULT_TOP_N,
                   help=f"Top N 错误数（默认 {DEFAULT_TOP_N}）")
    p.add_argument("--context", type=int, default=DEFAULT_CONTEXT_LINES,
                   help=f"典型样例上下文行数（默认 {DEFAULT_CONTEXT_LINES}，"
                        "≥0 不限上限）")
    p.add_argument("--rule", "-r", default=None,
                   help="解析规则：模板名(generic/embedded/jenkins)或 YAML 路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="log-ai-compressor",
        description="日志AI压缩器：海量日志压缩投喂大模型 / 快速故障排查",
    )
    parser.add_argument("--version", action="version",
                        version=f"log-ai-compressor v{__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # run 子命令
    p_run = sub.add_parser("run", help="分析单个日志文件")
    p_run.add_argument("file", help="日志文件路径")
    _add_filter_options(p_run)
    p_run.add_argument("-o", "--output", default=None,
                       help="输出报告路径（默认 <日志名>_report.md）")
    p_run.add_argument("--format", "-f", default=None,
                       choices=sorted(_FORMAT_TABLE),
                       help="输出格式（默认按输出后缀推断，否则 md）")
    p_run.add_argument("--no-analysis", action="store_true",
                       help="跳过智能分析（根因/异常/优先级）")
    p_run.add_argument("--quiet", "-q", action="store_true",
                       help="抑制进度输出")
    p_run.set_defaults(func=cmd_run)

    # compare 子命令
    p_cmp = sub.add_parser("compare", help="多文件对比分析（2~3 个文件）")
    p_cmp.add_argument("files", nargs="+", help="日志文件（第一个为基准）")
    _add_filter_options(p_cmp)
    p_cmp.add_argument("-o", "--output", default=None,
                       help="输出报告路径（默认 compare_report.md）")
    p_cmp.set_defaults(func=cmd_compare)

    # rules 子命令
    p_rules = sub.add_parser("rules", help="查看解析规则模板")
    p_rules.add_argument("action", nargs="?", default="list",
                         choices=["list", "show"],
                         help="list 列出模板 / show 查看模板内容")
    p_rules.add_argument("name", nargs="?", default=None, help="模板名")
    p_rules.set_defaults(func=cmd_rules)

    # verify 子命令（取证层自校验入口）
    p_verify = sub.add_parser(
        "verify",
        help="证据自校验：核对结论出处是否仍与原始日志一致")
    p_verify.add_argument("files", nargs="*", default=[],
                          help="日志文件路径（与 --report 二选一或同时给出）")
    _add_filter_options(p_verify)
    p_verify.add_argument(
        "--report", default=None, metavar="REPORT.json",
        help="复核一份已导出的 JSON 报告（推荐：报告发出后日志可能已变）")
    p_verify.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_verify.set_defaults(func=cmd_verify)

    # web 子命令（主界面）
    p_web = sub.add_parser("web", help="启动本地 Web 界面（默认，双击启动器用的就是它）")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="绑定地址（默认仅本机；绑定非本机地址需加 --allow-remote）")
    p_web.add_argument("--allow-remote", action="store_true",
                       help="确认要绑定非本机地址（如 0.0.0.0）。服务无鉴权，"
                            "网络内任何人都能读本机文件、提交分析，仅限可信内网使用")
    p_web.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p_web.add_argument("--no-browser", action="store_true",
                       help="启动后不自动打开浏览器")
    p_web.add_argument("--log-level", default="warning",
                       choices=["critical", "error", "warning", "info", "debug"])
    p_web.set_defaults(func=cmd_web)

    # mcp 子命令
    p_mcp = sub.add_parser("mcp", help="启动 MCP 服务器（供 Claude Code / Codex 等调用）")
    p_mcp.add_argument("--transport", default="stdio",
                       choices=["stdio", "streamable-http", "sse"],
                       help="stdio=本地 Agent；streamable-http=远程（仅本机）")
    p_mcp.add_argument("--port", type=int, default=8766, help="http 传输端口")
    p_mcp.add_argument("--install", metavar="CLIENT", nargs="?", const="claude-code",
                       help="打印指定客户端的 MCP 配置片段后退出")
    p_mcp.set_defaults(func=cmd_mcp)

    # ai 子命令
    p_ai = sub.add_parser("ai", help="AI 解读（可选功能，不配置也能用其他全部能力）")
    ai_sub = p_ai.add_subparsers(dest="ai_action", required=True)
    p_ai_status = ai_sub.add_parser("status", help="查看 AI 配置状态")
    p_ai_status.set_defaults(func=cmd_ai_status)
    p_ai_set = ai_sub.add_parser("config", help="配置 AI 服务商")
    p_ai_set.add_argument("--provider", required=True,
                          help="none/deepseek/qwen/glm/kimi/openai/ollama/custom")
    p_ai_set.add_argument("--base-url", default="", help="Base URL（默认取服务商预设）")
    p_ai_set.add_argument("--model", default="", help="模型名（默认取服务商预设）")
    p_ai_set.add_argument("--key", default="", help="API Key（可留空，改用环境变量）")
    p_ai_set.set_defaults(func=cmd_ai_config)
    p_ai_test = ai_sub.add_parser("test", help="发一个最小请求验证连通性")
    p_ai_test.set_defaults(func=cmd_ai_test)
    p_ai_explain = ai_sub.add_parser("explain", help="对日志文件生成 AI 解读")
    p_ai_explain.add_argument("file", help="日志文件路径")
    p_ai_explain.add_argument("--cluster", type=int, default=None,
                              help="只解读指定簇号（默认整份结果）")
    p_ai_explain.add_argument("-q", "--question", default="",
                              help="额外追问（可选）")
    # --top / --level / --context / --rule 等由 _add_filter_options 统一提供
    _add_filter_options(p_ai_explain)
    p_ai_explain.set_defaults(func=cmd_ai_explain)
    return parser


# ---------------------------------------------------------------------------
# 进度显示
# ---------------------------------------------------------------------------
class _ConsoleProgress:
    """终端进度显示（仅 TTY 下逐行刷新，避免污染管道输出）。"""

    def __init__(self, enabled: bool):
        self._enabled = enabled and sys.stderr.isatty()
        self._t0 = time.time()

    def __call__(self, data: dict) -> None:
        if not self._enabled:
            return
        if data.get("phase") == "done":
            sys.stderr.write("\n")
            return
        sys.stderr.write(
            f"\r已处理 {data['lines']:>10,} 行 | "
            f"{data['lps']:>8,.0f} 行/秒 | 错误种类 {data['clusters']:>4} | "
            f"耗时 {data['elapsed']:>5.1f}s")
        sys.stderr.flush()


def _parse_keywords(raw: str) -> List[str]:
    return [k.strip() for k in raw.split(",") if k and k.strip()] if raw else []


def _parse_levels(raw: str) -> List[str]:
    """级别列表解析：先经别名归一，再校验有效性。

    修复：此前只认 LEVEL_ORDER 里的规范名，其余一律丢弃。于是
    `--level WARNING`（用户明确只要警告）会因为 WARNING 不在
    LEVEL_ORDER 里而被整条丢弃、退回默认的 ERROR,FAIL —— 报告里全是
    错误、用户要的警告一条没有，且没有任何提示；
    `--level FATAL,INFO` 更荒唐：FATAL 被丢，只剩 INFO。
    现在按 constants.canonical_level 归一（FATAL/WARNING/ERR → ERROR/
    WARN/ERROR 等），保持用户书写顺序去重，全非法才回退默认。

    注意用 canonical_level 而不是 normalize_level：后者对不认识的写法
    保守返回 INFO，那样 `--level EROR`（拼错）会变成「只看 INFO」，
    报告里全是信息级条目 —— 静默给出用户没要的东西比报错更糟。
    """
    levels: List[str] = []
    for lv in (raw or "").split(","):
        canon = canonical_level(lv)
        if canon and canon not in levels:
            levels.append(canon)
    return levels or ["ERROR", "FAIL"]


def _resolve_format(args) -> str:
    if args.format:
        return args.format
    if args.output:
        return _SUFFIX_FORMAT.get(Path(args.output).suffix.lower(), "md")
    return "md"


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"错误：日志文件不存在 {path}", file=sys.stderr)
        return EXIT_ERROR

    progress = _ConsoleProgress(enabled=not args.quiet)
    try:
        result = analyze_file(
            path,
            levels=_parse_levels(args.level),
            include=_parse_keywords(args.include),
            exclude=_parse_keywords(args.exclude),
            top_n=args.top,
            context_lines=args.context,
            rule=args.rule,
            analyze=not args.no_analysis,
            progress_cb=progress,
        )
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except RuleSetError as exc:
        # 自定义规则 YAML 写错是用户输入问题，给一句话而不是 traceback
        print(f"错误：解析规则有问题 —— {exc}", file=sys.stderr)
        return EXIT_ERROR

    # 摘要输出到 stdout（可直接管道组合）
    print(brief_summary(result, top_n=args.top))

    # 报告落盘
    output = Path(args.output) if args.output else \
        path.with_name(f"{path.stem}_report.{_resolve_format(args)}")
    fmt = _resolve_format(args)
    content = _FORMAT_TABLE[fmt](result, top_n=args.top)
    output.write_text(content, encoding="utf-8")

    size_kb = output.stat().st_size / 1024
    print(f"报告已导出: {output}（{fmt.upper()}，{size_kb:.1f} KB）")
    if result.stats.truncated:
        print("注意：处理被取消，报告基于已完成的增量结果。", file=sys.stderr)
        return EXIT_CANCELLED
    return EXIT_OK


def cmd_compare(args) -> int:
    paths = [Path(f) for f in args.files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"错误：文件不存在 {' '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR
    if len(paths) < 2:
        print("错误：对比分析至少需要 2 个日志文件", file=sys.stderr)
        return EXIT_ERROR
    if len(paths) > 3:
        print("错误：最多支持 3 个日志文件对比", file=sys.stderr)
        return EXIT_ERROR

    try:
        results = compare_files(
            paths,
            levels=_parse_levels(args.level),
            include=_parse_keywords(args.include),
            exclude=_parse_keywords(args.exclude),
            top_n=args.top,
            context_lines=args.context,
            rule=args.rule,
        )
    except Exception as exc:  # 规则文件错误等用户输入问题
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    content = compare_to_markdown(results)
    output = Path(args.output) if args.output else Path("compare_report.md")
    output.write_text(content, encoding="utf-8")
    print(f"对比报告已导出: {output}")
    return EXIT_OK


def cmd_rules(args) -> int:
    from log_ai_compressor.rules.engine import list_presets, load_ruleset

    if args.action == "list":
        print("可用解析规则模板：")
        for name in list_presets():
            try:
                rs = load_ruleset(name)
                print(f"  {name:<12} {rs.description}")
            except Exception:
                print(f"  {name:<12} (加载失败)")
        print("\n提示：--rule 支持传入自定义 YAML 规则文件路径")
    else:
        name = args.name or "generic"
        try:
            rs = load_ruleset(name)
        except Exception as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"规则集：{rs.name}（来源 {rs.source}）")
        print(f"说明：{rs.description}")
        print(f"模式（{len(rs.patterns)} 条）：")
        for rule in rs.patterns:
            print(f"  - {rule.name}: {rule.pattern_text[:90]}")
        print(f"堆栈特征（{len(rs.stack_indicators)} 条）、"
              f"级别提示（{len(rs.level_hints)} 级）")
    return EXIT_OK


def cmd_verify(args) -> int:
    """证据自校验：把结论的出处拉回原始日志逐条核对。

    两种模式，含义完全不同，别混用：

    `--report report.json`（**有意义的那个**）
        复核一份**之前导出**的报告。报告发出去之后日志可能已经变了
        （轮转清理、有人手改、归档重写）—— 这时才发现「当初那条结论
        其实对不上原文」，比从来没给过结论更糟。

    `verify app.log`（自检）
        当场分析当场校验。它证明的是「当前这一刻的结论与当前日志
        一致」，是个健康检查，不是取证。**不要**拿它当「这份报告还
        成立吗」的答案 —— 那必然成立，因为它就是拿这份日志算的。
    """
    from log_ai_compressor import service as S
    from log_ai_compressor.core.evidence import EvidenceRef

    refs: List = []
    fingerprint = ""
    source_label = ""

    if args.report:
        report_path = Path(args.report)
        if not report_path.is_file():
            print(f"错误：报告文件不存在 {report_path}", file=sys.stderr)
            return EXIT_ERROR
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"错误：报告不是合法 JSON —— {exc}", file=sys.stderr)
            return EXIT_ERROR
        for c in payload.get("clusters") or []:
            for e in c.get("evidence") or []:
                refs.append(EvidenceRef(
                    evidence_id=e.get("evidence_id", ""),
                    source=e.get("source", ""),
                    line_start=int(e.get("line_start", 0)),
                    line_end=int(e.get("line_end", 0)),
                    kind=e.get("kind", "sample"),
                    digest=e.get("digest", ""),
                    # 修复缺陷#1：复核要复现摄入时的解码路径（旧报告无此
                    # 字段则回退自动探测）
                    encoding=e.get("encoding", ""),
                    note=e.get("note", "")))
        source_label = f"报告 {report_path.name}"
        if not refs:
            print(f"错误：{report_path} 里没有任何证据引用"
                  f"（可能是旧版本导出的报告，或不是本工具导出的 JSON）",
                  file=sys.stderr)
            return EXIT_ERROR
    else:
        if not args.files:
            print("用法：verify <日志文件…> 或 verify --report <report.json>",
                  file=sys.stderr)
            return EXIT_ERROR
        paths = [Path(f) for f in args.files]
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            print(f"错误：文件不存在 {' '.join(missing)}", file=sys.stderr)
            return EXIT_ERROR
        try:
            result = analyze_files(paths, levels=_parse_levels(args.level),
                                   include=_parse_keywords(args.include),
                                   exclude=_parse_keywords(args.exclude),
                                   top_n=args.top, context_lines=args.context,
                                   rule=args.rule)
        except RuleSetError as exc:
            print(f"错误：解析规则有问题 —— {exc}", file=sys.stderr)
            return EXIT_ERROR
        except FileNotFoundError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_ERROR
        refs = [ref for c in result.clusters for ref in c.evidence]
        fingerprint = S.result_fingerprint(result)
        source_label = result.stats.source

    from log_ai_compressor.core.evidence import verify_refs
    report = verify_refs(refs).as_dict()
    report["fingerprint"] = fingerprint
    report["source"] = source_label
    report["mode"] = "report" if args.report else "self-check"

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK if report["ok"] else EXIT_ERROR

    mode_label = ("复核已导出报告" if args.report else "当场自检（仅证明此刻自洽）")
    print(f"模式：{mode_label}")
    print(f"对象：{source_label}")
    print(f"证据：共 {report['checked']} 条引用，"
          f"匹配 {report['matched']} 条，失配 {report['mismatched']} 条，"
          f"源不可读 {report['missing_source']} 条")
    if fingerprint:
        print(f"确定性指纹：{fingerprint}")
    for d in report["details"]:
        if d["status"] in ("MISMATCH", "OUT_OF_RANGE", "MISSING_SOURCE",
                           "NO_SOURCE"):
            print(f"  ✗ {d['status']} {d.get('evidence_id')} {d.get('source')}"
                  f" 期望 {d.get('expected', '-')} 实际 {d.get('actual', '-')}",
                  file=sys.stderr)
    print()
    print(f"自校验结论：{report['verdict']}")
    if report["verdict"] == "VERIFIED":
        print("每条结论都能在原始日志里逐行复核。")
        return EXIT_OK
    if report["verdict"] == "NO_EVIDENCE":
        print("没有可回读的证据引用（粘贴文本，或条目超出摄入窗口）。")
        return EXIT_OK
    print("存在对不上的引用 —— 结论可能来自已被修改的日志，请勿直接采信。",
          file=sys.stderr)
    return EXIT_ERROR


def cmd_web(args) -> int:
    """启动本地 Web 界面。"""
    try:
        from log_ai_compressor.web.server import serve
    except ImportError as exc:
        print(f"错误：Web 组件不可用（{exc}）。"
              f"请执行：pip install fastapi \"uvicorn[standard]\"", file=sys.stderr)
        return EXIT_ERROR
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # 修复缺陷#5：远端绑定必须显式确认 —— 此前只打一行警告照样启动，
        # 而服务无鉴权、/api/fs/file 可读任意路径，脚本化启动时警告会被
        # 滚屏吞掉，等于把本机文件系统挂到网络上。
        if not args.allow_remote:
            print(f"错误：拒绝绑定 {args.host} —— 服务没有鉴权，网络内任何设备"
                  f"都能读本机文件、提交分析。仅限可信内网时，请明确加"
                  f" --allow-remote 再启动。", file=sys.stderr)
            return EXIT_ERROR
        print(f"警告：已按 --allow-remote 绑定 {args.host}，局域网内其他设备"
              f"可访问本机日志分析能力且服务没有鉴权。", file=sys.stderr)
    try:
        serve(host=args.host, port=args.port,
              open_browser=not args.no_browser, log_level=args.log_level)
    except OSError as exc:
        print(f"错误：端口 {args.port} 启动失败（{exc}）。"
              f"换一个：--port 8766", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已停止")
    return EXIT_OK


# 各 AI 客户端的 MCP 配置片段（--install 打印用）
_MCP_CLIENTS = {
    "claude-code": (
        "Claude Code / Claude Desktop",
        'claude mcp add --scope user log-ai-compressor -- '
        'python -m log_ai_compressor.mcp.server',
    ),
    "codex": (
        "Codex",
        'codex mcp add log-ai-compressor -- '
        'python -m log_ai_compressor.mcp.server',
    ),
    "mavis": (
        "MiniMax Code / 其它支持 JSON 配置的客户端",
        '{\n'
        '  "mcpServers": {\n'
        '    "log-ai-compressor": {\n'
        '      "command": "python",\n'
        '      "args": ["-m", "log_ai_compressor.mcp.server"]\n'
        '    }\n'
        '  }\n'
        '}',
    ),
}


def cmd_mcp(args) -> int:
    """启动 MCP 服务器或打印客户端配置。"""
    if args.install:
        title, snippet = _MCP_CLIENTS.get(
            args.install,
            (args.install, _MCP_CLIENTS["mavis"][1]))
        print(f"# {title}\n{snippet}")
        return EXIT_OK
    try:
        from log_ai_compressor.mcp.server import main as mcp_main
    except ImportError as exc:
        print(f"错误：MCP 组件不可用（{exc}）。"
              f"请执行：pip install mcp", file=sys.stderr)
        return EXIT_ERROR
    if args.transport == "stdio":
        # stdio 传输下 stdout 是协议通道，任何 print 都会污染 JSON-RPC
        print("MCP 服务器以 stdio 模式启动（stdout 专用于协议，"
              "诊断信息走 stderr）", file=sys.stderr)
    else:
        print(f"MCP 服务器以 {args.transport} 启动："
              f"http://127.0.0.1:{args.port}", file=sys.stderr)
    mcp_main(args.transport, args.port)
    return EXIT_OK


def main_mcp() -> int:
    """独立 MCP 入口（console script ``log-ai-compressor-mcp``）。

    为什么需要它：MCP 客户端与官方 Registry 都按「**可执行名 = 包名**」
    的约定去拉起服务器（例如 ``uvx log-ai-compressor``）。而主 CLI 需要
    子命令，裸跑只会打印帮助并以退出码 2 结束 —— 客户端拿到的不是 MCP
    服务器，而是一段 usage 文本。这个入口让该约定成立，Registry 收录
    后才能真正开箱即用。

    诊断信息一律走 stderr：stdio 传输下 stdout 是 JSON-RPC 协议通道，
    任何 print 都会污染协议流。
    """
    try:
        from log_ai_compressor.mcp.server import main as mcp_main
    except ImportError as exc:
        print(f"错误：MCP 组件不可用（{exc}）。\n"
              f"      安装：pip install \"log-ai-compressor[mcp]\"",
              file=sys.stderr)
        return EXIT_ERROR
    print("log-ai-compressor MCP 服务器启动中"
          "（stdout 为 JSON-RPC 协议通道，诊断信息走 stderr）",
          file=sys.stderr)
    mcp_main("stdio")
    return EXIT_OK


def _import_ai():
    """导入 AI 层；缺可选依赖时给出可执行的提示而不是裸 ImportError。

    httpx 被拆成了 [ai] extra（见 pyproject），裸装包的用户会遇到它。
    """
    try:
        from log_ai_compressor import ai as ai_mod
        return ai_mod
    except ImportError as exc:
        print(f"错误：AI 解读需要额外的 httpx 依赖（{exc}）。\n"
              f"      安装：pip install \"log-ai-compressor[ai]\"\n"
              f"      不装也不影响其它功能 —— 聚类 / 根因判定 / 异常检测 / "
              f"报告导出都是本地算法。", file=sys.stderr)
        return None


def cmd_ai_status(args) -> int:
    """打印 AI 配置状态。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    st = ai.describe_config()
    cfg = st.get("config", {})
    print(f"AI 解读：{'已启用' if st.get('available') else '未启用'}")
    if st.get("available"):
        print(f"  服务商：{cfg.get('provider')}   模型：{cfg.get('model')}")
        print(f"  地址：  {cfg.get('base_url')}")
        print(f"  Key：   {cfg.get('masked_key') or '（无需 Key）'}")
    else:
        print(f"  原因：{st.get('reason')}")
    print(f"\n配置文件：{st.get('config_file')}")
    print("\n可用服务商：")
    for p in st.get("providers", []):
        need = f"（环境变量 {p['env_key']}）" if p.get("env_key") else "（无需 Key）"
        print(f"  {p['key']:<10} {p['label']} {need}")
        if p.get("note"):
            print(f"             {p['note']}")
    print("\n提示：AI 解读是可选的。不配置也能正常使用聚类 / 根因判定 / "
          "异常检测 / 导出等全部本地能力。")
    return EXIT_OK


def cmd_ai_config(args) -> int:
    """保存 AI 服务商配置。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    try:
        cfg = ai.load_config({
            "provider": args.provider,
            "base_url": args.base_url,
            "model": args.model,
            "api_key": args.key,
        })
        ai.save_config(cfg)
    except ai.LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:                          # noqa: BLE001
        print(f"错误：保存失败 {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"已保存：{cfg.provider} / {cfg.model} / {cfg.base_url}")
    if not cfg.enabled:
        print("提示：当前配置尚未启用。"
              + ("（该服务商需要 API Key，用 --key 或环境变量提供）"
                 if cfg.provider != "ollama" else "（请确认 Ollama 已启动并已拉取模型）"))
    return EXIT_OK


def cmd_ai_test(args) -> int:
    """发一个最小请求验证连通性。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    cfg = ai.load_config()
    if not cfg.enabled:
        print("AI 未启用，先执行 log-ai-compressor ai config --provider <名字>",
              file=sys.stderr)
        return EXIT_ERROR
    print(f"测试 {cfg.provider} / {cfg.model} @ {cfg.base_url} …")
    try:
        out = ai.chat("只回复两个字：可用", cfg=cfg)
    except ai.LLMError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"成功，模型返回：{out[:80]}")
    return EXIT_OK


def cmd_ai_explain(args) -> int:
    """对日志文件生成 AI 解读。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    from log_ai_compressor.core.pipeline import analyze_file
    path = Path(args.file)
    if not path.is_file():
        print(f"错误：日志文件不存在 {path}", file=sys.stderr)
        return EXIT_ERROR
    print("本地分析中…", file=sys.stderr)
    try:
        result = analyze_file(
            path,
            levels=_parse_levels(args.level),
            include=_parse_keywords(args.include),
            exclude=_parse_keywords(args.exclude),
            context_lines=args.context,
            rule=args.rule,
        )
    except Exception as exc:                          # noqa: BLE001
        print(f"错误：本地分析失败 {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"调用 AI 解读中…（{path.name}，"
          f"{len(result.clusters)} 个错误簇）", file=sys.stderr)
    try:
        if args.cluster is not None:
            text = ai.explain_cluster(result, args.cluster, question=args.question)
        else:
            text = ai.explain_result(result, top_n=args.top, question=args.question)
    except ai.LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)
    return EXIT_OK


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
