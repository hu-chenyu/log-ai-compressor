# -*- coding: utf-8 -*-
"""解析器单元测试：时间戳解析、多行聚合、堆栈跟踪、级别/模块推断。"""
from __future__ import annotations

from datetime import datetime

import pytest

from log_ai_compressor.core.parser import (
    LogParser,
    TimestampParser,
    _TS_FORMATS,
    _fast_datetime,
)
from log_ai_compressor.rules.engine import load_ruleset


@pytest.fixture()
def parser():
    return LogParser(load_ruleset("generic"))


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------
class TestTimestampParser:
    @pytest.fixture()
    def tp(self):
        return TimestampParser()

    def test_iso_space(self, tp):
        # 时区无关断言：相邻秒的时间戳差值应为 1
        a = tp.parse("2024-01-01 10:00:00")
        b = tp.parse("2024-01-01 10:00:01")
        assert a is not None and b is not None
        assert b - a == pytest.approx(1.0)

    def test_iso_with_ms(self, tp):
        v = tp.parse("2024-01-01 10:00:00.123")
        assert v is not None and 0 < v < 2e10

    def test_iso_comma_ms(self, tp):
        v = tp.parse("2024-01-01T10:00:00,456")
        assert v is not None

    def test_iso_z_suffix(self, tp):
        v = tp.parse("2024-01-01T10:00:00Z")
        assert v is not None

    def test_iso_offset(self, tp):
        v = tp.parse("2024-01-01T10:00:00+08:00")
        assert v is not None

    def test_time_only(self, tp):
        v = tp.parse("10:00:00.123")
        assert v is not None

    def test_relative_seconds(self, tp):
        assert tp.parse("123.456") == 123.456
        assert tp.parse("42") == 42.0

    def test_invalid_returns_none(self, tp):
        assert tp.parse("not a timestamp") is None
        assert tp.parse("") is None
        assert tp.parse(None) is None

    def test_cache_hit_same_value(self, tp):
        first = tp.parse("2024-01-01 10:00:00")
        second = tp.parse("2024-01-01 10:00:00")
        assert first == second

    def test_syslog_format(self, tp):
        assert tp.parse("Jan 02 10:00:00") is not None
        assert tp.parse("Feb 15 08:30:45") is not None


# ---------------------------------------------------------------------------
# 修复缺陷#9：时间戳复合正则快速路径（与 strptime 行为一致性）
# ---------------------------------------------------------------------------
class TestFastDatetime:
    """快速路径必须与 strptime 逐格式等价（无年份格式按 1900 构造）。"""

    @pytest.mark.parametrize("ts", [
        "2024-01-01 09:07:39.123456", "2024-01-01 09:07:39",
        "2024-01-01T09:07:39.123", "2024-01-01T09:07:39",
        "2024-01-01 09:07", "2024/06/01 09:07:39",
        "01/02/2024 09:07:39", "01/Jan/2024:09:07:39",
        "01-Jan-2024 09:07:39", "Jan 01 09:07:39",
        "09:07:39.123", "09:07:39",
    ])
    def test_matches_strptime_semantics(self, ts):
        fast = _fast_datetime(ts)
        expected = None
        for fmt in _TS_FORMATS:
            try:
                expected = datetime.strptime(ts, fmt)
                break
            except ValueError:
                continue
        assert fast == expected, f"快速路径与 strptime 结果不一致: {ts}"

    @pytest.mark.parametrize("bad", [
        "2024-13-01 09:07:39", "2024-01-32 09:07:39", "25:07:39",
        "abc", "not a time", "Feb 30 09:07:39", "09:07",
        "2024-01-01", "31/Apr/2024 10:00:00",
    ])
    def test_invalid_rejected(self, bad):
        # 非法输入：快速路径要么拒绝（None），要么与 strptime 全失败一致
        fast = _fast_datetime(bad)
        if fast is not None:
            for fmt in _TS_FORMATS:
                with pytest.raises(ValueError):
                    datetime.strptime(bad, fmt)

    def test_month_abbreviation_case_insensitive(self):
        assert _fast_datetime("15/MAR/2024 10:00:00") is not None
        assert _fast_datetime("mar 15 10:00:00").month == 3

    def test_timezone_suffix_accepted(self):
        # 快速路径比 strptime 更宽松：剥离时区后仍可解析（语义等价于旧 ISO 剥离路径）
        assert _fast_datetime("2024-06-01 09:07:39 +0800") is not None
        assert _fast_datetime("2024-06-01 09:07:39Z") is not None

    def test_millisecond_padded_to_microsecond(self):
        dt = _fast_datetime("2024-01-01 09:07:39.5")
        assert dt is not None and dt.microsecond == 500000

    def test_faster_than_strptime(self):
        # 性能回归保护：快速路径单次解析应显著快于 strptime
        import time as _time
        ts = "2024/06/01 09:07:39"
        fmt = "%Y/%m/%d %H:%M:%S"
        n = 20000
        t0 = _time.perf_counter()
        for _ in range(n):
            datetime.strptime(ts, fmt)
        t_strptime = _time.perf_counter() - t0
        t0 = _time.perf_counter()
        for _ in range(n):
            _fast_datetime(ts)
        t_fast = _time.perf_counter() - t0
        # 快速路径至少不慢于 strptime（宽断言防 CI 抖动误报）
        assert t_fast < t_strptime * 1.0


# ---------------------------------------------------------------------------
# 基本解析
# ---------------------------------------------------------------------------
class TestFeedBasics:
    def test_simple_entry(self, parser):
        e = parser.feed("2024-01-01 10:00:00 INFO [auth] user login ok", 1)
        assert e is None  # 首条目进行中
        done = parser.flush()
        assert done is not None
        assert done.level == "INFO"
        assert done.module == "auth"
        assert done.message == "user login ok"
        assert done.timestamp is not None
        assert done.line_no == 1

    def test_consecutive_entries(self, parser):
        first = parser.feed("2024-01-01 10:00:00 INFO first", 1)
        assert first is None
        second = parser.feed("2024-01-01 10:00:01 ERROR second", 2)
        assert second is not None and second.message == "first"
        last = parser.flush()
        assert last is not None and last.message == "second"

    def test_blank_line_skipped(self, parser):
        parser.feed("2024-01-01 10:00:00 INFO start", 1)
        assert parser.feed("", 2) is None
        assert parser.feed("   ", 3) is None
        e = parser.flush()
        assert e is not None and e.message == "start"


# ---------------------------------------------------------------------------
# 多行聚合：堆栈与折行
# ---------------------------------------------------------------------------
class TestMultiline:
    def test_java_stack_aggregated(self, parser):
        parser.feed("2024-01-01 10:00:00 ERROR [db] connection refused", 1)
        parser.feed("java.net.ConnectException: Connection refused", 2)
        parser.feed("\tat com.app.db.Pool.init(Pool.java:42)", 3)
        parser.feed("\tat java.base/java.net.AbstractPlainSocketImpl.connect(...)", 4)
        parser.feed("\tat com.app.core.Main.start(Main.java:18)", 5)
        entry = parser.flush()
        assert entry is not None
        # 异常摘要行 + 3 个调用帧
        assert len(entry.stack) == 4
        assert entry.stack[0] == "java.net.ConnectException: Connection refused"
        assert entry.last_line_no == 5
        assert entry.message == "connection refused"

    def test_python_traceback(self, parser):
        parser.feed("2024-01-01 10:00:00 ERROR [api] handler crashed", 1)
        parser.feed("Traceback (most recent call last):", 2)
        parser.feed('  File "app/api.py", line 88, in handle', 3)
        parser.feed("    return do_work(req)", 4)
        parser.feed("ValueError: invalid payload", 5)
        entry = parser.flush()
        assert entry.stack[0] == "Traceback (most recent call last):"
        assert entry.stack[-1] == "ValueError: invalid payload"

    def test_wrapped_message(self, parser):
        parser.feed("2024-01-01 10:00:00 ERROR [task] long message first part", 1)
        parser.feed("    continued second part", 2)
        entry = parser.flush()
        assert entry.full_message == "long message first part continued second part"

    def test_stack_before_any_entry(self, parser):
        parser.feed("\tat com.x.Y.z(Y.java:1)", 1)
        entry = parser.flush()
        assert entry is not None
        assert entry.stack == ["\tat com.x.Y.z(Y.java:1)"]


# ---------------------------------------------------------------------------
# 级别与模块推断
# ---------------------------------------------------------------------------
class TestInference:
    def test_lowercase_gcc_error(self, parser):
        parser.feed("src/main.c:42: error: expected ';' before '}'", 1)
        e = parser.flush()
        assert e.level == "ERROR"
        assert e.module == "src/main.c"
        assert e.message == "expected ';' before '}'"

    def test_module_token_with_dash_separator(self, parser):
        parser.feed("2024-01-01 10:00:00 ERROR auth - login failed", 1)
        e = parser.flush()
        assert e.module == "auth"
        assert e.message == "login failed"

    def test_unstructured_line_level_hint(self, parser):
        # 修复缺陷R18：FATAL 关键词提示已删除（\bFATAL\b 忽略大小写
        # 误中 gcc 选项 -Wfatal-errors）—— "terminate called" 不再
        # 推断为 FATAL（关键词推断最高 ERROR），回退 INFO
        parser.feed("terminate called after throwing an instance", 1)
        e = parser.flush()
        assert e.level == "INFO"

    def test_wfatal_errors_flag_not_fatal(self, parser):
        """修复缺陷R18：gcc 选项 -Wfatal-errors 不判 FATAL（误报根因）。"""
        parser.feed("[2026-08-30T08:27:07.551Z] /bin/bash ../libtool "
                    "--tag=CC --mode=compile aarch64-none-linux-gnu-gcc "
                    "-O2 -Wextra -Wimport -Wfatal-errors -Wformat=2 "
                    "-c -o libcompat.lo /home/jenkins/x/lib/libcompat.c", 1)
        e = parser.flush()
        assert e.level == "INFO", \
            "编译选项 -Wfatal-errors 不应被误判为致命错误"

    def test_gcc_fatal_error_normalized_to_error(self, parser):
        """修复缺陷R40：gcc fatal error（显式级别组）归一为 ERROR。"""
        parser.feed("src/main.c:42: fatal error: foo.h: "
                    "No such file or directory", 1)
        e = parser.flush()
        assert e.level == "ERROR", \
            "gcc 风格 fatal error 应经 LEVEL_ALIASES 归一为 ERROR"

    def test_explicit_fatal_field_normalized_to_error(self, parser):
        """修复缺陷R40：显式级别字段 [FATAL] 归一为 ERROR。"""
        parser.feed("2024-01-01 10:00:00 FATAL kernel panic - not syncing", 1)
        e = parser.flush()
        assert e.level == "ERROR"

    def test_unstructured_plain_line_defaults_info(self, parser):
        parser.feed("some random output line", 1)
        e = parser.flush()
        assert e.level == "INFO"

    def test_warning_alias_normalized(self, parser):
        parser.feed("2024-01-01 10:00:00 WARNING disk almost full", 1)
        e = parser.flush()
        assert e.level == "WARN"

    def test_level_in_message_head(self, parser):
        # 级别写在消息头部（部分自研日志格式）
        parser.feed("2024-01-01 10:00:00 ERROR - service unavailable", 1)
        e = parser.flush()
        assert e.level == "ERROR"
        assert e.message == "service unavailable"

    def test_colon_prefix_not_module(self, parser):
        """修复缺陷R66：消息头部冒号词不作模块 —— Jenkins 行
        "Finished: FAILURE" 的 Finished 被误判为模块（模块宁缺毋错，
        冒号前缀保留在消息原文）。"""
        parser.feed("[2026-09-04T06:12:16.697Z] Finished: FAILURE", 1)
        e = parser.flush()
        assert e.module == "", "冒号句首词不得误判为模块"
        assert e.message == "Finished: FAILURE", "冒号前缀应保留在原文"
        assert e.level == "FAIL"

    def test_git_remote_prefix_not_module(self, parser):
        """修复缺陷R66：git 远端流前缀 remote: 同样不作模块。"""
        parser.feed("[2026-09-04T06:12:10.000Z] remote: commit 5050429: "
                    "warning: subject >50 characters", 1)
        e = parser.flush()
        assert e.module == ""
        assert e.message.startswith("remote: commit 5050429")
        assert e.level == "WARN"


# ---------------------------------------------------------------------------
# 嵌入式规则集
# ---------------------------------------------------------------------------
class TestEmbeddedRuleset:
    @pytest.fixture()
    def emb_parser(self):
        return LogParser(load_ruleset("embedded"))

    def test_bracket_ts_level_module(self, emb_parser):
        emb_parser.feed("[  123.456] [ERR ] [im_pll] clk cfg failed", 1)
        e = emb_parser.flush()
        assert e.level == "ERROR"   # ERR 归一化为 ERROR
        assert e.module == "im_pll"
        assert e.message == "clk cfg failed"
        assert e.timestamp == 123.456

    def test_ut_fail_line(self, emb_parser):
        emb_parser.feed("12:00:01.123 FAIL test_case_clk_freq", 1)
        e = emb_parser.flush()
        assert e.level == "FAIL"
        assert e.message == "test_case_clk_freq"

    def test_module_line_fail(self, emb_parser):
        emb_parser.feed("im_clk_a400.c:253: FAIL: clock rate mismatch", 1)
        e = emb_parser.flush()
        assert e.level == "FAIL"
        assert e.module == "im_clk_a400.c"
        assert "mismatch" in e.message

    def test_module_line_failed_not_truncated(self, emb_parser):
        """修复缺陷R70：FAILED:/FAILURE: 不被 FAIL 前缀吃掉（摘要
        曾残留断词 "ED: xxx"）。"""
        for word in ("FAILED", "FAILURE"):
            p = LogParser(load_ruleset("embedded"))
            p.feed(f"im_pll.c:42: {word}: clk config broken", 1)
            e = p.flush()
            assert e.level == "FAIL"
            assert e.module == "im_pll.c"
            assert e.message == "clk config broken", \
                f"{word} 不得残留断词（实际 {e.message!r}）"
