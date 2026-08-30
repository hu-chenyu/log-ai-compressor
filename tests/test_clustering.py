# -*- coding: utf-8 -*-
"""过滤与聚类单元测试。"""
from __future__ import annotations

from log_ai_compressor.core.clustering import (
    ErrorClusterer,
    fingerprint,
    mask_text,
    similarity,
)
from log_ai_compressor.core.filters import EntryFilter, FilterConfig
from log_ai_compressor.core.models import LogEntry


def entry(msg, level="ERROR", module="db", ts=None, line=1, stack=None):
    return LogEntry(line_no=line, raw=msg, level=level, module=module,
                    message=msg, timestamp=ts, stack=stack or [],
                    last_line_no=line)


# ---------------------------------------------------------------------------
# 过滤器
# ---------------------------------------------------------------------------
class TestFilter:
    def test_default_levels_error_fail(self):
        f = EntryFilter(FilterConfig(levels=["ERROR", "FAIL"]))
        assert f.match(entry("x", level="ERROR"))
        assert f.match(entry("x", level="FAIL"))
        assert not f.match(entry("x", level="WARN"))
        assert not f.match(entry("x", level="INFO"))

    def test_fatal_always_passes_level_gate(self):
        # 致命错误必须始终进入结果（自动前置要求）
        f = EntryFilter(FilterConfig(levels=["FAIL"]))
        assert f.match(entry("x", level="FATAL"))
        # 但 include/exclude 关键字语义照常生效
        f2 = EntryFilter(FilterConfig(levels=["FAIL"], include=["timeout"]))
        assert not f2.match(entry("out of memory", level="FATAL"))

    def test_include_keyword(self):
        f = EntryFilter(FilterConfig(levels=["ERROR"], include=["timeout"]))
        assert f.match(entry("request timeout after 30s"))
        assert not f.match(entry("disk corruption detected"))

    def test_exclude_keyword(self):
        f = EntryFilter(FilterConfig(levels=["ERROR"], exclude=["healthcheck"]))
        assert not f.match(entry("GET /healthcheck failed"))
        assert f.match(entry("GET /api/users failed"))

    def test_include_searches_stack_and_module(self):
        f = EntryFilter(FilterConfig(levels=["ERROR"], include=["Pool.init"]))
        e = entry("connection failed", module="db",
                  stack=["java.net.ConnectException: x",
                         "\tat com.app.db.Pool.init(Pool.java:42)"])
        assert f.match(e)

    def test_include_case_insensitive(self):
        f = EntryFilter(FilterConfig(levels=["ERROR"], include=["TIMEOUT"]))
        assert f.match(entry("Request Timeout"))

    def test_config_roundtrip(self):
        cfg = FilterConfig(levels=["ERROR"], include=["a", "b"],
                           exclude=["c"], top_n=5, context_lines=3)
        d = cfg.as_dict()
        cfg2 = FilterConfig.from_dict(d)
        assert cfg2.as_dict() == d

    def test_config_from_dict_invalid_tolerant(self):
        cfg = FilterConfig.from_dict({"levels": "bad", "top_n": "x"})
        assert cfg.levels == ["ERROR", "FAIL"]
        assert cfg.top_n == 20


# ---------------------------------------------------------------------------
# 指纹归一化
# ---------------------------------------------------------------------------
class TestMask:
    def test_numbers_masked(self):
        assert mask_text("retry 3 of 5 failed") == "retry N of N failed"

    def test_hex_masked(self):
        assert mask_text("addr 0x4a1b2c error") == "addr 0xH error"

    def test_hex_session_id_masked(self):
        # 无 0x 前缀的十六进制令牌（会话ID/请求ID）也需掩码
        assert mask_text("token expired for session 9a1b2c") == \
            "token expired for session H"
        assert mask_text("req 6b5a4f failed") == "req H failed"

    def test_pure_alpha_word_not_masked(self):
        # 纯字母十六进制字符组合（英文单词）不误伤
        assert mask_text("beaded decoration failed") == "beaded decoration failed"

    def test_uuid_masked(self):
        assert mask_text("req 550e8400-e29b-41d4-a716-446655440000 failed") == \
            "req U failed"

    def test_quoted_masked(self):
        assert mask_text("cannot open 'file1.txt'") == "cannot open S"

    def test_path_masked(self):
        masked = mask_text("failed at src/db/pool.c:120")
        assert "src/db/pool.c" not in masked
        assert "P" in masked

    def test_ws_collapsed(self):
        assert mask_text("a   b\t c") == "a b c"


class TestFingerprint:
    def test_line_number_diff_ignored(self):
        a = entry("main.c:120 assert failed")
        b = entry("main.c:999 assert failed")
        assert fingerprint(a) == fingerprint(b)

    def test_different_messages_differ(self):
        assert fingerprint(entry("timeout")) != fingerprint(entry("disk full"))

    def test_stack_included(self):
        e = entry("x", stack=["at a.B.c(B.java:1)", "at a.B.d(B.java:2)",
                              "at a.B.e(B.java:3)", "at a.B.f(B.java:4)"])
        fp = fingerprint(e)
        assert "B.f" not in fp   # 第 4 帧不参与指纹（仅前 3 行）

    def test_similarity(self):
        assert similarity("abcd", "abcd") == 1.0
        assert similarity("", "") == 1.0
        assert similarity("abc", "xyz") < 0.5


# ---------------------------------------------------------------------------
# 聚类器
# ---------------------------------------------------------------------------
class TestClusterer:
    def test_identical_errors_merge(self):
        c = ErrorClusterer()
        for i in range(10):
            c.add(entry("Connection refused", line=i + 1, ts=100.0 + i))
        assert len(c) == 1
        assert c.clusters[0].count == 10

    def test_param_diffs_merge(self):
        c = ErrorClusterer()
        c.add(entry("request 12345 to 10.0.0.7 failed"))
        c.add(entry("request 98765 to 10.0.0.9 failed"))
        assert len(c) == 1

    def test_line_no_in_message_diffs_merge(self):
        c = ErrorClusterer()
        c.add(entry("im_pll.c:120 clk cfg failed"))
        c.add(entry("im_pll.c:666 clk cfg failed"))
        assert len(c) == 1

    def test_different_errors_separate(self):
        c = ErrorClusterer()
        c.add(entry("Connection refused"))
        c.add(entry("disk full on /var"))
        assert len(c) == 2

    def test_stack_param_diffs_merge(self):
        c = ErrorClusterer()
        stack1 = ["java.net.ConnectException: Connection refused",
                  "\tat com.app.db.Pool.init(Pool.java:42)",
                  "\tat com.app.core.Main.start(Main.java:18)"]
        stack2 = ["java.net.ConnectException: Connection refused",
                  "\tat com.app.db.Pool.init(Pool.java:99)",
                  "\tat com.app.core.Main.start(Main.java:77)"]
        c.add(entry("db connect failed", stack=stack1))
        c.add(entry("db connect failed", stack=stack2))
        assert len(c) == 1

    def test_cluster_metadata(self):
        c = ErrorClusterer()
        c.add(entry("boom", ts=100.0, line=5))
        c.add(entry("boom", ts=200.0, line=50))
        cl = c.clusters[0]
        assert cl.count == 2
        assert cl.first_line == 5 and cl.last_line == 50
        assert cl.first_seen == 100.0 and cl.last_seen == 200.0
        assert cl.level == "ERROR" and cl.module == "db"

    def test_sample_replaced_with_stack_version(self):
        c = ErrorClusterer()
        c.add(entry("boom", line=1))
        assert c.clusters[0].sample.entry.stack == []
        replaced = c.add(entry("boom", line=2,
                               stack=["Traceback (most recent call last):"]))
        assert replaced[1] is True
        assert c.clusters[0].sample.entry.has_stack

    def test_before_context_kept_in_sample(self):
        c = ErrorClusterer()
        c.add(entry("boom", line=10), before_lines=["line-9", "line-8"])
        assert c.clusters[0].sample.before == ["line-9", "line-8"]

    def test_similarity_fallback_merges_wording_variants(self):
        c = ErrorClusterer()
        c.add(entry("failed to connect to database host"))
        c.add(entry("failed to connect to database node"))
        assert len(c) == 1

    def test_similarity_within_level_only(self):
        # 级别不同 -> 指纹前缀不同 -> 不应合并
        c = ErrorClusterer()
        c.add(entry("failed to connect to database host", level="ERROR"))
        c.add(entry("failed to connect to database host", level="FAIL"))
        assert len(c) == 2

    def test_variant_registered_for_exact_hit(self):
        # 相似度合并后，新变体模板应注册进精确表：再次出现直接 O(1) 命中
        c = ErrorClusterer()
        c.add(entry("cannot allocate memory for buffer"))
        c.add(entry("cannot allocate memory for buffers"))   # 相似度合并
        assert len(c) == 1
        c.add(entry("cannot allocate memory for buffers"))   # 精确命中
        assert len(c) == 1
        assert c.clusters[0].count == 3

    def test_cluster_histogram_records(self):
        c = ErrorClusterer()
        for i in range(5):
            c.add(entry("boom", ts=1000.0 + i))
        assert c.clusters[0].hist.total == 5


# ---------------------------------------------------------------------------
# 修复缺陷R4：簇实例记录（全屏簇展开数据源）
# ---------------------------------------------------------------------------
class TestClusterInstances:
    def test_instances_match_count(self):
        """每个错误实例都被记录：count == len(instances)。"""
        c = ErrorClusterer()
        for i in range(12):
            c.add(entry(f"boom {i}", ts=100.0 + i, line=i + 1))
        cl = c.clusters[0]
        assert cl.count == 12
        assert len(cl.instances) == 12
        assert not cl.instances_truncated

    def test_instance_fields(self):
        """实例记录时间戳/行号/摘要/完整条目/前上下文。"""
        c = ErrorClusterer()
        c.add(entry("Connection refused", ts=1700000000.0, line=42,
                    stack=["java.net.ConnectException: x"]),
              before_lines=["ctx-40", "ctx-41"])
        inst = c.clusters[0].instances[0]
        assert inst.timestamp == 1700000000.0
        assert inst.line_no == 42
        assert "Connection refused" in inst.summary
        assert inst.entry is not None
        assert inst.entry.has_stack
        assert inst.before == ["ctx-40", "ctx-41"]

    def test_instance_order_chronological(self):
        """实例按出现顺序记录（时间戳/行号递增）。"""
        c = ErrorClusterer()
        for i in range(5):
            c.add(entry("boom", ts=1000.0 + i, line=10 * (i + 1)))
        insts = c.clusters[0].instances
        assert [i.timestamp for i in insts] == sorted(i.timestamp
                                                      for i in insts)
        assert [i.line_no for i in insts] == [10, 20, 30, 40, 50]

    def test_detailed_cap_only_first_n_have_entry(self):
        """超出详情上限（200/簇）后实例仅记元数据（entry=None）。"""
        from log_ai_compressor.constants import (
            MAX_CLUSTER_INSTANCES_DETAILED,
        )
        c = ErrorClusterer()
        n = MAX_CLUSTER_INSTANCES_DETAILED + 5
        for i in range(n):
            c.add(entry("boom", line=i + 1))
        cl = c.clusters[0]
        assert cl.count == n
        assert len(cl.instances) == n          # 元数据仍全量（未到 2000）
        assert cl.instances[0].entry is not None
        assert cl.instances[MAX_CLUSTER_INSTANCES_DETAILED - 1].entry \
            is not None
        assert cl.instances[MAX_CLUSTER_INSTANCES_DETAILED].entry is None
        # 元数据实例无前上下文（内存有界）
        assert cl.instances[-1].before == []

    def test_meta_cap_marks_truncated(self):
        """超出元数据上限（2000/簇）后标记 truncated 且不再记录。"""
        from log_ai_compressor.constants import (
            MAX_CLUSTER_INSTANCES_META,
        )
        c = ErrorClusterer()
        n = MAX_CLUSTER_INSTANCES_META + 10
        for i in range(n):
            c.add(entry("boom", line=i + 1))
        cl = c.clusters[0]
        assert cl.count == n
        assert cl.instances_truncated is True
        assert len(cl.instances) == MAX_CLUSTER_INSTANCES_META

    def test_instances_do_not_break_dedup(self):
        """实例记录不影响聚类去重（行为与旧版一致）。"""
        c = ErrorClusterer()
        c.add(entry("request 12345 to 10.0.0.7 failed"))
        c.add(entry("request 98765 to 10.0.0.9 failed"))
        c.add(entry("disk full on /var"))
        assert len(c) == 2
        assert [cl.count for cl in c.clusters] == [2, 1]
        assert [len(cl.instances) for cl in c.clusters] == [2, 1]
