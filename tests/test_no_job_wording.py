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
import re
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
    "0067 0065 0072 0072 0069 0074",   # 内部代码评审系统名（曾出现在脱敏规则的示例主机名里）
    "0069 006D 0076 002D 0063 0069",   # 内部构建账号前缀（曾出现在测试夹具的 SSH 地址里）
    "8BDD 672F",   # 沟通表达类用词（曾出现在缺口清单的说明里）
    "5783 573E",   # 负面情绪用词（曾出现在解析器与测试的注释里）
    "70C2",   # 负面情绪用词（曾出现在项目自述与测试注释里）
    "6700 5F3A",   # 夸大宣传用词（曾出现在断言与竞品对照的说明里）
    "4EAE 70B9",   # 自我评价类用词（曾出现在早期 README 的小节标题里）
    "8D85 8D8A",   # 夸大宣传用词（曾出现在 GUI 坐标注释里）
    "52A0 5206",   # 自我评价类用词（曾出现在排序权重的说明里）
    "0031 0033 85AA",
    "0031 0034 85AA",
    "0031 0035 85AA",
    "0041 0049 590D 6838",
    "0041 0049 5BA1 67E5",
    "0041 0049 5BA1 6838",
    "0041 0049 68C0 67E5",
    "0041 0049 6A21 578B",
    "0041 0049 8F85 52A9",
    "0041 0049 9A8C 8BC1",
    "0042 7AD9",
    "0043 0068 0061 0074 0047 0050 0054",
    "0043 006C 0061 0075 0064 0065 0020 0046 0061 0062 006C 0065",
    "0043 006C 0061 0075 0064 0065 0020 004F 0070 0075 0073",
    "0043 006F 0064 0065 0042 0075 0064 0064 0079",
    "0043 006F 0064 0065 0069 0075 006D",
    "0043 006F 0070 0069 006C 006F 0074",
    "0044 0065 0065 0070 0053 0065 0065 006B 0020 0056 0034",
    "0044 0065 0065 0070 0053 0065 0065 006B 0020 0056 0034 002D 0046 006C 0061 0073 0068",
    "0044 0065 0065 0070 0053 0065 0065 006B 0020 0056 0034 002D 0050 0072 006F",
    "0044 0065 0065 0070 0053 0065 0065 006B 0020 0056 0034 002E 0031",
    "0044 006F 0075 0062 0061 006F",
    "0046 0041 0045",
    "0047 004C 004D 002D 0035 002E 0033",
    "0047 004C 004D 002D 0035 0056",
    "0047 0050 0054 002D 0035",
    "0047 0050 0054 002D 0035 002E 0036",
    "0047 0069 0074 0048 0075 0062 0020 0043 006F 0070 0069 006C 006F 0074",
    "004B 0069 006D 0069 0020 004B 0032",
    "004B 0069 006D 0069 0020 004B 0033",
    "004C 0065 0061 0064 0065 0072",
    "004D 0069 006E 0069 004D 0061 0078 0020 004D 0032",
    "004D 0069 006E 0069 004D 0061 0078 0020 004D 0033",
    "004F 0070 0065 006E 0043 006F 0064 0065",
    "0051 0077 0065 006E 0033",
    "0051 0077 0065 006E 0033 002E 0037",
    "0051 0077 0065 006E 0033 002E 0038",
    "0054 0072 0061 0065",
    "0054 0072 0061 0065 0043 006F 0064 0065",
    "0054 0072 0061 0065 0063 006F 0064 0065",
    "0057 0069 006E 0064 0073 0075 0072 0066",
    "005A 002E 0061 0069",
    "4E0A 6D77",
    "4E94 9669 4E00 91D1",
    "4EE3 7B14",
    "4F18 79C0",
    "4F4F 5740",
    "5165 804C",
    "516B 80A1 6587",
    "516C 4F17 53F7",
    "516C 79EF 91D1",
    "5185 7F51 5730 5740",
    "5185 90E8 57DF 540D",
    "5185 90E8 7CFB 7EDF",
    "5185 90E8 9879 76EE 4EE3 53F7",
    "524D 0031 0025",
    "524D 7AEF 5F00 53D1",
    "52A0 5206 9879",
    "52A0 73ED 7533 8BF7",
    "52A0 73ED 8D39",
    "5317 4EAC",
    "5343 5206 4E4B 4E00",
    "540A 6253",
    "540D 6821",
    "540E 7AEF 5F00 53D1",
    "5411 0048 0052",
    "5411 004C 0065 0061 0064 0065 0072",
    "5411 8001 677F",
    "5411 9886 5BFC",
    "5468 62A5",
    "5468 62A5 8BDD 672F",
    "5927 5B66",
    "5927 8BED 8A00 6A21 578B",
    "5B66 9738",
    "5B66 9738 4EBA 8BBE",
    "5B9E 4E60 751F",
    "5BD2 6B66 7EAA",
    "5C0F 7EA2 4E66",
    "5E2E 5FD9 5199",
    "5E2E 6211 5199",
    "5E74 7EC8 5956",
    "5F20 6C5F",
    "5F71 5FAE 521B 65B0",
    "5F85 9047",
    "5FAE 4FE1",
    "5FAE 535A",
    "5FD7 613F",
    "603B 5305",
    "6076 5FC3",
    "624B 6495",
    "627E 5DE5 4F5C",
    "6280 672F 5E7F 5EA6",
    "6280 672F 6808",
    "6280 672F 6DF1 5EA6",
    "6296 97F3",
    "63CD 6241",
    "6539 5199",
    "6587 6848",
    "65E5 62A5 8BDD 672F",
    "664B 5347",
    "667A 80FD 4F53",
    "667A 8C31 0041 0049",
    "670B 53CB 5708",
    "676D 5DDE",
    "6C14 6B7B 4E86",
    "6C47 62A5",
    "6C5F 897F 8D22 7ECF 5927 5B66",
    "6DA6 8272",
    "6DF1 5733",
    "73B0 573A 5E94 7528 5DE5 7A0B 5E08",
    "7533 8BF7",
    "767E 5206 4E4B 4E00",
    "767E 5EA6",
    "786C 5B9E 529B",
    "78BE 538B",
    "798F 5229",
    "79D2 6740",
    "7A0E 524D",
    "7A0E 540E",
    "7ACB 51AC 54E5",
    "7B80 5386 9879 76EE",
    "7B97 6CD5 5DE5 7A0B 5E08",
    "8001 677F",
    "804C 4F4D",
    "804C 573A",
    "804C 573A 5C0F 767D",
    "804C 573A 6C9F 901A",
    "804C 7EA7",
    "817E 8BAF",
    "85AA 916C",
    "88C5 903C",
    "8BC1 660E 80FD 529B",
    "8C46 5305",
    "8EAB 4EFD 8BC1 53F7",
    "8F6F 4EF6 5DE5 7A0B",
    "8F6F 5B9E 529B",
    "9010 5B57 7A3F",
    "963F 91CC 901A 4E49",
    "9762 8BD5 9898",
    "9876 5C16",
    "9886 5BFC",
    "9AD8 60C5 5546",
    "9AD8 8003",
    "81EA 6211 5C55 793A",
    "699C 5355 7B2C 4E00",
    "7EFC 5408 7B2C 4E00",
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


def _is_ascii_word(word: str) -> bool:
    """这个词是不是纯 ASCII（拉丁字母 / 数字 / 常见符号）。

    判据为什么按这个词分：**纯 ASCII 的词按「词」匹配，含中文的按子串匹配。**
    原因有二，都是实测踩出来的：

    1. **码点表自己会拼出英文单词。** 本文件的词表按十六进制码点存放，
       而汉字的码点里也含 A-F：有些汉字的码点连起来就是一个英文缩写
       （例：U+5FAE 那个字，文件里就出现了字面量那个缩写）。按子串匹配，
       **门禁会把自己判红** —— 而那个字符串不是任何人的词。
    2. **英文词里的片段。** 有一个缩写是 through / three / chrome 里都有的
       片段；按子串匹配，一次全量扫描会报出 2.7 万行假阳性，报告会被
       噪声淹掉。

    边界用 ASCII 字符集（不是 \\b）：中文紧挨着英文时**仍然要命中**
    （「8 年经验 + 一个英文词」这种混排是最典型的形态，而 Python 的 \\b
    把汉字当成单词字符，用 \\b 会漏掉它）。

    本函数的测试用例里也不写字面量（用码点拼出来）—— 否则这道门禁
    又会把自己判红。
    """
    return all(ord(c) < 128 for c in word)


def _count(text: str, word: str) -> int:
    """text 里 word 出现了几次（口径见 :func:`_is_ascii_word`）。"""
    if _is_ascii_word(word):
        pat = r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])"
        return len(re.findall(pat, text))
    return text.count(word)


def _hits_in(text: str) -> list[tuple[str, int]]:
    return [(w, n) for w in FORBIDDEN if (n := _count(text, w))]


def test_ascii_words_match_whole_words_only():
    """ASCII 词按**整词**匹配 —— 码点表与英文词里的片段都不算命中。

    这一条是门禁自己的牙齿：词表按十六进制存放，汉字码点里含 A-F，
    按子串匹配的话**门禁会把自己判红**；而有一个缩写是英文常用词里的
    片段，按子串匹配一次扫描会报 2.7 万行假阳性。

    反过来，中英混排里**必须命中** —— 「8 年经验 + 一个英文词」是最典型
    的形态，而 Python 的 \\b 把汉字当单词字符，用 \\b 会漏掉它。

    用例里的词**按码点拼出来**：写字面量的话，这条用例本身就会被门禁判红。
    """
    def s(*codes):
        return "".join(chr(c) for c in codes)

    abbr = s(0x46, 0x41, 0x45)                       # 一个英文缩写
    en_word = s(0x6F, 0x66, 0x66, 0x65, 0x72)        # 另一个英文词
    cn_word = s(0x5EFA, 0x4EF7)                       # 码点里含 A-F 的那个汉字
    cn_other = s(0x7B80, 0x5386)                      # 另一个中文词
    mixed = s(0x38, 0x5E74, 0x7ECF, 0x9A8C)            # 「8 年经验」

    assert _count("5FAE 4FE1 " + cn_word, abbr) == 0, "码点里的十六进制片段被当成了词"
    assert _count("through three chrome", "hr") == 0, "英文词里的片段被当成了词"
    assert _count("zzz qqq", "qq") == 0, "更长单词里的片段被当成了词"
    assert _count(mixed + " " + en_word, en_word) == 1, "中英混排里漏掉了"
    assert _count(abbr + " " + s(0x662F, 0x7F29, 0x5199), abbr) == 1, "独立出现的词漏掉了"
    assert _count(cn_other, cn_other) == 1, "中文词按子串匹配，不该被边界规则影响"


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
                if _count(line, word):
                    hits.append((word, line.strip()[:160]))
                    break
    assert not hits, "历史版本的文件内容里出现了禁用词：\n" + "\n".join(
        "  %s —— %s" % (word, line) for word, line in hits[:20])
