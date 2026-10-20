#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""措辞审计：把一个 git 仓库的**五个面**扫一遍，报出命中词与出处。

为什么要有这个脚本
------------------
`tests/test_no_job_wording.py` 是**门禁**：它只看本仓库，命中就红。
但门禁回答不了另一个问题 ——「**已经发布出去的那一份**到底干不干净？」

要回答它，就得能对着**任意一个克隆**跑同一套扫描：本仓库、重写前的旧
克隆、GitHub 上拉下来的发布态。用门禁做不了（门禁扫的是自己的
`ROOT`）。所以这里把同一套口径抽成一个可以指定目标仓库的脚本。

**口径只有一份**：词表与匹配规则都从门禁文件里取（`--words-from` 默认
`<本仓库>/tests/test_no_job_wording.py`）。复制两份就等于两份会漂移的
真相，而那正是本项目反复栽过的形态。

五个面
------
| 面 | 扫什么 | 为什么这一面不能少 |
| --- | --- | --- |
| 1 工作区 | HEAD 全部跟踪文件的内容 | 贡献者看得见的那一份 |
| 2 提交信息 | 全部提交信息（subject + body） | 重写漏掉的就是这一面 |
| 3 历史内容 | 全部历史版本的文件内容 | 已经删掉的版本里也可能有 |
| 4 tag 说明 | 每个 tag 的说明文字 | 门禁不扫这一面 |
| 5 ref 名 | 所有 ref 的名字 | 名字也是内容 |

用法：
    python scripts/audit_wording.py                      # 扫本仓库
    python scripts/audit_wording.py --repo <克隆目录>     # 扫任意克隆
    python scripts/audit_wording.py --json               # 机器可读
    python scripts/audit_wording.py --words-from <文件>   # 换一份词表
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Windows 控制台默认 cp1252/cp936，打印中文会抛 UnicodeEncodeError ——
# 症状极具误导性：审计本身跑得好好的，却因为「打印表格时编码失败」让
# 整个 step 变红，看起来像功能坏了。审计是给人看的，必须在任何平台跑完。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent

#: git 的**绝对路径**。bandit 的 B607 报「用部分路径启动进程」——
# scripts/mutation_scorecard.py 早就这么处理了，这里照抄那条做法。
_GIT = shutil.which("git")

#: 一次 git grep 传多少个 rev。命令行长度有限，批次大了会在 Windows 上撞上限。
_CHUNK = 64


def _child_env() -> dict:
    """子进程的环境变量：显式把编码钉死，不继承开发机的那一套。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _git(repo: Path, *args: str, check: bool = False) -> str:
    if _GIT is None:
        raise RuntimeError("找不到 git —— 这个脚本要读全历史，它跑不了")
    proc = subprocess.run(
        [_GIT, "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_child_env(), check=check)
    if check and proc.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def load_gate(path: Path):
    """加载门禁模块 —— 词表与匹配规则都从它那里来（口径只有一份）。

    刻意**不去 import** 那个测试模块：import 会在 import 期执行它的模块级
    代码，而这里只需要常量与两个函数。按路径加载最直白。
    """
    spec = importlib.util.spec_from_file_location("wording_gate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("读不到门禁文件 %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not getattr(mod, "FORBIDDEN", None):
        raise RuntimeError(
            "%s 里没有 FORBIDDEN —— 门禁的形状变了，审计脚本要跟着改" % path)
    return mod


def _is_binary(raw: bytes) -> bool:
    return b"\0" in raw[:8000]


def audit(repo: Path, gate) -> dict:
    """扫五个面，返回命中明细。"""
    words = list(gate.FORBIDDEN)
    count = gate._count            # 匹配规则同样取自门禁
    hits = []

    def add(surface, word, where, snippet):
        hits.append({"surface": surface, "word": word, "where": where,
                     "snippet": snippet[:200]})

    # ---- 面 1：工作区跟踪文件 ----
    for rel in _git(repo, "ls-files").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        p = repo / rel
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if _is_binary(raw):
            continue
        text = raw.decode("utf-8", "replace")
        for i, line in enumerate(text.splitlines(), 1):
            for w in words:
                n = count(line, w)
                if n:
                    add("worktree", w, "%s:%d" % (rel, i), line.strip())

    # ---- 面 2：提交信息 ----
    out = _git(repo, "log", "--all", "--format=%H%x01%s%x01%b%x02")
    for blk in out.split("\x02"):
        if not blk.strip():
            continue
        parts = blk.split("\x01")
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        for line in "\n".join(parts[1:]).splitlines():
            for w in words:
                n = count(line, w)
                if n:
                    add("commit_msg", w, sha[:10], line.strip())

    # ---- 面 3：历史版本文件内容 ----
    revs = _git(repo, "rev-list", "--all").split()
    patterns = []
    for w in words:
        patterns += ["-e", w]
    for start in range(0, len(revs), _CHUNK):
        chunk = revs[start:start + _CHUNK]
        out = _git(repo, "grep", "-I", "-h", "-n", *patterns, *chunk)
        for line in out.splitlines():
            for w in words:
                if count(line, w):
                    add("history", w, "rev", line.strip()[:180])
                    break

    # ---- 面 4：tag 说明 ----
    for line in _git(repo, "for-each-ref", "--format=%(refname:short)%09%(contents)",
                     "refs/tags").splitlines():
        if "\t" not in line:
            continue
        name, content = line.split("\t", 1)
        for ln in content.splitlines():
            for w in words:
                n = count(ln, w)
                if n:
                    add("tag_msg", w, name, ln.strip())

    # ---- 面 5：ref 名 ----
    for name in _git(repo, "for-each-ref", "--format=%(refname)").splitlines():
        for w in words:
            n = count(name, w)
            if n:
                add("ref_name", w, name, name)

    by_surface = Counter(h["surface"] for h in hits)
    by_word = Counter()
    for h in hits:
        by_word[h["word"]] += 1
    # 五个面都要出现（没有命中也给 0）—— 「这个面没扫过」和「这个面是干净的」
    # 必须能分辨开。
    faces = ("worktree", "commit_msg", "history", "tag_msg", "ref_name")
    return {"repo": str(repo), "word_count": len(words),
            "by_surface": {f: by_surface.get(f, 0) for f in faces},
            "by_word": dict(by_word),
            "total": len(hits), "hits": hits}


def _print_report(result: dict, max_report: int) -> None:
    print("仓库：%s" % result["repo"])
    print("词表：%d 条" % result["word_count"])
    print()
    print("%-22s %8s" % ("扫描面", "命中条目"))
    for face, label in (("worktree", "工作区文件内容"),
                        ("commit_msg", "提交信息"),
                        ("history", "历史版本文件内容"),
                        ("tag_msg", "tag 说明文字"),
                        ("ref_name", "ref 名字")):
        print("%-22s %8d" % (label, result["by_surface"].get(face, 0)))
    print("%-22s %8d" % ("合计", result["total"]))
    if result["by_word"]:
        print()
        print("按词分布（%d 个词有命中）：" % len(result["by_word"]))
        for w, n in sorted(result["by_word"].items(), key=lambda kv: -kv[1]):
            print("  %-14s %6d" % (w, n))
    if result["hits"]:
        print()
        print("出处（前 %d 条）：" % min(max_report, len(result["hits"])))
        for h in result["hits"][:max_report]:
            print("  [%s] %s —— %s x1"
                  % (h["surface"], h["word"], h["where"]))
        if len(result["hits"]) > max_report:
            print("  …… 还有 %d 条" % (len(result["hits"]) - max_report))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="措辞审计：扫一个 git 仓库的五个面，报命中词与出处")
    ap.add_argument("--repo", default=str(ROOT), help="要扫的仓库目录（默认本仓库）")
    ap.add_argument("--words-from", default=None,
                    help="门禁文件（词表与匹配规则的来源；默认 <repo>/tests/test_no_job_wording.py）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--max-report", type=int, default=40, help="最多打印多少条出处")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print("不是一个 git 仓库：%s" % repo, file=sys.stderr)
        return 2
    words_from = Path(args.words_from) if args.words_from else \
        ROOT / "tests" / "test_no_job_wording.py"
    try:
        gate = load_gate(words_from)
        result = audit(repo, gate)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result, args.max_report)
    return 0 if not result["hits"] else 1


if __name__ == "__main__":
    sys.exit(main())
