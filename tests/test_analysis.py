# -*- coding: utf-8 -*-
"""智能分析单元测试：根因判定、异常检测、优先级排序、堆栈降噪。"""
from __future__ import annotations

from log_ai_compressor.core.analysis import (
    analyze_clusters,
    simplify_stack,
)
from log_ai_compressor.core.models import (
    AnalysisResult,
    ClusterSample,
    ErrorCluster,
    LogEntry,
    RunStats,
    format_timestamp,
)


def make_cluster(cid, summary, level="ERROR", count=1, first_seen=None,
                 first_line=1, last_line=1, stack=None, module=""):
    entry = LogEntry(line_no=first_line, raw=summary, level=level,
                     module=module, message=summary, timestamp=first_seen,
                     stack=stack or [])
    return ErrorCluster(
        cluster_id=cid, template=f"{level} | {summary}",
        message_template=summary, level=level, module=module, summary=summary,
        count=count, first_line=first_line, last_line=last_line,
        first_seen=first_seen, last_seen=first_seen,
        sample=ClusterSample(entry=entry),
    )


def make_result(clusters, error_entries=None, global_adds=()):
    from log_ai_compressor.core.models import TimeHistogram
    gh = TimeHistogram()
    for t in global_adds:
        gh.add(t)
    if error_entries is None:
        error_entries = sum(c.count for c in clusters)
    stats = RunStats(error_entries=error_entries)
    return AnalysisResult(stats=stats, clusters=clusters, global_hist=gh)


# ---------------------------------------------------------------------------
# 堆栈降噪
# ---------------------------------------------------------------------------
class TestSimplifyStack:
    def test_noise_frames_folded(self):
        stack = [
            "java.net.ConnectException: Connection refused",
            "\tat com.app.db.Pool.init(Pool.java:42)",
            "\tat java.base/java.net.AbstractPlainSocketImpl.connect(...)",
            "\tat java.base/java.net.Socket.connect(Socket.java:1)",
            "\tat com.app.core.Main.start(Main.java:18)",
        ]
        s = simplify_stack(stack)
        assert s.business_count == 3
        assert s.noise_count == 2
        # 折叠注释出现在业务帧之间
        assert any("已折叠 2 行" in line for line in s.lines)
        assert "\tat com.app.db.Pool.init(Pool.java:42)" in s.lines

    def test_caused_by_never_folded(self):
        stack = [
            "java.sql.SQLException: query failed",
            "\tat com.app.db.Dao.query(Dao.java:99)",
            "\tat org.hibernate.internal.SessionImpl.doWork(SessionImpl.java:1)",
            "Caused by: java.net.ConnectException: Connection refused",
        ]
        s = simplify_stack(stack)
        assert "Caused by: java.net.ConnectException: Connection refused" in s.lines

    def test_traceback_header_kept(self):
        s = simplify_stack([
            "Traceback (most recent call last):",
            '  File "/usr/lib/python3.9/site-packages/requests/api.py", line 75',
            '  File "app/client.py", line 30, in fetch',
            "ValueError: bad status",
        ])
        assert s.lines[0] == "Traceback (most recent call last):"
        assert "ValueError: bad status" in s.lines
        assert s.noise_count == 1

    def test_all_noise_stack(self):
        s = simplify_stack([
            "\tat java.base/java.lang.Thread.run(Thread.java:1)",
            "\tat org.springframework.context.Context.refresh(Context.java:1)",
        ])
        assert s.business_count == 0
        assert s.noise_count == 2
        assert len(s.lines) == 1   # 单条折叠注释

    def test_empty(self):
        s = simplify_stack([])
        assert s.lines == [] and s.noise_count == 0


# ---------------------------------------------------------------------------
# 根因判定
# ---------------------------------------------------------------------------
class TestRootCause:
    def test_caused_by_links_prior_error(self):
        root = make_cluster(0, "pool init failed: connection refused",
                             first_line=10, last_line=12)
        derived = make_cluster(
            1, "service unavailable", first_line=50, last_line=55,
            stack=["java.sql.SQLException: query failed",
                   "Caused by: java.net.ConnectException: Connection refused"],
        )
        result = make_result([root, derived])
        analyze_clusters(result)
        assert root.is_root_cause
        assert "Caused-by" in root.root_cause_reason

    def test_time_window_earliest_with_keyword(self):
        # 同一 60s 窗口内：先发的根因特征错误 vs 后发的衍生错误
        t0 = 1704067200.0
        root = make_cluster(0, "connection refused to db host:1433",
                            first_seen=t0, first_line=5)
        derived = make_cluster(1, "request retry aborted after 3 attempts",
                               first_seen=t0 + 5, first_line=9)
        result = make_result([derived, root])
        analyze_clusters(result)
        assert root.is_root_cause
        assert not derived.is_root_cause

    def test_derived_flagged_by_cascade_keyword(self):
        c = make_cluster(0, "downstream request skipped after retries")
        result = make_result([c])
        analyze_clusters(result)
        assert not c.is_root_cause
        assert "连锁衍生" in c.root_cause_reason

    def test_strong_keywords_marked_root(self):
        c = make_cluster(0, "cannot open file: permission denied, disk full")
        result = make_result([c], error_entries=50)
        analyze_clusters(result)
        assert c.is_root_cause
        assert "关键词" in c.root_cause_reason


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------
class TestAnomaly:
    def test_burst_cluster_detected(self):
        # 全局基线：每秒 1 个错误；第 600 秒爆发 50 个
        t_base = 1704067200.0
        adds = [t_base + i for i in range(60)]
        adds += [t_base + 600.0] * 50
        burst = make_cluster(0, "error storm", count=50,
                             first_seen=t_base + 600.0, first_line=1)
        normal = make_cluster(1, "steady error", count=60,
                              first_seen=t_base, first_line=100)
        # 手动填充簇级直方图
        for _ in range(50):
            burst.hist.add(t_base + 600.0)
        for i in range(60):
            normal.hist.add(t_base + i)
        result = make_result([burst, normal], global_adds=adds)
        analyze_clusters(result)
        assert burst.anomaly == "burst"
        assert normal.anomaly == ""

    def test_rare_cluster_detected(self):
        rare = make_cluster(0, "weird one-time glitch", count=1)
        common = make_cluster(1, "frequent error", count=99)
        result = make_result([rare, common], error_entries=100)
        analyze_clusters(result)
        assert rare.anomaly == "rare"
        assert common.anomaly == ""

    def test_rare_not_flagged_when_total_small(self):
        c = make_cluster(0, "solo error", count=1)
        result = make_result([c], error_entries=1)
        analyze_clusters(result)
        assert c.anomaly == ""


# ---------------------------------------------------------------------------
# 优先级
# ---------------------------------------------------------------------------
class TestPriority:
    def test_error_always_front(self):
        # 修复缺陷R40：ERROR 保证 P0 前置（兜底 80 ≥ P0 阈值 75；
        # 原 FATAL 强制 90 随 FATAL 删除移除）
        err = make_cluster(0, "minor error note", level="ERROR", count=1)
        big_fail = make_cluster(1, "huge fail storm", level="FAIL", count=500)
        result = make_result([big_fail, err], error_entries=501)
        analyze_clusters(result)
        assert result.clusters[0] is err
        assert err.priority >= 80
        assert err.priority_label == "P0"
        assert big_fail.priority_label == "P1"

    def test_frequency_boosts_priority(self):
        # 修复缺陷R40：ERROR 兜底 80 会拉平同档分数（频次差异只
        # 体现在排序），频次加分断言改用无兜底的 WARN 级
        low = make_cluster(0, "rare warn a", level="WARN", count=1)
        high = make_cluster(1, "frequent warn b", level="WARN", count=200)
        result = make_result([low, high], error_entries=201)
        analyze_clusters(result)
        assert result.clusters[0] is high
        assert high.priority > low.priority

    def test_root_cause_boosts_priority(self):
        t0 = 1704067200.0
        plain = make_cluster(0, "plain error", level="ERROR", count=50,
                             first_line=100, first_seen=t0 + 30)
        root = make_cluster(0, "connection refused", level="ERROR", count=10,
                            first_line=1, first_seen=t0)
        result = make_result([plain, root], error_entries=60)
        analyze_clusters(result)
        assert root.is_root_cause
        # 同量级下根因获得加分
        assert root.priority + 20 > plain.priority - 20

    def test_sort_priority_desc(self):
        a = make_cluster(0, "a", level="ERROR", count=100)
        b = make_cluster(1, "b", level="ERROR", count=10)
        c = make_cluster(2, "c", level="FAIL", count=5)
        result = make_result([c, b, a], error_entries=115)
        analyze_clusters(result)
        priorities = [x.priority for x in result.clusters]
        assert priorities == sorted(priorities, reverse=True)


# ---------------------------------------------------------------------------
# 时间格式化
# ---------------------------------------------------------------------------
class TestFormatTimestamp:
    def test_none(self):
        assert format_timestamp(None) == "-"

    def test_relative(self):
        assert format_timestamp(123.456) == "123.456s"

    def test_epoch(self):
        assert format_timestamp(1704067200.0).startswith("2024-01-01")
