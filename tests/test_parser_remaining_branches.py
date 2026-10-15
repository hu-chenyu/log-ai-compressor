# -*- coding: utf-8 -*-
"""解析器里「测试从没走到过」的几条分支 —— 逐条用真实输入打通。

背景：v2.6.0 交付时 `core/parser.py` 停在 92%，16 行未覆盖。当时的结论是
「探查后确认不可达」，于是如实记录在模块 docstring 里。v2.7.0 重新逐条
验证了一遍，结论**部分被推翻**：其中 15 行其实是可达的，只是没人喂对输入。

这里的方法论值得留着：**「覆盖率没到」不等于「代码不可达」**。先把探针写
出来问「什么样的输入才会走到这里」，再判断是代码死了还是用例没写。
本文件里每一个输入都是先探针验证、再落成断言的，不是照着分支写的假用例。

唯一确认不可达的是 `_to_epoch` 里的 `except OSError/OverflowError`
（1970 前的 naive 时间在入口就被 `replace(year=2000)` 归一化了），
跨平台兜底价值大于覆盖率数字，保留并继续记录在模块 docstring。
"""
from __future__ import annotations

import math

from log_ai_compressor.core.parser import (
    LogParser,
    TimestampParser,
    _to_epoch,
)
from log_ai_compressor.rules.engine import load_ruleset


def _parser() -> LogParser:
    return LogParser(load_ruleset("generic"))


def _first(parser: LogParser, line: str):
    parser.feed(line, 1)
    return parser._current


# ---------------------------------------------------------------------------
# 一、超长纯数字时间戳：这不是理论风险，是静默污染
# ---------------------------------------------------------------------------


def test_absurd_numeric_timestamp_is_rejected_not_inf() -> None:
    """400 位纯数字时间戳必须判 None，不能放 inf 进来。

    `_TS_NUMERIC` 是 `^\\d+(?:\\.\\d+)?$`，`float()` 对这种串**从不抛异常** ——
    309 位以上它静默返回 inf。原先那圈 `try/except ValueError` 拦不住它
    （压根不抛），于是 inf 会顺着 LogEntry.timestamp 进入 TimeHistogram、
    突发窗口与时间范围：一条畸形时间戳能把整份报告的时间轴变成 NaN。
    """
    tp = TimestampParser()
    assert tp.parse("9" * 400) is None, "超长数字串被当成了合法 epoch"
    # 对照：正常值仍要照常解析（别把这条改成「数字一律拒绝」）
    assert tp.parse("1700000000") == 1700000000.0
    assert tp.parse("1700000000.5") == 1700000000.5


def test_long_numeric_timestamp_does_not_poison_the_histogram() -> None:
    """端到端再验一次：畸形时间戳不得影响其余行的正常时间轴。

    注意 `feed()` 返回的是**上一个**条目（首行必为 None），所以要靠
    flush() 拿最后一条 —— 这一点本文件第一版就写错过。
    """
    p = _parser()
    p.feed("2024-01-15 10:00:00 ERROR first", 1)
    first = p.feed("9" * 400 + " ERROR second", 2)
    fin = p.flush()
    assert first is not None and fin is not None
    assert first.timestamp is not None and math.isfinite(first.timestamp)
    assert fin.timestamp is None, \
        f"400 位数字串竟解析出了时间戳：{fin.timestamp}"
    ts = [e.timestamp for e in (first, fin) if e.timestamp is not None]
    assert all(math.isfinite(t) for t in ts), \
        f"时间戳里混进了非有限值：{ts}"


# ---------------------------------------------------------------------------
# 二、结构化条目的消息头剥离（模块前缀 / 级别前缀）
# ---------------------------------------------------------------------------


def test_head_token_becomes_module_when_rule_has_none() -> None:
    """规则没抓到模块时，消息头的 `token - ` 前缀应被剥成模块。

    探针来源：`iso_level_module` 的 module 组是可选的，写
    `ERROR auth - login failed` 时模块组为空、消息体是 `auth - login failed`，
    于是走头部剥离。此前这条分支一次没被走到。
    """
    e = _first(_parser(), "2024-01-15 10:00:00 ERROR auth - login failed")
    assert (e.module, e.message) == ("auth", "login failed"), \
        (e.module, e.message)


def test_head_token_pipe_separator_also_works() -> None:
    """分隔符除了 `-` 还有 `|` 与 `>`，三种都要剥。"""
    e = _first(_parser(), "2024-01-15 10:00:00 ERROR net | socket reset")
    assert (e.module, e.message) == ("net", "socket reset"), \
        (e.module, e.message)


def test_head_level_token_promotes_level_when_rule_has_none() -> None:
    """级别 token 前缀应被剥成级别 —— 用 DEBUG 而不是 WARN 做判据。

    为什么不用 WARN：`infer_level_by_keyword` 本来就会扫原文里的
    ERROR/FAIL/WARN 关键词，用 WARN 做断言，分支跑没跑都绿，等于没测。
    DEBUG 只在头部剥离路径里才可能出现，断言才有区分力。
    """
    e = _first(_parser(), "20240115-10:00:00|svc|DEBUG - verbose trace")
    assert e.level == "DEBUG", f"级别前缀没被剥成级别，实得 {e.level}"
    assert e.message == "verbose trace", e.message


def test_head_token_does_not_override_explicit_module() -> None:
    """规则抓到模块时，头部 token 不得覆盖它（模块名绝不能猜错）。"""
    e = _first(_parser(), "20240115-10:00:00|svc|auth - login failed")
    assert e.module == "svc", f"头部 token 覆盖了规则给出的模块：{e.module}"


# ---------------------------------------------------------------------------
# 三、未缩进堆栈兜底（分支 E）
# ---------------------------------------------------------------------------


def test_unindented_hex_pointer_line_is_appended_as_stack() -> None:
    """裸指针行（`0x... <`）没有缩进也没命中规则，仍应并进堆栈。

    其它堆栈特征（`at ` / `Caused by` / `File ` / `raise` / `Traceback` /
    `Backtrace` / `terminate`）都在 `_FLUSH_STACK_PREFIXES` 里被前置分流，
    异常摘要行也被 `_EXCEPTION_LINE_RE` 接走 —— 只剩十六进制指针这一类
    能落到兜底分支。它在真实日志里是 native crash 的标配，不该被当成
    独立条目。
    """
    p = _parser()
    p.feed("2024-01-15 10:00:00 ERROR outer boom", 1)
    assert p.feed("0x00007f8e4c0a1234 <memory>", 2) is None, \
        "裸指针行被当成新条目开出去了"
    e = p.flush()
    assert e.stack == ["0x00007f8e4c0a1234 <memory>"], e.stack


# ---------------------------------------------------------------------------
# 四、剥时区后 ISO 仍失败 -> 交给快速路径
# ---------------------------------------------------------------------------


def test_z_suffixed_pure_time_falls_through_to_fast_path() -> None:
    """`10:30:00 Z` 剥掉 Z 后 fromisoformat 仍失败，必须落到快速路径。

    这条分支的语义是「紧凑时区后缀剥完，剩下的是 ISO 就用 ISO，不是就
    交给第 4/5 步」。纯时间（无日期）正是这种形状：补上 `+00:00` 后
    `fromisoformat("10:30:00+00:00")` 依然不认，于是必须继续往下走，
    而不是返回 None 把这条时间戳丢掉。
    """
    tp = TimestampParser()
    got = tp.parse("10:30:00 Z")
    assert got is not None, "剥掉 Z 之后被整条丢掉了"
    assert math.isfinite(got)


# ---------------------------------------------------------------------------
# 五、结构化日志（JSON / logfmt）的头剥离 —— 与规则路径是两条独立分支
# ---------------------------------------------------------------------------


def test_structured_head_token_becomes_module_when_service_absent() -> None:
    """JSON 行没有 service 字段时，消息头 `token - ` 应剥成模块。"""
    import json as _json
    line = _json.dumps({"timestamp": "2024-01-15T10:00:00Z",
                        "message": "auth - login failed"})
    e = _first(_parser(), line)
    assert (e.module, e.message) == ("auth", "login failed"), \
        (e.module, e.message)


def test_structured_head_level_token_promotes_level() -> None:
    """JSON 行没有 level 字段时，头部级别 token 应被剥成级别。

    同样用 DEBUG 做判据：`infer_level_by_keyword` 只认 ERROR/FAIL/WARN，
    走不到这条分支的话级别会停在默认的 INFO，断言因此有区分力。
    """
    import json as _json
    line = _json.dumps({"timestamp": "2024-01-15T10:00:00Z",
                        "message": "DEBUG - verbose trace"})
    e = _first(_parser(), line)
    assert e.level == "DEBUG", f"头部级别前缀没被剥成级别，实得 {e.level}"
    assert e.message == "verbose trace", e.message


def test_structured_explicit_service_is_never_overridden() -> None:
    """有 service 字段时，消息头 token 不得覆盖它、也不得剥离消息。

    这条是本分支最容易写坏的一处：条件是 `if not module or not parsed.level`
    —— 级别缺失时**仍会进来**，若不看 `elif not module` 就把模块猜成
    "auth"，结构化日志里本来就有的 service 字段就被丢了。
    """
    import json as _json
    line = _json.dumps({"timestamp": "2024-01-15T10:00:00Z",
                        "service": "svc", "message": "auth - login failed"})
    e = _first(_parser(), line)
    assert e.module == "svc", f"结构化 service 被头部 token 覆盖：{e.module}"
    assert e.message == "auth - login failed", \
        f"service 存在时不应剥离消息，实得 {e.message!r}"


def test_logfmt_head_token_becomes_module() -> None:
    """logfmt 走同一条分支，行为必须一致（不能只有 JSON 能剥）。"""
    e = _first(_parser(), 'level=INFO msg="auth - login failed"')
    assert (e.module, e.message) == ("auth", "login failed"), \
        (e.module, e.message)


# ---------------------------------------------------------------------------
# 六、确认仍不可达的那条：保留跨平台兜底，不为了覆盖率删掉
# ---------------------------------------------------------------------------


def test_pre_epoch_datetime_is_normalised_to_a_safe_year() -> None:
    """1970 前的时间被统一换成 2000 年基准（`_to_epoch` 入口归一化）。

    这正是 `_to_epoch` 里那圈 `except OSError/OverflowError` 永远走不到的
    原因：入口处 `year < 1970` 已经全部改写过了。保留该 except 是为了
    跨平台安全（Windows 上 1970 前的 naive 时间 `timestamp()` 会抛），
    **不**为了覆盖率去删 —— 删了换一个更隐蔽的崩溃点。
    """
    assert math.isfinite(_to_epoch(__import__("datetime").datetime(1900, 1, 1)))
    assert math.isfinite(TimestampParser().parse("Jan  1 00:00:00"))
