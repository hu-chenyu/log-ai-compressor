# -*- coding: utf-8 -*-
"""流式管线集成测试：端到端解析/过滤/聚类/分析/上下文/取消。"""
from __future__ import annotations

import threading

import pytest

from log_ai_compressor.core.pipeline import (
    LogPipeline,
    PipelineConfig,
    analyze_file,
    analyze_text,
)
from log_ai_compressor.core.filters import FilterConfig


SAMPLE_LOG = """\
2024-01-01 09:00:00 INFO [auth] service started
2024-01-01 09:00:01 DEBUG [auth] config loaded
2024-01-01 09:00:02 WARN [db] connection pool nearly exhausted
2024-01-01 09:00:05 ERROR [db] connection refused to db-primary:5432
java.net.ConnectException: Connection refused
\tat com.app.db.Pool.init(Pool.java:42)
\tat java.base/java.net.Socket.connect(Socket.java:1)
\tat com.app.core.Main.start(Main.java:18)
2024-01-01 09:00:06 ERROR [api] request 123 failed
2024-01-01 09:00:07 ERROR [api] request 456 failed
2024-01-01 09:00:08 ERROR [api] request 789 failed
2024-01-01 09:00:20 INFO [api] recovered
2024-01-01 09:01:00 FATAL [core] out of memory in worker 3
2024-01-01 09:01:01 ERROR [core] worker 3 aborted after retries
2024-01-01 09:02:00 ERROR [auth] token expired for session 99a1
"""


# ---------------------------------------------------------------------------
# 基本端到端
# ---------------------------------------------------------------------------
class TestAnalyzeText:
    def test_basic_stats(self):
        r = analyze_text(SAMPLE_LOG)
        s = r.stats
        assert s.total_lines == 15
        assert s.error_lines == 7        # ERROR×6 + FATAL×1
        assert s.error_entries == 7      # FATAL 始终放行
        assert s.time_start is not None and s.time_end is not None
        assert s.duration > 0
        assert s.truncated is False

    def test_clusters_merged_and_sorted(self):
        r = analyze_text(SAMPLE_LOG)
        # request N failed 三次归一簇；clustered: pool refused / request / oom / worker / token
        counts = {c.summary: c.count for c in r.clusters}
        assert any("request" in k and v == 3 for k, v in counts.items())
        # FATAL 置顶
        assert r.clusters[0].level == "FATAL"
        assert r.clusters[0].summary.startswith("out of memory")

    def test_stack_attached_to_cluster(self):
        r = analyze_text(SAMPLE_LOG)
        pool = next(c for c in r.clusters if "refused" in c.summary)
        assert pool.sample.entry.has_stack
        assert any("Pool.init" in line for line in pool.sample.entry.stack)

    def test_root_cause_detected(self):
        r = analyze_text(SAMPLE_LOG)
        pool = next(c for c in r.clusters if "refused" in c.summary)
        assert pool.is_root_cause

    def test_filter_levels(self):
        r = analyze_text(SAMPLE_LOG, levels=["FATAL"])
        assert all(c.level == "FATAL" for c in r.clusters)
        assert r.stats.error_entries == 1

    def test_filter_keywords(self):
        r = analyze_text(SAMPLE_LOG, include=["token"])
        assert len(r.clusters) == 1
        assert "token" in r.clusters[0].summary

    def test_filter_exclude(self):
        r = analyze_text(SAMPLE_LOG, exclude=["request"])
        assert not any("request" in c.summary for c in r.clusters)

    def test_context_before_after(self):
        r = analyze_text(SAMPLE_LOG, context_lines=2)
        token = next(c for c in r.clusters if "token" in c.summary)
        sample = token.sample
        assert sample is not None
        # 前两行：worker aborted / OOM
        assert any("worker 3" in line for line in sample.before)
        # 后上下文：文件末尾无后续行
        assert sample.after == []

    def test_context_after_following_lines(self):
        log = "\n".join([
            "INFO ctx-1",
            "INFO ctx-2",
            "2024-01-01 09:00:00 ERROR [db] boom",
            "INFO ctx-3",
            "INFO ctx-4",
            "INFO ctx-5",
            "INFO ctx-6",
            "INFO ctx-7",
        ])
        r = analyze_text(log, context_lines=3)
        c = r.clusters[0]
        assert c.sample.before == ["INFO ctx-1", "INFO ctx-2"]
        assert c.sample.after == ["INFO ctx-3", "INFO ctx-4", "INFO ctx-5"]

    def test_analyze_disabled_keeps_fields_default(self):
        r = analyze_text(SAMPLE_LOG, analyze=False)
        assert r.clusters[0].priority == 0.0
        assert not any(c.is_root_cause for c in r.clusters)

    def test_global_hist_counts(self):
        r = analyze_text(SAMPLE_LOG)
        assert r.global_hist.total == r.stats.error_entries

    def test_level_counts(self):
        r = analyze_text(SAMPLE_LOG)
        assert r.stats.level_counts.get("ERROR") == 6
        assert r.stats.level_counts.get("FATAL") == 1
        assert r.stats.level_counts.get("INFO") == 2
        assert r.stats.level_counts.get("WARN") == 1
        assert r.stats.level_counts.get("DEBUG") == 1


# ---------------------------------------------------------------------------
# 文件模式（编码探测联动）
# ---------------------------------------------------------------------------
class TestAnalyzeFile:
    def test_utf8_file(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text(SAMPLE_LOG, encoding="utf-8")
        r = analyze_file(p)
        assert r.stats.total_lines == 15
        assert r.stats.encoding == "utf-8"

    def test_gbk_file(self, tmp_path):
        p = tmp_path / "gbk.log"
        p.write_text("2024-01-01 09:00:00 ERROR [db] 中文错误：连接失败\n" * 5,
                     encoding="gbk")
        r = analyze_file(p)
        assert r.stats.encoding == "gb18030"
        assert len(r.clusters) == 1
        assert "中文错误" in r.clusters[0].summary

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            analyze_file(tmp_path / "nope.log")

    def test_custom_rule_yaml(self, tmp_path):
        rule = tmp_path / "r.yaml"
        rule.write_text(
            "name: r\n"
            "patterns:\n"
            "  - regex: '^X (?P<level>ERR) (?P<message>.*)$'\n",
            encoding="utf-8",
        )
        p = tmp_path / "x.log"
        p.write_text("X ERR boom\nX ERR boom\n", encoding="utf-8")
        r = analyze_file(p, rule=str(rule))
        assert len(r.clusters) == 1
        assert r.clusters[0].count == 2
        assert r.stats.rule_name == "r"


# ---------------------------------------------------------------------------
# 进度与取消
# ---------------------------------------------------------------------------
class TestProgressAndCancel:
    def test_progress_callback_phases(self):
        events = []
        # 超过 PROGRESS_EVERY_LINES 才会触发 parsing 相位，这里验证完成相位
        analyze_text(SAMPLE_LOG, progress_cb=lambda d: events.append(d))
        assert events and events[-1]["phase"] == "done"
        assert events[-1]["lines"] == 15

    def test_progress_parsing_phase_large_input(self):
        events = []
        big = "\n".join(
            f"2024-01-01 09:00:00 INFO [t] line {i}" for i in range(50000))
        analyze_text(big, progress_cb=events.append)
        phases = {e["phase"] for e in events}
        assert "parsing" in phases
        assert events[-1]["lines"] == 50000

    def test_cancel_midway_returns_partial(self):
        ev = threading.Event()
        # 生成足量行数：进度回调（16384 行触发）设置取消标记，
        # 下一个取消检测点（4096 的倍数）生效
        lines = []
        for i in range(50000):
            level = "ERROR" if i % 100 == 0 else "INFO"
            lines.append(f"2024-01-01 09:00:00 {level} [t] msg {i}")
        text = "\n".join(lines)

        def cb(d):
            if d["phase"] == "parsing" and d["lines"] >= 16384:
                ev.set()

        r = analyze_text(text, progress_cb=cb, cancel_event=ev)
        assert r.stats.truncated is True
        assert r.stats.total_lines < 50000
        assert r.stats.total_lines >= 16384

    def test_cancel_before_start(self):
        ev = threading.Event()
        ev.set()
        # 行数需超过取消检测间隔（4096）才会触发
        text = "\n".join(f"2024-01-01 09:00:00 INFO [t] msg {i}"
                         for i in range(5000))
        r = analyze_text(text, cancel_event=ev)
        assert r.stats.truncated is True
        assert 0 < r.stats.total_lines < 5000


# ---------------------------------------------------------------------------
# 复用与规则集注入
# ---------------------------------------------------------------------------
class TestPipelineReuse:
    def test_pipeline_config_object(self):
        cfg = PipelineConfig(filter_config=FilterConfig(levels=["ERROR", "FAIL"]))
        pipeline = LogPipeline(cfg)
        r1 = pipeline.run_text(SAMPLE_LOG)
        r2 = pipeline.run_text(SAMPLE_LOG)
        assert r1.stats.total_lines == r2.stats.total_lines

    def test_memory_bounded_clusters(self):
        # 2 万行「不同」错误：数字被掩码后指纹一致 -> 聚合为 1 簇
        # （内存仅存 1 份样例 + 计数，与出现次数无关 —— 内存有界的直接证明）
        text = "\n".join(f"2024-01-01 09:00:00 ERROR [t] unique error {i}"
                         for i in range(20000))
        r = analyze_text(text)
        assert len(r.clusters) == 1
        assert r.clusters[0].count == 20000
        assert r.stats.error_entries == 20000


# ---------------------------------------------------------------------------
# 修复缺陷#9：小日志性能保障（100 行内 1 秒出结果）
# ---------------------------------------------------------------------------
class TestSmallLogPerformance:
    def test_100_lines_under_1s(self):
        # 100 行混合日志（含错误行）：整体分析应在 1 秒内完成
        import time as _time
        lines = []
        for i in range(100):
            ts = f"2024-01-01 09:{i // 60:02d}:{i % 60:02d}"
            if i % 10 == 0:
                lines.append(f"{ts} ERROR [db] connection refused to host {i}")
            else:
                lines.append(f"{ts} INFO [core] heartbeat ok {i}")
        t0 = _time.perf_counter()
        r = analyze_text("\n".join(lines))
        elapsed = _time.perf_counter() - t0
        assert r.stats.total_lines == 100
        assert r.stats.error_entries == 10
        # 宽松上限 1.0s（含 CI 冷启动开销；正常本机 < 0.05s）
        assert elapsed < 1.0, f"小日志分析耗时 {elapsed:.2f}s 超过 1s"

    def test_20k_lines_under_10s(self):
        # 2 万行中等日志：流式管线应在 10 秒内完成（性能回归保护）
        import time as _time
        lines = []
        for i in range(20000):
            ts = f"2024-01-01 09:{(i // 60) % 60:02d}:{i % 60:02d}.{i % 1000:03d}"
            if i % 10 == 0:
                lines.append(f"{ts} ERROR [db] connection refused to host {i % 5}")
            else:
                lines.append(f"{ts} INFO [core] heartbeat ok {i}")
        t0 = _time.perf_counter()
        r = analyze_text("\n".join(lines))
        elapsed = _time.perf_counter() - t0
        assert r.stats.total_lines == 20000
        assert elapsed < 10.0, f"2 万行分析耗时 {elapsed:.2f}s 超过 10s"
