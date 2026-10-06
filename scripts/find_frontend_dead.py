#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端死代码扫描（只读）：未调用的 JS 函数 + 未被引用的 CSS 类 / HTML id。

为什么需要
----------
零构建的前端没有打包器替你做 tree-shaking，写完删掉或改坏一个函数，
不会有任何报错 —— 只会静静躺在 35KB 的 app.js 里。Python 侧有 ruff 的
F401/F811 兜着，前端这边什么都���没有。

判定口径（宁可漏报不可误报）：
- JS：顶层 `function NAME` / `const NAME =` / `class NAME`，
  在 js+html 里统计出现次数，扣掉定义处后 = 0 才算未使用。
  事件回调（onclick="NAME(...)" 之类）也算引用，所以只看出现次数即可。
- CSS：只扫**单类名选择器** `.foo`，跳过 `.a.b`、`:hover` 后缀、
  伪元素、媒体查询里的嵌套。类名在 css/html/js 三者里都找不到即未使用。
- HTML：id 必须在 js 或 css 里被引用才有效；纯展示用的除外由人工看。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "log_ai_compressor" / "web" / "static"
JS = STATIC / "app.js"
CSS = STATIC / "style.css"
HTML = STATIC / "index.html"


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def main() -> int:
    if not JS.is_file():
        raise SystemExit(f"找不到 {JS}")
    js_raw = JS.read_text(encoding="utf-8")
    css_raw = CSS.read_text(encoding="utf-8")
    html_raw = HTML.read_text(encoding="utf-8")
    js = strip_comments(js_raw)
    css = strip_comments(css_raw)
    html = strip_comments(html_raw)

    print("=" * 70)
    print("  前端死代码扫描")
    print("=" * 70)

    # ---- JS 未调用函数 ----
    defs: list[tuple[str, str, int]] = []
    for m in re.finditer(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M):
        defs.append(("function", m.group(1), js[:m.start()].count("\n") + 1))
    for m in re.finditer(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)",
                         js, re.M):
        defs.append(("const", m.group(1), js[:m.start()].count("\n") + 1))

    js_dead = []
    for kind, name, line in defs:
        # 不能用 \b：它对 $ 这类非单词字符不成立，会把 app.js 里到处都在用的
        # $ / $$ 判成死代码。改用显式的前后向断言，且把 $ 算作标识符字符。
        pat = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")
        hits = 0
        for text in (js, html):
            for i, l in enumerate(text.splitlines(), 1):
                n = len(pat.findall(l))
                if text is js and i == line:
                    n = max(0, n - 1)          # 扣掉定义行
                hits += n
        if hits == 0:
            js_dead.append((line, kind, name))
    print(f"\n  JS 未被引用的顶层定义：{len(js_dead)} 个")
    for line, kind, name in js_dead:
        print(f"    app.js:{line:<5} {kind:<9} {name}()")

    # ---- CSS 未引用类 ----
    # 先找出 JS 里用模板拼接生成的类名前缀（如 `class="lv lv-${c.level}"`
    # 会生成 lv-ERROR / lv-DEBUG …）。这类类名在字面量里永远搜不到，
    # 不排除就会一直误报 —— 误报的扫描器等于没有扫描器。
    dynamic: set[str] = set()

    def _prefix(tok: str) -> str:
        # 捕获组已含尾部连字符（`v-${v}` -> `v-`），别再拼一个变成 `v--`
        return tok if tok.endswith("-") else tok + "-"

    for m in re.finditer(r'class(?:Name)?\s*=\s*(?:"([^"]*)"|`([^`]*)`)', js):
        content = m.group(1) or m.group(2) or ""
        for tok in re.findall(r"([A-Za-z_][\w-]*)\s*\$\{", content):
            dynamic.add(_prefix(tok))
        for tok in re.findall(r"\$\{[^}]*\}\s*([A-Za-z_][\w-]*)", content):
            dynamic.add(_prefix(tok))

    classes: dict[str, int] = {}
    for m in re.finditer(r"\.([a-zA-Z_][\w-]*)", css):
        classes.setdefault(m.group(1), css[:m.start()].count("\n") + 1)
    css_dead, css_dyn = [], []
    for name, line in classes.items():
        if name in html or name in js:
            continue
        if any(name.startswith(p) for p in dynamic):
            css_dyn.append((line, name))
            continue
        css_dead.append((line, name))
    print(f"\n  CSS 中定义但 HTML/JS 均未引用的类：{len(css_dead)} 个")
    for line, name in css_dead:
        print(f"    style.css:{line:<5} .{name}")
    if css_dyn:
        prefixes = "、".join(sorted(dynamic))
        print(f"\n  以下 {len(css_dyn)} 个由 JS 模板拼接生成（{prefixes} 前缀），已排除：")
        print("    " + ", ".join(f".{n}" for _, n in sorted(css_dyn)))

    # ---- HTML 未被引用的 id ----
    ids = re.findall(r'\bid="([\w-]+)"', html)
    id_dead = [i for i in ids if i not in js and i not in css]
    print(f"\n  HTML 中定义但 JS/CSS 均未引用的 id：{len(id_dead)} 个")
    for i in id_dead:
        print(f"    #{i}")

    print("\n  注意：JS 里通过字符串拼接或 dataset 访问的元素需人工复核后再删。")
    return 0


if __name__ == "__main__":
    sys.exit(main())