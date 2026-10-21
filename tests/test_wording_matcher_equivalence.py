# -*- coding: utf-8 -*-
"""多模式匹配器必须与「逐词 re.findall」**逐项相等**。

为什么要有这一份
----------------
2026-10-21 为了把记分卡从 19~34 分钟压下来，把门禁的匹配从「逐词 200 次
`re.findall`」换成了「一次 Aho–Corasick 多模式扫描」。

**换实现就是换口径**，除非有一份东西钉住两者的等价性。
所以这里就是那份东西：同一批文本，两种实现的结果必须**逐项相等** ——
不等就是红线破了，不管新实现多快。

三层语料
--------
1. **真实历史 blob** —— 门禁真正要扫的东西（抽样，理由见那条测试）。
2. **词表自组合** —— 每个词放进十几种上下文：贴边、大小写、自重叠、
   词内嵌词、相邻拼接。
3. **定种子随机串** —— 从「词的首字 + ASCII 词字符 + 汉字」里随机拼，
   撞出前两层没覆盖到的巧合。

外加一层**直接测匹配器这个类**的刁难词集 —— 见
:func:`test_matcher_equals_the_oracle_on_adversarial_word_sets`：
真实词表里几乎没有会与自身重叠的词，光靠它**测不到**「非重叠」那条规则。

**为什么参照实现要自己写一遍**
------------------------------
参照实现如果住在门禁文件里，那么「把逐词改成多模式」的那一次改动，
会把**预言机也一起改掉** —— 等价性测试就变成自己跟自己比，永远绿。
所以下面那份参照实现**不调用门禁里的任何一个函数**：它自己按
「ASCII 词整词（``[A-Za-z0-9_]`` 不挨着）、中文词按子串」重写一遍正则。

**为什么本文件里没有字面量**
---------------------------
本文件也在门禁的扫描范围内。词从门禁文件里读出来，不写字面量 ——
否则门禁会先把自己判红（「守门的人」和「被守的东西」住在同一处）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_GATE = ROOT / "tests" / "test_no_job_wording.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("tnjw_under_test", _GATE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_GATE_MOD = _load_gate()
_WORDS = list(_GATE_MOD.FORBIDDEN)
#: 用来做「折行」「被空格拆开」那些断言的词 —— 必须**至少两个字符**：
#: 单字词拆开之后还是它自己，断言会变成自相矛盾。
_LONG_WORD = next(w for w in _WORDS if len(w) >= 2)


def _prng(seed: str, count: int) -> list[int]:
    """定种子的伪随机整数序列 —— **刻意不用 `random`**。

    两个理由：

    1. bandit 的 B311 会把 `random` 报成「新发现」。这一层只是要一批
       **可复现的样本**，与安全无关。本项目的规矩是「可以修就别开豁免」
       （B607 就是把测试里的硬编码路径改成 `shutil.which` 修掉的）——
       豁免要写理由，能改就改。
    2. SHA-256 派生**跨平台、跨 Python 版本都一致**，而 `random.Random`
       的序列属于「当前版本恰好如此」。
    """
    out: list[int] = []
    block = hashlib.sha256(seed.encode("utf-8")).digest()
    while len(out) < count:
        out.extend(block)
        block = hashlib.sha256(block).digest()
    return out[:count]


# ---------------------------------------------------------------------------
# 参照实现（本文件自带，不依赖门禁里的任何一个函数）
# ---------------------------------------------------------------------------
def _oracle_for(words, text: str) -> list[tuple[str, int]]:
    """逐词扫一遍 —— 口径与门禁改动前**完全一致**。

    规则：纯 ASCII 的词按「整词」匹配（左右都不能挨着 ``[A-Za-z0-9_]``），
    含中文的词按「子串」匹配。计数是**非重叠**的（`re.findall` 的推进方式）。
    """
    out: list[tuple[str, int]] = []
    for w in words:
        if all(ord(c) < 128 for c in w):
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])"
        else:
            pattern = re.escape(w)
        n = len(re.findall(pattern, text))
        if n:
            out.append((w, n))
    return out


def _oracle(text: str) -> list[tuple[str, int]]:
    return _oracle_for(_WORDS, text)


def _matcher(text: str) -> list[tuple[str, int]]:
    """门禁实际用的那条路径（多模式扫描）。"""
    return _GATE_MOD._hits_in(text)


def _assert_same(text: str, label: str) -> None:
    expected = _oracle(text)
    actual = _matcher(text)
    assert actual == expected, (
        "%s：多模式扫描与逐词口径不一致\n"
        "  逐词：%s\n  多模式：%s" % (label, expected[:12], actual[:12]))


# ---------------------------------------------------------------------------
# 三层语料
# ---------------------------------------------------------------------------
def test_matcher_equals_the_oracle_on_real_history_blobs() -> None:
    """真实历史 blob（门禁真正要扫的东西）上必须逐项相等。

    **抽样是确定性的**：按 blob 的 sha 排序后取固定间隔，复现时拿到同一批。
    全量要 ~80 秒（参照实现很慢），放进常规测试不划算。
    """
    texts = _GATE_MOD._all_blob_texts()
    assert texts, "没读到历史 blob —— 这条测试自己失效了"
    keys = sorted(texts)
    picked: list[str] = []
    budget = 0
    for k in range(0, len(keys), 23):
        text = texts[keys[k]]
        if not text or len(text) > 120_000:
            continue
        picked.append(text)
        budget += len(text)
        if budget > 2_000_000:
            break
    assert len(picked) >= 10, "样本太少，这条测试没意义：%d 段" % len(picked)
    for text in picked:
        _assert_same(text, "真实历史 blob")


def test_matcher_equals_the_oracle_on_every_word_in_many_contexts() -> None:
    """每个词放进十几种上下文，逐项相等。

    这些上下文专门打三件事：边界（贴 ASCII 词字符）、大小写（口径区分）、
    自重叠（同一个词的两次出现**部分重叠**）。

    **自重叠那一组必须按 border 长度逐个试**：只有「词的前缀 == 词的后缀」
    时，同一个词才可能在同一段文本里出现两次而**部分重叠**
    （`w + w[k:]` 里第 0 位与第 k 位各出现一次）。
    只试 `w + w[1:]` 的话，border 长度不是 1 的词就测不到。
    """
    for w in _WORDS:
        cases = [
            w,
            w + w,
            "A" + w + "0",
            "_" + w + "_",
            " " + w + " ",
            "\n" + w + "\n",
            "汉字" + w + "汉字",
            w.upper(),
            w.lower(),
            "prefix-" + w + "-suffix",
            w + " " + w + " " + w,
        ]
        # 自重叠：把词按每一种 border 长度再拼一次
        for k in range(1, len(w)):
            cases.append(w + w[k:])
            cases.append(w[:-k] + w)
        for text in cases:
            _assert_same(text, "词表自组合")


def test_matcher_equals_the_oracle_on_adjacent_word_pairs() -> None:
    """两个词拼在一起 —— 短词落在长词内部的那种情形。

    逐词口径下两个词**各自**计数；交替式匹配器会把短词吞掉
    （那正是当初不用正则交替的原因）。
    """
    for i, w1 in enumerate(_WORDS):
        for w2 in _WORDS[i + 1:i + 4]:
            for text in (w1 + w2, w2 + w1, w1 + w2 + w1):
                _assert_same(text, "相邻拼接")


def test_matcher_equals_the_oracle_on_seeded_random_texts() -> None:
    """定种子随机串 —— 撞出前两层没覆盖到的巧合。

    种子写死：这条测试要**每次都跑同一批**，否则「昨天绿今天红」没法查。
    """
    alphabet = [w[:1] for w in _WORDS]
    alphabet += list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ ")
    alphabet += ["\n", "\t", "，", "。", "的", "了", "-", "/"]
    stream = _prng("wording-matcher-random-strings", 150 * 200)
    for i in range(150):
        size = 5 + stream[i * 200] % 136
        base = i * 200
        text = "".join(alphabet[stream[base + k] % len(alphabet)]
                       for k in range(size))
        _assert_same(text, "定种子随机串")


def test_matcher_equals_the_oracle_on_random_texts_built_from_real_words() -> None:
    """随机串的第二种：直接从词表里**抽词**拼出来。

    随机字符那一层撞的是「字符巧合」；这一层撞的是「词与词相邻」——
    比如一个词恰好是另一个词的前缀（中文两字词里很常见）。
    """
    stream = _prng("wording-matcher-random-words", 150 * 8)
    separators = ["", " ", "\n", "，", "A"]
    for i in range(150):
        base = i * 8
        n = 2 + stream[base] % 5
        parts = [_WORDS[stream[base + 1 + j] % len(_WORDS)] for j in range(n)]
        sep = separators[stream[base + 6] % len(separators)]
        text = sep.join(parts)
        _assert_same(text, "抽词拼接")


# ---------------------------------------------------------------------------
# 直接测「匹配器这个类」—— 用故意刁难的词集
# ---------------------------------------------------------------------------
def test_matcher_equals_the_oracle_on_adversarial_word_sets() -> None:
    """直接测 :class:`_MultiMatcher`，用几组故意刁难的词。

    **为什么不能只测词表里的词**：真实词表里 200 个词中只有 **1 个**带非空
    border（自己的前缀等于自己的后缀）—— 而只有这种词才可能**与自身重叠**。
    换句话说，「非重叠」那条规则在真实语料上**几乎从不触发**：
    把它删掉，真实语料上的结果一模一样，**测试也就不会红**。

    规则要靠**构造出来的**用例守着，不能靠「恰好没人踩到」。
    （这条测试是记分卡里 ``wording/multi-matcher-non-overlap-check-dropped``
    那条变异的判据 —— 删掉那条检查，这里必须变红。）
    """
    word_sets = [
        ["中中"],                  # 自重叠（中文，无边界判定 → 真的会重叠）
        ["人人人"],                # 自重叠，border 长 1
        ["中中中"],                # 自重叠，border 长 2
        ["中", "中中"],            # 一个词是另一个的子串
        ["ab", "abc"],             # 一个词是另一个的前缀
        ["abc", "bc"],             # 一个词是另一个的后缀
        ["a.b", "c+d", "x*y"],     # 正则元字符 —— 必须按字面匹配
        ["aa", "aaa"],             # 长度不同的自重叠词
        ["aA", "Aa"],              # 只差大小写
        ["aa", "aa"],              # 重复词 —— 两份要各自独立计数
        [],                        # 空词表 —— 不许崩
    ]
    texts = [
        "中", "中中", "中中中", "中中中中", "人人人", "人人", "中中中中中",
        "a", "aa", "aaa", "aaaa", "ab", "aba", "abab", "ababa", "abcabc",
        "a.b", "xa.by", "a.b.c", "c+d", "x*y", "aA", "Aa", "aaAa", "中文文中",
    ]
    for words in word_sets:
        matcher = _GATE_MOD._MultiMatcher(words)
        for text in texts:
            got = [(w, n) for w, n in zip(words, matcher.counts(text)) if n]
            want = _oracle_for(words, text)
            assert got == want, (
                "词集 %r 在 %r 上不等：多模式 %s，逐词 %s"
                % (words, text, got, want))


# ---------------------------------------------------------------------------
# 形状与「不空转」
# ---------------------------------------------------------------------------
def test_hit_list_keeps_the_word_table_order() -> None:
    """命中列表的**顺序**必须与词表一致 —— 它要按这个顺序上报。"""
    text = " ".join(_WORDS[:5])
    hits = _matcher(text)
    assert [h[0] for h in hits] == [h[0] for h in _oracle(text)]
    for w in _WORDS[:5]:
        assert w in [h[0] for h in hits], "词表里的词没被报出来：%r" % w


def test_matcher_has_teeth() -> None:
    """探测器必须真的有牙齿 —— 「恒返回空」也能让上面的等价性测试全绿。

    这一条也是记分卡里那条变异的判据：把边界判定改坏，这里必须变红。
    """
    w = _LONG_WORD
    assert dict(_matcher(w)).get(w) == 1, "埋进去的词没被找到"
    assert dict(_matcher("前面 " + w + " 后面")).get(w) == 1, "被前后文字包着时没找到"
    assert dict(_matcher(w[0] + "\n" + w[1:])).get(w, 0) == 0, "折行的词被判成了命中"
    assert dict(_matcher(w[0] + " " + w[1:])).get(w, 0) == 0, "被空格拆开的词被判成了命中"
    # ASCII 词贴上 ASCII 词字符时**不算**命中（与逐词口径一致）。
    ascii_words = [x for x in _WORDS if all(ord(c) < 128 for c in x)]
    assert ascii_words, "词表里没有 ASCII 词 —— 这条断言没意义"
    aw = ascii_words[0]
    assert dict(_matcher("A" + aw + "0")).get(aw, 0) == 0, "贴上词字符也算命中"
    assert dict(_matcher("A " + aw + " 0")).get(aw) == 1, "隔着空白就该命中"
