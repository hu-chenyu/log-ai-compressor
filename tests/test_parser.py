# -*- coding: utf-8 -*-
"""解析器单元测试：时间戳解析、多行聚合、堆栈跟踪、级别/模块推断。"""
from __future__ import annotations

import pytest

from log_ai_compressor.core.parser import LogParser, TimestampParser
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
        parser.feed("terminate called after throwing an instance", 1)
        e = parser.flush()
        assert e.level == "FATAL"

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
