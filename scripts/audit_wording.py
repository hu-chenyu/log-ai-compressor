#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""措辞审计：把一个 git 仓库的**七个面**扫一遍，报出命中词与出处。

为什么要有这个脚本
------------------
`tests/test_no_job_wording.py` 是**门禁**：它只看本仓库，命中就红。
但门禁回答不了另一个问题 ——「**已经发布出去的那一份**到底干不干净？」

要回答它，就得能对着**任意一个克隆**跑同一套扫描：本仓库、重写前的旧
克隆、GitHub 上拉下来的发布态。用门禁做不了（门禁扫的是自己的
`ROOT`）。所以这里把同一套口径抽成一个可以指定目标仓库的脚本。

**口径只有一份**：词表、匹配正则、blob 读取、tag 读取**全部**从门禁
文件里取（`--words-from` 默认 `<本仓库>/tests/test_no_job_wording.py`）。
复制两份就等于两份会漂移的真相，而那正是本项目反复栽过的形态。

2026-10 的外部复核抓到过一次漂移：门禁的面 3 已经从「逐行」改成
「整 blob」，而**审计脚本还在逐行**。两边的**命中结果**当时其实相同
（对不含换行符的词，逐行与整段必然等价）—— 但那是**碰巧**：一旦有人
改动其中一边的读法，两边就会给出不同的答案，而「审计说干净」正是用来
回答「发布态干不干净」的那句话。现在两边的读取与匹配函数**都直接取自
门禁文件**，漂移在结构上不可能再发生。

七个面
------
| 面 | 扫什么 | 为什么这一面不能少 |
| --- | --- | --- |
| 1 工作区 | HEAD 全部跟踪文件的内容 | 贡献者看得见的那一份 |
| 2 提交信息 | 全部提交信息（subject + body） | 重写漏掉的就是这一面 |
| 3 历史内容 | 全部历史版本的文件内容（**整 blob**） | 已经删掉的版本里也可能有 |
| 4 tag 说明 | 每个 tag 的说明文字（**整条**，不是第一行） | 门禁曾只扫每个 tag 的第一行 |
| 5 ref 名 | 所有 ref 的名字 | 名字也是内容 |
| 6 Release | GitHub 上每个 Release 的**标题与正文** | 这两个字段只在 GitHub 的数据库上，git 里没有 |
| 7 不可达对象 | 对象库里**不可达**的 blob 与 commit（reflog 持有、被强推替换掉的那批） | `rev-list --all` 按定义看不见它们 |

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
import re
import shutil
import subprocess
import sys
import urllib.request
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


def _releases(repo: Path) -> list[tuple[str, str]]:
    """GitHub 上每个 Release 的**标题与正文**（面 6）。

    这两个字段**不在 git 里** —— 它们只存在于 GitHub 的数据库上。2026-10 的
    复核在这里抓到一处漏网：某个 Release 的**标题**带着违禁词，而它的正文与
    CHANGELOG 对应小节都已经改过了（只改了正文，忘了标题）。

    走匿名 API（公开仓库够用）；拿不到就**报错**，不静默跳过 ——
    「扫了 0 个 Release 却报通过」是最没用的结论。
    """
    url = ("https://api.github.com/repos/%s/releases?per_page=100"
           % _repo_slug(repo))
    req = urllib.request.Request(url, headers={
        "User-Agent": "lac-wording-audit", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("读不到 Release 列表（%s）：%r" % (url, exc)) from exc
    out = []
    for rel in data:
        out.append((rel["tag_name"] + "（标题）", rel.get("name") or ""))
        out.append((rel["tag_name"] + "（正文）", rel.get("body") or ""))
    return out


def _repo_slug(repo: Path) -> str:
    """从 remote URL 里取出 owner/repo。

    **只放行合法的 slug**（字母数字与 `.` `_` `-`）—— 这个值会拼进
    api.github.com 的 URL，放行任意字符就等于把 URL 交给别处。
    """
    url = _git(repo, "remote", "get-url", "origin").strip()
    if url.endswith(".git"):
        url = url[:-4]
    parts = [p for p in url.replace(":", "/").split("/") if p]
    if len(parts) >= 2:
        slug = "%s/%s" % (parts[-2], parts[-1])
        if re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", slug):
            return slug
    return "hu-chenyu/log-ai-compressor"


def _unreachable(repo: Path) -> list[tuple[str, str]]:
    """**不可达对象**（面 7）：对象库里够不着、但磁盘上还在的那些。

    `git rev-list --all` 只给可达的；`cat-file --batch-all-objects` 给全集。
    两者相减 = 被强推替换掉的提交、reflog 还持有的提交 —— 2026-10 的复核
    就是在这里抓到一笔「不在可达历史里、GitHub 侧却仍能直读」的提交，
    它的信息里带着违禁词。
    """
    out = subprocess.run(
        [_GIT, "-C", str(repo), "cat-file", "--batch-all-objects",
         "--batch-check=%(objectname) %(objecttype)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_child_env(), timeout=900).stdout
    all_objs = [ln.split() for ln in out.splitlines() if len(ln.split()) >= 2]
    reach = set()
    for line in _git(repo, "rev-list", "--objects", "--all").splitlines():
        parts = line.split(" ", 1)
        if parts:
            reach.add(parts[0])
    reach_commits = set(_git(repo, "rev-list", "--all").split())

    res = []
    for sha, typ in all_objs:
        if typ == "blob" and sha not in reach:
            res.append(("blob " + sha[:10], _read_blob(repo, sha)))
        elif typ == "commit" and sha not in reach_commits:
            res.append(("commit " + sha[:10], _git(repo, "cat-file", "commit", sha)))
    return [(n, t) for n, t in res if t]


def _read_blob(repo: Path, sha: str) -> str:
    """读一个 blob 的正文；二进制不当文本扫（与门禁同口径）。"""
    p = subprocess.run([_GIT, "-C", str(repo), "cat-file", "blob", sha],
                       capture_output=True, env=_child_env(), timeout=300)
    raw = p.stdout
    if b"\0" in raw[:8000]:
        return ""
    return raw.decode("utf-8", "replace")


def _matches(text: str, words: list[str], pattern_fn):
    """text 里每个词的出现位置（行号 + 该行内容）。

    **用门禁的正则**（`gate._pattern`）而不是自己再写一套 —— 两边口径
    必须一模一样，否则「门禁说干净」与「审计说干净」就是两件事，
    而审计才是回答「发布态干不干净」的那个。

    整段匹配（不按行拆），与门禁同一口径。

    **但有一件事要说清**：对**不含换行符**的词，整段匹配与逐行匹配的
    **命中结果必然相同** —— 一个词跨不过换行符。2026-10 门禁面 3 从逐行
    改成整 blob 时，写下的理由是「整 blob 能看见折行的词组」，那句话
    **不成立**（本轮已更正）。整 blob 真正换来的是：一次批量读、对词表
    正则元字符的免疫、显式的二进制判定。
    """
    lines = text.splitlines()
    for w in words:
        for m in re.finditer(pattern_fn(w), text):
            lineno = text.count("\n", 0, m.start()) + 1
            snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            yield w, lineno, snippet


def audit(repo: Path, gate) -> dict:
    """扫七个面，返回命中明细。"""
    words = list(gate.FORBIDDEN)
    pattern = gate._pattern         # 匹配正则取自门禁
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
        for w, lineno, snippet in _matches(text, words, pattern):
            add("worktree", w, "%s:%d" % (rel, lineno), snippet)

    # ---- 面 2：提交信息 ----
    out = _git(repo, "log", "--all", "--format=%H%x01%s%x01%b%x02")
    for blk in out.split("\x02"):
        if not blk.strip():
            continue
        parts = blk.split("\x01")
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        text = "\n".join(parts[1:])
        for w, lineno, snippet in _matches(text, words, pattern):
            add("commit_msg", w, sha[:10], snippet)

    # ---- 面 3：历史版本文件内容（**整 blob**，与门禁同口径）----
    reach = gate._reachable_blobs(repo=repo)
    texts = gate._read_blobs(sorted(reach), repo=repo)
    for sha, text in texts.items():
        for w, lineno, snippet in _matches(text, words, pattern):
            add("history", w, sha[:10], snippet)

    # ---- 面 4：tag 说明（**整条**，不是第一行）----
    for name, text in gate._tag_messages(repo=repo):
        for w, lineno, snippet in _matches(text, words, pattern):
            add("tag_msg", w, name, snippet)

    # ---- 面 5：ref 名 ----
    for name in _git(repo, "for-each-ref", "--format=%(refname)").splitlines():
        for w, lineno, snippet in _matches(name, words, pattern):
            add("ref_name", w, name, snippet)

    # ---- 面 6：GitHub 上的 Release 标题与正文（不在 git 里）----
    for name, text in _releases(repo):
        for w, lineno, snippet in _matches(text, words, pattern):
            add("release", w, name, snippet)

    # ---- 面 7：不可达对象（reflog 持有、被强推替换掉的那批）----
    for name, text in _unreachable(repo):
        for w, lineno, snippet in _matches(text, words, pattern):
            add("unreachable", w, name, snippet)

    by_surface = Counter(h["surface"] for h in hits)
    by_word = Counter()
    for h in hits:
        by_word[h["word"]] += 1
    # 七个面都要出现（没有命中也给 0）—— 「这个面没扫过」和「这个面是干净的」
    # 必须能分辨开。
    faces = ("worktree", "commit_msg", "history", "tag_msg", "ref_name",
             "release", "unreachable")
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
                        ("ref_name", "ref 名字"),
                        ("release", "Release 标题与正文"),
                        ("unreachable", "不可达对象")):
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
        description="措辞审计：扫一个 git 仓库的七个面，报命中词与出处")
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
