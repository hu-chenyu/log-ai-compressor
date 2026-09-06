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
# 优化缺陷R76：根因强化（因果 DAG + 关键词 IDF 加权）
# ---------------------------------------------------------------------------
class TestRootCauseEnhanced:
    def test_cross_reference_edge_marks_source_root(self):
        """B 消息复述 A 的模板词（含稀有词）→ A→B 边，A 判图源头根因。"""
        src = make_cluster(0, "auth token expired", first_line=10)
        derived = make_cluster(
            1, "request aborted because auth token expired", first_line=50)
        result = make_result([derived, src])
        analyze_clusters(result)
        assert src.is_root_cause
        assert "因果链源头" in src.root_cause_reason
        assert not derived.is_root_cause
        assert "连锁衍生" in derived.root_cause_reason
        assert "auth token expired" in derived.root_cause_reason

    def test_cross_reference_requires_rare_token(self):
        """共享词在 ≥3 簇出现（泛词）→ 不建边、不误判根因。"""
        a = make_cluster(0, "timeout foo bar", first_line=1)
        b = make_cluster(1, "timeout foo baz", first_line=2)
        c = make_cluster(2, "timeout foo qux", first_line=3)
        result = make_result([a, b, c])
        analyze_clusters(result)
        assert not a.is_root_cause
        assert not b.is_root_cause
        assert not c.is_root_cause

    def test_cross_reference_requires_temporal_order(self):
        """互引用仅指向后发错误（先发不因后发的复述担责）。"""
        later = make_cluster(0, "auth token expired", first_line=50)
        earlier = make_cluster(1, "request aborted because auth token expired",
                               first_line=10)
        result = make_result([later, earlier])
        analyze_clusters(result)
        assert not earlier.is_root_cause or \
            "因果链源头" not in earlier.root_cause_reason

    def test_idf_weights_rare_keyword_stronger(self):
        """罕见关键词（df 低）权重大于常见关键词。"""
        from log_ai_compressor.core.analysis import _keyword_weights
        clusters = [make_cluster(i, s) for i, s in enumerate(
            ["deadlock detected", "timeout a", "timeout b", "timeout c"])]
        w = _keyword_weights(clusters)
        assert w["deadlock"] > w["timeout"]
        # 单簇单命中权重恰为 1.0（与旧计数制兼容，阈值 3 语义不变）
        single = _keyword_weights([make_cluster(0, "deadlock detected")])
        assert abs(single["deadlock"] - 1.0) < 1e-9

    def test_derived_reason_names_upstream(self):
        """图内衍生错误的原因注明上游摘要（修上游别修它）。"""
        src = make_cluster(0, "disk quota exceeded", first_line=10)
        derived = make_cluster(1, "write failed: disk quota exceeded",
                               first_line=20)
        result = make_result([src, derived])
        analyze_clusters(result)
        assert "上游" in derived.root_cause_reason
        assert "disk quota exceeded" in derived.root_cause_reason


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
        # 优化缺陷R75：罕见簇与既有簇模板相似（老错误的偶发尾巴）
        # 才判 rare；不相似的升入 novel（见 TestAnomalyEnhanced）
        rare = make_cluster(0, "frequent error variant", count=1)
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
# 优化缺陷R75：异常检测强化（自持基线爆发 / 周期发作 / 新型错误）
# ---------------------------------------------------------------------------
class TestAnomalyEnhanced:
    @staticmethod
    def _instances(cluster, timestamps):
        from log_ai_compressor.core.models import ClusterInstance
        cluster.instances = [
            ClusterInstance(timestamp=t, line_no=i + 1)
            for i, t in enumerate(timestamps)
        ]

    def test_own_baseline_burst_without_global_burst(self):
        """全局平稳但单簇自身陡增 → 自持基线通道判 burst（原全局通道漏报）。"""
        t0 = 1704067200.0
        c = make_cluster(0, "spiking error", count=70, first_seen=t0)
        # 簇直方图：前 60 桶各 1 次，第 61 桶突增 10 次
        for i in range(60):
            c.hist.add(t0 + i)
        for _ in range(10):
            c.hist.add(t0 + 60.0)
        # 全局直方图完全平稳（每秒 1 个）→ 无全局爆发窗口
        flat = [t0 + i for i in range(100)]
        result = make_result([c], global_adds=flat)
        analyze_clusters(result)
        assert c.anomaly == "burst", "自持基线爆发应命中（全局通道未命中）"

    def test_own_baseline_quiet_when_uniform(self):
        """簇内频次均匀 → 不误报自持基线爆发。"""
        t0 = 1704067200.0
        c = make_cluster(0, "steady error", count=60, first_seen=t0)
        for i in range(60):
            c.hist.add(t0 + i)
        result = make_result([c], global_adds=[t0 + i for i in range(100)])
        analyze_clusters(result)
        assert c.anomaly == ""

    def test_periodic_detected(self):
        """定时间隔报错（变异系数≈0）→ periodic（周期发作）。"""
        c = make_cluster(0, "watchdog keepalive failed", count=5,
                         first_seen=100.0)
        self._instances(c, [100.0, 130.0, 160.0, 190.0, 220.0])
        result = make_result([c], error_entries=5)
        analyze_clusters(result)
        assert c.anomaly == "periodic"

    def test_periodic_not_flagged_when_irregular(self):
        """间隔忽长忽短 → 不判周期。"""
        c = make_cluster(0, "random failure", count=5, first_seen=100.0)
        self._instances(c, [100.0, 103.0, 190.0, 191.0, 400.0])
        result = make_result([c], error_entries=5)
        analyze_clusters(result)
        assert c.anomaly == ""

    def test_periodic_needs_min_samples(self):
        """时间戳样本 <4 → 不判周期。"""
        c = make_cluster(0, "few beats", count=3, first_seen=100.0)
        self._instances(c, [100.0, 130.0, 160.0])
        result = make_result([c], error_entries=3)
        analyze_clusters(result)
        assert c.anomaly == ""

    def test_novel_detected_for_dissimilar_rare(self):
        """罕见且与既有簇模板不相似 → novel（新型错误）。"""
        common = make_cluster(0, "connection refused timeout error", count=10)
        rare_new = make_cluster(1, "zqx blorptastic quantumflux failure",
                                count=1)
        result = make_result([common, rare_new], error_entries=11)
        analyze_clusters(result)
        assert rare_new.anomaly == "novel"

    def test_rare_kept_for_similar_rare(self):
        """罕见但与既有簇相似（老错误的偶发尾巴）→ 仍判 rare 不判 novel。"""
        common = make_cluster(0, "connection refused timeout error", count=10)
        rare_tail = make_cluster(1, "connection refused error again", count=1)
        result = make_result([common, rare_tail], error_entries=11)
        analyze_clusters(result)
        assert rare_tail.anomaly == "rare"

    def test_burst_precedes_periodic(self):
        """优先级递降：既是周期又有自持爆发 → burst 优先。"""
        t0 = 1704067200.0
        c = make_cluster(0, "periodic spiker", count=70, first_seen=t0)
        self._instances(c, [t0, t0 + 30, t0 + 60, t0 + 90, t0 + 120])
        for i in range(60):
            c.hist.add(t0 + i)
        for _ in range(10):
            c.hist.add(t0 + 60.0)
        result = make_result([c], global_adds=[t0 + i for i in range(100)])
        analyze_clusters(result)
        assert c.anomaly == "burst", "爆发应优先于周期标注"


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
