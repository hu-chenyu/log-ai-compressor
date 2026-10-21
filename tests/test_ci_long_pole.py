# -*- coding: utf-8 -*-
"""第七道门禁不许住在「卡死预算」里 —— 2026-10-21 那次事故的守卫。

事故是什么
----------
`test` job 的 `timeout-minutes: 20` 是**整个 job** 的上限。第七道门禁
（变异记分卡）原来挂在 `test` job 的最后一步，而它的耗时与其它十道差一个
数量级：95 条变异，每条都要重跑一组测试。

实测（2026-10-21，ubuntu-3.12，run `39548713439` → `39559092094`）：

| 步骤 | 改之前 | 改之后 |
| --- | --- | --- |
| 单测 + 覆盖率 | 3.12 min | 4.53 min |
| 变异记分卡 | 11.02 min（跑完，余 4.8 min） | 14.73 min **没跑完，被砍** |

`98378a3` 给措辞门禁加了渲染口径断言，两步一起变大，而记分卡排在**最后** ——
于是它吃的是别人剩下的预算。job 在第 20 分钟被砍，run 结论是 **cancelled**，
而**前面十道门禁全都 success**。

一个把「十道过了、一道超时」显示成「整个 run 取消」的结论比红本身更费时间：
它让人先去猜是不是门禁坏了，而不是去看哪一步超时。所以记分卡被拆成了
独立的 `mutation` job，20 分钟的卡死预算**只**用来抓真正的卡死。

判据为什么是**相对**的
----------------------
这里不写死任何分钟数。写死「记分卡必须有 45 分钟」这种断言有两个坏处：
一是它会在 CI 机器变快/变慢时变成噪声，二是**它不描述真正要守的东西**。
真正要守的是**关系**：

1. 记分卡必须在**别的 job** 里 —— 否则它花的是短杆的预算；
2. 那个 job 的 `timeout-minutes` 必须**严格大于** `test` job 的 ——
   否则预算又变成共享的；
3. `test` job 里不许再有记分卡这一步（1 的另一面）；
4. 记分卡那个 job 必须**取完整历史** —— 记分卡里有几条变异直接改
   `tests/test_no_job_wording.py`，而那道门禁要扫「全部历史版本的文件内容」；
5. 记分卡不许 `continue-on-error`、不许 `|| true`（门禁一旦能失败后继续，
   它就不再是门禁）。

每一条都配一条「故意做坏 → 必须变红」的负向测试。
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"

_SCORECARD = "python scripts/mutation_scorecard.py"


def _doc() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _scorecard_steps(doc: dict) -> list[tuple[str, dict]]:
    """(job 名, 步骤) —— 认 `run:` 正文，不认步骤名。

    步骤名可以随便改，`run:` 正文改不了 —— 判据要认**实际执行的东西**。
    """
    out = []
    for job_name, job in doc.get("jobs", {}).items():
        for step in job.get("steps", []):
            if _SCORECARD in str(step.get("run", "")):
                out.append((job_name, step))
    return out


def _timeout(job: dict) -> int:
    value = job.get("timeout-minutes")
    assert value is not None, "这个 job 没有 timeout-minutes —— 预算变成了无限的"
    return int(value)


def _check(doc: dict) -> None:
    """上面那 5 条判据。破了就 raise AssertionError 并说清破在哪。"""
    hits = _scorecard_steps(doc)
    assert hits, (
        "ci.yml 里没有真正执行变异记分卡的步骤 —— 第七道门禁在 GitHub 上"
        "一天都不会跑，而从外面完全看不出来")
    assert len(hits) == 1, (
        f"变异记分卡被执行了 {len(hits)} 次（job：{[n for n, _ in hits]}）—— "
        "重复跑三遍纯属浪费，且会让「跑了几次」这件事失去意义")

    job_name, step = hits[0]

    # ① 它不在 `test` job 里
    assert job_name != "test", (
        "变异记分卡又回到 `test` job 里了 —— 它的耗时与其它十道差一个数量级，"
        "放在同一个 job 里会吃光那 20 分钟的卡死预算。"
        "2026-10-21 就是这么被砍成 cancelled 的："
        "前面十道门禁全 success，run 结论却是取消。")

    # ③ `test` job 里不许再有它
    for s in doc["jobs"]["test"].get("steps", []):
        assert _SCORECARD not in str(s.get("run", "")), (
            "`test` job 里还有一步在跑变异记分卡 —— 与 ① 矛盾："
            "它既在独立 job 里，又在短杆的预算里")

    # ② 独立 job 的预算必须比短杆宽
    short = _timeout(doc["jobs"]["test"])
    long_ = _timeout(doc["jobs"][job_name])
    assert long_ > short, (
        f"记分卡那个 job 的 timeout-minutes={long_} 不大于 `test` job 的 {short} —— "
        "预算还是共享的，它照样会把短杆挤爆")

    # ④ 必须取完整历史
    checkout = next((s for s in doc["jobs"][job_name].get("steps", [])
                     if str(s.get("uses", "")).startswith("actions/checkout")), None)
    assert checkout is not None, f"{job_name} job 里没有 checkout 步骤"
    assert int(checkout.get("with", {}).get("fetch-depth", 1)) == 0, (
        "记分卡那个 job 没有取完整历史 —— 记分卡里有几条变异直接改 "
        "tests/test_no_job_wording.py，而那道门禁要扫「全部历史版本的文件内容」。"
        "浅克隆会让它退化成只看当前那一个版本 —— "
        "那正是「扫了 0 个文件却报通过」那个形状的翻版。")

    # ⑤ 不许失败后继续
    assert step.get("continue-on-error") is None, (
        "变异记分卡允许失败后继续 —— 门禁一旦能失败后继续，它就不再是门禁")
    body = str(step.get("run", ""))
    assert "|| true" not in body and "|| exit 0" not in body, (
        "变异记分卡允许失败后继续（run 正文里有 || true / || exit 0）")


class TestLongPoleIsNotInTheHangDetectorBudget:
    def test_the_real_ci_satisfies_the_invariant(self):
        _check(_doc())

    # ------------------------------------------------------------------
    # 负向测试：每一条判据都要有一份「故意做坏 → 必须变红」的证据。
    # 判据本身是从没红过的断言，比没有断言更糟。
    # ------------------------------------------------------------------
    def test_moving_it_back_into_the_short_job_is_rejected(self):
        doc = _doc()
        job_name, step = _scorecard_steps(doc)[0]
        doc["jobs"]["test"]["steps"].append(step)
        doc["jobs"][job_name]["steps"].remove(step)
        with pytest.raises(AssertionError, match="test"):
            _check(doc)

    def test_equal_budget_is_rejected(self):
        doc = _doc()
        job_name, _ = _scorecard_steps(doc)[0]
        doc["jobs"][job_name]["timeout-minutes"] = doc["jobs"]["test"]["timeout-minutes"]
        with pytest.raises(AssertionError, match="timeout-minutes"):
            _check(doc)

    def test_a_shallow_clone_is_rejected(self):
        doc = _doc()
        job_name, _ = _scorecard_steps(doc)[0]
        for s in doc["jobs"][job_name].get("steps", []):
            if str(s.get("uses", "")).startswith("actions/checkout"):
                s.setdefault("with", {})["fetch-depth"] = 1
        with pytest.raises(AssertionError, match="fetch-depth"):
            _check(doc)

    def test_fail_open_is_rejected(self):
        doc = _doc()
        job_name, step = _scorecard_steps(doc)[0]
        step["continue-on-error"] = True
        with pytest.raises(AssertionError, match="continue-on-error"):
            _check(doc)

    def test_a_missing_scorecard_is_rejected(self):
        doc = _doc()
        job_name, step = _scorecard_steps(doc)[0]
        doc["jobs"][job_name]["steps"].remove(step)
        with pytest.raises(AssertionError, match="没有真正执行"):
            _check(doc)

    def test_a_duplicated_scorecard_is_rejected(self):
        doc = _doc()
        job_name, step = _scorecard_steps(doc)[0]
        doc["jobs"][job_name]["steps"].append(copy.deepcopy(step))
        with pytest.raises(AssertionError, match="被执行了"):
            _check(doc)
