# -*- coding: utf-8 -*-
"""规则引擎单元测试：加载、编译、占位符展开、匹配、级别推断。"""
from __future__ import annotations

import pytest

from log_ai_compressor.rules.engine import (
    BUILTIN_RULESET,
    RuleSet,
    RuleSetError,
    detect_rule,
    list_presets,
    load_ruleset,
    stratified_sample,
)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
class TestLoadRuleset:
    def test_list_presets_contains_builtin_templates(self):
        presets = list_presets()
        assert "generic" in presets
        assert "embedded" in presets
        assert "jenkins" in presets

    def test_load_by_name(self):
        rs = load_ruleset("embedded")
        assert rs.name == "embedded"
        assert len(rs.patterns) > 0

    def test_load_default_is_generic(self):
        assert load_ruleset(None).name == "generic"
        assert load_ruleset("").name == "generic"

    def test_load_from_yaml_file(self, tmp_path):
        custom = tmp_path / "custom.yaml"
        custom.write_text(
            "name: custom\n"
            "patterns:\n"
            "  - name: p1\n"
            "    regex: '^CUSTOM (?P<level>{LEVEL}) (?P<message>.*)$'\n",
            encoding="utf-8",
        )
        rs = load_ruleset(str(custom))
        assert rs.name == "custom"
        m = rs.match_line("CUSTOM ERROR boom")
        assert m is not None and m.group("level") == "ERROR"

    def test_load_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_ruleset("no/such/file.yaml")

    def test_load_unknown_name_raises_with_available(self):
        with pytest.raises(RuleSetError, match="generic"):
            load_ruleset("nonexistent-rule")

    def test_builtin_fallback_when_presets_missing(self):
        # BUILTIN_RULESET 必须可构建（presets 目录缺失时的兜底路径）
        rs = RuleSet.from_dict(BUILTIN_RULESET)
        assert rs.match_line("2024-01-01 10:00:00 ERROR [db] boom") is not None


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
class TestRuleSetValidation:
    def test_empty_patterns_rejected(self):
        with pytest.raises(RuleSetError, match="pattern"):
            RuleSet.from_dict({"name": "x", "patterns": []})

    def test_invalid_regex_rejected(self):
        with pytest.raises(RuleSetError, match="非法"):
            RuleSet.from_dict({"name": "x", "patterns": [{"regex": "(unclosed"}]})

    def test_level_placeholder_expanded(self):
        rs = load_ruleset("generic")
        # {LEVEL} 展开后应能匹配任一标准级别令牌
        m = rs.match_line("2024-01-01 10:00:00 WARNING disk almost full")
        assert m is not None
        assert m.group("level") == "WARNING"


# ---------------------------------------------------------------------------
# 匹配行为
# ---------------------------------------------------------------------------
class TestMatchLine:
    @pytest.fixture()
    def generic(self):
        return load_ruleset("generic")

    def test_iso_with_bracket_module(self, generic):
        m = generic.match_line("2024-01-01 10:00:00,123 ERROR [db] Connection refused")
        assert m.group("timestamp") == "2024-01-01 10:00:00,123"
        assert m.group("level") == "ERROR"
        assert m.group("module") == "db"
        assert "Connection refused" in m.group("message")

    def test_bracket_iso_timestamp(self, generic):
        """修复缺陷R59：方括号 ISO 时间戳（Jenkins/CI 构建日志）。"""
        m = generic.match_line(
            "[2026-09-04T06:12:12.244Z] error: could not apply 84b4dc4")
        assert m is not None, "方括号 ISO 时间戳行应命中 bracket_iso 模式"
        assert m.group("timestamp") == "2026-09-04T06:12:12.244Z"
        assert m.group("level").upper() == "ERROR"
        assert "could not apply" in m.group("message")

    def test_bracket_iso_without_level(self, generic):
        """方括号时间戳 + 无级别消息：级别缺省（关键词推断兜底）。"""
        m = generic.match_line("[2026-09-04T06:12:12.244Z] + git fetch ssh://x")
        assert m is not None
        assert m.group("timestamp") == "2026-09-04T06:12:12.244Z"
        assert (m.groupdict().get("level") or "") == ""

    def test_iso_with_module_token(self, generic):
        m = generic.match_line("2024-01-01T10:00:00 INFO auth - user login ok")
        assert m.group("level") == "INFO"
        # 模块未用括号包裹时由解析器后处理推断，此处 message 含模块词
        assert "auth" in m.group("message")

    def test_gcc_lowercase_level(self, generic):
        m = generic.match_line("src/main.c:42:15: error: expected ';' before '}'")
        assert m is not None
        assert m.group("module").endswith("main.c")
        assert m.group("level") == "error"

    def test_time_only_prefix(self, generic):
        m = generic.match_line("10:00:00.123 [auth] ERROR token expired")
        assert m is not None
        assert m.group("module") == "auth"
        assert m.group("level") == "ERROR"

    def test_last_pattern_cache_optimization(self, generic):
        # 热路径优化：同格式连续行应命中缓存索引且结果正确
        line = "10:00:00.123 [auth] ERROR token expired"
        first = generic.match_line(line)
        second = generic.match_line(line)
        assert first.group("level") == second.group("level") == "ERROR"
        assert generic._last_pattern >= 0

    def test_stack_indicator(self):
        rs = load_ruleset("embedded")
        assert rs.match_stack_indicator("  #2  0x08004a1b in im_clk_a400_enable")
        assert rs.match_stack_indicator("	at com.foo.Bar.run(Bar.java:10)")
        assert not rs.match_stack_indicator("INFO normal line")

    def test_level_hint_inference(self):
        rs = load_ruleset("jenkins")
        assert rs.infer_level_by_keyword("BUILD FAILURE in 30s") == "FAIL"
        assert rs.infer_level_by_keyword("[2024-01-01T00:00:00Z] [err] compile fail") == "ERROR"
        assert rs.infer_level_by_keyword("plain output line") is None

    def test_jenkins_ts_channel_message(self):
        rs = load_ruleset("jenkins")
        m = rs.match_line("[2024-01-01T12:00:00.123Z] [out] echo hello")
        assert m is not None
        assert m.group("timestamp") == "2024-01-01T12:00:00.123Z"
        # 前导空白行不应匹配（交给续行/堆栈逻辑处理）
        assert rs.match_line("   indented continuation") is None


# ---------------------------------------------------------------------------
# 修复缺陷#9：合并正则加速（与逐条匹配语义一致性）
# ---------------------------------------------------------------------------
class TestCombinedRegexEquivalence:
    """合并正则必须与逐条匹配在判定结果上完全一致。"""

    @pytest.mark.parametrize("name", ["generic", "embedded", "jenkins"])
    def test_stack_indicator_equivalence(self, name):
        rs = load_ruleset(name)
        assert rs._stack_combined is not None, "堆栈特征应已合并"
        samples = [
            "\tat com.foo.Bar.run(Bar.java:10)",
            "Traceback (most recent call last):",
            "Caused by: java.lang.NullPointerException",
            "  #2  0x08004a1b in im_clk_a400_enable",
            '  File "app.py", line 88, in handle',
            "raise ValueError('boom')",
            "Backtrace: 0x4a1b2c",
            "java.net.ConnectException: Connection refused",
            "INFO normal line without stack",
            "plain text 123",
            "",
        ]
        for line in samples:
            combined = rs._stack_combined.search(line) is not None
            sequential = any(p.search(line) for p in rs.stack_indicators)
            assert combined == sequential, f"{name} 堆栈判定不一致: {line!r}"

    @pytest.mark.parametrize("name", ["generic", "embedded", "jenkins"])
    def test_level_hint_equivalence(self, name):
        rs = load_ruleset(name)
        samples = [
            "BUILD FAILURE in 30s",
            "[err] compile fail",
            "terminate called after throwing an instance",
            "uncaught exception in worker",
            "warning: disk almost full",
            "segfault at 0x4a1b2c",
            "assert failed: rate mismatch",
            "plain output line",
            "routine heartbeat ok",
        ]
        for text in samples:
            fast = rs.infer_level_by_keyword(text)
            sequential = None
            for level in ("ERROR", "FAIL", "WARN"):
                if any(p.search(text) for p in rs.level_hints.get(level, ())):
                    sequential = level
                    break
            assert fast == sequential, f"{name} 级别推断不一致: {text!r}"

    def test_hint_combined_built_for_generic(self):
        rs = load_ruleset("generic")
        # 级别提示已按级别合并（修复缺陷R18：FATAL 提示已删除，
        # ERROR/FAIL/WARN 各一条）
        for level in ("ERROR", "FAIL", "WARN"):
            assert level in rs._hint_combined, f"{level} 提示未合并"
        assert "FATAL" not in rs._hint_combined, \
            "修复缺陷R18：FATAL 关键词提示应已删除（防 -Wfatal-errors 误判）"


# ---------------------------------------------------------------------------
# 优化缺陷R71：规则自动识别（分层采样 + 打分选择）
# ---------------------------------------------------------------------------
class TestDetectRule:
    _EMBEDDED_LOG = [
        "[  123.456] [ERR ] [im_pll] clk config failed",
        "[  123.789] [WARN] [im_div] rate mismatch",
        "12:00:01.123 FAIL test_case_clk_freq",
    ]
    _GENERIC_LOG = [
        "2024-01-01 10:00:00 ERROR [db] connection refused",
        "2024-01-01 10:00:01 WARN [cache] memory above 90%",
        "2024-01-01 10:00:02 ERROR [api] request failed",
    ]
    _JENKINS_STYLE_LOG = [
        "[2026-09-04T06:12:12.244Z] error: could not apply 84b4dc4",
        "[2026-09-04T06:12:16.697Z] Finished: FAILURE",
        "[2026-09-04T06:12:10.000Z] remote: warning: subject >50",
    ]

    def test_embedded_log_detects_embedded(self):
        """相对秒+模块格式：embedded 命中率/时间戳/模块全面领先。"""
        assert detect_rule(self._EMBEDDED_LOG) == "embedded"

    def test_iso_log_detects_generic(self):
        assert detect_rule(self._GENERIC_LOG) == "generic"

    def test_bracket_iso_tiebreak_generic(self):
        """方括号 ISO（generic 与 jenkins 同分）→ 兜底 generic。"""
        assert detect_rule(self._JENKINS_STYLE_LOG) == "generic"

    def test_garbage_fallback_generic(self):
        """全都不命中（0 分同分）→ 兜底 candidates 首位 generic。"""
        assert detect_rule(["{{{{随机文本}}}}", "!!!!"]) == "generic"

    def test_stratified_sample_covers_head_mid_tail(self):
        lines = [f"line-{i}" for i in range(1000)]
        sample = stratified_sample(lines, 150)
        assert len(sample) == 150
        assert sample[0] == "line-0", "应含头部"
        assert "line-999" in sample, "应含尾部"
        mid_idx = (1000 - 50) // 2
        assert f"line-{mid_idx}" in sample, "应含中部"

    def test_stratified_sample_small_input(self):
        lines = ["a", "b"]
        assert stratified_sample(lines) == lines
