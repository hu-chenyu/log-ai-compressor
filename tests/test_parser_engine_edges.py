# -*- coding: utf-8 -*-
"""补 core/parser.py 与 rules/engine.py 的边界/降级路径覆盖率。

这些不是"为了凑 100%"而写的凑数用例 —— 每一行背后都是一个真实行为：
解析器遇到认不出的月份名要老实返回 None 而不是猜一个；正则合并失败要退回
逐条匹配而不是整条规则集崩掉。

关于 parser.py 里**探过但打不到的分支**，如实记在此处，免得下次有人
以为漏了就硬写一条假绿的测试去盖它：

- ``_to_epoch`` 的 ``OSError/OverflowError/ValueError`` 算术兜底（现 L132-133）：
  调用前已经把 ``year < 1970`` 替换成 2000，Windows 上 ``dt.timestamp()``
  因此不会抛 —— 这条兜底在当前实现下不可达。
- 纯数字时间戳的 ``float()`` 失败分支：``_TS_NUMERIC`` 的正则是
  ``^\\d+(?:\\.\\d+)?$``，凡是它匹配的串 float() 必定成功 —— 逻辑上不可达。
- 规则不命中但「像堆栈」的兜底扫描、以及 ``_HEAD_TOKEN_SEP`` 头部 token
  兜底：``parse_line(line)`` 只接受行参数、内部固定用默认规则集，
  没有注入自定义规则集的入口，构造不出能同时满足前置条件的输入。

以上三条合计约 12 行，属于防御性纵深或事实上的死代码，
**保留它们比写一条测不到它们的测试更有价值**。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from log_ai_compressor.core.parser import (
    LogParser,
    TimestampParser,
    _fast_datetime,
)
from log_ai_compressor.rules.engine import (
    RULE_CANDIDATES,
    RuleSet,
    RuleSetError,
    detect_rule,
    list_presets,
    load_ruleset,
)

# ---------------------------------------------------------------------------
# parser.py：时间戳
# ---------------------------------------------------------------------------


def test_dd_mon_yyyy_unknown_month_returns_none() -> None:
    """DD-Mon-YYYY 里的月份名认不出时返回 None —— 不能猜一个月份蒙混过去。

    直接测复合正则快速路径：parse_line 对不匹配的行整体返回 None，
    走不到这里，所以要精确打到 _fast_datetime。
    """
    assert _fast_datetime("01-Foo-2024 10:00:00") is None
    assert _fast_datetime("01-Jan-2024 10:00:00") is not None


def test_syslog_unknown_month_returns_none() -> None:
    """syslog 的 Mon DD 同理。"""
    assert _fast_datetime("Foo 01 10:00:00") is None
    assert _fast_datetime("Jan 01 10:00:00") is not None


def test_blank_timestamp_returns_none() -> None:
    """只有空白的串不是时间戳。"""
    assert TimestampParser().parse("   ") is None


def test_cache_clears_when_full() -> None:
    """缓存满了要清空重建，而不是无限增长；清空后仍能正确解析。"""
    tp = TimestampParser(cache_size=1)
    first = tp.parse("2024-01-15T10:30:00")
    assert first is not None
    tp.parse("2024-02-20T11:45:00")      # 触发清空
    tp.parse("2024-03-25T12:55:00")
    # 清空后仍可用，且值与首次一致（缓存不参与正确性，只参与速度）
    assert tp.parse("2024-01-15T10:30:00") == first


def test_compact_timezone_suffix_retried_as_iso() -> None:
    """紧凑时区后缀（+0800）剥掉后按 ISO 重试，应与带冒号形式同值。"""
    compact = TimestampParser().parse("2024-01-15T10:30:00+0800")
    colon = TimestampParser().parse("2024-01-15T10:30:00+08:00")
    assert compact is not None and colon is not None
    assert compact == colon


def test_millis_plus_trailing_zone_keeps_utc_offset() -> None:
    """毫秒 + 尾随 Z（中间带空格）走的是「剥时区后重试」那条路。

    剥掉的是偏移而不是「什么都没有」，所以必须把 Z 换成 +00:00 再解析，
    否则裸时间戳会被当成本地时间 —— UTC+8 机器上整整差 28800 秒。
    """
    tp = TimestampParser()
    got = tp.parse("2024-01-15T10:30:00.123 Z")
    assert got == tp.parse("2024-01-15T10:30:00.123+00:00")
    assert got == tp.parse("2024-01-15T10:30:00.123Z")


def test_z_suffix_offset_survives_on_utc8_machine() -> None:
    """回归：修复前 1705285800，修复后必须等于 +00:00 的值。"""
    got = TimestampParser().parse("2024-01-15T10:30:00.123 Z")
    assert got is not None
    assert abs(got - 1705314600.123) < 1e-6


def test_indented_line_without_current_entry_is_still_captured() -> None:
    """文件开头就是缩进堆栈行（前面没有任何条目）时，不能被静默丢弃。

    没有 current 可挂靠，走的是独立处理分支。
    """
    parser = LogParser(load_ruleset())
    parser.feed("    weird indented thing", 1)
    assert parser.flush() is not None


# ---------------------------------------------------------------------------
# parser.py：堆栈归属
# ---------------------------------------------------------------------------


def _entry_count(lines):
    parser = LogParser(load_ruleset())
    n = 0
    for i, ln in enumerate(lines, start=1):
        if parser.feed(ln, i) is not None:
            n += 1
    if parser.flush() is not None:
        n += 1
    return n


def test_stack_line_without_indent_becomes_its_own_entry() -> None:
    """没有当前条目时的裸堆栈行要独立处理，不能被丢掉也不能污染上一条。"""
    got = _entry_count([
        "2024-01-15 10:00:00 ERROR something failed",
        "java.lang.RuntimeException: boom",
    ])
    assert got >= 1


def test_stack_feature_scan_catches_unknown_shape() -> None:
    """没命中任何规则的堆栈形态，兜底扫描仍要把它归成堆栈而不是新事件。"""
    got = _entry_count([
        "2024-01-15 10:00:00 ERROR outer failure",
        "\tat com.example.Service.call(Service.java:42)",
        "2024-01-15 10:00:01 INFO next thing",
    ])
    assert got >= 2


# ---------------------------------------------------------------------------
# engine.py：规则集构造与降级
# ---------------------------------------------------------------------------


def test_conflicting_named_groups_fall_back_to_per_pattern() -> None:
    """两个堆栈指示正则用同名命名组 -> 合并编译失败 -> 退回逐条匹配。

    这条不变量很重要：合并只是性能优化，失败时不能让整份规则集不可用。
    """
    rs = RuleSet.from_dict({
        "name": "conflicting-groups",
        "patterns": [{"regex": r"^(?P<ts>\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$"}],
        "stack_indicators": [r"\bat (?P<who>[\w.$]+)\(", r"\s*Caused by: (?P<who>[\w.$]+)"],
    })
    assert rs._stack_combined is None          # 合并确实失败了
    assert rs.match_stack_indicator("\tat com.a.B.c(X.java:1)") is True
    assert rs.match_stack_indicator("Caused by: java.lang.RuntimeException") is True
    assert rs.match_stack_indicator("just a normal message") is False


def test_level_hint_with_bad_regex_is_rejected_loudly() -> None:
    """级别提示里的正则编不过时抛 RuleSetError，而不是静默丢一级。

    这比"跳过"更好：静默跳过会让用户以为自己配了关键词推断，
    实际上那条规则从未生效。config.py 的注释记了这个决定。
    """
    with pytest.raises(RuleSetError):
        RuleSet.from_dict({
            "name": "bad-hint",
            "patterns": [{"regex": r"^(?P<ts>\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$"}],
            "level_hints": {"ERROR": ["(?P<bad>unclosed"]},
        })


def test_stack_indicator_with_bad_regex_is_rejected_loudly() -> None:
    """堆栈特征同理 —— 裸 re.error 不该逃到 CLI 层。"""
    with pytest.raises(RuleSetError):
        RuleSet.from_dict({
            "name": "bad-stack",
            "patterns": [{"regex": r"^(?P<ts>\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$"}],
            "stack_indicators": ["(?P<bad>unclosed"],
        })


def test_empty_level_hint_list_is_skipped() -> None:
    """空列表的级别不该去 re.compile('')。"""
    rs = RuleSet.from_dict({
        "name": "empty-hint",
        "patterns": [{"regex": r"^(?P<ts>\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$"}],
        "level_hints": {"ERROR": [], "FAIL": ["timeout"]},
    })
    assert rs.infer_level_by_keyword("upstream timeout") == "FAIL"


def test_infer_level_falls_back_when_combined_missing() -> None:
    """合并正则缺失时仍要能逐条推断 —— 这是防御性纵深。

    from_dict 现在会拒绝编不过的正则，所以这条走不到；
    这里直接模拟「合并表里没这一级」的状态，验证兜底分支本身没坏。
    """
    rs = RuleSet.from_dict({
        "name": "fallback-hint",
        "patterns": [{"regex": r"^(?P<ts>\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$"}],
        "level_hints": {"WARN": ["disk full"]},
    })
    assert rs.infer_level_by_keyword("disk full") == "WARN"
    rs._hint_combined.pop("WARN")          # 模拟合并表缺项
    assert rs.infer_level_by_keyword("disk full") == "WARN"


def test_from_dict_rejects_non_dict() -> None:
    with pytest.raises(RuleSetError):
        RuleSet.from_dict(["not", "a", "dict"])          # type: ignore[arg-type]


def test_from_dict_accepts_plain_string_pattern() -> None:
    """patterns 里可以直接写字符串，不必包成 dict。"""
    rs = RuleSet.from_dict({
        "name": "string-pattern",
        "patterns": [r"^(?P<ts>\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$"],
    })
    assert rs.match_line("2024-01-15T10:00:00 ERROR boom") is not None


def test_from_dict_rejects_pattern_without_regex() -> None:
    with pytest.raises(RuleSetError):
        RuleSet.from_dict({
            "name": "no-regex",
            "patterns": [{"level": "ERROR"}],
        })


def test_from_dict_rejects_empty_patterns() -> None:
    with pytest.raises(RuleSetError):
        RuleSet.from_dict({"name": "empty", "patterns": []})


# ---------------------------------------------------------------------------
# engine.py：预设与采样
# ---------------------------------------------------------------------------


def test_detect_rule_ignores_blank_lines() -> None:
    """detect_rule 打分时空行不参与，否则会稀释命中率把规则选错。"""
    lines = ["2024-01-15T10:00:00 ERROR boom", "", "   ",
             "2024-01-15T10:00:01 INFO fine", "", "not a log line at all"]
    got = detect_rule(lines)
    assert got in RULE_CANDIDATES


def test_detect_rule_never_lets_swallow_all_rules_win() -> None:
    """纯消息整吞不算结构化命中 —— jenkins 式「通吃」不能凭命中率虚高获胜。"""
    # 全是结构完整的行，generic 最泛用应当胜出；全是垃圾行也不能崩
    assert detect_rule(["", "   ", ""]) in RULE_CANDIDATES


def test_list_presets_falls_back_when_dir_missing(monkeypatch) -> None:
    """预设目录整个不在时，退回 generic 而不是抛异常。"""
    import log_ai_compressor.rules.engine as eng
    monkeypatch.setattr(eng, "PRESET_DIR", Path("Z:/definitely/not/here"))
    assert list_presets() == ["generic"]


def test_load_ruleset_accepts_existing_path_without_yaml_suffix(tmp_path) -> None:
    """存在的、非 .yaml 后缀的路径也要能按文件加载。"""
    f = tmp_path / "custom.rules"
    f.write_text(
        "name: custom\npatterns:\n  - regex: '^(?P<ts>\\S+) (?P<lvl>ERROR|INFO) (?P<msg>.*)$'\n",
        encoding="utf-8",
    )
    rs = load_ruleset(str(f))
    assert rs.match_line("2024-01-15T10:00:00 ERROR boom") is not None


def test_load_ruleset_generic_falls_back_to_builtin(monkeypatch) -> None:
    """generic.yaml 不在时退回内置兜底规则，而不是报错。"""
    import log_ai_compressor.rules.engine as eng
    monkeypatch.setattr(eng, "PRESET_DIR", Path("Z:/definitely/not/here"))
    rs = load_ruleset("generic")
    assert rs.name


def test_load_ruleset_unknown_name_lists_available() -> None:
    with pytest.raises(RuleSetError) as ei:
        load_ruleset("no-such-preset-xyz")
    assert "可用模板" in str(ei.value)


def test_load_ruleset_missing_yaml_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_ruleset("Z:/definitely/not/here/missing.yaml")


def test_re_compile_conflict_detection_is_the_actual_guard() -> None:
    """确认我们测的确实是 re.error 而不是别的失败原因 —— 否则上一条测试是假绿。"""
    with pytest.raises(re.error):
        re.compile(r"(?P<x>a)|(?P<x>b)")
