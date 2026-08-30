# -*- coding: utf-8 -*-
"""规则引擎单元测试：加载、编译、占位符展开、匹配、级别推断。"""
from __future__ import annotations

import pytest

from log_ai_compressor.rules.engine import (
    BUILTIN_RULESET,
    RuleSet,
    RuleSetError,
    list_presets,
    load_ruleset,
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
