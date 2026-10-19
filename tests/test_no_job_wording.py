# -*- coding: utf-8 -*-
"""守住一条承诺：本仓库的措辞里不出现与「个人就业 / 身份」相关的词。

**为什么要有这道门禁**：2026-10 做过一次全历史重写，把这类措辞从 383 笔
提交里清干净。清完之后复盘发现 —— 当时**没有任何门禁看着它**。也就是说，
下一次有人（很可能就是三个月后的自己）在 README 里写一句普通的说明，就
会把它重新写回来，而 CI 全绿。这正是 AUDIT.md 形态八那条：
「表里的数全对，而表外那句断言没人看守」。

**三个扫描面**（少一个就少一半）：
  1. HEAD 全部跟踪文件的**工作区内容** —— 贡献者看得见的那一份
  2. 全部提交信息（subject + body）
  3. 全部历史版本的文件内容 —— 已经删掉的版本里也可能有

**为什么词表按码点存放**：
  这份文件自己也在扫描范围内。把禁用词写成字面量，门禁会先把自己判红 ——
  于是「守门的人」和「被守的东西」住在同一处。码点形式在磁盘上不构成
  字面量。

  要加词时，在自己的终端里跑（**不要**把词写进仓库）：
      python -c "print(' '.join('%04X' % ord(c) for c in '词'))"

**豁免**：`_EXEMPTED` 里的三个词**不在**禁用表内 —— 豁免只有「一个词整体
不进表」这一种形态，没有「按文件放行」（那等于开一个洞）。
另外两个数字串在全历史扫描里出现过，但它们不在表内：那是 CSS 里的数值
（`right=0.985`、`rgba(52,211,153)`），属于措辞扫描的假阳性，不是措辞。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: git 的**绝对路径**。bandit 的 B607 报「用部分路径启动进程」——
#: scripts/mutation_scorecard.py 里的 git_exe() 早就这么处理了（它还多写了
#: 一段注释说明为什么**修而不豁免**）。这里照抄那条做法：写死 "git" 在
#: git 不在 PATH 时抛出来的是 FileNotFoundError，看不出是「git 缺失」还是
#: 「工作区不干净」，而这道门禁的整个卖点就是后者。
_GIT = shutil.which("git")

#: 禁用词表 —— 码点形式（原因见模块 docstring）。顺序即上报顺序。
_FORBIDDEN_CODES = (
    "9762 8BD5",
    "9762 8BD5 5B98",
    "7B80 5386",
    "793E 62DB",
    "6821 62DB",
    "6C42 804C",
    "6295 9012 7B80 5386",
    "5185 63A8",
    "006F 0066 0066 0065 0072",
    "5E94 8058",
    "8DF3 69FD",
    "516B 80A1",
    "7B14 8BD5",
    "62DB 8058",
    "8BD5 7528 671F",
    "79BB 804C",
    "5C97 4F4D",
    "6D4B 8BD5 5F00 53D1",
    "6D4B 5F00",
    "79CB 62DB",
    "6625 62DB",
    "5B9E 4E60",
    "5305 88C5",
    "542B 91D1 91CF",
    "7EE9 6548",
    "5927 5382",
    "80CC 8C03",
    "730E 5934",
    "85AA 8D44",
    "6708 85AA",
    "5E74 85AA",
    "5DE5 8D44",
    "5BB6 5EAD 4F4F 5740",
    "6BD5 4E1A 9662 6821",
    "5B66 5386",
    "672C 79D1",
    "7855 58EB",
    "53CC 4E00 6D41",
)


def _decode(codes: str) -> str:
    return "".join(chr(int(c, 16)) for c in codes.split())


FORBIDDEN = tuple(_decode(c) for c in _FORBIDDEN_CODES)

#: 豁免词与理由。豁免 = 「这个词整体不进禁用表」，不是「按文件放行」。
_EXEMPTED = (
    ("投递", "本项目自有术语：测把表头「交付」改名会不会让对账脱管"),
    ("身份证", "脱敏功能的默认规则之一 —— 写的是功能本身"),
    ("手机号", "脱敏功能的默认规则之一 —— 写的是功能本身"),
)

#: 一次 git grep 传多少个 rev。命令行长度有限，批次大了会在 Windows 上撞上限。
_CHUNK = 64

#: 扫描面至少要看到这么多东西，否则「没报错」可能只是「什么都没扫」
_MIN_FILES = 50
_MIN_COMMITS = 2


def _env() -> dict:
    """显式给子进程定编码。

    不给 env 就继承开发机的全部环境变量 —— 那正是「本地绿、CI 红」的根源：
    本地恰好继承到的是对的编码。这条门禁的解码口径必须与跑它的那台机器
    的控制台无关（契约见 tests/test_subprocess_encoding_contract.py）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _git(*args: str) -> str:
    if _GIT is None:
        raise AssertionError("找不到 git —— 这道门禁要读全历史，它跑不了")
    proc = subprocess.run(
        [_GIT, "-C", str(ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_env())
    # grep 没有命中时退出码是 1 —— 那不是错误
    if proc.returncode not in (0, 1):
        raise AssertionError("git %s 失败：%s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def _tracked_paths() -> list[str]:
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def _all_revs() -> list[str]:
    return _git("rev-list", "--all").split()


def _read_text(rel: str) -> str:
    try:
        raw = (ROOT / rel).read_bytes()
    except OSError:
        return ""
    if b"\0" in raw[:8000]:
        return ""  # 二进制 —— 与 git grep -I 的口径保持一致
    return raw.decode("utf-8", errors="replace")


def _hits_in(text: str) -> list[tuple[str, int]]:
    return [(w, text.count(w)) for w in FORBIDDEN if w in text]


def test_scans_are_not_vacuous():
    """门禁本身不能是空的 —— 这一条是下面三条的地基。

    「扫了 0 个文件却报通过」是本项目反复栽过的形态（见 AGENTS.md §3：
    `rglob` 静默返回空 → 报「核验通过」）。所以先证明扫描面真的存在。
    """
    assert len(FORBIDDEN) >= 30, (
        "禁用词表只剩 %d 条 —— 门禁会退化成恒真" % len(FORBIDDEN))
    assert len(_EXEMPTED) >= 1, "豁免表空了"

    files = _tracked_paths()
    assert len(files) >= _MIN_FILES, (
        "只扫到 %d 个跟踪文件（门限 %d）—— 扫描面塌了，"
        "这条门禁等于没跑" % (len(files), _MIN_FILES))

    revs = _all_revs()
    assert len(revs) >= _MIN_COMMITS, (
        "只找到 %d 笔提交（门限 %d）—— 这是浅克隆的形状。在这种克隆下，"
        "历史扫描只能看当前那一个版本，而提交信息扫描只看得到一笔 —— "
        "门禁会**看起来在跑**，而它只能看见 1/N 的东西。"
        "CI 的 checkout 必须写 fetch-depth: 0" % (len(revs), _MIN_COMMITS))


def test_head_tree_has_no_forbidden_wording():
    """扫描面 1：贡献者看得见的那一份文件内容。"""
    hits = []
    for rel in _tracked_paths():
        for word, n in _hits_in(_read_text(rel)):
            hits.append((rel, word, n))
    assert not hits, "跟踪文件里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (rel, n) for rel, word, n in hits)


def test_commit_messages_have_no_forbidden_wording():
    """扫描面 2：全部提交信息。"""
    out = _git("log", "--all", "--format=%H%x01%s%x01%b%x02")
    hits = []
    for block in out.split("\x02"):
        if not block.strip():
            continue
        parts = block.split("\x01")
        sha = parts[0].strip()
        text = "\n".join(parts[1:])
        for word, n in _hits_in(text):
            hits.append((sha[:8], word, n))
    assert not hits, "提交信息里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (sha, n) for sha, word, n in hits)


def test_history_contents_have_no_forbidden_wording():
    """扫描面 3：全部历史版本的文件内容。

    已经删掉的版本里也可能有 —— 那是「三百多笔提交」里的一部分。
    """
    revs = _all_revs()
    patterns = []
    for word in FORBIDDEN:
        patterns += ["-e", word]

    hits = []
    for start in range(0, len(revs), _CHUNK):
        chunk = revs[start:start + _CHUNK]
        out = _git("grep", "-I", "-h", "-n", *patterns, *chunk)
        for line in out.splitlines():
            for word in FORBIDDEN:
                if word in line:
                    hits.append((word, line.strip()[:160]))
                    break
    assert not hits, "历史版本的文件内容里出现了禁用词：\n" + "\n".join(
        "  %s —— %s" % (word, line) for word, line in hits[:20])
