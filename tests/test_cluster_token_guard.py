# -*- coding: utf-8 -*-
"""记录一个**已诊断、但本版不修**的漏判：编辑距离看不见「样板雷同、用词不同」。

这个文件不是守卫测试，是诊断证据。
----------------------------------------------------
Loghub HPC 默认档只覆盖 63 / 74 种标注事件（85.1%），漏掉的大头是系统事件。
逐条核对找到并簇实例：

    E57  ``N node-N action start N N boot (command N)``      41 字符
    E49  ``N node-N action start N N risBoot (command N)``   44 字符
    ratio = 2 x 41 / (41 + 44) = 0.965   >= 0.85  ->  被并成同一簇

**正常 boot 与异常 risBoot 是两件事**，Loghub 标注也分开算。但 38 个字符的
样板前缀把 3 个字符的区分词淹没了 —— 字符级相似度看不见那 3 个字符恰恰是
唯一的区别。

为什么本版不修
--------------
直接加「字面词元集合相同才准合并」这条守卫，**实测代价大于收益**：

    数据集      守卫前(簇/交付口径)      守卫后(簇/交付口径)
    HPC          76 / 23.0%          ->  2690 / 8.1%
    Proxifier   117 / 81.8%          ->  1834 / 45.5%
    Zookeeper    83 / 22.5%          ->   107 / 22.5%

覆盖率的账面收益是真的（HPC 同预算 85.1% -> 93.2%），但**用户拿到手的那
20 个簇从「23% 的事件」掉到「8% 的事件」**，报告从 76 行变成 2,690 行 ——
那是坏掉的工具，不是更好的工具。

根因比守卫更深一层：**mask_text 没把主机名、带点的 IP、端口归一化**，它们
以字面词元留在模板里，守卫一挡就全线碎片化（Proxifier 的
``proxy.example.org:5070`` 就是这种）。正确的修法是**补掩码**，不是在校类
阶段加一道会误伤的闸门。补掩码要动全链路并重新全量实测，是独立的一件事。

下面三条断言只钉**诊断事实**（它们不会随修法变化而失效），不钉合并行为。
"""
from __future__ import annotations

from log_ai_compressor.core.clustering import literal_tokens, mask_text, similarity

BOOT = "2558385 node-127 action start 1073118184 1 boot  (command 1894)"
RISBOOT = "2568664 node-136 action start 1074119886 1 risBoot  (command 1899)"


def test_literal_tokens_drops_placeholders():
    t = mask_text("1074119886 node-136 action start 1074119886 risBoot")
    toks = literal_tokens(t)
    assert "risBoot" in toks
    # 掩码出来的 N / H / S / P / U / 0xH 都不该算字面词元
    assert not ({"N", "H", "S", "P", "U", "0xH"} & toks)


def test_boot_and_risboot_are_different_token_sets():
    """这是整条诊断的根：字符上只差 3 个，词上是两个不同的词。"""
    assert literal_tokens(mask_text(BOOT)) != literal_tokens(mask_text(RISBOOT))


def test_but_they_are_above_the_merge_threshold():
    """**前提**：这两条今天会被合并。阈值 0.85 拦不住 0.965。

    若哪天这条断言失败（说明聚类已经能分开它们），本文件就该删掉 ——
    那意味着问题真的修好了，而不是换了个地方出问题。
    """
    s = similarity(mask_text(BOOT), mask_text(RISBOOT))
    assert s >= 0.85, (
        f"相似度已降到 {s:.3f}（< 0.85），低于合并阈值 —— "
        f"这个诊断可能已经过期，请重新核对并删掉本文件")


def test_masking_does_not_normalize_hostnames():
    """根因的另一半：**主机名没被掩码**，所以守卫一挡就全线碎片化。

    端口这类纯数字确实会被归一化（5070 -> N），但域名/主机名是字面词元，
    会留在模板里 —— 于是「不同主机」变成「不同词元集合」，守卫一挡，
    Proxifier 那类日志就从一个簇炸成几十个。

    若哪天这条断言失败（说明掩码已经能归一化主机名），那么「守卫会误伤」
    这个理由就过期了，可以重新评估加守卫。
    """
    t = mask_text("[10.30 16:49:06] chrome.exe - proxy.example.org:5070 close")
    assert "proxy.example.org" in t, (
        "掩码已经能归一化主机名了 —— 「守卫会误伤」这个理由可能已过期，"
        f"请重新评估：当前模板 {t!r}")
    assert ":N" in t, "纯数字端口应当已被归一化（这条与主机名是两回事）"
