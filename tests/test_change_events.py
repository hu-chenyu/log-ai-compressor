# -*- coding: utf-8 -*-
"""部署/变更事件与错误首发的时间窗关联（跨服务/时间窗/部署事件关联）。

为什么值得单独测
----------------
"上线后开始报错"是值班排查时第一个会问的问题，而发版/重启行几乎
都是 INFO 级 —— 默认「只看 ERROR/FAIL」过滤下会被整条丢掉。所以本
特性的核心风险有两个：

1. **捕获被级别过滤吞掉**：捕获点必须在过滤 return 之前，否则功能
   在默认配置下永远为空（等于没做）；
2. **语义越界**：时间窗里的先后共现只是统计线索，绝不允许因此把
   置信档位提到 CONFIRMED —— 那会把"上线后 30 秒报错"讲成"发版
   导致报错"，而这恰是本工具最反对的自信错误。
"""
from __future__ import annotations

from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.core.evidence import result_fingerprint

# 发版（INFO 级）后 60s 开始报错 —— 典型"上线后开始报错"场景
DEPLOY_THEN_ERROR = "\n".join([
    "2024-03-01 09:00:00 INFO  deploy deployment of order-service v2.3.1 completed",
    "2024-03-01 09:01:00 ERROR failed to connect to db: connection refused",
    "2024-03-01 09:02:00 ERROR failed to connect to db: connection refused",
]) + "\n"

# 变更事件在错误首发 1 小时前（超出 30 分钟窗口）—— 不应关联
STALE_DEPLOY = "\n".join([
    "2024-03-01 08:00:00 INFO  deploy deployment of order-service v2.3.1 completed",
    "2024-03-01 09:00:00 ERROR failed to connect to db: connection refused",
]) + "\n"

# 没有任何变更事件
NO_EVENT = "\n".join([
    "2024-03-01 09:00:00 ERROR failed to connect to db: connection refused",
    "2024-03-01 09:01:00 ERROR failed to connect to db: connection refused",
]) + "\n"


class TestCapture:
    def test_captured_before_level_filter(self):
        """发版行是 INFO 级：默认过滤（ERROR/FAIL）下仍必须捕获。"""
        result = analyze_text(DEPLOY_THEN_ERROR)
        texts = [e.text for e in result.change_events]
        assert any("deploy" in t.lower() for t in texts)

    def test_event_metadata(self):
        result = analyze_text(DEPLOY_THEN_ERROR)
        ev = result.change_events[0]
        assert ev.ts is not None
        assert ev.line_no == 1
        assert ev.text

    def test_no_event_when_absent(self):
        result = analyze_text(NO_EVENT)
        assert result.change_events == []


class TestWindowLink:
    def test_links_when_error_follows_within_window(self):
        result = analyze_text(DEPLOY_THEN_ERROR)
        c = result.clusters[0]
        assert "变更" in c.change_context or "deploy" in c.change_context
        assert "后 60s" in c.change_context

    def test_no_link_outside_window(self):
        result = analyze_text(STALE_DEPLOY)
        assert result.clusters[0].change_context == ""

    def test_no_link_without_events(self):
        result = analyze_text(NO_EVENT)
        assert result.clusters[0].change_context == ""

    def test_text_rendering_mentions_change(self):
        from log_ai_compressor.export.reporters import to_markdown, to_text
        result = analyze_text(DEPLOY_THEN_ERROR)
        assert "变更关联" in to_markdown(result)
        assert "变更关联" in to_text(result)
        assert "非因果" in to_markdown(result)


class TestSemanticsBoundary:
    def test_never_raises_confidence(self):
        """时间共现绝不提升置信档位：无 Caused-by 链就不得 CONFIRMED。"""
        result = analyze_text(DEPLOY_THEN_ERROR)
        for c in result.clusters:
            assert c.root_cause_confidence != "CONFIRMED"

    def test_fingerprint_includes_change_context(self):
        """change_context 是结论的一部分，进确定性指纹。"""
        r1 = analyze_text(DEPLOY_THEN_ERROR)
        r2 = analyze_text(DEPLOY_THEN_ERROR)
        assert result_fingerprint(r1) == result_fingerprint(r2)
        # 篡改 change_context 必须改变指纹
        r1.clusters[0].change_context = "tampered"
        assert result_fingerprint(r1) != result_fingerprint(r2)
