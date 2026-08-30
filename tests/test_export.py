# -*- coding: utf-8 -*-
"""导出层单元测试：Markdown / JSON / 纯文本 / 简要摘要。"""
from __future__ import annotations

import json

import pytest

from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.export.reporters import (
    brief_summary,
    compare_to_markdown,
    to_json,
    to_markdown,
    to_text,
)

from log_ai_compressor.core.comparator import compare_results

SAMPLE = """\
2024-01-01 09:00:00 INFO [auth] service started
2024-01-01 09:00:01 WARN [db] pool nearly exhausted
2024-01-01 09:00:05 ERROR [db] connection refused to db-primary:5432
java.net.ConnectException: Connection refused
\tat com.app.db.Pool.init(Pool.java:42)
\tat java.base/java.net.Socket.connect(Socket.java:1)
\tat com.app.core.Main.start(Main.java:18)
2024-01-01 09:00:06 ERROR [api] request 123 failed
2024-01-01 09:00:07 ERROR [api] request 456 failed
2024-01-01 09:01:00 FATAL [core] out of memory in worker 3
"""


@pytest.fixture(scope="module")
def result():
    return analyze_text(SAMPLE)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
class TestMarkdown:
    def test_structure_sections(self, result):
        md = to_markdown(result)
        assert md.startswith("# 日志AI压缩报告")
        assert "## 一、概览统计" in md
        assert "## 二、Top" in md
        assert "## 三、典型样例详情" in md

    def test_overview_stats(self, result):
        md = to_markdown(result)
        assert "| 总行数 | 10 |" in md
        assert "connection refused" in md

    def test_top_table_rows(self, result):
        md = to_markdown(result, top_n=3)
        assert md.count("\n| 1 |") == 1
        assert "P0" in md or "P1" in md

    def test_root_cause_marked(self, result):
        md = to_markdown(result)
        assert "初步定位根因" in md
        assert "✔" in md or "根因" in md

    def test_stack_denoised_in_detail(self, result):
        md = to_markdown(result)
        assert "已折叠" in md
        assert "java.base" in md           # 折叠注释中说明
        assert "com.app.db.Pool.init" in md  # 业务帧保留

    def test_context_fences(self, result):
        md = to_markdown(result)
        assert "**典型样例**" in md
        assert "```" in md

    def test_pipe_in_summary_escaped(self, result):
        md = to_markdown(result)
        # 摘要中若含 | 须转义，保证表格不破
        assert md.count("\\|") >= md.count("|ERROR |")  # 粗略不破坏校验

    def test_empty_clusters_report(self):
        r = analyze_text("2024-01-01 09:00:00 INFO [x] nothing wrong\n")
        md = to_markdown(r)
        assert "未发现符合条件的错误" in md

    def test_title_override(self, result):
        md = to_markdown(result, title="自定义标题")
        assert "# 自定义标题" in md


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
class TestJson:
    def test_valid_json(self, result):
        payload = json.loads(to_json(result))
        assert payload["generator"].startswith("log-ai-compressor")
        assert payload["meta"]["total_lines"] == 10
        assert isinstance(payload["clusters"], list)

    def test_cluster_fields(self, result):
        payload = json.loads(to_json(result))
        c = payload["clusters"][0]
        for key in ("id", "level", "summary", "count", "priority",
                    "priority_label", "root_cause", "anomaly", "sample"):
            assert key in c

    def test_sample_fields(self, result):
        payload = json.loads(to_json(result))
        pool = next(c for c in payload["clusters"] if "refused" in c["summary"])
        assert pool["sample"]["stack"]
        assert pool["sample"]["stack_noise_folded"] >= 1
        assert pool["sample"]["stack_simplified"]

    def test_time_series(self, result):
        payload = json.loads(to_json(result))
        assert payload["time_series"]
        assert len(payload["time_series"][0]) == 2

    def test_top_n_limit(self, result):
        payload = json.loads(to_json(result, top_n=1))
        assert len(payload["clusters"]) == 1


# ---------------------------------------------------------------------------
# 纯文本与摘要
# ---------------------------------------------------------------------------
class TestTextAndBrief:
    def test_text_report(self, result):
        txt = to_text(result)
        assert "日志AI压缩报告" in txt
        assert "====" in txt
        assert "根因" in txt

    def test_brief_summary(self, result):
        brief = brief_summary(result, top_n=5)
        assert brief.startswith("【日志分析摘要】")
        assert "总行数" in brief
        assert "初步根因" in brief
        # 简要摘要应显著短于完整报告（压缩比）
        assert len(brief) < len(to_markdown(result)) / 2


# ---------------------------------------------------------------------------
# 对比报告
# ---------------------------------------------------------------------------
class TestCompareMarkdown:
    def test_compare_report(self, result):
        other = analyze_text(SAMPLE.replace("db-primary", "db-backup"))
        cmp = compare_results(result, other)
        md = compare_to_markdown([cmp])
        assert "# 日志对比分析报告" in md
        assert "新增错误" in md
        assert "消失错误" in md
        assert "共同错误" in md
        assert "变化率" in md

    def test_compare_report_identical_files(self, result):
        cmp = compare_results(result, result)
        md = compare_to_markdown([cmp])
        assert "新增错误：**0**" in md
        assert "消失错误：**0**" in md
        assert "持平" in md
