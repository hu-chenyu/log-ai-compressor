# -*- coding: utf-8 -*-
"""数据模型单元测试：自适应时间直方图与模型基础行为。"""
from __future__ import annotations

from log_ai_compressor.core.models import (
    AnalysisResult,
    ClusterSample,
    ErrorCluster,
    LogEntry,
    RunStats,
    TimeHistogram,
)


class TestTimeHistogram:
    def test_first_sample_initializes(self):
        h = TimeHistogram()
        h.add(100.0)
        assert h.total == 1
        assert h.series() == [(100.0, 1)]

    def test_series_sorted_and_counts(self):
        h = TimeHistogram()
        for t in (10.0, 10.4, 10.9, 13.0, 13.2):
            h.add(t)
        s = h.series()
        assert [c for _, c in s] == [3, 2]  # 桶宽 1s：[10,11) 3 个，[13,14) 2 个
        assert s[0][0] == 10.0

    def test_backward_time_shift(self):
        h = TimeHistogram()
        h.add(100.0)
        h.add(95.0)  # 时间回跳
        assert h.total == 2
        times = [t for t, _ in h.series()]
        assert times == sorted(times)

    def test_widen_keeps_memory_bounded(self):
        # 覆盖远超 max_buckets 的时间跨度 -> 桶宽自适应扩宽，桶数受控
        h = TimeHistogram(max_buckets=8)
        for i in range(1000):
            h.add(float(i * 100))
        assert len(h.series()) <= 8 * 8   # 扩宽期间桶数有界
        assert h.total == 1000
        assert h.width >= 1.0

    def test_burst_buckets_detects_spike(self):
        h = TimeHistogram()
        for i in range(50):
            h.add(float(i))       # 平稳基线：每桶 1 个
        for _ in range(20):
            h.add(60.0)           # 爆发桶
        bursts = h.burst_buckets(k=3.0)
        assert any(c >= 20 for _, c in bursts)

    def test_burst_buckets_flat_returns_empty(self):
        h = TimeHistogram()
        for i in range(30):
            h.add(float(i))
        assert h.burst_buckets() == []

    def test_empty_histogram(self):
        h = TimeHistogram()
        assert h.series() == []
        assert h.total == 0
        assert h.burst_buckets() == []
        assert h.start is None


class TestModels:
    def test_log_entry_full_message_joins_wrapped(self):
        e = LogEntry(line_no=1, message="start of message",
                     message_extra=["wrapped part 1", "part 2"])
        assert e.full_message == "start of message wrapped part 1 part 2"

    def test_error_cluster_priority_label(self):
        c = ErrorCluster(cluster_id=0, template="t", priority=80)
        assert c.priority_label == "P0"
        c.priority = 60
        assert c.priority_label == "P1"
        c.priority = 40
        assert c.priority_label == "P2"
        c.priority = 10
        assert c.priority_label == "P3"

    def test_run_stats_as_dict(self):
        s = RunStats(source="a.log", total_lines=100, error_lines=10,
                     duration=0.5, lines_per_second=200.0)
        d = s.as_dict()
        assert d["source"] == "a.log"
        assert d["total_lines"] == 100
        assert d["duration_sec"] == 0.5

    def test_analysis_result_filters(self):
        clusters = [
            ErrorCluster(cluster_id=0, template="a", level="ERROR", module="db"),
            ErrorCluster(cluster_id=1, template="b", level="FAIL", module="auth"),
        ]
        r = AnalysisResult(stats=RunStats(), clusters=clusters)
        assert len(r.clusters_of_module("db")) == 1
        assert len(r.clusters_of_level("FAIL")) == 1

    def test_cluster_sample_context(self):
        sample = ClusterSample(entry=LogEntry(line_no=10, message="boom"),
                               before=["ctx-1"], after=["ctx+1"])
        assert sample.entry.message == "boom"
        assert sample.before == ["ctx-1"]
