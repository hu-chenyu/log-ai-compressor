# -*- coding: utf-8 -*-
"""Android logcat 格式支持：行头解析 + 单行内嵌因果链。

**为什么要单独立一组测试**
--------------------------
Android logcat 把整个异常压在一行里：

    12-17 19:42:44.457  3168  3307 E PushErr: com.x.PushException: boom  Caused by: java.net.SocketException: abort

此前有两处让它完全失效，导致实测 **Android_v1 上 131 条真实链的召回率是 0%**
（同一份代码在 Hadoop 上是 91.4%）：

1. generic 规则没有 logcat 模式 —— 无年份、级别是单字母、还夹着 PID/TID，
   整行落进无结构兜底，时间戳/模块全丢、级别靠关键词猜
2. ``Caused by`` 的两条正则都锚在行首 ``^``，单行形态整条链看不见，
   工具只能判 INSUFFICIENT

修完之后必须守住两件事：**能认**，以及**不乱认**。后者靠下面那条
「caused by: user input 不得触发 CONFIRMED」——收紧点就是行内 ``Caused by:``
之后必须紧跟一个异常类名。
"""
from __future__ import annotations

import pytest

from log_ai_compressor import service
from log_ai_compressor.core.analysis import CONF_CONFIRMED, deepest_caused_by

_HEADER = "12-17 19:42:44.457  3168  3307"


def _write(tmp_path, lines):
    p = tmp_path / "logcat.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _analyze(path, **kw):
    # 默认带上 DEBUG：logcat 的 D 级是 DEBUG，若只放行 INFO/WARN/ERROR，
    # DEBUG 行会在聚类前被级别过滤掉 —— 那是过滤器在正常工作，不是解析失败。
    params = {"levels": ["DEBUG", "INFO", "WARN", "ERROR"],
              "top_n": 0, "context_lines": 2}
    params.update(kw)
    return service.analyze(paths=[str(path)], params=params)


# ---------------------------------------------------------------------------
# 1. 行头解析：时间戳 / 级别 / 模块
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "letter,expect",
    [("E", "ERROR"), ("W", "WARN"), ("I", "INFO"), ("D", "DEBUG")],
)
def test_logcat_single_letter_level_maps(tmp_path, letter, expect):
    """单字母级别必须映射正确，不能靠关键词猜。"""
    p = _write(tmp_path, [
        f"{_HEADER} {letter} MyTag: something happened here",
        f"{_HEADER} {letter} MyTag: another line",
    ])
    r = _analyze(p)
    levels = {c.level for c in r.clusters}
    assert expect in levels, f"级别 {letter} 应映射为 {expect}，实得 {levels}"


def test_logcat_tag_becomes_module(tmp_path):
    """Tag 应当进 module 字段，而不是留在消息里当噪声。"""
    p = _write(tmp_path, [
        f"{_HEADER} E PowerManager_screenOn: display fade level=1.0",
        f"{_HEADER} E PowerManager_screenOn: display fade level=1.0",
    ])
    r = _analyze(p)
    mods = {c.module for c in r.clusters if c.module}
    assert "PowerManager_screenOn" in mods, f"Tag 未进 module，实得 {mods}"


def test_logcat_timestamp_is_parsed_not_left_in_message(tmp_path):
    """时间戳必须被剥掉 —— 留在消息里会变成指纹噪声，每行都不同。"""
    p = _write(tmp_path, [f"{_HEADER} E Tag: repeated message"])
    r = _analyze(p)
    assert r.clusters, "应至少产生一个簇"
    assert not any("19:42:44" in (c.summary or "") for c in r.clusters), \
        "时间戳残留在消息里，会让同事件无法并簇"


def test_logcat_tag_with_trailing_spaces(tmp_path):
    """`TCP     : msg` 这种 Tag 后带空格再跟冒号的形态也要能解析。"""
    p = _write(tmp_path, [f"{_HEADER} W TCP     : socket closed by peer"])
    r = _analyze(p)
    assert r.clusters
    assert "TCP" in {c.module for c in r.clusters}


# ---------------------------------------------------------------------------
# 2. 单行内嵌因果链 —— 本组测试的核心
# ---------------------------------------------------------------------------
def test_inline_caused_by_yields_confirmed(tmp_path):
    p = _write(tmp_path, [
        f"{_HEADER} E PushErr: com.x.PushException: connect failed "
        f"Caused by: java.net.SocketException: connection abort",
    ])
    r = _analyze(p)
    confirmed = [c for c in r.clusters if c.root_cause_confidence == CONF_CONFIRMED]
    assert confirmed, "单行内嵌 Caused by 必须产生 CONFIRMED"
    assert "SocketException" in (confirmed[0].root_cause_reason or ""), \
        f"根因应指向最内层异常，实得 {confirmed[0].root_cause_reason!r}"


def test_inline_caused_by_deepest_wins(tmp_path):
    """行内出现多次 Caused by 时，取最内层（最后一条）。"""
    line = (f"{_HEADER} E Tag: com.x.OuterException: a "
            f"Caused by: java.io.IOException: b "
            f"Caused by: java.lang.OutOfMemoryError: c")
    r = _analyze(_write(tmp_path, [line]))
    confirmed = [c for c in r.clusters if c.root_cause_confidence == CONF_CONFIRMED]
    assert confirmed
    assert "OutOfMemoryError" in confirmed[0].root_cause_reason


def test_plain_caused_by_without_exception_class_is_not_confirmed(tmp_path):
    """**收紧点**：行内 `caused by:` 后面不是异常类名时，不得触发 CONFIRMED。

    没有这条，一个只是抱怨"caused by: user input"的普通错误会被抬成确定性
    根因 —— 那正是本项目最不能犯的错。
    """
    p = _write(tmp_path, [
        f"{_HEADER} E Tag: request rejected, caused by: user input was invalid",
    ])
    r = _analyze(p)
    assert not [c for c in r.clusters
                if c.root_cause_confidence == CONF_CONFIRMED], \
        "非异常类名的行内 caused by 不得触发 CONFIRMED"


def test_multiline_caused_by_still_works(tmp_path):
    """多行堆栈形态不能因为加了行内识别而退化。"""
    p = _write(tmp_path, [
        "2015-10-19 17:41:30,296 ERROR [Task] com.x.OuterException: query failed",
        "\tat com.x.Query.run(Query.java:42)",
        "Caused by: java.sql.SQLTimeoutException: timeout after 30s",
        "\tat java.sql.PreparedStatement.executeQuery(PreparedStatement.java:96)",
        "\t... 9 more",
    ])
    r = _analyze(p)
    confirmed = [c for c in r.clusters if c.root_cause_confidence == CONF_CONFIRMED]
    assert confirmed, "多行 Caused by 仍须产生 CONFIRMED"
    assert "SQLTimeoutException" in confirmed[0].root_cause_reason


# ---------------------------------------------------------------------------
# 3. deepest_caused_by 的两种形态
# ---------------------------------------------------------------------------
def test_deepest_caused_by_handles_both_shapes():
    assert deepest_caused_by(["Caused by: java.io.IOException: boom"]) == \
        "java.io.IOException: boom"
    assert deepest_caused_by(
        ["12-17 19:42:44.457 E Tag: Caused by: java.net.SocketException: abort"]
    ) == "java.net.SocketException: abort"
    assert deepest_caused_by(["nothing here"]) == ""
    assert deepest_caused_by([]) == ""


def test_no_causal_chain_stays_insufficient(tmp_path):
    """无任何 Caused by 时必须继续弃权 —— 不能因为支持了新格式就放松。"""
    p = _write(tmp_path, [f"{_HEADER} E Tag: NullPointerException: plain error"])
    r = _analyze(p)
    assert not [c for c in r.clusters
                if c.root_cause_confidence == CONF_CONFIRMED]
