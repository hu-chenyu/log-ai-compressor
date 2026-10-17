# -*- coding: utf-8 -*-
"""记录一个**已诊断、已量化、但本版不修**的漏判：编辑距离看不见「样板雷同、用词不同」。

这个文件不是守卫测试，是**诊断证据 + 否定结果的守门人**。
----------------------------------------------------------------
Loghub HPC 同预算档是唯一的负例，病根是过度合并。最有名的一对：

    E57  ``N node-N action start N N boot (command N)``      41 字符
    E49  ``N node-N action start N N risBoot (command N)``   44 字符
    ratio = 2 x 41 / (41 + 44) = 0.965   >= 0.85  ->  被并成同一簇

**正常 boot 与异常 risBoot 是两件事**，Loghub 标注也分开算。但 38 个字符的
样板前缀把 3 个字符的区分词淹没了。

两版「猜规则」的修法都失败了
---------------------------
v2.9.8 加「字面词元集合相同才准合并」这条守卫：HPC 同预算 85.1% → 93.2%，
但报告从 76 个簇炸成 2,690 个、交付口径 23.0% → 8.1% -> **撤回**。
于是当时的结论写成「要按数据集自适应粒度」。

v2.10.0 把那条路走完，改为**先量再定**：把 `_find_similar` 的每一次合并决定
都记下来，用 Loghub 标注判对错，找能分开两类的特征。
复现命令见 `scripts/diagnose_merge_decisions.py`。

量到的结论（HPC + Proxifier 共 4,142 次合并决定）
--------------------------------------------------
                        次数   字符相似度 min/中位/max   词元 Jaccard min/中位/max
    HPC    该合并        2193     0.851 / 0.923 / 0.994     0.273 / 0.750 / 1.000
    HPC    不该合并        67     0.861 / 0.899 / 0.971     0.333 / 0.429 / 0.889
    Prox.  该合并        1390     0.850 / 0.899 / 0.997     0.600 / 0.846 / 0.920
    Prox.  不该合并       492     0.850 / 0.899 / 0.971     0.529 / 0.750 / 1.000

**两个特征都完全重叠。** HPC「该合并」的 Jaccard 最低 0.273，比「不该合并」
的中位数还低；Proxifier 有 Jaccard = 1.000（字面词元集合完全一致）却被
Loghub 判为不同事件 —— E6/E7/E8/E9 的区别只是「哪个 bytes 字段带 (N KB)」。

所以 **Loghub 的事件定义不是模板相似度的函数**。「按数据集自适应粒度」
不是调参问题，是**判据不存在**。

更正一处旧结论
--------------
v2.9.8 的注释写「主机名本身在 Proxifier 里就是事件身份，补掩码方向就是错的」
—— **这是错的**。Loghub 对 Proxifier 的标注里主机名与进程名都被写成 `<*>`：

    E9  ``<*> close, <*> bytes sent, <*> bytes received, lifetime <*>``

主机**不是**身份。补主机名掩码之所以失败，是**过度掩码**（第一版把
`chrome.exe` 也当成主机名），不是方向反了。这条更正把「补掩码」从
「已证伪」放回「值得再试」，所以下面那条断言守着它。

还必须说清楚的一件事
--------------------
**「按 Loghub 判对错」与「对用户好不好」不是同一个问题。** Proxifier 1882 次
合并里有 492 次按 Loghub 标准是错的，同预算覆盖率仍是 100%。只看
「Loghub 判错的比例」去调参，会把一个 100% 的数据集调坏。

本文件的断言
------------
前四条钉**诊断事实**（不随修法变化而失效）。后四条钉**否定结果**——
一旦某个特征开始能分开两类，那条断言就会失败，提示重新评估加守卫。
"""
from __future__ import annotations

import pytest

from log_ai_compressor.core.clustering import literal_tokens, mask_text, similarity

BOOT = "2558385 node-127 action start 1073118184 1 boot  (command 1894)"
RISBOOT = "2568664 node-136 action start 1074119886 1 risBoot  (command 1899)"

# v2.10.0 实测（HPC + Proxifier，4,142 次合并决定）。复现：
#   python scripts/diagnose_merge_decisions.py
# 数据集不在仓库里，所以这里存的是统计量而不是逐条记录 —— 脚本能重算，
# 这些数字只用来在「判据出现」时提醒我们重新评估。
_MEASURED = {
    # 数据集: {类别: (次数, ratio_min, ratio_mid, ratio_max,
    #                          jaccard_min, jaccard_mid, jaccard_max)}
    "HPC": {
        "该合并": (2193, 0.851, 0.923, 0.994, 0.273, 0.750, 1.000),
        "不该合并": (67, 0.861, 0.899, 0.971, 0.333, 0.429, 0.889),
    },
    "Proxifier": {
        "该合并": (1390, 0.850, 0.899, 0.997, 0.600, 0.846, 0.920),
        "不该合并": (492, 0.850, 0.899, 0.971, 0.529, 0.750, 1.000),
    },
}


# ---------------------------------------------------------------------------
# 诊断事实
# ---------------------------------------------------------------------------
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


def test_hostname_masking_still_misses_process_names():
    """补主机名掩码这条路试过两次、都失败，**原因不是「方向反了」**。

    第一次按「点 + 纯字母」的形状写（末段 2~24 位字母）：

        chrome.exe -> D    ftp.exe -> D

    Proxifier 117 簇 / 100% 被合并成 13 簇 / 72.7% —— 比不改还糟。

    第二次改用 TLD 白名单，进程名保住了，但 Proxifier 掉到 90.9%。
    两次失败的共同点是**过度掩码**，不是方向问题：Loghub 的标注里主机名与
    进程名都被写成 `<*>`，**主机名根本不是事件身份**。v2.9.8 的注释把这一点
    说反了，本条断言守着更正后的结论。
    """
    t = mask_text("[10.30 16:49:06] chrome.exe - proxy.example.org:5070 close")
    assert "chrome.exe" in t, (
        "进程名被当成主机名掩掉了 —— 第一次失败的形状；当前模板 %r" % t)
    assert "data.log" in mask_text("data.log missing"), (
        "日志文件名被当成域名掩掉了 —— 当前 mask_text 会把 .log 当 TLD")
    assert "proxy.example.org" in t, (
        "主机名已经被归一化了 —— 那么「补掩码会让不同事件合并」这个理由已过期，"
        "请重新评估；当前模板 %r" % t)


# ---------------------------------------------------------------------------
# 否定结果：没有特征能分开两类 —— 断言守着这个结论
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("feature", ["字符相似度", "词元 Jaccard"])
@pytest.mark.parametrize("ds", ["HPC", "Proxifier"])
def test_no_threshold_can_separate_right_from_wrong_merges(ds, feature):
    """两个类别的取值区间重叠 => 任何阈值都会切错一边。

    这是「为什么不加守卫」的**可执行**依据。哪天这条断言失败
    （正确合并的最低值已经高过错误合并的最高值），说明判据出现了，
    应当重新评估加守卫，并把新数字回填到 `_MEASURED`。
    """
    right = _MEASURED[ds]["该合并"]
    wrong = _MEASURED[ds]["不该合并"]
    lo = right[1] if feature == "字符相似度" else right[4]
    hi = wrong[3] if feature == "字符相似度" else wrong[6]
    assert lo <= hi, (
        f"{ds} 的{feature}已经能分开两类：正确合并最低 {lo:.3f} > "
        f"错误合并最高 {hi:.3f}。判据出现了 —— 请重新评估加守卫，"
        f"并更新 _MEASURED（复现：python scripts/diagnose_merge_decisions.py）")


def test_merge_decision_counts_are_recorded():
    """总决定数对得上，说明 _MEASURED 不是手抄错的。"""
    total = sum(v[0] for tbl in _MEASURED.values() for v in tbl.values())
    assert total == 4142, f"记录了 {total} 次决定，与实测的 4142 不符"
