# -*- coding: utf-8 -*-
"""对抗性评测的**评分机制**本身必须可失效。

背景
----
`adversarial_eval.py` 是 CI 第 2 道门禁，也是 README 里「该弃权时会不会闭嘴」
这条主张的唯一依据。但整份脚本此前**只被测过一次** —— 而且测的是
`test_invariants.py` 里的「打印中文前有没有重配 stdout」，跟评分判据无关。

后果是可以预见的：**把评分判据改坏，门禁照样全绿。** 实测过 4 处：

| 故意改坏的地方 | 后果 |
| --- | --- |
| `_SCORE_ITEMS` 去掉 `no_confident_wrong` | 该场景的这条判据**根本不再被求值**，检查数从 35 掉到 31，评分卡仍报「全绿」 |
| `_no_false_confirmed` 的 `bad` 恒为空 | 「良性文本被误判成根因」再也抓不到 |
| `ok` 去掉 `and not overreach` | **自信地给错根因**不再让门禁红 —— 而那正是这张卡最该压到 0 的数 |
| `_no_causal_language` 的 `banned` 清空 | 把 related 说成 caused 不再被拦 |

四条全绿，因为 `prove.py` 的 `claim_scorecard` 只断言 `summary["ok"]`，
而没有任何东西断言 `ok` 在什么情况下必须是 False、也没人钉住检查项的数量。

这组测试把「`ok` 变红的四种方式」逐条钉死，并让 CI 配置里的那段判据本身
成为被校对象。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    """按路径加载 scripts/adversarial_eval.py —— 它是脚本，不在包里。"""
    path = _ROOT / "scripts" / "adversarial_eval.py"
    spec = importlib.util.spec_from_file_location("adv_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load()


# ---------------------------------------------------------------------------
# 造结果的最小工具：只提供被判据真正读到的属性，不多不少
# ---------------------------------------------------------------------------
def _cluster(summary="x", *, conf="LIKELY", reason=None, evidence=("L1",)):
    return types.SimpleNamespace(
        summary=summary, root_cause_confidence=conf,
        root_cause_reason=reason, evidence=list(evidence))


def _result(clusters=(), *, verdict="INSUFFICIENT", can_conclude=False, gaps=()):
    return types.SimpleNamespace(
        clusters=list(clusters),
        evidence={"verdict": verdict, "can_conclude": can_conclude,
                  "gaps": [{"code": g} for g in gaps]})


def _report(case="c", *, checked=1, passed=1, verdict="INSUFFICIENT",
            confirmed=0, items=None):
    return {
        "case": case, "doc": "", "verdict": verdict,
        "checked": checked, "passed": passed,
        "items": items if items is not None else {},
        "clusters": 1, "confirmed": confirmed,
    }


# ---------------------------------------------------------------------------
# 1. 声明了却不再被评分的判据，等于没有这条判据
# ---------------------------------------------------------------------------
class TestEveryDeclaredCheckIsActuallyScored:
    def test_no_case_declares_a_check_the_scorecard_ignores(self):
        """这条杀的是「从 `_SCORE_ITEMS` 里删掉一项」那种变异。

        `run_case` 用 `checks.get(item)` 取函数，item 不在 `_SCORE_ITEMS`
        里就**整条不求值** —— 没有报错、没有告警，检查数悄悄变少，评分卡
        照样打印「全部通过」。删掉 `no_confident_wrong` 时正是如此：
        35 项变 31 项，而它本该是权重最高的负项。
        """
        declared = {n for c in A.CASES for n in c.checks} | set(A._ALWAYS)
        unscored = sorted(declared - set(A._SCORE_ITEMS))
        assert not unscored, (
            f"这些判据被场景声明了，却不在 _SCORE_ITEMS 里 —— 它们根本不会被求值：{unscored}")

    def test_score_items_are_unique(self):
        """重复项会把同一个判据数两遍，虚增检查数。"""
        items = list(A._SCORE_ITEMS)
        assert len(items) == len(set(items)), f"_SCORE_ITEMS 有重复：{items}"

    def test_the_card_actually_scores_something(self):
        """一道跑不出任何检查的门禁，和没有门禁在同一个方向上说谎。"""
        total = A.scorecard(A.run_all())["checks"]
        assert total > 0, "对抗评测一项检查都没跑到，这张卡什么都没证明"


# ---------------------------------------------------------------------------
# 2. `ok` 必须在这些情况下变红
# ---------------------------------------------------------------------------
class TestOkGoesRed:
    def test_red_when_one_check_fails(self):
        r = _report(checked=3, passed=2,
                    items={"a": {"pass": True, "detail": ""},
                           "b": {"pass": False, "detail": "抓到了"},
                           "c": {"pass": True, "detail": ""}})
        s = A.scorecard([r])
        assert s["ok"] is False, "有判据没过却仍然报 ok"
        assert s["passed"] == 2 and s["checks"] == 3

    def test_red_on_a_confident_wrong_case(self):
        """该弃权却给了 CONFIRMED —— 这是全表最该压到 0 的那个数。

        杀的是「`ok` 里去掉 `and not overreach`」那种变异：那一刻连
        `pass_rate` 都还是 100%，只有这条会红。
        """
        s = A.scorecard([_report(verdict="INSUFFICIENT", confirmed=2)])
        assert s["confident_wrong_cases"] == ["c"], s
        assert s["confident_wrong_rate"] == 100.0
        assert s["ok"] is False, "自信错误却没有让门禁红"

    def test_not_red_when_confidence_is_warranted(self):
        """反例对照：该给结论时给了 CONFIRMED，不是错误。"""
        s = A.scorecard([_report(verdict="CONFIRMED", confirmed=1)])
        assert s["confident_wrong_cases"] == []
        assert s["ok"] is True

    def test_red_when_nothing_was_scored(self):
        """零产出不得报 ok —— `0 == 0` 恒真，这是最省事的假绿。"""
        s = A.scorecard([])
        assert s["ok"] is False, "一个场景都没跑却报通过"

    def test_red_when_a_check_raised(self):
        """判据自己抛异常也算不通过（`run_case` 已如此兜底，这里锁住它）。"""
        r = _report(checked=1, passed=0,
                    items={"a": {"pass": False, "detail": "评分执行异常: X"}})
        assert A.scorecard([r])["ok"] is False


# ---------------------------------------------------------------------------
# 3. 判据本身必须真的能抓到东西（杀「判据恒真」那一类变异）
# ---------------------------------------------------------------------------
class TestJudgesHaveTeeth:
    def test_no_false_confirmed_catches_a_guilty_cluster(self):
        check = A._no_false_confirmed("assertions passed")
        res = _result([_cluster(summary="health check assertions passed",
                                conf=A.CONF_CONFIRMED)])
        ok, detail = check(res)
        assert ok is False, "把良性文本判成 CONFIRMED 竟然通过了"
        assert "assertions passed" in detail

    def test_no_false_confirmed_passes_a_clean_cluster(self):
        check = A._no_false_confirmed("assertions passed")
        res = _result([_cluster(summary="db connection refused", conf="LIKELY")])
        ok, _ = check(res)
        assert ok is True

    @pytest.mark.parametrize("banned", ["导致了", "由其引发", "caused by",
                                        "根因是", "根本原因是"])
    def test_causal_language_check_catches_overreach(self, banned):
        """把 related 说成 caused 是取证工具最致命的越界。

        杀的是「`banned` 清空」那种变异 —— 它会让这条判据恒真。
        """
        res = _result([_cluster(summary="a", conf="LIKELY", reason=f"上游{banned}它")])
        ok, detail = A._no_causal_language(res)
        assert ok is False, f"「{banned}」越界竟然通过了"
        assert banned in detail

    def test_causal_language_allows_confirmed_to_say_root_cause(self):
        """CONFIRMED 由 Caused-by 链直连，允许说「根因」——反例对照。"""
        res = _result([_cluster(conf=A.CONF_CONFIRMED, reason="根因是堆溢出")])
        ok, _ = A._no_causal_language(res)
        assert ok is True

    def test_evidence_present_catches_a_cluster_without_citation(self):
        res = _result([_cluster(summary="a", evidence=())])
        ok, detail = A._evidence_present(res)
        assert ok is False and "缺证据出处" in detail

    @pytest.mark.parametrize("code", ["no_causal_chain", "insufficient_context"])
    def test_gap_absent_catches_a_false_gap(self, code):
        """「不虚报缺口」的反向能力 —— 只会加缺口的工具同样不可信。"""
        res = _result(gaps=[code])
        ok, _ = A._gap_absent(code)(res)
        assert ok is False, f"虚报了缺口 {code} 却通过"

    def test_gap_absent_passes_when_the_gap_is_really_missing(self):
        res = _result(gaps=[])
        assert A._gap_absent("no_causal_chain")(res)[0] is True

    def test_abstain_requires_no_conclusion_at_all(self):
        """LIKELY 的 `can_conclude=False` 也算弃权 —— 它没把结论交出去。"""
        res = _result([_cluster(conf="LIKELY")], verdict="LIKELY", can_conclude=False)
        assert A._no_abstain(res)[0] is True

    def test_abstain_fails_when_it_handed_over_a_root_cause(self):
        res = _result([_cluster(conf=A.CONF_CONFIRMED)], can_conclude=True)
        assert A._no_abstain(res)[0] is False

    def test_confirmed_contains_checks_the_reason_not_the_summary(self):
        """查 `root_cause_reason`：summary 取的是条目**首行**（外层异常），
        而 CONFIRMED 指向链上**最内层**那个，两者不同恰恰是正确的。"""
        c = _cluster(summary="java.lang.RuntimeException: order 9001 failed",
                     conf=A.CONF_CONFIRMED, reason="Caused-by 链指向 "
                                                  "java.sql.SQLTimeoutException")
        assert A._confirmed_contains("SQLTimeoutException")(_result([c]))[0] is True
        assert A._confirmed_contains("SQLTimeoutException")(
            _result([c]))[0] is True
        wrong = _cluster(summary="RuntimeException", conf=A.CONF_CONFIRMED,
                         reason="Caused-by 链指向 SocketTimeoutException")
        assert A._confirmed_contains("SQLTimeoutException")(_result([wrong]))[0] is False


# ---------------------------------------------------------------------------
# 4. run_case 的记账
# ---------------------------------------------------------------------------
class TestRunCaseAccounting:
    def test_a_failing_check_is_counted_not_swallowed(self):
        def boom(result):
            return False, "故意失败"

        case = A.Case(name="synthetic", doc="", log="2026-01-01 INFO hello\n",
                      checks={"abstain_correctly": boom})
        rep = A.run_case(case)
        assert rep["checked"] == 3, rep          # 两条底线 + 这一条
        assert rep["passed"] == 2
        assert rep["items"]["abstain_correctly"]["pass"] is False
        assert A.scorecard([rep])["ok"] is False

    def test_a_raising_check_is_scored_as_failed_not_skipped(self):
        def boom(result):
            raise RuntimeError("判据自己炸了")

        case = A.Case(name="synthetic", doc="", log="2026-01-01 INFO hello\n",
                      checks={"abstain_correctly": boom})
        rep = A.run_case(case)
        assert rep["items"]["abstain_correctly"]["pass"] is False
        assert "评分执行异常" in rep["items"]["abstain_correctly"]["detail"]

    def test_a_scenario_that_skips_an_item_just_scores_fewer(self):
        """`checks.get(item) is None` 是**设计如此**（CONFIRMED 场景不考弃权），
        所以必须靠第 1 组的结构性检查来兜住「漏评」，不能靠这里。"""
        case = A.Case(name="synthetic", doc="", log="2026-01-01 INFO hello\n",
                      checks={})
        rep = A.run_case(case)
        assert rep["checked"] == 2 and rep["passed"] == 2


# ---------------------------------------------------------------------------
# 5. 真实语料上的总账：这张卡现在到底在说什么
# ---------------------------------------------------------------------------
class TestRealScorecard:
    @pytest.fixture(scope="class")
    def summary(self):
        return A.scorecard(A.run_all())

    def test_every_case_scores_at_least_the_two_baselines(self, summary):
        """`_ALWAYS` 是所有场景共用的两条底线（出处可回读 + 不越界用因果措辞）。"""
        for rep in A.run_all():
            assert rep["checked"] >= 2, f"{rep['case']} 连底线都没考到"

    def test_current_tree_is_green(self, summary):
        """锁住现状：修一个真缺陷时这张卡要变红，而不是悄悄变小。"""
        assert summary["ok"] is True, summary
        assert summary["cases"] >= 10, summary
        assert summary["confident_wrong_cases"] == [], summary

    def test_confident_wrong_rate_is_zero_and_abstain_is_high(self, summary):
        assert summary["confident_wrong_rate"] == 0.0, summary
        assert summary["correct_abstain_rate"] == 100.0, summary
