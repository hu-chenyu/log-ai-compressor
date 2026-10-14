# -*- coding: utf-8 -*-
"""结构化日志（JSON / logfmt）与调用链重建的回归测试。

两个功能各自守一条底线：
  A 结构化 —— **认领要准**（别名多、命名空间多），**误判要零**（普通
    文本里出现 key=value 是日常，两条就吞掉整行是最危险的失败）；
  B 调用链 —— 能重建、能关掉自己申报的缺口，但**绝不能因此升档**。
    链路只证明顺序。
"""
from __future__ import annotations

import pytest

from log_ai_compressor.core.models import LogEntry
from log_ai_compressor.core.parser import LogParser
from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.core.structured import parse_json, parse_line, parse_logfmt
from log_ai_compressor.core.traces import build_traces
from log_ai_compressor.rules.engine import load_ruleset

RULES = load_ruleset("generic")


def _entries(text: str):
    """跑一遍解析器，返回全部条目。"""
    parser = LogParser(RULES)
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        e = parser.feed(line, i)
        if e is not None:
            out.append(e)
    last = parser.flush()
    if last is not None:
        out.append(last)
    return out


# ---------------------------------------------------------------------------
# A 结构化日志
# ---------------------------------------------------------------------------
class TestJsonParsing:
    @pytest.mark.parametrize("raw,msg,level", [
        ('{"level":"ERROR","message":"db down"}', "db down", "ERROR"),
        ('{"severity":"WARN","msg":"slow"}', "slow", "WARN"),
        ('{"lvl":"INFO","message":"ok"}', "ok", "INFO"),
        ('{"severity_text":"fatal","message":"dead"}', "dead", "ERROR"),
    ])
    def test_level_and_message_aliases(self, raw, msg, level):
        e = _entries(raw)[0]
        assert e.message == msg
        assert e.level == level

    @pytest.mark.parametrize("raw,svc", [
        ('{"level":"INFO","message":"x","service.name":"api"}', "api"),
        ('{"level":"INFO","message":"x","logger":"svc.b"}', "svc.b"),
        ('{"level":"INFO","message":"x","app_name":"worker"}', "worker"),
    ])
    def test_service_aliases(self, raw, svc):
        assert _entries(raw)[0].module == svc

    def test_otel_dot_namespace_is_flattened(self):
        raw = ('{"@timestamp":"2026-10-14T09:00:00.100Z","log.level":"ERROR",'
               '"log.logger":"order.svc","message":"boom"}')
        e = _entries(raw)[0]
        assert e.level == "ERROR" and e.module == "order.svc"
        assert e.timestamp is not None

    def test_nested_namespace_is_flattened(self):
        raw = '{"log":{"level":"ERROR"},"service":{"name":"api"},"message":"x"}'
        e = _entries(raw)[0]
        assert e.level == "ERROR" and e.module == "api"

    def test_trace_and_request_ids(self):
        raw = ('{"level":"ERROR","message":"x","trace_id":"a1b2c3d4e5f6",'
               '"request_id":"r-998877","span_id":"sp-1"}')
        e = _entries(raw)[0]
        assert e.trace_id == "a1b2c3d4e5f6"
        assert e.request_id == "r-998877"
        assert e.correlation_id == "r-998877"      # request_id 优先

    def test_unclaimed_fields_are_kept(self):
        raw = '{"level":"ERROR","message":"x","http.status":503,"user":"u1"}'
        e = _entries(raw)[0]
        assert e.fields.get("http.status") == 503
        assert e.fields.get("user") == "u1"

    def test_exception_becomes_stack(self):
        raw = '{"level":"ERROR","message":"failed","exception.type":"SQLException"}'
        e = _entries(raw)[0]
        assert any("SQLException" in s for s in e.stack)

    def test_structured_flag_set(self):
        assert _entries('{"level":"ERROR","message":"x"}')[0].structured is True

    @pytest.mark.parametrize("bad", [
        '{"level":"ERROR","message":"truncated',     # 半行 JSON
        '{"level":',                                # 残缺
        '[1,2,3]',                                  # 数组不是对象
        '{}',                                       # 空对象
        '{"data":[1,2,3]}',                         # 既无 message 也无 level
    ])
    def test_malformed_json_degrades_to_text_path(self, bad):
        """坏 JSON 必须退化成纯文本路径，绝不能抛异常。"""
        assert parse_json(bad) is None
        assert _entries(bad)          # 不崩

    def test_error_object_does_not_crash(self):
        assert parse_json('{"level":{"nested":1},"message":{"x":2}}') is None


class TestLogfmtParsing:
    def test_basic(self):
        e = _entries('level=ERROR service=api msg=db down')[0]
        assert e.level == "ERROR" and e.module == "api"
        assert "db down" in e.message

    def test_quoted_value_with_spaces(self):
        e = _entries('level=ERROR msg="connection refused by peer" svc=db')[0]
        assert "connection refused by peer" in e.message

    def test_escapes_in_quotes(self):
        p = parse_logfmt(r'level=ERROR msg="say \"hi\" now"')
        assert p is not None and 'say "hi" now' in p.message

    @pytest.mark.parametrize("plain", [
        "connect failed after retries=3 timeout=30",     # 消息里带 kv
        "user id=12345 logged in",
        "GET /api/v1/users?id=42 returned 200 in 15ms",
        "2026-10-14 10:00:00 ERROR plain message no kv here",
    ])
    def test_plain_text_is_not_logfmt(self, plain):
        """误判是最危险的失败：整行被吞成字段，message 就丢了。"""
        assert parse_line(plain) is None
        e = _entries(plain)[0]
        assert e.structured is False


class TestStructuredEndToEnd:
    def test_json_log_is_clustered_by_real_level(self):
        """结构化日志必须按**字段**判级别，而不是按消息文本猜。"""
        log = ('{"@timestamp":"2026-10-14T09:00:00.100Z","level":"ERROR",'
               '"service.name":"inv","message":"db refused"}\n'
               '{"@timestamp":"2026-10-14T09:00:01.100Z","level":"ERROR",'
               '"service.name":"inv","message":"db refused"}\n'
               '{"@timestamp":"2026-10-14T09:00:02.100Z","level":"INFO",'
               '"service.name":"inv","message":"all good"}\n')
        r = analyze_text(log, levels=["ERROR"])
        assert r.stats.entry_lines == 3
        # INFO 那条不该进错误簇 —— 如果按文本猜，"db refused" 会让它变 ERROR
        assert all("all good" not in c.summary for c in r.clusters)

    def test_timestamps_parsed_from_json_fields(self):
        log = ('{"@timestamp":"2026-10-14T09:00:00.100Z","level":"ERROR","message":"a"}\n'
               '{"@timestamp":"2026-10-14T09:00:05.500Z","level":"ERROR","message":"a"}\n')
        r = analyze_text(log)
        assert r.stats.time_start is not None and r.stats.time_end is not None
        assert r.stats.time_end > r.stats.time_start

    def test_evidence_still_verifiable_for_json_logs(self, tmp_path):
        """结构化日志的证据引用同样必须能重读原文复核。"""
        p = tmp_path / "s.jsonl"
        p.write_text(
            '{"@timestamp":"2026-10-14T09:00:00.100Z","level":"ERROR",'
            '"service.name":"inv","message":"db refused"}\n'
            '{"@timestamp":"2026-10-14T09:00:05.100Z","level":"ERROR",'
            '"service.name":"inv","message":"db refused"}\n',
            encoding="utf-8")
        from log_ai_compressor import service as S
        from log_ai_compressor.core.pipeline import analyze_files
        r = analyze_files([p], context_lines=2)
        assert S.verify_result(r)["verdict"] == "VERIFIED"


# ---------------------------------------------------------------------------
# B 调用链
# ---------------------------------------------------------------------------
def _entry(line_no, cid, svc, msg, level="INFO", ts=None):
    return LogEntry(line_no=line_no, level=level, message=msg, module=svc,
                    timestamp=ts, request_id=cid, trace_id="",
                    structured=True)


class TestTraceBuilding:
    def test_cross_service_chain_in_time_order(self):
        chains = build_traces([
            _entry(3, "r-abc123", "api", "aborted", "ERROR", 300.0),
            _entry(1, "r-abc123", "gw", "received", "INFO", 100.0),
            _entry(2, "r-abc123", "inv", "db refused", "ERROR", 200.0),
        ])
        assert len(chains) == 1
        c = chains[0]
        assert c.cross_service
        assert c.services == ["gw", "inv", "api"]
        assert [s.line_no for s in c.steps] == [1, 2, 3]
        assert c.first_error.line_no == 2

    def test_order_falls_back_to_line_number(self):
        chains = build_traces([
            _entry(2, "r-abc123", "b", "second"),
            _entry(1, "r-abc123", "a", "first"),
        ])
        assert [s.line_no for s in chains[0].steps] == [1, 2]

    def test_short_numeric_ids_are_not_correlation_keys(self):
        """id=42 这种到处都是，用它建链只会产出一堆无意义的"链路"。"""
        chains = build_traces([
            _entry(1, "42", "a", "x"),
            _entry(2, "42", "b", "y"),
        ])
        assert chains == []

    def test_single_service_without_error_is_dropped(self):
        chains = build_traces([
            _entry(1, "r-abc123", "api", "started"),
            _entry(2, "r-abc123", "api", "finished"),
        ])
        assert chains == []

    def test_single_service_with_error_is_kept(self):
        chains = build_traces([
            _entry(1, "r-abc123", "api", "started"),
            _entry(2, "r-abc123", "api", "job failed", "ERROR"),
        ])
        assert len(chains) == 1 and not chains[0].cross_service

    def test_narrative_marks_first_error(self):
        chains = build_traces([
            _entry(1, "r-abc123", "a", "ok"),
            _entry(2, "r-abc123", "b", "boom", "ERROR"),
        ])
        assert "首个失败点 L2" in chains[0].narrative()


class TestEcosystemFieldNames:
    """各生态实际使用的字段名（键统一小写，原键由 _flatten 折叠）。

    这些不是凭记忆写的别名，是实测形态：Serilog/ECS 的 PascalCase
    （`TraceId`/`CorrelationId`）、OTel 的点号命名空间与 body、
    Grafana 的 `traceID`、structlog 的 `event`。任一条回归都意味着
    「真实项目的结构化日志认不出来字段」—— 结构化支持的全部价值就在
    这里。
    """

    @pytest.mark.parametrize("raw,expect", [
        # OTel Logs 数据模型
        ('{"body":"boom","severity_text":"ERROR","trace_id":"aaaa1111bbbb",'
         '"span_id":"cccc","time_unix_nano":"1750000000000000000"}',
         dict(message="boom", level="ERROR", trace="aaaa1111bbbb")),
        # ECS 点号命名空间 + @timestamp
        ('{"@timestamp":"2026-10-14T09:00:00Z","log.level":"ERROR",'
         '"service.name":"api","trace.id":"tdddeeee1111","event.original":"x"}',
         dict(message="x", level="ERROR", service="api", trace="tdddeeee1111")),
        # Serilog / zap 的 PascalCase（折叠为小写后命中）
        ('{"Timestamp":"2026-10-14T09:00:00Z","Level":"Error",'
         '"Message":"kaputt","TraceId":"9999aaaa8888",'
         '"CorrelationId":"req-1234"}',
         dict(message="kaputt", level="ERROR", trace="9999aaaa8888",
              request="req-1234")),
        # Grafana Loki JSON line
        ('{"level":"error","msg":"loki line","traceID":"loki1234id"}',
         dict(message="loki line", level="ERROR", trace="loki1234id")),
        # Python structlog
        ('{"event":"structlog event","level":"warning","logger":"app.mod"}',
         dict(message="structlog event", level="WARN", service="app.mod")),
        # winston / pino 常见形态
        ('{"level":"info","msg":"started","service":"api","req_id":"r-99"}',
         dict(message="started", level="INFO", service="api", request="r-99")),
    ])
    def test_recognises_ecosystem_field_names(self, raw, expect):
        """在 LogEntry 层断言（级别归一发生在解析层，不在字段映射层）。

        ParsedFields.level 保留原始串（"info"），normalize_level 之后才是
        "INFO" —— 在错误的层断言会得到一个测试全红但功能正确的假象。
        """
        parser = LogParser(load_ruleset("generic"))
        parser.feed(raw, 1)
        # feed 返回的是**上一个**条目（流式接口的语义）；当前条目要 flush 取
        entry = parser.flush()
        assert entry is not None, raw
        if "message" in expect:
            assert expect["message"] in entry.message, entry.message
        if "level" in expect:
            assert entry.level == expect["level"], entry.level
        if "service" in expect:
            assert entry.module == expect["service"], entry.module
        if "trace" in expect:
            assert entry.trace_id == expect["trace"], entry.trace_id
        if "request" in expect:
            assert entry.request_id == expect["request"], entry.request_id

    def test_dot_namespace_ecs_survives_nested_form(self):
        """真嵌套与点号扁平两种写法必须产出同一结果。"""
        flat = parse_line('{"log.level":"ERROR","service.name":"api"}')
        nest = parse_line('{"log":{"level":"ERROR"},"service":{"name":"api"}}')
        assert (flat.level, flat.service) == ("ERROR", "api")
        assert (nest.level, nest.service) == ("ERROR", "api")

    def test_b3_headers_recognised(self):
        """B3 传播头（Zipkin 系）也是关联键。"""
        p = parse_line('{"level":"ERROR","message":"x","X-B3-TraceId":"b3aaaabbb",'
                       '"X-B3-SpanId":"b3ccc"}')
        assert p.trace_id == "b3aaaabbb" and p.span_id == "b3ccc"

    def test_wrong_service_key_does_not_leak_into_message(self):
        """扩表不能反过来伤害消息：`name` 这类泛键不许顶掉真正的 message。"""
        p = parse_line('{"level":"ERROR","message":"the real message",'
                       '"name":"some-container","host":"pod-1"}')
        assert p.message == "the real message"


class TestTraceInAnalysis:
    LOG = (
        '{"@timestamp":"2026-10-14T09:00:00.100Z","level":"INFO",'
        '"service.name":"gateway","message":"received","trace_id":"t-aaa111"}\n'
        '{"@timestamp":"2026-10-14T09:00:00.200Z","level":"ERROR",'
        '"service.name":"inventory","message":"db refused","trace_id":"t-aaa111"}\n'
        '{"@timestamp":"2026-10-14T09:00:00.300Z","level":"ERROR",'
        '"service.name":"gateway","message":"aborted","trace_id":"t-aaa111"}\n'
        '{"@timestamp":"2026-10-14T09:00:00.400Z","level":"ERROR",'
        '"service.name":"gateway","message":"aborted","trace_id":"t-aaa111"}\n'
    )

    def test_chain_attached_to_clusters(self):
        r = analyze_text(self.LOG, context_lines=2)
        assert r.traces
        assert r.traces[0].cross_service
        assert any(c.traces for c in r.clusters)

    def test_chain_closes_its_own_evidence_gap(self):
        """核心闭环：工具自己申报的缺口，被它自己补掉。"""
        r = analyze_text(self.LOG, context_lines=2)
        codes = [g["code"] for g in r.evidence["gaps"]]
        assert "no_correlation_id" not in codes
        assert any("调用链" in u for u in r.evidence["inputs_used"])

    def test_gap_still_reported_when_no_ids(self):
        """没有关联键时必须照实申报 —— 缺口清单不能是固定话术。"""
        log = ("2026-10-14 09:00:00 ERROR db connection refused\n"
               "2026-10-14 09:00:01 ERROR db connection refused\n")
        r = analyze_text(log, context_lines=2)
        codes = [g["code"] for g in r.evidence["gaps"]]
        assert "no_correlation_id" in codes

    def test_chain_never_upgrades_to_confirmed(self):
        """**越界防线**：链路只证明顺序，不证明因果。

        这是整个「拿不出因果链就说不知道」立场最容易破的一格 ——
        「链路很完整，所以根因肯定是链上第一个」。
        """
        r = analyze_text(self.LOG, context_lines=2)
        assert r.traces and r.traces[0].cross_service
        assert all(c.root_cause_confidence != "CONFIRMED" for c in r.clusters)
        assert r.evidence["verdict"] in ("LIKELY", "INSUFFICIENT")

    def test_chain_does_not_change_determinism_contract(self):
        a = analyze_text(self.LOG, context_lines=2)
        b = analyze_text(self.LOG, context_lines=2)
        from log_ai_compressor.core.evidence import result_fingerprint
        assert result_fingerprint(a) == result_fingerprint(b)


# ---------------------------------------------------------------------------
# C 产品面：报告与 API
# ---------------------------------------------------------------------------
class TestTraceProductSurface:
    def test_markdown_report_has_trace_section(self):
        from log_ai_compressor import service as S
        r = analyze_text(TestTraceInAnalysis.LOG, context_lines=2)
        md = S.export_text(r, "md")
        assert "## 四、调用链" in md
        assert "不是因果关系" in md          # 措辞必须守住语义边界

    def test_text_report_has_trace_section(self):
        from log_ai_compressor import service as S
        r = analyze_text(TestTraceInAnalysis.LOG, context_lines=2)
        assert "[调用链]" in S.export_text(r, "txt")

    def test_plain_text_log_has_no_trace_section(self):
        from log_ai_compressor import service as S
        r = analyze_text("2026-10-14 09:00:00 ERROR db refused\n"
                         "2026-10-14 09:00:01 ERROR db refused\n", context_lines=2)
        assert "## 四、调用链" not in S.export_text(r, "md")

    def test_api_exposes_traces_and_structured_flags(self):
        from log_ai_compressor import service as S
        r = analyze_text(TestTraceInAnalysis.LOG, context_lines=2)
        d = S.result_to_dict(r, with_samples=True)
        assert d["traces"] and d["traces"][0]["correlation_id"]
        c = next(x for x in d["clusters"] if x["sample"])
        assert c["sample"]["structured"] is True
        assert c["sample"]["trace_id"]
        assert c["traces"]
