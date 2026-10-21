# -*- coding: utf-8 -*-
"""守住一条承诺：本仓库的措辞里不出现与「个人就业 / 身份」相关的词。

**为什么要有这道门禁**：2026-10 做过一次全历史重写，把这类措辞从 383 笔
提交里清干净。清完之后复盘发现 —— 当时**没有任何门禁看着它**。也就是说，
下一次有人（很可能就是三个月后的自己）在 README 里写一句普通的说明，就
会把它重新写回来，而 CI 全绿。这正是 AUDIT.md 形态八那条：
「表里的数全对，而表外那句断言没人看守」。

**七个扫描面**（少一个就少一半）：
  1. HEAD 全部跟踪文件的**工作区内容** —— 贡献者看得见的那一份
  2. 全部提交信息（subject + body）
  3. 全部历史版本的文件内容 —— 已经删掉的版本里也可能有
  4. 全部 tag 的说明文字（**整条**，不是第一行）
  5. 全部 ref 的名字
  6. GitHub 上 27 个 Release 的**标题、正文与附件名**（不在 git 里，只在 API 上）
  7. **不可达对象**（reflog 持有的、被强推替换掉的那些提交）

**为什么词表按码点存放**：
  这份文件自己也在扫描范围内。把禁用词写成字面量，门禁会先把自己判红 ——
  于是「守门的人」和「被守的东西」住在同一处。码点形式在磁盘上不构成
  字面量。

  要加词时，在自己的终端里跑（**不要**把词写进仓库）：
      python -c "print(' '.join('%04X' % ord(c) for c in '词'))"

**豁免**：`_EXEMPTED` 里的词**不在**禁用表内 —— 豁免只有「一个词整体
不进表」这一种形态，没有「按文件放行」（那等于开一个洞）。

**保留表的兜底判据**（2026-10 的外部复核指出这里有个两头落空的缝：README 里
出现的客户端名**不在禁用表里**，于是下一轮换个工具名又变成同一类问题）：

  凡是本仓库里以**接入对象**出现的 Agent / IDE / CLI / 模型服务专名
  （README 的接入矩阵、`ai/config.py` 的 provider 表、CLI 的 provider 选项、
  SKILL 的宿主说明）—— **包括清单里尚未列出的新增名称** —— 按「支持的客户端」
  保留；同一个名字若出现在「谁写的 / 谁改的 / 谁审的」这类**开发过程**语境里，
  仍须清除。判据是**这一处的谓语指向接入，而不是指向开发过程**，**不自动放行**。

**渲染口径**（面 1 与面 3 各加一条）：原文里没有、**渲染之后**才出现的词
（被 HTML 实体 / 标签 / Markdown 强调 / 行内代码 / 零宽字符藏起来的）也算命中。
它**不进红线**，是独立的一条「上界断言」，当前基线 **0**。

**匹配口径**：纯 ASCII 的词按**整词**匹配（含中文的按子串），且区分大小写。
  · 边界用 ASCII 字符集而不是 `\\b`：中文紧挨英文时仍然要命中
    （「8 年经验 + 一个英文词」这种混排是最典型的形态，而 `\\b` 把汉字当单词字符）。
  · 区分大小写：否则 ASCII 词会撞进英文常用词里（`hr` 落在 through/three）。
  · 不按整词：词表按十六进制存放，汉字码点里也含 A-F，文件里会出现字面量
    看起来像英文单词的片段。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: git 的**绝对路径**。bandit 的 B607 报「用部分路径启动进程」——
# scripts/mutation_scorecard.py 里的 git_exe() 早就这么处理了（它还多写了
# 一段注释说明为什么**修而不豁免**）。这里照抄那条做法：写死 "git" 在
# git 不在 PATH 时抛出来的是 FileNotFoundError，看不出是「git 缺失」还是
# 「工作区不干净」，而这道门禁的整个卖点就是后者。
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
    "47 4C 4D 2D 35 2E 32",   # 模型版本号（曾出现在第三方事故引证里）
    "47 50 54 2D 34",   # 模型版本号（provider 清单里的默认模型）
    "77E5 4E4E",   # 内容平台名（曾出现在引用来源里）
    "47 65 72 72 69 74",   # 内部代码评审系统名（另一种大小写形态）
    "4F 66 66 65 72",   # 另一种大小写形态
    "4F 46 46 45 52",   # 全大写形态
    "48 52",   # 独立成词时才算（英文常用词里的片段不算，见匹配口径）
)


def _decode(codes: str) -> str:
    return "".join(chr(int(c, 16)) for c in codes.split())


FORBIDDEN = tuple(_decode(c) for c in _FORBIDDEN_CODES)

#: 豁免词与理由。豁免 = 「这个词整体不进表」，不是「按文件放行」。
_EXEMPTED = (
    ("投递", "本项目自有术语：测把表头「交付」改名会不会让对账脱管"),
    ("身份证", "脱敏功能的默认规则之一 —— 写的是功能本身"),
    ("手机号", "脱敏功能的默认规则之一 —— 写的是功能本身"),
)

#: 一次 git grep 传多少个 rev。命令行长度有限，批次大了会在 Windows 上撞上限。
_CHUNK = 64

#: 扫描面至少要看到这么多东西，否则「没报错」可能只是「什么都没扫」
_MIN_FILES = 50
_MIN_COMMITS = 100
_MIN_BLOBS = 50
#: 面 6 的判据**不写死字段数**，而是**算出来**：每个 Release 贡献
#: 标题 + 正文 + 附件名。写死一个数是不行的 —— 2026-10-21 删掉 27 个
#: 旧 Release 之后，字段数从 108 掉到 4，写死的门限当场变红，
#: 而那**不是**塌陷，是「只剩最新版本」的正常状态。
#: 现在用**相对判据**：字段数必须等于 `2 × Release 数 + 附件数`。

#: 「被空白拆开的禁用词」在**历史**里的上界（当前 6 处）。
#: 变小是好事（历史重写会清掉），变大说明有人新写进了一处。
_WHITESPACE_SPLIT_BASELINE = 6

#: 带 U+FFFD（替换字符）的历史对象数量上界（当前 91 个）。
#:
#: 这**不是**措辞问题 —— 那 91 个 blob 里没有一个词表里的词。它是另一类：
#: **读出来的不是原文**。一个汉字的 3 个 UTF-8 字节被逐字节替换成 U+FFFD，
#: 于是那句话在扫描口径下根本不存在，而门禁照样是绿的。
#:
#: 分布（实测）：CHANGELOG.md 72 + 变异记分卡 9 + 门禁文件 3 + 另 4 个文件各 1，
#: 其中 **3 个是提交对象**（3 笔提交信息里各有一处坏字）。
#: 上界不是等号：变小是好事（历史重写会清掉），变大说明有人新写进了一处。
#: 口径是**可达** blob —— CI 上是浅对象库，看不到不可达的那些，看到的只会更少。
_FFFD_HISTORY_BASELINE = 91

#: GitHub API 的仓库地址（面 6 用）
_RELEASES_API = ("https://api.github.com/repos/hu-chenyu/log-ai-compressor"
                 "/releases?per_page=100")

#: GitHub API 的 token —— **环境里有就用**。匿名的额度是**每个 IP 每小时
#: 60 次**，而面 6 每跑一次门禁就要拉一次 Release 列表；共享出口 IP
#: （CI runner、公司网络）很容易撞上限，于是门禁会**红** —— 那是诚实的红，
#: 但红的原因与仓库无关。实测踩过：一次自测连续拉了几十次就把额度烧光，
#: 门禁当场红成「读不到 Release 列表」。
_TOKEN = (os.environ.get("GITHUB_TOKEN")
          or os.environ.get("GH_TOKEN") or "")


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
    （「8 年经验 + 一个英文词」这种混排是最典型的形态，而 \\b 把汉字
    当成单词字符，用 \\b 会漏掉它）。
    """
    return all(ord(c) < 128 for c in word)


def _pattern(word: str) -> str:
    """这个词的匹配正则（口径见 :func:`_is_ascii_word`）。

    单独抽出来，是因为**审计脚本也要用同一套口径定位出处** ——
    它要报「第几行」，光有次数不够，得有匹配位置。
    """
    import re
    if _is_ascii_word(word):
        return r"(?<![A-Za-z0-9_])" + re.escape(word) + r"(?![A-Za-z0-9_])"
    return re.escape(word)


def _is_cjk(ch: str) -> bool:
    """这个字符是不是汉字（CJK 扩展 A 与统一表意文字）。

    判据为什么只认这两块：这个词表里的中文词条**只应该**由常用汉字
    组成。出现表外的字符（比如兼容表意文字、扩展 B、假名）几乎总是
    **码点打错了** —— 手打十六进制最容易错在位数与高位上。
    """
    cp = ord(ch)
    return (0x3400 <= cp <= 0x4DBF) or (0x4E00 <= cp <= 0x9FFF)


def _count(text: str, word: str) -> int:
    """text 里 word 出现了几次（口径见 :func:`_is_ascii_word`）。"""
    import re
    return len(re.findall(_pattern(word), text))


def _hits_in(text: str) -> list[tuple[str, int]]:
    return [(w, n) for w in FORBIDDEN if (n := _count(text, w))]


#: 块级 HTML 标签 —— 它们的**作用是分隔**（段落、换行、列表项），
#: 归一化时变成**空白**而不是被删掉。删掉它会把两段本来不相邻的文字
#: **粘成一个词** —— 那不是「读者看到的」，那是我们自己造出来的命中。
_BLOCK_TAGS = ("p", "br", "div", "li", "ul", "ol", "tr", "td", "th",
               "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
               "hr", "table", "section", "article", "header", "footer")


def _render_normalize(text: str) -> str:
    """去掉「读者看不见的语法与字符」，得到**渲染之后**的文本。

    七件事：HTML 实体解码 → NFKC 归一化 → 块级 HTML 标签变空白、行内标签删掉
    → Markdown 链接（含**引用式**）/ 强调 / 行内代码的语法删掉
    → 零宽、软连字符与其它**不可见字符**删掉。

    **不做空白折叠** —— 那是「被拆开」那一条的判据（另外按 report-only 处理）。
    两套口径混在一起，就会既不是红线也不是报告。

    **U+2028 / U+2029 归一化成换行、而不是删掉**：它们与 `\n` 是同一类东西
    （硬换行），而 `\n` 是**保留**的 —— 早期版本把这两个删掉，于是
    「面\u2028试」会被粘成一个词，而「面\n试」不会。**同一个换行两套规则**
    —— 2026-10-21 的外部复核之后自己发现的，已改。

    渲染口径的**新增命中是 0**（外部复核独立测出同一个数；那次的管线少了
    几种藏法，补上后基线**不变**，说明仓库里本来就没有被那些藏法藏起来的词）。

    **规模（工作区文本文件数、历史文本对象数）刻意不写在这里** —— 这两个数
    **每次提交都会变**，写死就是下一个过期陈述（2026-10-21 的一次实测分别是
    178 与 1,895）。`test_scans_are_not_vacuous` 每次运行都会打印实测值并
    对门限断言 —— **要数字请看那里。**
    """
    import html
    import re
    import unicodedata

    t = html.unescape(text)
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"(?is)</?(?:%s)\b[^>]*>" % "|".join(_BLOCK_TAGS), " ", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # 引用式链接 `[文字][ref]` 渲染出来是**链接文字**，`[ref]: url` 那一行
    # 根本不渲染成正文 —— 早期版本两样都没处理，于是「[X][ref]Y」这种
    # 写法整个看不见（读者看到的是那个完整的词）。
    # **注意**：示例里只能写占位符 —— 写实字会被这条门禁自己拼成
    # 一个完整的词，然后**先把自己判红**（2026-10-21 实测过一次）。
    t = re.sub(r"(?m)^\s*\[[^\]]+\]:\s*\S+.*$", "", t)
    t = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", t)
    t = re.sub(r"(?s)(`+)(.*?)\1", r"\2", t)
    for mark in ("***", "___", "**", "__", "~~", "*", "_"):
        t = t.replace(mark, "")
    # 不可见字符：零宽、软连字符、双向控制、word joiner 与不可见运算符、
    # 阿拉伯字母标记、蒙文元音分隔符、词间注记。读者一个都看不见。
    # 不可见字符：零宽、软连字符、双向控制、word joiner 与不可见运算符、
    # 定向隔离符（U+2066-U+2069）、阿拉伯字母标记、蒙文元音分隔符、
    # 组合用记号（U+034F）、词间注记、变体选择符（U+FE00-U+FE0F）。
    # 读者一个都看不见 —— 而「看不见」正是「藏起来」的定义。
    t = re.sub("[\u200b-\u200f\ufeff\u00ad\u061c\u180e\u202a-\u202e"
               "\u2060-\u2064\u2066-\u2069\ufff9-\ufffb\u034f\ufe00-\ufe0f]", "", t)
    return t


def _render_hidden_hits(text: str) -> list[tuple[str, int]]:
    """**渲染之后**才出现的词（原文里没有）—— 即「被语法藏起来」的。

    判据：归一化后的命中数**多于**原文 —— 多出来的那几次才是「被语法藏起来」的。
    否则那只是普通的字面命中，已经由面 1 / 面 3 报过了。

    **为什么按「次数差」而不是按「词集合差」**：早期版本比的是**词集合** ——
    只要这个词在原文里出现过，渲染后的命中就整条不算。后果是：**一个文件里
    既有字面命中、又有被藏起来的命中时，藏起来的那部分完全不可见。**
    而工作区/历史的基线是 0，于是这条洞在报告里长得跟「一切正常」一模一样。
    2026-10-21 的外部复核抓到的就是这一条。
    """
    plain = dict(_hits_in(text))
    out = []
    for w, n in _hits_in(_render_normalize(text)):
        extra = n - plain.get(w, 0)
        if extra > 0:
            out.append((w, extra))
    return out


def _env() -> dict:
    """显式给子进程定编码。

    不给 env 就继承开发机的全部环境变量 —— 那正是「本地绿、CI 红」的根源：
    本地恰好继承到的是对的编码。这条门禁的解码口径必须与跑它的那台机器的
    控制台无关（契约见 tests/test_subprocess_encoding_contract.py）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _git(*args: str, repo: Path | None = None) -> str:
    """跑一条 git 命令。

    `repo` 默认是本仓库。**临时仓库的反向测试要传它** —— 面 4 的反向测试
    就在一个「只有一条 tag 说明」的临时仓库上跑（见
    `test_tag_scan_detects_a_word_on_the_second_line`）。
    """
    if _GIT is None:
        raise AssertionError("找不到 git —— 这道门禁要读全历史，它跑不了")
    proc = subprocess.run(
        [_GIT, "-C", str(repo or ROOT), *args],
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
    return _decode_text(raw)


def _decode_text(raw: bytes) -> str:
    """把一段字节解成文本：先严格 UTF-8，失败再试 GB18030，最后才降级。

    **为什么要兜底**：只用 `utf-8, errors="replace"` 的话，GBK 编码的
    中文会变成一串 U+FFFD —— 那不是「里面没有词」，那是**看不见**。
    2026-10 的外部复核抓到过两个这样的文件（工作区的一个 GBK 夹具、
    历史里一个 GBK 编码的批处理文件）：当时它们都是 0 命中，但那是
    **因为里面本来就没有词**，不是因为门禁看得见。

    **为什么是 GB18030 而不是 GBK**：GB18030 是 GBK 的超集，也覆盖
    GBK 之外的汉字与符号，试它一个就够。

    **降级路径（两个编码都失败）会产生 U+FFFD** —— 那条路只留给真正的
    二进制乱码；门禁里另有一条断言盯着它（见
    `test_non_utf8_text_files_are_still_scanned`）。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _replacement_chars(text: str) -> list[int]:
    """text 里 U+FFFD（替换字符）的下标。

    为什么单独查它：**解码失败时每个坏字节会变成一个 U+FFFD**，
    而一个 U+FFFD 能把一个词**劈成两半** —— 于是「这个词在文件里」
    这件事在扫描口径下彻底消失，而门禁还是绿的。

    这是「0 命中」最阴的一种：不是扫不到，是**读到的不是原文**。
    本仓库实测到过 6 处，形态是**一个汉字的 3 个 UTF-8 字节被逐字节替换**
    （例：注释里的一句中文变成了「什么都<U+FFFD><U+FFFD><U+FFFD>没有」）。
    原文不可考 —— 逐版本比对过，那几个文件的**每一版**都已经是坏的。
    """
    return [i for i, c in enumerate(text) if c == "\ufffd"]


# ---------------------------------------------------------------------------
# 对象库：可达的 blob 与不可达的 blob
# ---------------------------------------------------------------------------
def _object_blobs() -> list[str]:
    """对象库里所有 blob 的 SHA（**含不可达的**）。

    `git rev-list --objects --all` 只给可达的；`--batch-all-objects` 给
    对象库全集。两者相减 = **不可达对象** —— 那些被强推替换掉的提交、
    reflog 还持有的提交，都在这里，而 `rev-list --all` 永远看不见它们。
    2026-10 的复核就是这么抓到一笔「只在 reflog 里、GitHub 侧仍可直读」的
    提交里带着违禁词。
    """
    return [s for s, t in _object_types() if t == "blob"]


def _object_types(repo: Path | None = None) -> list[tuple[str, str]]:
    """对象库里所有对象的 (sha, type)（**含不可达的**）。"""
    out = subprocess.run(
        [_GIT, "-C", str(repo or ROOT), "cat-file", "--batch-all-objects",
         "--batch-check=%(objectname) %(objecttype)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_env(), timeout=900).stdout
    pairs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def _reachable_blobs(repo: Path | None = None) -> set[str]:
    out = _git("rev-list", "--objects", "--all", repo=repo)
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def _read_blobs(shas: list[str], repo: Path | None = None) -> dict[str, str]:
    """一次进程读完多个 blob（`cat-file --batch`）。

    逐个 `git cat-file blob` 会起 1,300 次进程；一次批量读只要一个。

    **二进制跳过**：把图片当 UTF-8 解，会解出随机的「词」—— 2026-10 那次
    复核就撞到过：一张 jpg 被解成文本后，字节序列里恰好出现了某个缩写。
    判据与 `_read_text` 一致（前 8000 字节里有 NUL 即视为二进制）✓。
    """
    if not shas:
        return {}
    payload = ("\n".join(shas) + "\n").encode("utf-8")
    proc = subprocess.run(
        [_GIT, "-C", str(repo or ROOT), "cat-file", "--batch"],
        input=payload, capture_output=True, env=_env(), timeout=1800)
    raw = proc.stdout
    texts: dict[str, str] = {}
    pos = 0
    for sha in shas:
        nl = raw.find(b"\n", pos)
        if nl < 0:
            break
        header = raw[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3:
            continue
        size = int(header[2])
        body = raw[pos:pos + size]
        pos += size + 1
        if b"\0" in body[:8000]:
            continue          # 二进制：不当文本扫（与 _read_text 同口径）
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            # 非 UTF-8 → 走了 GB18030 兜底。**记下来**：那是「这条通道真的
            # 被走过」的证据，没有它，这条通道可以悄悄消失而门禁全绿。
            _FALLBACK_BLOBS.add(sha)
        texts[sha] = _decode_text(body)
    return texts


#: 走了解码兜底（非 UTF-8 → GB18030）的 blob —— 那条通道存在过的证据。
_FALLBACK_BLOBS: set[str] = set()


#: 全部可达 blob 的正文（**只读一次**）。面 3 与「被拆开的词」那条断言
#: 要的是同一批文本 —— 读两遍只是白花时间。
_BLOB_TEXTS: dict[str, str] | None = None


def _all_blob_texts() -> dict[str, str]:
    """全部可达 blob 的正文（读一次，缓存住）。"""
    global _BLOB_TEXTS
    if _BLOB_TEXTS is None:
        _BLOB_TEXTS = _read_blobs(sorted(_reachable_blobs()))
    return _BLOB_TEXTS


def _flatten(text: str) -> str:
    """去掉所有空白后的文本（**只用来做候选发现**）。"""
    return "".join(text.split())


def _flex_pattern(word: str) -> str:
    """词的「允许词内出现空白」正则。

    - **每个字符之间**允许任意空白（`\\s*`）；
    - 词条**本身**的空白位置也用 `\\s*` 填充 —— 所以那 11 条**自带空格**的
      词条（模型名那一族）也能匹配折行形式（写成字面空格就匹配不到，
      那是第一版的第二个缺陷）；
    - 纯 ASCII 的词保留**整词边界**（否则那个两字母缩写会撞进 `X…Y`
      这种更长的词里）。

    **这里刻意不举具体词条的例子** —— 本文件也在扫描范围内，例子一写出来，
    门禁就会先把自己判红（2026-10 栽过两次：一次在 docstring 里引用了
    一整句被清掉的话，一次就在这个函数的注释里）。
    """
    import re
    parts = [r"\s*" if c.isspace() else re.escape(c) for c in word]
    body = r"\s*".join(parts)
    if _is_ascii_word(word):
        return r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9_])"
    return body


#: 所有词条的「去空白形式」合成一个正则 —— **只用来做候选发现**。
#: 长的排前面（Python 的 alternation 是 leftmost-first，短的先匹配会盖住长的）。
_FLAT_COMBINED = None


def _flat_combined():
    global _FLAT_COMBINED
    if _FLAT_COMBINED is None:
        import re
        needles = sorted({("".join(w.split()), w) for w in FORBIDDEN},
                         key=lambda t: -len(t[0]))
        _FLAT_COMBINED = re.compile(
            "|".join(re.escape(n) for n, _w in needles))
    return _FLAT_COMBINED


def _whitespace_split_hits(text: str) -> list[str]:
    """text 里**被空白拆开**的禁用词（渲染口径下才会命中的那些）。

    **两道工序**（2026-10 的外部复核把这一版的三个缺陷逐条复现了，全是真缺陷）：

    1. **候选发现**（便宜）：在**去空白**后的文本上，用一个合成正则找出
       可能命中的词条。**这一步不判定** —— 扁平化会把 ASCII 词的词边界
       也毁掉（更长的词会被当成含那个缩写），判定必须回到原文。
    2. **精确判定**（在**原文**上）：用「允许词内出现空白」的正则逐词找，
       然后看**那一次匹配的原文片段**：
       * 片段 == 原词 → 那是**字面**（红线会报它），不算「被拆开」；
       * 片段里**没有空白** → 那是变体（`ClaudeOpus`），也不算；
       * 否则 → 中间插进了空白 → **算被拆开**。

    第一版（只做扁平判定）有三个缺陷：字面命中被算成拆开（`all([])` 恒真）、
    **11 条自带空格的词条连字面都匹配不到**、每词每文本只记一次。

    **第三条没有改代码，而是被写成了单位定义**：本函数的计数单位是
    **（对象, 词）对**，不是「处」—— 同一个文本里两次拆开只记一次，
    正好就是「这个对象上这个词出现了拆开形态」。基线 6 的含义因此是
    「6 个对象各配 1 个词」这一族配对，不是「6 处」。
    （2026-10-21 的外部复核把这一条记成「声明已修、代码未变」—— 准确的说法是
    它被写成了单位定义，而不是被改掉了。）
    """
    import re
    flat = _flatten(text)
    if not flat:
        return []
    cands = {m.group(0) for m in _flat_combined().finditer(flat)}
    if not cands:
        return []
    out = []
    for w in FORBIDDEN:
        if "".join(w.split()) not in cands:
            continue
        for m in re.finditer(_flex_pattern(w), text):
            span = text[m.start():m.end()]
            if span == w:                       # 字面 —— 红线会报它
                continue
            if not any(ch.isspace() for ch in span):   # 变体，不是「被拆开」
                continue
            out.append(w)
            break
    return out


def _tag_messages(repo: Path | None = None) -> list[tuple[str, str]]:
    """每个 annotated tag 的**整条**说明文字（面 4）。

    **不能靠 `for-each-ref --format='%(contents)'`。** 那个字段是**多行**值：
    一个 tag 的说明有多行时，一次 `splitlines()` 会把它拆成多行，
    而后面「只认含 tab 的行」那一步会把没有 tab 的**续行整条丢掉** ——
    于是**每个 tag 只有第一行**被检查。

    2026-10 的外部复核实测过这个洞：本仓库 32 个 annotated tag 的
    **整条对象**共 270 行（**其中说明文字只有 110 行**，其余是
    `object`/`type`/`tag`/`tagger` 头部与空行），旧实现只扫到 **33 行** ——
    169 行被直接跳过。12.2% 是对整条对象算的；若只对说明文字算，是 30%。
    当时没漏到东西（整条读也是 0 命中），但那是运气，不是口径。

    改成按**对象**读：`cat-file tag <sha>` 给出 tag 对象的全文
    （头部 + 说明，多行完整保留）。顺带把 tagger 署名也扫进去了 ——
    署名也是内容。

    轻量 tag（`%(objecttype)` 是 commit）没有说明文字，跳过。
    """
    out = _git("for-each-ref",
               "--format=%(refname:short)%09%(objecttype)%09%(objectname)",
               "refs/tags", repo=repo)
    msgs: list[tuple[str, str]] = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        name, otype, sha = parts
        if otype != "tag":
            continue          # 轻量 tag 没有说明文字
        msgs.append((name, _git("cat-file", "tag", sha, repo=repo)))
    return msgs


def _api_headers() -> dict:
    """面 6 的 API 请求头（有 token 就带上 —— 见 `_TOKEN` 的注释）。"""
    headers = {"User-Agent": "lac-wording-gate",
               "Accept": "application/vnd.github+json"}
    if _TOKEN:
        headers["Authorization"] = "Bearer " + _TOKEN
    return headers


def _release_bodies() -> tuple[list[tuple[str, str]], int, int]:
    """GitHub 上 27 个 Release 的**标题、正文与附件名**（面 6）。

    标题与正文**不在 git 里** —— 它们只存在于 GitHub 的数据库上。
    2026-10 的复核在这里抓到一处漏网：某个 Release 的**标题**带着违禁词，
    而它的正文与 CHANGELOG 对应小节都已经改过了（只改了正文，忘了标题）。

    附件名一并扫：它也是公开内容。

    **返回三个值**：字段列表 + **Release 数** + **附件数**。有了它们，判据
    能做一条**精确**的对账：字段数必须等于 `2 × Release 数 + 附件数` ——
    不靠任何写死的数字（写死的数字在「只剩最新版本」时当场变成假红），
    也不靠「至少拿到 N 个」这种塌到 10 个还绿的门槛。

    走匿名 API（本仓库是公开的），拿不到就**报错**而不是静默跳过 ——
    「扫了 0 个 Release 却报通过」是这道门禁最没用的结论。
    """
    req = urllib.request.Request(_RELEASES_API, headers=_api_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise AssertionError(
            "读不到 Release 列表（%s）—— 面 6 扫不了，"
            "**不要**当成「扫过了、没问题」" % exc) from exc
    out = []
    n_assets = 0
    n_releases = 0
    for rel in data:
        n_releases += 1
        out.append((rel["tag_name"] + "（标题）", rel.get("name") or ""))
        out.append((rel["tag_name"] + "（正文）", rel.get("body") or ""))
        assets = rel.get("assets", [])
        n_assets += len(assets)
        for asset in assets:
            out.append((rel["tag_name"] + "（附件名）", asset.get("name") or ""))
    return out, n_releases, n_assets


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_scans_are_not_vacuous():
    """门禁本身不能是空的 —— 这一条是下面几条的地基。

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

    blobs = _object_blobs()
    assert len(blobs) >= _MIN_BLOBS, (
        "对象库里只有 %d 个 blob —— 历史内容扫描面塌了" % len(blobs))


def test_worktree_files_are_all_actually_read():
    """**列出来的文件必须真的读到** —— 否则「扫过了」是假的。

    `_read_text` 遇到读不了的路径会返回空串（与 git grep -I 的口径一致），
    但那也让「一个跟踪但本地缺失的文件」被无声跳过。这里数一遍：
    列出多少、读到多少，对不上就报出来。
    """
    files = _tracked_paths()
    read = 0
    skipped = []
    for rel in files:
        p = ROOT / rel
        try:
            raw = p.read_bytes()
        except OSError:
            skipped.append(rel)
            continue
        if b"\0" in raw[:8000]:
            continue          # 二进制：确实不读，但也不算「漏」
        read += 1
    assert not skipped, (
        "这 %d 个跟踪文件读不到（列出来了却没扫）：%s"
        % (len(skipped), skipped[:5]))
    assert read >= _MIN_FILES, (
        "只真正读到 %d 个文件（门限 %d）" % (read, _MIN_FILES))


def test_head_tree_has_no_forbidden_wording():
    """扫描面 1：贡献者看得见的那一份文件内容。"""
    hits = []
    for rel in _tracked_paths():
        for word, n in _hits_in(_read_text(rel)):
            hits.append((rel, word, n))
    assert not hits, "跟踪文件里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (rel, n) for rel, word, n in hits)


def test_worktree_has_no_render_hidden_word():
    """面 1 的**渲染口径**：原文里没有、渲染之后才出现的词。

    「被语法藏起来」的那一类（HTML 实体 / 标签 / Markdown 强调 / 行内代码 /
    零宽字符）。它**不进红线** —— 字面口径才是红线 —— 但它**是一条独立的
    上界断言**：当前基线 **0**，涨了要红。

    **先证真**：探测器必须真的抓得到东西，否则「0 处」只是「恒返回空」。
    探针用词表里的词**自己拼**出来（这份文件里不能出现词面量）。
    """
    probe = next(w for w in FORBIDDEN if len(w) >= 2)
    hidden = "**%s**%s" % (probe[0], probe[1:])
    assert _render_hidden_hits(hidden), (
        "渲染探测器什么都抓不到 —— 它形同虚设，"
        "下面那条「0 处」也就没有意义了")
    assert not _render_hidden_hits("一句没有禁用词的话"), (
        "渲染探测器把干净的文本判成了有 —— 它的判据反了")

    hits = []
    for rel in _tracked_paths():
        for word, n in _render_hidden_hits(_read_text(rel)):
            hits.append((rel, word, n))
    assert not hits, (
        "有词被渲染语法藏起来了（工作区）：\n"
        + "\n".join("  %s —— %d 处" % (rel, n) for rel, word, n in hits[:20]))


def test_history_has_no_render_hidden_word():
    """面 3 的**渲染口径**：全部历史版本的文件内容。

    与工作区同一条判据，同样是**上界**（当前基线 0）。
    历史里也可能有被语法藏起来的词 —— 上一轮的重写只按**字面**口径清过。

    **这条自己也要有探针**：早期版本它只断言「没有命中」，于是单独跑这一条时
    （`pytest -k history_has_no_render_hidden_word`）探测器恒返回空也照样绿
    —— 而门禁一旦「能通过但不检查」，它就不算门禁了。探针用词表里的词
    **自己拼**出来（这份文件里不能出现词面量）。
    """
    probe = next(w for w in FORBIDDEN if len(w) >= 2)
    assert _render_hidden_hits("**%s**%s" % (probe[0], probe[1:])), (
        "渲染探测器什么都抓不到 —— 这条历史断言形同虚设，"
        "「0 处」只是「恒返回空」")
    hits = []
    for sha, text in _all_blob_texts().items():
        for word, n in _render_hidden_hits(text):
            hits.append((sha[:8], word, n))
    assert not hits, (
        "有词被渲染语法藏起来了（历史）：\n"
        + "\n".join("  %s —— %d 处" % (sha, n) for sha, word, n in hits[:20]))


def test_render_hidden_count_is_a_difference_not_a_word_set_difference():
    """同一个词「字面出现一次 + 被语法藏起来一次」时，**藏起来的那次不能被吞掉**。

    早期实现按**词集合**做差：只要这个词在原文里出现过，渲染后的命中就整条
    不算。后果是 —— 一个文件里既有字面命中、又有被藏起来的命中时，
    **藏起来的那部分完全不可见**，而基线是 0，于是这条洞在报告里长得
    跟「一切正常」一模一样。

    2026-10-21 的外部复核抓到的就是这一条。
    """
    probe = next(w for w in FORBIDDEN if len(w) >= 2)
    hidden = "**%s**%s" % (probe[0], probe[1:])
    assert _render_hidden_hits(hidden), "探测器抓不到单独出现的隐藏词"
    both = "这里有 %s 一次字面\n另一行有 %s 一次隐藏\n" % (probe, hidden)
    assert _render_hidden_hits(both), (
        "字面出现一次就把藏起来的那次吞掉了 —— 判据按**词集合**做差，"
        "于是「既有字面又有隐藏」时，隐藏那部分完全不可见")


def test_render_pipeline_sees_the_hiding_vectors_it_claims_to_see():
    """每一种「被语法藏起来」都要能被抓到。

    基线 0 只说明**这一批**词没被藏起来，不说明管线**看得见**这种藏法。
    每种藏法都要有一份能被抓到的样本，否则「0 处」与「什么都没看见」
    在输出里长得一模一样。
    """
    probe = next(w for w in FORBIDDEN if len(w) >= 2)
    head, tail = probe[0], probe[1:]
    vectors = [
        ("零宽字符", head + "\u200b" + tail),
        ("软连字符", head + "\u00ad" + tail),
        ("双向控制", head + "\u202e" + tail),
        ("word joiner", head + "\u2060" + tail),
        ("定向隔离符", head + "\u2066" + tail),
        ("组合用记号", head + "\u034f" + tail),
        ("变体选择符", head + "\ufe00" + tail),
        ("引用式链接", "[%s][ref]%s\n\n[ref]: /x" % (head, tail)),
        ("行内标签", "<b>%s</b>%s" % (head, tail)),
        ("十进制实体", "%s&#%d;" % (head, ord(tail))),
        ("Markdown 强调", "**%s**%s" % (head, tail)),
    ]
    for label, text in vectors:
        assert _render_hidden_hits(text), (
            "渲染管线看不见「%s」这种藏法 —— 基线 0 只说明**这一批**词"
            "没被藏起来，不说明这一类藏法看得见" % label)
    # 反例对照：块级标签之间是**分段**，不是空格粘起来 —— 不该算命中。
    assert not _render_hidden_hits("<p>%s</p><p>%s</p>" % (head, tail)), (
        "块级标签被当成了可删掉的行内标签 —— 那会把两段本来不相邻的文字"
        "粘成一个词，造出读者根本没看见的命中")


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

    **整 blob 匹配**，与面 1 / 面 2 的整段口径一致。

    **这里要更正一句曾经写在这段里的说法**：「整 blob 能看见折行的词组」
    —— **不成立**。一个词跨不过换行符，所以对**不含换行符**的词，
    整段匹配与逐行匹配的**命中结果必然相同**（实测：七个面逐条比对，
    零处差异）。换成整 blob 真正换来的是三样：

    1. **一次批量读**（`cat-file --batch`）比「200 个正则 × N 个 rev」的
       `git grep` 便宜得多；
    2. **对词表里正则元字符的免疫** —— 词表用 `re.escape` 处理，
       不再靠 shell 传 200 个 `-e` 参数；
    3. **二进制判定显式化**（判据与面 1 同一份），而不是委托给 `git grep -I`。

    折行的词**两种口径都看不见** —— 那是判据问题，不是实现问题，
    见 `test_matching_semantics_are_literal_substring`。
    """
    texts = _all_blob_texts()
    hits = []
    for sha, text in texts.items():
        for word, n in _hits_in(text):
            hits.append((sha[:10], word, n))
    assert not hits, "历史版本的文件内容里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (sha, n) for sha, word, n in hits[:20])


def test_unreachable_objects_have_no_forbidden_wording():
    """扫描面 7：**不可达对象**（reflog 持有、被强推替换掉的那些提交）。

    `git rev-list --all` 按定义看不见它们。2026-10 的复核在这里抓到一笔
    「不在可达历史里、但 GitHub 侧仍能直读」的提交，它的信息里带着违禁词。

    **blob 与 commit 都扫**：commit 对象里存的就是提交信息（面 2 漏掉的那种）。
    """
    reachable_blobs = _reachable_blobs()
    reachable_commits = set(_all_revs())
    unreachable_blobs = [s for s in _object_blobs() if s not in reachable_blobs]
    unreachable_commits = [s for s, t in _object_types()
                           if t == "commit" and s not in reachable_commits]
    hits = []
    for sha, text in _read_blobs(unreachable_blobs).items():
        for word, n in _hits_in(text):
            hits.append(("blob " + sha[:10], word, n))
    for sha in unreachable_commits:
        text = _git("cat-file", "commit", sha)
        for word, n in _hits_in(text):
            hits.append(("commit " + sha[:10], word, n))
    assert not hits, (
        "不可达对象里出现了禁用词（%d 个不可达 blob、%d 个不可达 commit）：\n"
        % (len(unreachable_blobs), len(unreachable_commits))
        + "\n".join("  %s —— %d 处" % (sha, n) for sha, word, n in hits[:20]))


def test_tag_messages_have_no_forbidden_wording():
    """扫描面 4：每个 tag 的说明文字（**整条**，不是第一行）。"""
    msgs = _tag_messages()
    hits = []
    for name, text in msgs:
        for word, n in _hits_in(text):
            hits.append((name, word, n))
    assert not hits, "tag 说明文字里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (name, n) for name, word, n in hits)


def test_tag_messages_are_read_in_full():
    """面 4 的**非空断言**：读到的是整条说明，不只是第一行。

    这条是给**盲区本身**设的哨。判据刻意不绑实现方式，而只问一件事：
    「读到的文本里必须有换行」。一旦有人把读取改回
    `for-each-ref --format='%(contents)'` 那种逐行取法，这里立刻变红 ——
    而面 4 那条**不会**变红（它扫的东西更少，当然也更干净）。
    """
    msgs = _tag_messages()
    assert len(msgs) >= 20, (
        "只读到 %d 个 tag 的说明 —— 面 4 塌了（本仓库有 30 多个 annotated tag）"
        % len(msgs))
    multiline = [name for name, text in msgs if "\n" in text]
    assert multiline, (
        "读到的 tag 说明**全是单行** —— 说明只取到了第一行。"
        "`for-each-ref --format='%(contents)'` 是多行值，"
        "splitlines() 之后只有含 tab 的那行会被扫")


def test_tag_scan_detects_a_word_on_the_second_line(tmp_path):
    """面 4 的**反向测试**：说明**第二行**里的词必须被抓到。

    照 AGENTS.md §2.2 的规矩：故意做坏 → 断言必须变红。这里造一个
    「只有一条 tag 说明」的临时仓库，把词放在**第二行**：

    * 旧实现（`for-each-ref` + `splitlines()` + 只认含 tab 的行）
      **扫不到** → 这条测试会红；
    * 现在的实现（`cat-file tag` 读整条）**扫得到** → 这条测试绿。

    词从词表里取，**不写字面量** —— 本文件也在门禁的扫描范围内。
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args: str, stdin: str | None = None) -> None:
        proc = subprocess.run(
            [_GIT, "-C", str(repo), *args], input=stdin,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_env(), timeout=120)
        assert proc.returncode == 0, (
            "git %s 失败：%s" % (" ".join(args), proc.stderr.strip()))

    g("-c", "init.defaultBranch=main", "init", "-q")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    g("add", "-A")
    g("-c", "user.name=wording-gate", "-c", "user.email=wording@example.invalid",
      "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    word = FORBIDDEN[0]
    g("-c", "user.name=wording-gate", "-c", "user.email=wording@example.invalid",
      "tag", "-a", "v0.0.0-test", "-F", "-",
      stdin="第一行没有词\n第二行有 %s\n" % word)

    msgs = _tag_messages(repo=repo)
    assert msgs, "没读到那条 tag 的说明 —— 读取逻辑坏了"
    hits = [(name, w, n) for name, text in msgs for w, n in _hits_in(text)]
    assert hits, (
        "说明**第二行**里的禁用词没被抓到 —— 面 4 又退回到「只扫第一行」了")


def test_matching_semantics_are_literal_substring():
    """匹配口径钉死：**字面子串** —— 被空格或换行拆开**不算**命中。

    这条测试的作用不是「检查代码」，而是把一个**判据问题**钉在明面上：
    一个词被空格或换行拆开时，算不算它？

    **当前答案：不算。** 依据是清单里的判据原文 ——「含中文的词按
    **子串**匹配」。一个被空格（或换行）拆开的词**不是**那个词的子串。
    （这里刻意不举具体例子 —— 本文件也在扫描范围内，例子一写出来，
    门禁就会先把自己判红。）

    **这个选择有代价，而且代价是实测过的**：按「渲染语义」（去掉所有空白
    后再匹配）扫七个面，本仓库会多出 6 处命中，**全在历史里**（工作区已
    清零）—— 全都是同一句保留表里的自述（那句话在否定句里点名了被清掉的
    做法，所以它既是一处命中，也是一条「清掉了什么」的记录）。

    **那句原文刻意不抄在这里。** 本文件也在扫描范围内：抄了它，一旦口径
    改成渲染语义，门禁会**先把自己判红** —— 守门的人与被守的东西住在
    同一处，形状与「手打码点」那次一模一样。引文请查 CHANGELOG「第五轮」
    那一节。

    也就是说：**被拆开的词现在是看不见的**，而这**不是**门禁坏了，
    是判据选了「字面」。

    钉死它的理由：口径若在没人察觉的情况下漂移，前后的扫描结果就不可比，
    而「不可比」是最难被发现的一类问题。要改这个判据，就**改这条测试**，
    并把代价重新量一遍。
    """
    word = FORBIDDEN[0]
    assert _count(word, word) == 1, "词自己都匹配不上 —— 匹配器坏了"
    assert _count("前面 " + word + " 后面", word) == 1, "词被前后文字包着时匹配不上"
    assert _count(word[0] + "\n" + word[1:], word) == 0, (
        "折行的词被判成了命中 —— 匹配口径变了。"
        "请把这条测试连同判据一起改，并把代价重新量一遍")
    assert _count(word[0] + " " + word[1:], word) == 0, (
        "被空格拆开的词被判成了命中 —— 匹配口径变了。"
        "请把这条测试连同判据一起改，并把代价重新量一遍")


def test_worktree_has_no_whitespace_split_forbidden_word():
    """工作区里不许有**被空白拆开**的禁用词（渲染口径下会命中的那些）。

    **这是本轮新加的一条**：上一轮把「代价我量过了」写进了 docstring，
    而那句话是**散文** —— 没人看守。本条把它变成可执行断言：工作区一旦
    出现一个被拆开的词，立刻红。

    历史那一侧的同口径计数见下一条（基线 6，全是保留表里的自述）。

    为什么单立一条、而不是并进面 1：**面 1 的红线是字面口径**，
    「被拆开的词算不算」是**判据问题**（见
    `test_matching_semantics_are_literal_substring`）。这条守的不是
    判据本身，而是「**工作区不许再长出新的这一类**」。
    """
    hits = []
    for rel in _tracked_paths():
        for w in _whitespace_split_hits(_read_text(rel)):
            hits.append((rel, w))
    # **先证真**：这个探测器本身要能抓到东西 —— 否则「工作区 0 处」这句
    # 结论可能只是「探测器恒返回空」。报数为 0 与报数为错在输出里长得一样。
    probe = FORBIDDEN[0]
    assert _whitespace_split_hits("x" + probe[0] + " " + probe[1:] + "y"), (
        "「被拆开的词」这个探测器什么都抓不到 —— 它形同虚设，"
        "下面那条「工作区 0 处」也就没有意义了")
    # **也要能分辨**：字面出现**不算**「被拆开」。少了这一条，探测器会把
    # 字面命中与被拆开混为一谈 —— 那个数字就不再是「缺口的大小」。
    assert not _whitespace_split_hits("x" + probe + "y"), (
        "字面出现被判成了「被拆开」—— 探测器把「字面命中」与「被拆开」"
        "混成了一笔账，那个数字就不能拿来做基线了")
    # **自带空格的词条也要探得到**（模型名那一族，11 条）。第一版把词条里
    # 那个空格当成了字面空格，于是这一族**连字面都匹配不到**。
    spaced = next((w for w in FORBIDDEN if " " in w), None)
    assert spaced, "词表里一条自带空格的词条都没有 —— 这一族怎么测？"
    assert _whitespace_split_hits(spaced.replace(" ", "\n")), (
        "自带空格的词条探测不到（模型名那一族）—— "
        "探测器把词条里的空格当成了字面空格")
    assert not _whitespace_split_hits(spaced), (
        "自带空格的词条，字面出现被判成了「被拆开」")
    assert not hits, (
        "工作区出现了被空白拆开的禁用词（渲染口径下会命中）：\n"
        + "\n".join("  %s —— %s" % (r, w) for r, w in hits[:20]))


def test_history_whitespace_split_hits_stay_within_baseline():
    """历史里「被拆开的词」不得超过基线（当前 6 处，全是保留表里的自述）。

    数字是**上界**，不是等号：历史重写会让它变小（变小是好事），
    而**变大**说明有人新写进了一处 —— 那要红。

    基线 6 = 5 个 CHANGELOG.md 的旧版本 + 1 个门禁文件旧版本
    （那一版在 docstring 里引用了那句话；本版起不再引用，所以工作区已 0）。

    **先证真**：这条断言自己也必须有牙齿 —— **单独跑它**时
    （`pytest -k history_whitespace_split`），探测器恒返回空也照样绿，
    而「不超过基线」与「一个都没看见」在输出里长得一模一样。
    """
    probe = FORBIDDEN[0]
    assert _whitespace_split_hits("x" + probe[0] + " " + probe[1:] + "y"), (
        "「被拆开的词」这个探测器什么都抓不到 —— 这条历史断言形同虚设，"
        "「不超过基线」只是「恒返回空」的另一种说法")
    hits = []
    for sha, text in _all_blob_texts().items():
        for w in _whitespace_split_hits(text):
            hits.append((sha, w))
    assert len(hits) <= _WHITESPACE_SPLIT_BASELINE, (
        "被空白拆开的禁用词有 %d 处，超过基线 %d —— "
        "要么有人新写进了一处，要么基线该更新了（先看清楚再改）"
        % (len(hits), _WHITESPACE_SPLIT_BASELINE))


def test_non_utf8_text_files_are_still_scanned():
    """**非 UTF-8 的文本必须也被扫到** —— 且这一维不许悄悄消失。

    2026-10 的外部复核发现：工作区有一个 GBK 编码的夹具、历史里有一个
    GBK 编码的批处理文件。当时它们都是 0 命中，但那是**因为里面本来就没
    有词**，不是因为门禁看得见 —— 旧实现用 `errors="replace"` 解码，
    GBK 的中文会变成 U+FFFD，任何中文词都匹配不到。

    现在解码有 GB18030 兜底（见 `_decode_text`），本条断言「这条通道
    真的存在」：工作区**与历史**里非 UTF-8 的文本都不得少于 1 个，
    且它们**确实被解出了文本**（解不出来就说明兜底没生效）。
    """
    work = []
    for rel in _tracked_paths():
        raw = (ROOT / rel).read_bytes()
        if b"\0" in raw[:8000]:
            continue
        try:
            raw.decode("utf-8")
            continue
        except UnicodeDecodeError:
            pass
        work.append((rel, _decode_text(raw)))
    assert len(work) >= 1, (
        "工作区里一个非 UTF-8 文本都没有 —— 这条断言形同虚设"
        "（要么夹具被删了，要么解码兜底把这条通道吃掉了）")
    for rel, text in work:
        assert text.strip(), "%s 解不出文本 —— GB18030 兜底没生效" % rel
        assert "\ufffd" not in text, (
            "%s 解出来带替换字符 —— 降级路径被用上了，"
            "这个文件的编码门禁读不懂" % rel)

    # **历史那一侧也要有哨** —— 否则「历史编码通道」可以悄悄消失而门禁全绿。
    texts = _all_blob_texts()
    assert len(_FALLBACK_BLOBS) >= 1, (
        "历史里一个非 UTF-8 的 blob 都没有 —— 历史侧的编码通道形同虚设")
    for sha in _FALLBACK_BLOBS:
        assert texts.get(sha), "blob %s 走了兜底却没读出文本" % sha[:10]


def test_worktree_has_no_replacement_characters():
    """工作区里不许有 U+FFFD —— 它是「读到的不是原文」的指纹。

    这条守的是**另一类**缺陷。前面那几条守的是「词表里的词跑进了文件」；
    这条守的是「文件里的字**读不出来**」。两者的症状在扫描结果上长得
    **一模一样**（都是 0 命中），但后者更危险：门禁是绿的，而人眼看到的是
    一句读不通的中文 —— 2026-10 实测到的 6 处里有 5 处在注释与 docstring 里。

    范围是**全部被跟踪的文本文件**，不只是文档：只扫文档的话，源码注释里
    的坏字就永远没人管（那 5 处里有 4 处在 `scripts/` 与 `tests/` 下）。
    """
    paths = _tracked_paths()
    hits = []
    scanned = 0
    skipped_binary = 0
    scanned_paths = []
    for rel in paths:
        raw = (ROOT / rel).read_bytes()
        if b"\0" in raw[:8000]:
            skipped_binary += 1
            continue          # 二进制 —— 与 _read_text 的口径一致
        scanned += 1
        scanned_paths.append(rel)
        if _replacement_chars(_decode_text(raw)):
            hits.append(rel)

    # **先证真**：探测器本身要能抓到东西 —— 否则「工作区 0 处」可能只是
    # 「探测器恒返回空」。报数为 0 与报数为错在输出里长得一样。
    assert _replacement_chars("好\ufffd坏"), (
        "U+FFFD 探测器什么都抓不到 —— 它形同虚设，"
        "下面那条「工作区 0 处」也就没有意义了")
    assert not _replacement_chars("好字无坏"), (
        "探测器把没有替换字符的文本判成了有 —— 它的判据反了")
    # **逐个对账**：读过的 + 跳过的 = 列出来的。少读一个文件却仍然报
    # 「0 处」—— 那是**什么都没扫**，而它看起来像「扫过了、没问题」。
    assert scanned + skipped_binary == len(paths), (
        "列出来的 %d 个文件里，只读了 %d 个、跳过 %d 个 —— 对不上："
        "有的文件根本没被读，而报告仍然会说「工作区 0 处」"
        % (len(paths), scanned, skipped_binary))
    assert scanned >= _MIN_FILES, (
        "只读到 %d 个文本文件 —— 扫描面塌了（不是仓库的问题，是读取坏了）"
        % scanned)
    # **范围不许缩水**：这两个目录是那 5 处坏字真正待着的地方。
    # 少扫一个目录就少一半，而报告仍然会说「工作区 0 处」。
    for prefix in ("scripts/", "tests/"):
        assert any(p.startswith(prefix) for p in scanned_paths), (
            "读到的文件里一个 %s 下的都没有 —— 范围缩了，"
            "而下面的「0 处」不再说明任何事" % prefix)
    assert not hits, (
        "工作区有文件里带 U+FFFD（读出来的不是原文）：\n"
        + "\n".join("  " + r for r in hits[:20]))


def test_history_replacement_characters_stay_within_baseline():
    """历史里带 U+FFFD 的对象不得超过基线（当前 91 个）。

    上界不是等号：历史重写会让它变小（变小是好事），而**变大**说明有人
    新写进了一处坏字 —— 那要红。

    这 91 个对象里**没有一个词表里的词**，所以它不是措辞问题；它是
    「读出来不是原文」。分布：CHANGELOG.md 72 + 变异记分卡 9 + 门禁文件 3
    + 另 4 个文件各 1，**其中 3 个是提交对象**（3 笔提交信息里各有一处坏字）。

    工作区那 6 处已在 v2.12.5 修掉，历史里的这些改不动了（重写历史不减少
    已发布的暴露面，见 CHANGELOG 第七轮）—— 于是这里只做**上界**，把它钉住，
    不让它悄悄长大。
    """
    hits = [sha for sha, text in _all_blob_texts().items()
            if _replacement_chars(text)]
    assert len(hits) <= _FFFD_HISTORY_BASELINE, (
        "带 U+FFFD 的历史对象有 %d 个，超过基线 %d —— "
        "要么有人新写进了一处坏字，要么基线该更新了（先看清楚再改）"
        % (len(hits), _FFFD_HISTORY_BASELINE))


def test_forbidden_table_entries_are_real_words():
    """词表里每一条都必须是一个**像样的词** —— 防住「手打码点」那类错。

    2026-10 的事故：某条新词的首字码位打成了同音的另一字，解码出来
    根本不是一个词。门禁按码点扫，**对它完全无感** —— 那条错误条目
    匹配不到任何东西，于是「表里有 200 条」与「每条都是词」同时绿着，
    而后者**从来没有人看守**。

    这条断言能自动抓住那一类错：解码后的条目里不允许出现
    **ASCII 与 CJK 之外**的字符 —— 手打十六进制打错时，落点常常在
    Latin-1 补充区之类的地方（那次错解出来是 `wZÖ¸` 这种东西）。

    **它只能**抓「字符集不对」这一类：「码点对、但词写错了」（比如两个
    字的顺序反了）抓不到 —— 那要靠人读。
    """
    for codes, word in zip(_FORBIDDEN_CODES, FORBIDDEN):
        bad = [c for c in word if not (c.isascii() or _is_cjk(c))]
        assert not bad, (
            "词表条目 %r 解码出来不是词（含非 ASCII 非 CJK 字符 %r）：%r"
            % (codes, bad, word))
    assert len(set(FORBIDDEN)) == len(FORBIDDEN), "词表里有重复条目"


def test_ref_names_have_no_forbidden_wording():
    """扫描面 5：所有 ref 的名字（名字也是内容）。"""
    hits = []
    for name in _git("for-each-ref", "--format=%(refname)").splitlines():
        for word, n in _hits_in(name):
            hits.append((name.strip(), word, n))
    assert not hits, "ref 名字里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (name, n) for name, word, n in hits)


def test_release_titles_and_bodies_have_no_forbidden_wording():
    """扫描面 6：GitHub 上 27 个 Release 的**标题与正文**。

    这两个字段只在 GitHub 的数据库上，git 里没有 —— 2026-10 的复核在这里
    抓到一处漏网：某个 Release 的标题带着违禁词，而它的正文与 CHANGELOG
    对应小节都已经改过了（只改了正文，忘了标题）。
    """
    releases, n_releases, n_assets = _release_bodies()
    assert n_releases >= 1, (
        "一个 Release 都没拿到 —— 面 6 塌了（这不是仓库的问题，是 API 没返回）")
    # **精确**对账，且**不写死任何数字**：每个 Release 贡献标题 + 正文 +
    # 它的每个附件名。少了任何一个字段，这条就红。
    # （写死一个下门限是不行的：2026-10-21 删掉 27 个旧 Release 之后，
    #   字段数从 108 掉到 4 —— 写死的门限当场变红，而那不是塌陷。）
    assert len(releases) == 2 * n_releases + n_assets, (
        "拿到 %d 个字段，与「%d 个 Release × (标题+正文) + %d 个附件名」"
        "算出来的 %d 对不上 —— 有字段被静默丢掉了"
        % (len(releases), n_releases, n_assets, 2 * n_releases + n_assets))
    hits = []
    for name, text in releases:
        for word, n in _hits_in(text):
            hits.append((name, word, n))
    assert not hits, "Release 的标题、正文或附件名里出现了禁用词：\n" + "\n".join(
        "  %s —— %d 处" % (name, n) for name, word, n in hits)
