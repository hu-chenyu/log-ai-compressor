#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全仓死代码/未使用符号扫描（只读）。

判定口径（刻意保守，宁可漏报不可误报）：
- 只统计**模块顶层**的 def / class / 常量赋值，跳过方法与私有名（_ 开头）
- 排除定义所在行本身
- 在整个项目（源码 + tests + scripts + docs 提及）里统计出现次数
- 出现 1 次（= 只有定义处）判定为「未使用」，并把证据打出来供人工复核
- 显式再导出（`from x import y as y`、以及出现在 __all__ 里）不算死代码
"""
from __future__ import annotations
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "log_ai_compressor"
SCAN_DIRS = [SRC, ROOT / "tests", ROOT / "scripts"]


def collect_text() -> dict[str, str]:
    out = {}
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".yaml", ".md", ".txt", ".html", ".js"):
                try:
                    out[str(p.relative_to(ROOT))] = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
    return out


def module_symbols(path: Path) -> list[tuple[str, str, int]]:
    """返回 [(kind, name, lineno)]，只取模块顶层非私有符号。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                out.append((type(node).__name__, node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and not t.id.startswith("_"):
                    out.append(("Const", t.id, node.lineno))
    return out


def main() -> int:
    texts = collect_text()
    dunder_all = set()
    for rel, t in texts.items():
        if not rel.endswith(".py"):
            continue
        for m in re.finditer(r"__all__\s*=\s*\[(.*?)\]", t, re.S):
            dunder_all.update(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))

    # 按「出现总次数」而不是「出现在几个文件」统计：
    # 只在自己模块内被使用的常量（LEVEL_TOKENS 等）必须算作已使用，
    # 否则会把活代码误判成死代码 —— 删代码时这种误报最贵。
    def occurrences(name: str, skip_rel: str, skip_line: int) -> int:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        n = 0
        for rel, t in texts.items():
            for i, line in enumerate(t.splitlines(), 1):
                hits = len(pat.findall(line))
                if rel == skip_rel and i == skip_line:
                    hits = max(0, hits - 1)      # 扣掉定义处本身
                n += hits
        return n

    print("=" * 74)
    print("  未使用符号扫描（模块顶层，扣除定义处后总出现次数 = 0 即未使用）")
    print("=" * 74)
    total = 0
    for py in sorted(SRC.rglob("*.py")):
        rel = str(py.relative_to(ROOT))
        syms = module_symbols(py)
        if not syms:
            continue
        dead = []
        for kind, name, lineno in syms:
            if occurrences(name, rel, lineno) == 0:
                dead.append((kind, name, lineno))
        if dead:
            print(f"\n  {rel}")
            for kind, name, lineno in dead:
                note = "  (__all__ 显式再导出，可能是对外 API)" if name in dunder_all else ""
                print(f"    L{lineno:<5} {kind:<14} {name}{note}")
                total += 1
    print(f"\n  合计候选：{total} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())