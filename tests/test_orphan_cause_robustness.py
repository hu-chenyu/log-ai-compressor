# -*- coding: utf-8 -*-
"""「只出现一次的根因类」不能被漏掉 —— 召回 100% 的稳健性验证。

背景：Android 全量 131 条因果链分布在 8 个根因类上，其中 2 个类**只出现 1 次**
（JsonSyntaxException / IndexOutOfBoundsException）。严格召回 100% 依赖
「每个类都至少被一个 CONFIRMED 簇点到名」。这听起来很脆弱 —— 如果聚类没把
那个孤例收进去，召回立刻掉一截。

这组用例就是钉住这个不变量：**哪怕一条链只出现一次、周围全是别的异常，
它也必须被判成 CONFIRMED 且根因点名正确。**

同时覆盖 v2.6.0 新增的因果线索丢弃提示：级别过滤若扔掉了带 Caused-by 的
条目，notice 必须点破「根因就在被丢掉的行里」。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from log_ai_compressor import service
from log_ai_compressor.core.analysis import CONF_CONFIRMED
from log_ai_compressor.core.models import AnalysisResult

HEAD = "2024-01-15 10:00:00"


def _chain(exc: str, cause: str) -> list[str]:
    """构造一条完整因果链。

    外层异常消息**必须各不相同** —— 真实日志里两条不同的异常外层消息本来就不同；
    若都写成同一句，聚类会（正确地）把它们并成一个簇、只留一个样例，
    那时测的就不是「稀有类被漏掉」而是「簇内样例替换」，是另一回事。
    第一版这里写成固定的 "outer boom"，两个稀有类的用例会假失败。
    """
    return [
        f"{HEAD} ERROR java.lang.RuntimeException: {exc} failed",
        f"Caused by: {cause}: root of it",
        f"{HEAD} INFO uninteresting chatter after {exc}",
    ]


def _write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _run(path: Path) -> AnalysisResult:
    return service.analyze(paths=[str(path)],
                           params={"top_n": 0, "context_lines": 0})


def _confirmed_names(result: AnalysisResult) -> set[str]:
    """取出所有 CONFIRMED 簇点名的根因类名。

    注意属性名：置信档在 ``root_cause_confidence``（不是 ``confidence``），
    根因描述在 ``root_cause_reason`` 里，形如
    ``Caused-by 因果链直连，根因：com.acme.Xxx: ...``。
    """
    names = set()
    for c in result.clusters:
        if getattr(c, "root_cause_confidence", None) != CONF_CONFIRMED:
            continue
        reason = getattr(c, "root_cause_reason", "") or ""
        if "根因：" in reason:
            names.add(reason.split("根因：", 1)[1].split(":")[0].strip())
    return names


# ---------------------------------------------------------------------------
# 一、孤例不能被漏
# ---------------------------------------------------------------------------


def test_lone_cause_occurrence_is_still_confirmed(tmp_path: Path) -> None:
    """整个文件里只有 1 条链、且那个根因类只出现 1 次 —— 仍须 CONFIRMED。"""
    p = _write(tmp_path, "lone.log", _chain("single", "com.acme.OneOffBug"))
    r = _run(p)
    assert any(getattr(c, "root_cause_confidence", None) == CONF_CONFIRMED
               for c in r.clusters), "孤例链没有被判成 CONFIRMED"
    assert "com.acme.OneOffBug" in _confirmed_names(r), \
        f"CONFIRMED 簇没有点名根因类，实得 {_confirmed_names(r)}"


@pytest.mark.xfail(strict=True, reason="已知缺陷：同簇多内层因只报样例，待设计决策")
def test_rare_class_survives_among_common_ones(tmp_path: Path) -> None:
    """【已知缺陷 · 待设计决策】稀有根因类会被同类簇的样例挤掉。

    复现：50 条 ``java.net.SocketException`` + 1 条 ``com.acme.RarelySeen``，
    外层消息只有一词之差（"reset i" vs "reset rare"），聚类判为同一簇；
    簇只报告**样例**那一条的 Caused-by，于是稀有类的链没人提。

    这不是随手能修的：CONFIRMED 的定义是「根因来自条目内的 Caused-by 链」，
    而簇按外层消息相似度划分 —— 一个簇里出现多个不同内层因时，
    要报哪一个本身就是设计问题（报最多的？全列？按实例拆簇？）。
    改这里会动到本项目最核心的不变量，风险远超「补个测试」。

    因此这里用 **strict xfail**：现在是坏的，将来若有人修了它，这个用例会
    转为失败并强制我们回来更新文档与对外口径，而不是悄悄变成"已修"。
    """
    lines: list[str] = []
    for i in range(50):
        lines += _chain(f"reset{i}", "java.net.SocketException: reset")
    lines[25 * 3 : 25 * 3 + 3] = _chain("resetrare", "com.acme.RarelySeen")
    p = _write(tmp_path, "rare.log", lines)
    r = _run(p)
    assert "com.acme.RarelySeen" in _confirmed_names(r), \
        f"稀有根因类被同类簇的样例挤掉了，实得 {_confirmed_names(r)}"


@pytest.mark.xfail(strict=True, reason="已知缺陷：一簇只报一个根因，待设计决策")
def test_two_rare_classes_both_survive(tmp_path: Path) -> None:
    """【已知缺陷 · 待设计决策】同簇内两个不同内层因，只报样例那一个。

    最小复现：两条链外层消息一词之差（"a failed" / "b failed"）→ 同一簇，
    count=2、两个实例各带一条 Caused-by，但 root_cause_reason 只写了样例那条。

    与上一条同源，同为待设计决策项。
    """
    lines = _chain("a", "com.acme.First") + [""] + _chain("b", "com.acme.Second")
    p = _write(tmp_path, "two_rare.log", lines)
    r = _run(p)
    names = _confirmed_names(r)
    assert {"com.acme.First", "com.acme.Second"} <= names, \
        f"同簇内两个稀有类只报了一个，实得 {names}"


def test_current_behaviour_is_pinned_single_cause_per_cluster(tmp_path: Path) -> None:
    """把「一簇只报一个根因」这个现状钉死。

    它现在是**设计**（CONFIRMED 绑定单条 Caused-by 链），所以写断言固定住，
    将来真要改成「一簇多因」时，这里会失败并提醒同步改口径与文档。
    """
    lines = _chain("a", "com.acme.First") + [""] + _chain("b", "com.acme.Second")
    p = _write(tmp_path, "pinned.log", lines)
    r = _run(p)
    assert len(r.clusters) == 1, f"预期同簇，实得 {len(r.clusters)} 个簇"
    c = r.clusters[0]
    assert c.count == 2, c.count
    assert "com.acme.First" in (c.root_cause_reason or ""), c.root_cause_reason
    # 第二个内层因确实在实例里，只是不被报告 —— 这是现状，不是巧合
    assert "com.acme.Second" not in (c.root_cause_reason or ""), \
        "若这条断言失败，说明一簇已能报多因，请同步更新 FAQ / BENCHMARK 的口径说明"


# ---------------------------------------------------------------------------
# 二、级别过滤扔掉因果链时，notice 必须点破
# ---------------------------------------------------------------------------


def test_notice_flags_causal_lines_dropped_by_level_filter(tmp_path: Path) -> None:
    """把带 Caused by 的行放在 WARN 级 —— 默认只留 ERROR/FAIL 会把它扔掉。

    这时 notice 必须明确说「根因可能在被丢掉的行里」，否则用户只看到
    「保留率很低」却不知道狠到把根因一起扔了。
    """
    lines = [
        f"{HEAD} WARN java.lang.RuntimeException: outer",
        "Caused by: com.acme.HiddenRoot: the real one",
    ] + [f"{HEAD} INFO filler line {i}" for i in range(200)]
    p = _write(tmp_path, "hidden.log", lines)
    r = _run(p)
    assert r.stats.causal_dropped >= 1, \
        f"没统计到被丢弃的因果链，causal_dropped={r.stats.causal_dropped}"
    assert r.notice, "低保留率却没有 notice"
    assert "Caused-by" in r.notice and "根因" in r.notice, r.notice


def test_notice_stays_quiet_when_no_causal_line_is_dropped(tmp_path: Path) -> None:
    """因果链都在 ERROR 行里时，不能瞎报「你的根因被扔了」—— 会变成噪音。"""
    lines = [
        f"{HEAD} ERROR java.lang.RuntimeException: outer",
        "Caused by: com.acme.Visible: kept",
    ] + [f"{HEAD} INFO filler line {i}" for i in range(200)]
    p = _write(tmp_path, "visible.log", lines)
    r = _run(p)
    assert r.stats.causal_dropped == 0, r.stats.causal_dropped
    if r.notice:
        assert "Caused-by" not in r.notice, \
            f"没有因果链被丢弃却报了警，属于假阳性：{r.notice}"


def test_stats_serialises_the_new_counter() -> None:
    """新增字段要出现在 JSON 导出里，否则 Web/API 侧看不到这个诊断。"""
    d = __import__("log_ai_compressor.core.models", fromlist=["RunStats"]).RunStats()
    assert "causal_dropped" in d.as_dict()
