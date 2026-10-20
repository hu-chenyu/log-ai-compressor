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

**词表只有一份**：`tests/test_no_job_wording.py` 里的 `_FORBIDDEN_CODES`
（按码点存放，仓库里不留字面量）。脚本不复制词表 —— 复制两份就等于
两份会漂移的真相，而那正是本项目反复栽过的形态。

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

退出码：0 = 五个面全 0 命中；1 = 有命中（并打印出处）；2 = 参数/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

#: 词表在源文件里的形状：`_FORBIDDEN_CODES = (` 后面的一串十六进制码点。
_CODES_BLOCK = re.compile(r"_FORBIDDEN_CODES\s*=\s*\((.*?)\n\)", re.S)
_CODE_ENTRY = re.compile(r'"([0-9A-Fa-f][0-9A-Fa-f\s]*)"')


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


def load_words(path: Path) -> list[str]:
    """从门禁文件里读出词表（码点 → 文本）。

    **故意不去 import 那个测试模块**：import 会在 import 期执行它的模块级
    代码，而这里只需要一个常量。读源码更直白，也避免了「门禁改了写法
    就把审计脚本弄坏」这种耦合。
    """
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("读不到词表文件 %s：%s" % (path, exc)) from exc
    m = _CODES_BLOCK.search(src)
    if not m:
        raise RuntimeError(
            "在 %s 里找不到 _FORBIDDEN_CODES = (...) —— "
            "词表的形状变了，审计脚本要跟着改" % path)
    words = []
    for raw in _CODE_ENTRY.findall(m.group(1)):
        codes = [int(c, 16) for c in raw.split()]
        words.append("".join(chr(c) for c in codes))
    if not words:
        raise RuntimeError("词表是空的 —— %s 里的 _FORBIDDEN_CODES 没有条目" % path)
    return words


def _is_binary(raw: bytes) -> bool:
    return b"\0" in raw[:8000]


def scan_worktree(repo: Path, words: list[str]) -> list[dict]:
    """面 1：HEAD 全部跟踪文件的工作区内容。"""
    hits = []
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
        for w in words:
            n = text.count(w)
            if n:
                hits.append({"surface": "worktree", "word": w, "where": rel, "count": n})
    return hits


def scan_commit_messages(repo: Path, words: list[str]) -> list[dict]:
    """面 2：全部提交信息（subject + body）。"""
    out = _git(repo, "log", "--all", "--format=%H%x01%s%x01%b%x02")
    hits = []
    for block in out.split("\x02"):
        if not block.strip():
            continue
        parts = block.split("\x01")
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        text = "\n".join(parts[1:])
        for w in words:
            n = text.count(w)
            if n:
                hits.append({"surface": "commit_messages", "word": w,
                             "where": sha[:12], "count": n})
    return hits


def scan_history_contents(repo: Path, words: list[str]) -> list[dict]:
    """面 3：全部历史版本的文件内容。"""
    revs = _git(repo, "rev-list", "--all").split()
    patterns = []
    for w in words:
        patterns += ["-e", w]
    hits = []
    for start in range(0, len(revs), _CHUNK):
        chunk = revs[start:start + _CHUNK]
        out = _git(repo, "grep", "-I", "-h", "-n", *patterns, *chunk)
        for line in out.splitlines():
            for w in words:
                if w in line:
                    hits.append({"surface": "history_contents", "word": w,
                                 "where": line.strip()[:160], "count": 1})
                    break
    return hits


def scan_tag_messages(repo: Path, words: list[str]) -> list[dict]:
    """面 4：每个 tag 的说明文字（门禁不扫这一面）。"""
    hits = []
    for line in _git(repo, "for-each-ref", "--format=%(refname:short)%09%(contents)",
                     "refs/tags").splitlines():
        if "\t" not in line:
            continue
        name, content = line.split("\t", 1)
        for w in words:
            n = content.count(w)
            if n:
                hits.append({"surface": "tag_messages", "word": w,
                             "where": name, "count": n})
    return hits


def scan_ref_names(repo: Path, words: list[str]) -> list[dict]:
    """面 5：所有 ref 的名字。"""
    hits = []
    for name in _git(repo, "for-each-ref", "--format=%(refname)").splitlines():
        for w in words:
            n = name.count(w)
            if n:
                hits.append({"surface": "ref_names", "word": w,
                             "where": name.strip(), "count": n})
    return hits


SURFACES = (
    ("worktree", "工作区文件内容", scan_worktree),
    ("commit_messages", "提交信息", scan_commit_messages),
    ("history_contents", "历史版本文件内容", scan_history_contents),
    ("tag_messages", "tag 说明文字", scan_tag_messages),
    ("ref_names", "ref 名字", scan_ref_names),
)


def audit(repo: Path, words: list[str]) -> dict:
    result = {"repo": str(repo), "word_count": len(words), "surfaces": {}, "hits": []}
    for key, label, fn in SURFACES:
        found = fn(repo, words)
        result["surfaces"][key] = {"label": label, "hits": len(found),
                                   "occurrences": sum(h["count"] for h in found)}
        result["hits"].extend(found)
    return result


def _print_report(result: dict, max_report: int) -> None:
    print("仓库：%s" % result["repo"])
    print("词表：%d 条" % result["word_count"])
    print()
    print("%-22s %8s %10s" % ("扫描面", "命中条目", "出现次数"))
    total_items = 0
    total_occ = 0
    for key, label, _ in SURFACES:
        s = result["surfaces"][key]
        total_items += s["hits"]
        total_occ += s["occurrences"]
        print("%-22s %8d %10d" % (label, s["hits"], s["occurrences"]))
    print("%-22s %8d %10d" % ("合计", total_items, total_occ))

    dist = Counter()
    for h in result["hits"]:
        dist[h["word"]] += h["count"]
    if dist:
        print()
        print("按词分布（%d 个词有命中）：" % len(dist))
        for w, n in dist.most_common():
            print("  %-8s %6d" % (w, n))

    if result["hits"]:
        print()
        print("出处（前 %d 条）：" % min(max_report, len(result["hits"])))
        for h in result["hits"][:max_report]:
            print("  [%s] %s —— %s x%d"
                  % (h["surface"], h["word"], h["where"], h["count"]))
        if len(result["hits"]) > max_report:
            print("  …… 还有 %d 条" % (len(result["hits"]) - max_report))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="措辞审计：扫一个 git 仓库的五个面，报命中词与出处")
    ap.add_argument("--repo", default=str(ROOT), help="要扫的仓库目录（默认本仓库）")
    ap.add_argument("--words-from", default=None,
                    help="词表文件（默认 <repo>/tests/test_no_job_wording.py）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--max-report", type=int, default=40, help="最多打印多少条出处")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print("不是一个 git 仓库：%s" % repo, file=sys.stderr)
        return 2
    words_from = Path(args.words_from) if args.words_from else \
        repo / "tests" / "test_no_job_wording.py"
    try:
        words = load_words(words_from)
        result = audit(repo, words)
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
