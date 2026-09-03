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
import sys
import time
from pathlib import Path
from typing import List, Optional

from log_ai_compressor import __version__
from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_TOP_N,
    LEVEL_ORDER,
)
from log_ai_compressor.core.comparator import compare_files
from log_ai_compressor.core.pipeline import analyze_file
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
    # 修复缺陷R10：默认级别含 FATAL（--level FATAL 同样受支持，
    # 与 GUI 复选框语义一致：勾选/传入才显示）
    p.add_argument("--level", "-l", default="FATAL,ERROR,FAIL",
                   help="级别过滤（逗号分隔，默认 FATAL,ERROR,FAIL；"
                        "可用值 FATAL/ERROR/FAIL/WARN/INFO/DEBUG/TRACE）")
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

    # gui 子命令
    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_gui.set_defaults(func=cmd_gui)
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
    levels = [lv.strip().upper() for lv in raw.split(",") if lv.strip()]
    valid = [lv for lv in levels if lv in LEVEL_ORDER]
    return valid if valid else ["ERROR", "FAIL"]


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


def cmd_gui(args) -> int:
    from log_ai_compressor.gui.app import main as gui_main
    gui_main()
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
