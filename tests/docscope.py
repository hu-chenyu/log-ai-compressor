# -*- coding: utf-8 -*-
"""「当前版本节」这个作用域**只有一份口径** —— 两个门禁都要用它。

为什么单独成文件
----------------
CHANGELOG 是**历史档案**：「当前值」只在**顶格那节**里（历史节里的数字是
关于**那次发布**的记录，不是对现状的声明 —— AGENTS.md §2.4「撤回的记录
不要删，历史引用刻意不重写」）；而 `docs/DEVELOPMENT.md`、`AUDIT.md`
这类**活文档**全文都算数。

这个口径原先写在 `tests/test_sast_gate.py` 里（函数 `_current_scope`）。
2026-10-24 第十三轮外部复核后又有一个门禁要**同一份**口径
（文档里凡是「声称这是记分卡长度」的行，那个数都得等于实际长度）——
于是把它提出来放在这里。
**口径只有一份**：复制两份就等于两份会漂移的真相，
而那正是本仓库反复栽过的形状。
"""
from __future__ import annotations

import re
from pathlib import Path

#: 仓库根目录（本文件在 `tests/` 下）。
ROOT = Path(__file__).resolve().parent.parent


def current_scope(doc: str, root: Path | None = None) -> str:
    """返回文档里**属于「当前」**的那部分文本。

    * `CHANGELOG.md` ⇒ 只取顶格 `## [...]` 节；
    * 其它文档 ⇒ 全文。

    为什么 CHANGELOG 要特殊对待：它的历史节里写着当时的条数
    （例如「记分卡 90 → **93 条**」），那是**关于那次发布**的记录。
    扫全文就等于每次基线增长都要去改历史记录 —— 而唯一的改法是
    篡改历史或让 CI 变红，两条都是错的。

    这正是本仓库记过多次的形状：**判据读到的不是它以为的那个对象**。
    """
    base = Path(root) if root is not None else ROOT
    text = (base / doc).read_text(encoding="utf-8")
    if Path(doc).name != "CHANGELOG.md":
        return text
    m = re.search(r"^## \[", text, re.M)
    assert m, "CHANGELOG.md 里找不到任何 `## [...]` 版本节"
    nxt = re.search(r"^## \[", text[m.end():], re.M)
    return text[m.start(): m.end() + nxt.start()] if nxt else text[m.start():]
