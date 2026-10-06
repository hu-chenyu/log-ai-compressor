# -*- coding: utf-8 -*-
"""级别过滤几乎全灭时的提示（pipeline._build_filter_notice）。

为什么值得单独测
----------------
这不是"加个提示"的小功能，而是堵一类**静默失败**：很多系统日志压根没有
级别字段（Loghub HealthApp / OpenStack 实测 21 万行里 99.7% 被默认的
ERROR/FAIL 过滤掉）。此前用户指着日志点一下，拿到一份几乎空的报告，
且**无法区分"日志里确实没有错误"和"工具没解析出来"** —— 这是最坏的
失败形态，因为看不出该换日志还是该修工具。

刻意**不自动放宽过滤**：用户要的是错误，静悄悄改成全量会给出他没要的
东西，还会让压缩比之类的数字失去意义。所以这里只解释、不改行为。
"""
from __future__ import annotations

import pytest

from log_ai_compressor import service
from log_ai_compressor.core.pipeline import (
    _build_filter_notice,
    analyze_text,
)
from log_ai_compressor.core.models import ErrorCluster, RunStats
from log_ai_compressor.export.reporters import (
    brief_summary,
    to_html,
    to_markdown,
    to_text,
)

# 200 条全是 INFO、2 条 ERROR —— 模拟"日志里几乎没有错误"
MOSTLY_INFO = "\n".join(
    [f"2024-01-01 10:00:{i % 60:02d} INFO heartbeat tick {i}" for i in range(200)]
    + ["2024-01-01 10:01:00 ERROR disk failure detected",
       "2024-01-01 10:01:05 ERROR disk failure detected again"]
) + "\n"


def _fake_clusters(n: int, instances: int):
    return [ErrorCluster(cluster_id=i, template=f"t{i}", message_template=f"t{i}",
                         level="ERROR", summary=f"t{i}", count=instances,
                         instances=[]) for i in range(n)]


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------
class TestNoticeTrigger:
    def test_fires_when_filter_drops_almost_everything(self):
        stats = RunStats(entry_lines=200, level_counts={"INFO": 198, "ERROR": 2},
                         error_entries=2)
        notice = _build_filter_notice(stats, _fake_clusters(2, 1))
        assert notice is not None, "保留 1% 时必须给解释"
        assert "1.0%" in notice
        assert "INFO" in notice and "198" in notice, "要给出级别分布，帮用户判断"

    def test_falls_back_to_error_entries_when_no_instances_recorded(self):
        """实例有全局上限，极端情况下可能一条都没记下来。

        此时必须退回 stats.error_entries，而不是误判成"过滤没生效"
        而静默 —— 静默正是这个功能要消灭的东西。
        """
        stats = RunStats(entry_lines=200, level_counts={"INFO": 190, "ERROR": 10},
                         error_entries=10)
        notice = _build_filter_notice(stats, _fake_clusters(10, 0))
        assert notice is not None
        assert "5.0%" in notice

    def test_silent_when_nothing_was_filtered(self):
        stats = RunStats(entry_lines=200, level_counts={"INFO": 200})
        assert _build_filter_notice(stats, _fake_clusters(200, 1)) is None

    def test_silent_when_only_half_filtered(self):
        """保留 50% 不算异常 —— 正常日志本来就有一半非错误。"""
        stats = RunStats(entry_lines=100, level_counts={"INFO": 50, "ERROR": 50})
        assert _build_filter_notice(stats, _fake_clusters(50, 1)) is None

    def test_silent_on_tiny_logs(self):
        """小日志不放提示：样本噪声大，提示反而是干扰。"""
        stats = RunStats(entry_lines=5, level_counts={"INFO": 5})
        assert _build_filter_notice(stats, _fake_clusters(1, 1)) is None

    def test_silent_when_no_clusters(self):
        stats = RunStats(entry_lines=500, level_counts={"INFO": 500})
        assert _build_filter_notice(stats, []) is None


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------
class TestNoticeEndToEnd:
    def test_default_levels_on_level_less_log_gives_notice(self):
        """无级别字段日志 + 默认 ERROR/FAIL -> 必须有 notice。"""
        r = analyze_text(MOSTLY_INFO)
        assert r.notice, "默认过滤几乎全灭时不得静默"
        assert "当前级别过滤只保留了" in r.notice

    def test_all_levels_gives_no_notice(self):
        """放开门槛后不再提示 —— 提示必须是条件性的，不能变成噪音。"""
        r = analyze_text(MOSTLY_INFO, levels=["INFO", "ERROR"])
        assert not r.notice

    @pytest.mark.parametrize("fmt", ["md", "text", "html", "summary"])
    def test_all_report_formats_carry_the_notice(self, fmt):
        """四种导出格式都要带上提示，否则换格式就丢了关键信息。"""
        r = analyze_text(MOSTLY_INFO)
        out = service.export_text(r, fmt)
        assert "当前级别过滤只保留了" in out, f"{fmt} 格式丢失了提示"

    def test_service_dict_exposes_notice(self):
        """Web / MCP 通过 result_to_dict 取提示，键必须存在。"""
        d = service.result_to_dict(analyze_text(MOSTLY_INFO))
        assert "notice" in d
        assert d["notice"]

    def test_report_formats_omit_notice_when_not_triggered(self):
        r = analyze_text(MOSTLY_INFO, levels=["INFO", "ERROR"])
        for fmt in ("md", "text", "html", "summary"):
            out = service.export_text(r, fmt)
            assert "当前级别过滤只保留了" not in out
            # HTML 里也不该凭空多出提示容器
            if fmt == "html":
                assert 'class="notice"' not in out


# ---------------------------------------------------------------------------
# 导出的四个格式各自直调（绕过 service.export_text 的分发表）
# ---------------------------------------------------------------------------
class TestNoticeInEachReporter:
    def test_markdown(self):
        r = analyze_text(MOSTLY_INFO)
        assert "⚠" in to_markdown(r)

    def test_text(self):
        r = analyze_text(MOSTLY_INFO)
        assert "! 提示:" in to_text(r)

    def test_html_has_notice_container(self):
        r = analyze_text(MOSTLY_INFO)
        assert 'class="notice"' in to_html(r)

    def test_summary(self):
        r = analyze_text(MOSTLY_INFO)
        assert "⚠" in brief_summary(r)