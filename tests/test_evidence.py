# -*- coding: utf-8 -*-
"""证据充分性评估的测试（v2 差异化核心）。

重点守住三条不变量：
1. 只有 Caused-by 因果链才算 CONFIRMED —— 关键词/时间先后只是 LIKELY
2. 没有 CONFIRMED 时 must not 输出「根因」措辞
3. 缺口清单非空且能指出具体缺什么
"""
from __future__ import annotations

from pathlib import Path

from log_ai_compressor import service as S
from log_ai_compressor.core.analysis import (
    CONF_CONFIRMED,
    CONF_INSUFFICIENT,
    CONF_LIKELY,
    assess_evidence,
)
from log_ai_compressor.core.models import ErrorCluster, RunStats
from log_ai_compressor.core.pipeline import analyze_file, analyze_text
from log_ai_compressor.export.reporters import brief_summary, to_markdown, to_text

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"


def _cluster(cid: int, summary: str, priority: float,
             confidence: str) -> ErrorCluster:
    """现造一个簇。

    「造出来」而不是「从示例日志里找」：依赖语料的用例会随语料一起失效，
    而失效的形式是**静默跳过**，比测试失败更难发现。
    """
    return ErrorCluster(cluster_id=cid, template=f"tp{cid}",
                        message_template=f"tp{cid}", level="ERROR",
                        module="probe", summary=summary, count=1,
                        first_line=1, last_line=1, priority=priority,
                        root_cause_confidence=confidence,
                        is_root_cause=confidence == CONF_CONFIRMED,
                        root_cause_reason=f"probe {cid}")

# 关键词根因特征（不含 Caused-by 链）—— 只能到 LIKELY
LIKELY_LOG = """
2024-01-01 10:00:00 ERROR [db] connection refused to db-primary:5432
2024-01-01 10:00:01 ERROR [db] connection refused to db-secondary:5432
2024-01-01 10:00:02 ERROR [db] connection refused to db-primary:5433
2024-01-01 10:00:03 ERROR [db] connection refused to db-secondary:5434
2024-01-01 10:00:04 INFO [api] GET /health 200 3ms
"""

# 带 Caused-by 链 —— 唯一够得上 CONFIRMED 的形态
CONFIRMED_LOG = """
2024-01-01 10:00:00 ERROR [order] request failed
2024-01-01 10:00:01 ERROR [db] connection pool exhausted, 0/50 available
java.lang.RuntimeException: order create failed
\tat com.app.OrderService.create(OrderService.java:42)
Caused by: java.sql.SQLException: connection refused
\tat com.zaxxer.hikari.pool.HikariPool.createConnection(HikariPool.java:696)
"""


class TestConfidenceTiers:
    def test_keyword_only_is_likely_not_confirmed(self):
        """关键词/时间先后只是统计线索，绝不能标成 CONFIRMED。"""
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        ev = r.evidence
        assert ev["verdict"] in (CONF_LIKELY, CONF_INSUFFICIENT)
        assert ev["verdict"] != CONF_CONFIRMED
        assert ev["can_conclude"] is False

    def test_caused_by_chain_is_confirmed(self):
        r = analyze_text(CONFIRMED_LOG, levels=["ERROR"])
        ev = r.evidence
        assert ev["verdict"] == CONF_CONFIRMED
        assert ev["can_conclude"] is True
        assert any(c["confidence"] == CONF_CONFIRMED for c in ev["candidates"])

    def test_every_cluster_has_a_confidence(self):
        """不允许留空 —— 空串会让下游把「没结论」和「证据不足」搞混。"""
        r = analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"])
        for c in r.clusters:
            assert c.root_cause_confidence in (
                CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT)

    def test_non_root_clusters_are_insufficient(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        for c in r.clusters:
            if not c.is_root_cause:
                assert c.root_cause_confidence == CONF_INSUFFICIENT


class TestEvidenceGaps:
    def test_gaps_non_empty_when_unproven(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        assert r.evidence["gaps"], "证据不足时必须给出缺口清单"

    def test_gap_has_actionable_fields(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        for g in r.evidence["gaps"]:
            assert g["title"] and g["why"] and g["missing"]
            assert isinstance(g["weight"], int)

    def test_gaps_sorted_by_weight_desc(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        weights = [g["weight"] for g in r.evidence["gaps"]]
        assert weights == sorted(weights, reverse=True)

    def test_no_causal_chain_gap_when_unproven(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        codes = {g["code"] for g in r.evidence["gaps"]}
        assert "no_causal_chain" in codes

    def test_single_module_gap(self):
        """全部错误来自同一个模块 → 分不清是自己坏了还是被传导。"""
        text = "\n".join(
            f"2024-01-01 10:00:{i:02d} ERROR [api] request failed: {i}" for i in range(6))
        r = analyze_text(text, levels=["ERROR"])
        codes = {g["code"] for g in r.evidence["gaps"]}
        assert "single_module" in codes

    def test_no_timestamps_gap(self):
        text = "\n".join(f"ERROR [db] boom number {i}" for i in range(6))
        r = analyze_text(text, levels=["ERROR"])
        codes = {g["code"] for g in r.evidence["gaps"]}
        assert "no_timestamps" in codes

    def test_confirmed_has_no_gap_block(self):
        """已证实时不该再罗列缺口（那是噪音）。"""
        r = analyze_text(CONFIRMED_LOG, levels=["ERROR"])
        assert r.evidence["verdict"] == CONF_CONFIRMED
        assert not r.evidence["gaps"], "CONFIRMED 不应报缺口"

    def test_inputs_used_recorded(self):
        r = analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"])
        used = r.evidence["inputs_used"]
        assert used, "必须记录本次实际用到了哪些证据种类"
        assert any("时间戳" in u for u in used)


class TestReportHonesty:
    """报告措辞必须跟着判定走 —— 这才是差异化的落点。"""

    def test_markdown_says_cause_only_when_confirmed(self):
        unproven = to_markdown(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "初步定位根因" not in unproven
        assert "非因果证明" in unproven
        assert "证据充分性评估" in unproven

        proven = to_markdown(analyze_text(CONFIRMED_LOG, levels=["ERROR"]))
        assert "已定位根因" in proven
        assert "非因果证明" not in proven

    def test_brief_summary_carries_gaps(self):
        s = brief_summary(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "非因果证明" in s
        assert "缺：" in s

    def test_text_report_flags_gaps(self):
        t = to_text(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "缺：" in t

    def test_html_report_flags_gaps(self):
        from log_ai_compressor.export.reporters import to_html
        h = to_html(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "证据不足" in h

    def test_gap_names_the_missing_artifact(self):
        """缺口必须说清"补什么"，不能只说"证据不足"。"""
        s = brief_summary(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert ("堆栈" in s) or ("调用链" in s) or ("上游" in s)


class TestSerialization:
    def test_evidence_in_payload(self):
        d = S.result_to_dict(analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"]))
        assert d["evidence"]["verdict"] in (
            CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT)
        assert isinstance(d["evidence"]["gaps"], list)

    def test_cluster_confidence_in_payload(self):
        d = S.result_to_dict(analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"]))
        for c in d["clusters"]:
            assert c["root_cause_confidence"] in (
                CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT, "")

    def test_assess_evidence_direct_call(self):
        r = analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"])
        a = assess_evidence(r.clusters, r.stats)
        assert a.verdict in (CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT)
        assert a.to_dict()["verdict"] == a.verdict

    def test_headline_and_candidates_never_disagree(self):
        """headline 点名的簇，必须与候选清单的首位是**同一个**（v2.8.0 修）。

        此前 `headline` 取 `confirmed[0]`（入参顺序的第一个），而
        `candidates` 按 (-priority, -count) 排序。两者可以指向不同的簇 ——
        于是同一份报告里，标题说「根因是 A」、候选清单首位却是 B。
        端到端跑不出来（`analyze_clusters` 恰好先排序再调用），但
        `assess_evidence` 是**公开函数**，测试就直接调它。

        **场景在这里现造，不靠语料碰运气**（v2.11.0）。此前这条写的是
        「若 sample_system.log 里没有 CONFIRMED 簇就 skip」—— 那是
        **数据相关的 skip**：哪天有人改了示例日志，这个用例就静默不再
        执行，而它守护的 bug 一秒钟就能回来，且不会有任何人发现。
        一条会自己关掉自己的门禁，和没有门禁的差别只在于它还占着 CI 一行绿。
        """
        stats = RunStats()
        # 故意让「入参里第一个」与「优先级最高」不是同一个簇：
        # 旧的 `confirmed[0]` 写法会取到 A，而排序后的首位是 B。
        weak = _cluster(1, "A 低优先级但先到", priority=10.0,
                        confidence=CONF_CONFIRMED)
        strong = _cluster(2, "B 优先级最高", priority=99.0,
                          confidence=CONF_CONFIRMED)
        decoy = _cluster(3, "C 只是 LIKELY", priority=50.0,
                         confidence=CONF_LIKELY)

        forward = assess_evidence([weak, strong, decoy], stats)
        backward = assess_evidence([decoy, strong, weak], stats)
        assert forward.headline == backward.headline, (
            "只调换簇的入参顺序，headline 就变了 —— 它依赖入参顺序而非排序")
        top = forward.candidates[0]
        assert top["id"] == strong.cluster_id, (
            f"候选首位是簇 {top['id']}，而优先级最高的是 {strong.cluster_id}")
        # headline 按**摘要**点名，所以反过来断言「它点名的是同一个簇」：
        # 必须出现强簇的摘要，且**不能**出现先到但低优先级那个的摘要 ——
        # 后半句才是这条用例真正在守的东西（旧的 `confirmed[0]` 会选它）。
        assert top["summary"] in forward.headline, (
            f"headline {forward.headline!r} 与候选首位（簇 {top['id']}）"
            "不是同一个簇")
        assert weak.summary not in forward.headline, (
            f"headline {forward.headline!r} 点名了入参里第一个簇，"
            "而不是优先级最高的那个 —— 它依赖入参顺序")


class TestTheFailureMode:
    """回归：v1 会在这个场景自信地给出错误根因。"""

    TRAP = (
        "2024-01-01 10:00:00 FATAL [order] Connection is not available, "
        "request timed out after 30000ms\n"          # 真正的因，只 1 次
        + "2024-01-01 10:00:01 ERROR [db-pool] connection pool exhausted\n" * 20
    )

    def test_frequent_error_not_stated_as_root_cause(self):
        """刷屏的错误不该被当成「已定位的根因」输出。

        「这个陷阱输入不得判 CONFIRMED」是本用例的**核心断言**，
        不是一个前置条件。此前它写成了 `pytest.skip`（v2.11.0 改掉）：
        那等于把「守卫触发」翻译成「跳过」，于是产品真的开始在这类
        非因果输入上自信下结论时，CI 报的是**绿的**。
        skip 的正当用途是「环境不具备条件」，而这里恰恰是「条件被破坏」。
        """
        r = analyze_text(self.TRAP, levels=["ERROR", "FAIL"])
        ev = r.evidence
        assert ev["verdict"] != CONF_CONFIRMED, (
            "刷屏错误 + 无 Caused-by 链被判成 CONFIRMED —— "
            "这正是本项目最不能犯的错：自信的错误根因比没有答案更糟")
        assert ev["can_conclude"] is False
        assert "初步定位根因" not in to_markdown(r)
        assert "非因果证明" in to_markdown(r)
