# -*- coding: utf-8 -*-
"""Caused-by 因果链识别原语。

为什么独立成模块：这三样东西此前住在 analysis.py，而**摄入层**（pipeline）
也需要它们 —— 「簇内每条实例各带什么内层因」必须在条目入簇时就累计，
否则分析层只能看见保留下来的那几份实例详情（每簇上限
MAX_CLUSTER_INSTANCES_DETAILED = 200），大簇的计数就成了有偏样本。

放在这里而不是 analysis 里，是为了**不制造循环依赖**：analysis 依赖模型，
模型依赖 evidence；若 pipeline 想用就 import analysis，等于 core 内部成环。
本模块只依赖标准库，是因果链逻辑的最底层。
"""
from __future__ import annotations

import re
from typing import Sequence

# 关键因果行（降噪时永不折叠）
_CAUSED_BY_RE = re.compile(r"^\s*Caused by\s*[:：]", re.IGNORECASE)
# Caused by 行里**异常本体**（`Caused by: java.io.IOException: boom` → 异常文本）
_CAUSED_BY_TEXT = re.compile(r"^\s*Caused by\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
# **行内** Caused by —— 单行异常形态。
#
# Android logcat / 部分移动端与嵌入式日志把整个异常压成一行：
#   `E PushLog2824: com.x.PushException: boom  Caused by: java.net.SocketException: abort`
# 上面两条正则都锚在 ^，于是这种链**整条看不见** —— 工具只能判 INSUFFICIENT。
# 实测代价：Android_v1 上 131 条真实 Caused-by 链，召回率 **0%**。不是判断变
# 保守了，是压根没看见链。
#（此处原先写着「同一份代码在 Hadoop 上是 91.4%」，v2.7.0 起删除：那个 Hadoop
#  数据集的来源已确认不可得，91.4% 被撤回，见 docs/BENCHMARK.md。代码注释也不该
#  拿一个撤掉的数字当论据。）
#
# 收紧点（防止把无关条目抬成 CONFIRMED）：行内 `Caused by:` 之后必须紧跟一个
# **异常类名**（Exception/Error/Throwable/Fault 结尾）。消息里随口一句
# "caused by: user input" 不满足该形状，不会触发。
_CAUSED_BY_INLINE = re.compile(
    r"Caused by\s*[:：]\s*"
    r"(?P<sig>[A-Za-z_][\w.$]*(?:Exception|Error|Throwable|Fault)[^\n]*)",
    re.IGNORECASE)


def _causal_lines(entry) -> list:
    """条目内所有可能承载因果链的文本：堆栈行 + 消息本体。

    多行堆栈把 Caused by 放在 stack 里；单行异常（logcat）它落在 message 里。
    两者都要看，否则单行形态永远漏。
    """
    if entry is None:
        return []
    out = list(getattr(entry, "stack", None) or [])
    msg = getattr(entry, "message", None)
    if msg:
        out.append(msg)
    return out


def _has_caused_by(lines: Sequence[str]) -> bool:
    return any(_CAUSED_BY_RE.match(ln) or _CAUSED_BY_INLINE.search(ln)
               for ln in lines or [])


def deepest_caused_by(stack: Sequence[str]) -> str:
    """从堆栈里取**最内层**（最后一条）Caused by 的异常文本。

    这才是真正的根因。Java 语义下：

        java.lang.RuntimeException: query failed      <- 外层：症状
        Caused by: java.sql.SQLTimeoutException: ... <- 内层：因
        Caused by: java.lang.OutOfMemoryError: ...  <- 最内层：根因

    同时认两种形态：行首 `Caused by:`（多行堆栈）与行内 `... Caused by: X`
    （单行异常，见 `_CAUSED_BY_INLINE` 的说明）。

    历史实现把「时间上紧邻在它之前的另一个错误簇」当成因，那是**时间相邻性**
    —— 统计猜测，不是因果证明。现已改为只认本函数给出的链内证据。
    """
    deepest = ""
    for line in stack or []:
        m = _CAUSED_BY_TEXT.match(line)
        if m:
            deepest = m.group(1).strip()
            continue
        mi = _CAUSED_BY_INLINE.search(line)
        if mi:
            deepest = mi.group("sig").strip()
    return deepest
