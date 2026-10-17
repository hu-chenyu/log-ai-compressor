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


def test_hostname_masking_was_tried_and_falsified():
    """补主机名掩码这条路**试过两次、都证伪了**，这里把过程钉住。

    第一次按「点 + 纯字母」的形状写（末段 2~24 位字母）：

        chrome.exe -> D    ftp.exe -> D

    Proxifier 的事件类型**恰恰靠进程名区分**，于是 117 簇 / 100% 被合并成
    **13 簇 / 72.7%** —— 比不改还糟。

    第二次改用 TLD 白名单（com/org/net/io/internal/...），进程名保住了，
    但主机名本身在 Proxifier 里**就是事件身份**，于是：

        Proxifier  117 簇 / 100.0%  ->  20 簇 / 90.9%
        Zookeeper   83 簇 / 92.1%   ->  82 簇 / 91.0%

    仍然是把不同事件合并。**方向就是错的**，不是调参能救的。

    真正的修法是按数据集自适应粒度（Proxifier 对主机敏感、HPC 对用词敏感），
    那是独立的一件事。
    """
    t = mask_text("[10.30 16:49:06] chrome.exe - proxy.example.org:5070 close")
    assert "chrome.exe" in t, (
        "进程名被当成主机名掩掉了 —— 第一次失败的形状；当前模板 %r" % t)
    assert "data.log" in mask_text("data.log missing"), (
        "日志文件名被当成域名掩掉了 —— 当前 mask_text 会把 .log 当 TLD")
    assert "proxy.example.org" in t, (
        "主机名已经被归一化了 —— 「守卫会误伤」这个理由可能已过期，"
        "请重新评估加守卫：当前模板 %r" % t)
