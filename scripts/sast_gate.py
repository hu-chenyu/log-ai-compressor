#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SAST 门禁：bandit 的发现必须逐条有交代。

**为什么不直接 `bandit -r` 退出码了事**
-------------------------------------
bandit 一开就是两千多条，其中绝大多数是 `B101 assert_used`（精确条数由每次
运行实测并打在输出里 —— 写死在这里的那个数早就漂了，而它长得很像实测值）——
pytest 的断言就是测试的语言，逐条豁免没有意义也写不完。于是真正的问题变成：

> 怎么让「我知道它在这儿，我判断过了，我写下了理由」这件事，
> 在**半年后**仍然是真的？

只靠 `# nosec` 做不到：nosec 是就地的一行注释，没有理由、没有归属、
也不检查是否已过期。本门禁用一份**带理由的基线**解决这四件事：

1. **新发现 → 红**。任何不在基线里的发现都让门禁失败。
2. **豁免过期 → 红**。基线里写了、但代码里已经不存在的条目同样失败。
   少了这一条，基线就只增不减，最后变成一张谁也不敢删的旧账 ——
   而**没人再看的那条豁免，等于没有豁免也没有检查**。
3. **没写理由 → 红**。`reason` 为空或缺失的条目直接失败。
   「没有理由的豁免等于开后门」这句话要由门禁执行，不能只写在文档里。
4. **数量对不上 → 红**。同一 (规则, 文件) 下多出一条也要更新基线并写理由。

扫描面
------
`log_ai_compressor`（发布物）、`scripts`（评测与发布工具）、`tests`。
唯一**按规则**排除的是 `tests/` 下的 `B101`；`log_ai_compressor/` 与
`scripts/` 里的 `assert` 仍在射程内 —— 发布代码里的裸 assert 是真 bug
（`-O` 会把它删掉）。

用法
----
    python scripts/sast_gate.py                  # 跑门禁
    python scripts/sast_gate.py --print-baseline # 把当前发现打成 JSON 骨架
    python scripts/sast_gate.py --write-baseline # 用当前发现覆写基线（**要写理由**）

改动代码后如果 bandit 报出新东西，正确做法是：先判断是不是真问题，
真问题就**修**（本门禁已有 5 处是这么修掉的，见 docs/BENCHMARK.md），
实在不该修再加进基线并写清楚为什么。`--write-baseline` 生成的 `reason`
全是空字符串，门禁会拒绝它 —— 这是故意的。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Windows 控制台默认 cp1252/cp936，打印中文会抛 UnicodeEncodeError ——
# 而且症状极具误导性：门禁本身全绿，却因为「打印表格时编码失败」让整个
# step 变红，看起来像功能坏了。**这一条是真的发生过**：v2.11.5 首次把
# 三道新门禁接进 CI，windows-3.12 就在 SAST 这步红了，而 ubuntu 两条
# 全绿 —— 门禁必须只在**判定**失败时失败，不能因为控制台编码。
# 模式与 scripts/adversarial_eval.py 一致，不是新发明。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "sast_baseline.json"
TARGETS = ("log_ai_compressor", "scripts", "tests")

# 按 (规则, 路径前缀) 排除。**每一条都要在这里写清楚为什么**，
# 因为排除项和基线条目一样，是「我看过、我决定的」的一种记录形式。
EXCLUDES: List[Tuple[str, str, str]] = [
    ("B101", "tests/",
     "pytest 的断言就是这门语言的表达方式，逐条豁免既没意义也写不完"
     "（本次扫描实测排除量见下方输出，不是写死的数字）。"
     "只排除 tests/ 下的 B101：log_ai_compressor/ 与 scripts/ 里的裸 assert "
     "仍然是真问题（-O 会整条删掉）。"),
]


def _rel(path: str) -> str:
    """bandit 给的绝对路径 -> 仓库相对路径 + 正斜杠（跨平台一致）。"""
    try:
        return os.path.relpath(Path(path).resolve(), ROOT).replace("\\", "/")
    except ValueError:                       # Windows 上跨盘符时 relpath 会抛
        return str(path).replace("\\", "/")


def _excluded(test_id: str, rel_path: str) -> Optional[str]:
    for tid, prefix, _why in EXCLUDES:
        if tid == test_id and rel_path.startswith(prefix):
            return _why
    return None


def scan() -> Tuple[Counter, List[str], Counter]:
    """跑 bandit，返回 (发现计数, 被排除的发现说明, 被排除的条数)。

    第三个返回值是**实测**的排除条数。这里刻意把它算出来，而不是在
    排除理由里写死一个数：那个数写死的当天就是对的，之后每次新增测试
    都悄悄变成一句假话，而它出现在门禁的输出里、看起来像实测值。
    """
    for mod in ("bandit",):
        try:
            __import__(mod)
        except ImportError:
            print("缺 bandit：pip install bandit（CI 走 requirements-dev.txt）")
            raise SystemExit(2)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "bandit.json"
        proc = subprocess.run(
            [sys.executable, "-m", "bandit", "-r", *TARGETS,
             "-f", "json", "-o", str(out)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        # bandit 有发现时退出码是 1，那是「有发现」不是「跑不动」。
        # 真正跑不动：没产出文件，或退出码不是 0/1。
        if not out.is_file():
            print("bandit 没产出结果文件，stderr：\n%s" % (proc.stderr or "")[:2000])
            raise SystemExit(2)
        if proc.returncode not in (0, 1):
            print("bandit 异常退出 %d：\n%s" % (proc.returncode, proc.stderr[:2000]))
            raise SystemExit(2)
        data = json.loads(out.read_text(encoding="utf-8"))

    counts: Counter = Counter()
    notes: List[str] = []
    excluded: Counter = Counter()
    for r in data.get("results", []):
        rel = _rel(r["filename"])
        why = _excluded(r["test_id"], rel)
        if why:
            notes.append("%s %s（%s）：%s" % (r["test_id"], rel, why.split("。")[0] + "。", ""))
            for tid, prefix, _w in EXCLUDES:
                if tid == r["test_id"] and rel.startswith(prefix):
                    excluded["%s|%s" % (tid, prefix)] += 1
                    break
            continue
        counts["%s|%s" % (r["test_id"], rel)] += 1
    return counts, notes, excluded


def load_baseline() -> List[dict]:
    if not BASELINE.is_file():
        print("缺 %s —— 没有基线就没有「已知且已交代」这个概念" % BASELINE)
        raise SystemExit(2)
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        print("%s 的 entries 不是数组" % BASELINE)
        raise SystemExit(2)
    return entries


def check(counts: Counter, excluded: Optional[Counter] = None) -> int:
    """excluded 只用来在输出里报**实测**的排除条数；不传就不打这一段。"""
    excluded = excluded or Counter()
    entries = load_baseline()
    problems: List[str] = []

    base_map: Dict[str, dict] = {}
    for e in entries:
        key = "%s|%s" % (e.get("test_id"), e.get("path"))
        if key in base_map:
            problems.append("基线里 %s 出现两次，去重后再跑" % key)
            continue
        base_map[key] = e
        if not str(e.get("reason") or "").strip():
            problems.append(
                "%s 没有写理由 —— 「我看过、决定不修」必须落成文字，"
                "否则这条豁免和没检查过没有区别" % key)

    for key, n in sorted(counts.items()):
        e = base_map.get(key)
        if e is None:
            problems.append(
                "新发现 %s（%d 条）不在基线里。先判断是不是真问题："
                "真问题就修，不该修就加进 docs/sast_baseline.json 并写理由"
                % (key, n))
            continue
        want = e.get("count")
        if want != n:
            problems.append(
                "数量对不上：%s 实测 %d 条，基线写 %s 条" % (key, n, want))

    for key, e in sorted(base_map.items()):
        if counts.get(key, 0) == 0:
            problems.append(
                "过期豁免：%s 在代码里已经不报错了，但基线里还留着 —— "
                "删掉它，或者说明为什么它该重新出现" % key)

    print("扫描面：%s" % "  ".join(TARGETS))
    print("计入判定的发现：%d 类 / %d 条"
          % (len(counts), sum(counts.values())))
    print("按规则排除：")
    for tid, prefix, why in EXCLUDES:
        n = excluded.get("%s|%s" % (tid, prefix), 0)
        print("  %s @ %s —— %s（本次实测 %d 条）" % (tid, prefix, why, n))

    if problems:
        print("\nSAST 门禁失败（%d 项）：" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("\nSAST 门禁通过：%d 条豁免全部有理由，且没有过期项"
          % len(base_map))
    return 0


def print_baseline(counts: Counter) -> int:
    """把当前发现打成可粘贴的 JSON 骨架（reason 留空，门禁会拒绝）。"""
    out = []
    for key, n in sorted(counts.items()):
        tid, _, path = key.partition("|")
        out.append({"test_id": tid, "path": path, "count": n, "reason": ""})
    doc = {
        "_readme": "bandit 基线。每条必须有 reason（门禁强制），"
                   "且代码里不再报错的条目必须删掉（门禁也强制）。",
        "scanned": list(TARGETS),
        "exclude_tests_B101": EXCLUDES[0][2],
        "entries": out,
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SAST 门禁（bandit 基线对账）")
    ap.add_argument("--print-baseline", action="store_true")
    args = ap.parse_args(argv)

    counts, _notes, excluded = scan()
    if args.print_baseline:
        return print_baseline(counts)
    return check(counts, excluded)


if __name__ == "__main__":
    raise SystemExit(main())
