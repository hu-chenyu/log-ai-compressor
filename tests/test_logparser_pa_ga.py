# -*- coding: utf-8 -*-
"""scripts/eval_logparser_pa_ga.py 的测试 —— **别人的指标**，得测得特别狠。

为什么这批测试比别的更严
------------------------
PA / GA 不是本项目发明的，是 LogParser（Jiang et al. 2019）定的，Drain /
Drain3 / LILAC / LLMParser / LibreLog 全部在用。所以这里的失败**不是**
「我们的分数不好看」，而是「我们连人家的指标都跑错了」—— 那属于把
别人的尺子量错了，读的人会以为我们的 61% 是 Loghub 意义上的 61%。

四条最容易跑错的：

1. **GA 的条件 (b)**：「一组包含该 EventId 的**全部**出现」才算对。
   少写这一条，聚类越碎分数越高 —— 而越碎恰恰是聚类做坏了的形态。
2. **分母**：是**有标注的行数**，不是预测侧行数。写成后者，漏解析的行
   会自动消失，分数凭空变高。
3. **未解析的行算错**，不能从分母里剔掉。
4. **模板归一化必须两侧同一个函数**：LibreLog 两侧用的是两套不同的清洗，
   照抄会引入「哪边被洗得更干净」的偏袒。本项目两侧共用一个。

口径来源（两份独立实现互相印证，测试里也钉住出处）：
  * logpai/logparser  logparser/utils/evaluator.py
    @10e31f3a1ff2bb783508a27efb24eb868b97287d
  * zeyang919/LibreLog parser/accuracy.py
    @36c8a45d6e7d1f6486f88767ca02dc3729463fb6
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "eval_logparser_pa_ga", ROOT / "scripts" / "eval_logparser_pa_ga.py")
assert _SPEC and _SPEC.loader
pa_ga = importlib.util.module_from_spec(_SPEC)
sys.modules["eval_logparser_pa_ga"] = pa_ga
_SPEC.loader.exec_module(pa_ga)


# ---------------------------------------------------------------------------
# clean_content —— LibreLog 同名函数的逐行移植
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("a,b.c", "abc"),                       # 去 , 和 .
    ("Failed to <*> now", "Failedtonow"),   # 含 <*> 的整段清空
    ("a b  c", "abc"),                      # 空格不保留
    ("", ""),
])
def test_clean_content_matches_librelog(raw, want):
    assert pa_ga.clean_content(raw) == want


def test_clean_content_is_applied_symmetrically():
    """两侧共用一个函数 —— 不允许为了某一边「洗得更干净」而分叉。

    这是本项目相对 LibreLog 的一处**有意的偏离**（它两侧各洗各的），
    偏离的理由必须被守住，否则下一个人「照抄官方实现」就会把它改回去。

    判据认的是**模块里实际存在的函数**，不是源码文本。第一版写的是
    「源码里不能出现 clean_regex_content 这个词」，结果当场把自己判红了
    —— 因为这个文件顶部的 docstring 恰恰要解释为什么**不**用它。
    判据指错了对象：它读的是「这个文件怎么谈论它」，不是「它存不存在」。
    """
    assert not hasattr(pa_ga, "clean_regex_content"), (
        "不得引入 LibreLog 那套非对称的预测侧清洗（它只对正则模板有意义）")
    assert sum(1 for n in dir(pa_ga) if n.startswith("clean_")) == 1, (
        "只允许一个清洗函数")


# ---------------------------------------------------------------------------
# GA
# ---------------------------------------------------------------------------
GT = {1: "E1", 2: "E1", 3: "E2", 4: "E2", 5: "E3"}


def test_ga_perfect_grouping():
    ga, n = pa_ga.grouping_accuracy(GT, {1: "a", 2: "a", 3: "b", 4: "b", 5: "c"}, 5)
    assert n == 5 and ga == 1.0


def test_ga_condition_b_blocks_partial_groups():
    """E2 只被聚到一半 -> 整组 0 分。少了这一条，簇越碎分越高。"""
    ga, n = pa_ga.grouping_accuracy(GT, {1: "a", 2: "a", 3: "b"}, 5)
    assert n == 2 and ga == pytest.approx(0.4)


def test_ga_condition_a_blocks_mixed_groups():
    """一个预测组里混了两个地真事件 -> **整组** 0 分。

    这里把 E3（第 5 行）塞进 E1 那个组里，于是：
      组 a = {1,2,5} -> 跨了 E1 与 E3 -> 条件 (a) 不成立 -> 0 分
      组 b = {3,4}   -> E2 且完整      -> +2
    所以是 2/5，而不是「只扣掉混进来的那一行」的 4/5 ——
    判定是**按组**的，一组脏了整组都不算，这条是最容易写松的地方。
    """
    ga, n = pa_ga.grouping_accuracy(
        GT, {1: "a", 2: "a", 3: "b", 4: "b", 5: "a"}, 5)
    assert n == 2 and ga == pytest.approx(0.4)


def test_ga_denominator_is_annotated_lines_not_parsed_lines():
    """分母是**标注总行数**。写成预测侧行数，漏解析的行就凭空消失了。"""
    ga, _ = pa_ga.grouping_accuracy(GT, {1: "a", 2: "a", 3: "b", 4: "b"}, 10)
    assert ga == pytest.approx(0.4)


def test_ga_no_predictions_scores_zero():
    ga, n = pa_ga.grouping_accuracy(GT, {}, 5)
    assert ga == 0.0 and n == 0


def test_ga_empty_denominator_is_zero_not_crash():
    assert pa_ga.grouping_accuracy({}, {}, 0)[0] == 0.0


# ---------------------------------------------------------------------------
# PA
# ---------------------------------------------------------------------------
def test_pa_exact_template_match():
    gt = {1: "Failed to connect to <*>", 2: "Failed to connect to <*>"}
    pa, n = pa_ga.parsing_accuracy(gt, dict(gt), 2)
    assert pa == 1.0 and n == 2


def test_pa_missing_line_counts_as_wrong_and_stays_in_denominator():
    gt = {1: "a <*>", 2: "b <*>"}
    pa, n = pa_ga.parsing_accuracy(gt, {1: "a <*>"}, 2)
    assert pa == 0.5 and n == 1


def test_pa_equal_after_shared_cleaning():
    """两侧的标点差异由 clean_content 吸收 —— 这正是共用一个函数的效果。"""
    assert pa_ga.parsing_accuracy({1: "a, b <*>"}, {1: "a b <*>"}, 1)[0] == 1.0


def test_pa_empty_prediction_is_wrong():
    assert pa_ga.parsing_accuracy({1: "boom"}, {1: ""}, 1)[0] == 0.0


def test_pa_wrong_template_is_wrong():
    assert pa_ga.parsing_accuracy({1: "connect <*>"}, {1: "disconnect <*>"}, 1)[0] == 0.0


# ---------------------------------------------------------------------------
# 模板适配：占位符折叠
# ---------------------------------------------------------------------------
def test_entry_template_collapses_masked_runs_into_one_placeholder():
    """IP 地址在 Loghub 地真值里是**一个** `<*>`，我们也必须折叠成一个。"""
    t = pa_ga.entry_template("Failed to connect to 10.0.0.1 from 10.0.0.2")
    assert t == "Failed to connect to <*> from <*>", t


def test_entry_template_keeps_literal_words():
    assert pa_ga.entry_template("Digest: done") == "Digest: done"


def test_entry_template_keeps_hex_and_paths_as_one_placeholder():
    t = pa_ga.entry_template("bad handle 0xdeadbeef at /var/log/app/x.log")
    assert "<*>" in t and "deadbeef" not in t and "/var/log" not in t


# ---------------------------------------------------------------------------
# 脚本自身
# ---------------------------------------------------------------------------
def test_self_check_passes():
    """`--self-check` 是一组手算期望值，它自己红了就等于口径没人验过。"""
    assert pa_ga.self_check() == 0


def test_two_reference_implementations_are_cited():
    """口径必须写明出自哪两份实现，且两份都在。

    只写一份就退化成了「读了一份就信」。两份独立实现互相印证才是核实。
    """
    assert "logpai/logparser" in pa_ga.LOGPARSER_REF
    assert "10e31f3a1ff2bb783508a27efb24eb868b97287d" in pa_ga.LOGPARSER_REF
    assert "LibreLog" in pa_ga.LIBRELOG_REF
    assert "36c8a45d6e7d1f6486f88767ca02dc3729463fb6" in pa_ga.LIBRELOG_REF


def test_naming_caveat_is_documented():
    """logparser 自己的代码把这个值打印成 Parsing_Accuracy，其实是 GA。

    这个坑不写下来，下一个照着 logparser 读代码的人会以为这里标错了，
    然后「好心」把 PA/GA 调换过来 —— 而那样出来的数字毫无意义。
    """
    src = (ROOT / "scripts" / "eval_logparser_pa_ga.py").read_text(
        encoding="utf-8")
    assert "evaluator.py" in src and "Parsing_Accuracy" in src


def test_raw_and_content_both_measured():
    """两种输入都必须测，只测一种等于挑了个好看的报。"""
    src = (ROOT / "scripts" / "eval_logparser_pa_ga.py").read_text(
        encoding="utf-8")
    assert 'for source in ("raw", "content")' in src


def test_control_arm_is_present():
    """没有对照臂的话，「PA 61%」无从判断高低。"""
    src = (ROOT / "scripts" / "eval_logparser_pa_ga.py").read_text(
        encoding="utf-8")
    assert "def run_drain(" in src
    assert "drain3" in src
