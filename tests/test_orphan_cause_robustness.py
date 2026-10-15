# -*- coding: utf-8 -*-
"""「只出现一次的根因类」不能被漏掉 —— 召回 100% 的稳健性验证。

背景：Android 全量 131 条因果链分布在 8 个根因类上，其中 2 个类**只出现 1 次**
（JsonSyntaxException / IndexOutOfBoundsException）。严格召回 100% 依赖
「每个类都至少被一个 CONFIRMED 簇点到名」。这听起来很脆弱 —— 如果聚类没把
那个孤例收进去，召回立刻掉一截。

这组用例就是钉住这个不变量：**哪怕一条链只出现一次、周围全是别的异常，
它也必须被判成 CONFIRMED 且根因点名正确。**

同时覆盖：
- v2.7.0 修复的「一簇只报一因」漏报（原先只有样例那条被报告）；
- v2.6.0 新增的因果线索丢弃提示：级别过滤若扔掉了带 Caused-by 的
  条目，notice 必须点破「根因就在被丢掉的行里」。
"""
from __future__ import annotations

from pathlib import Path

from log_ai_compressor import service
from log_ai_compressor.constants import MAX_CAUSES_PER_CLUSTER
from log_ai_compressor.core.analysis import CONF_CONFIRMED, analyze_clusters
from log_ai_compressor.core.evidence import verify_refs
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


def _class_of(text: str) -> str:
    """`com.acme.Xxx: root of it` -> `com.acme.Xxx`"""
    return text.split(":", 1)[0].strip()


def _confirmed_names(result: AnalysisResult) -> set[str]:
    """所有 CONFIRMED 簇点名的根因类名（含变体）。

    注意属性名：置信档在 ``root_cause_confidence``（不是 ``confidence``），
    根因描述在 ``root_cause_reason`` 里，形如
    ``Caused-by 因果链直连，根因：com.acme.Xxx: ...``；v2.7.0 起同簇的其余
    内层因在 ``root_cause_variants`` 里，也算「被点名」——
    只看 reason 就等于把刚修好的漏报又当成缺陷。
    """
    names = set()
    for c in result.clusters:
        if getattr(c, "root_cause_confidence", None) != CONF_CONFIRMED:
            continue
        reason = getattr(c, "root_cause_reason", "") or ""
        if "根因：" in reason:
            head = reason.split("根因：", 1)[1].split("（")[0]
            names.add(_class_of(head))
        for v in getattr(c, "root_cause_variants", []) or []:
            if not v.reason.startswith("（"):        # 跳过溢出记账行
                names.add(_class_of(v.reason))
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


def test_rare_class_survives_among_common_ones(tmp_path: Path) -> None:
    """稀有根因类不会被同类簇的样例挤掉。

    复现：50 条 ``java.net.SocketException`` + 1 条 ``com.acme.RarelySeen``，
    外层消息只有一词之差（"reset i" vs "reset rare"），聚类判为同一簇。

    v2.6.0 时这条是 **strict xfail**（只报样例那一条，稀有类没人提）。
    v2.7.0 改为按摄入期累计的次数排名，主因写进 reason、其余进 variants，
    所以现在必须真的通过 —— 若哪天又退回只报一因，这里会直接红。
    """
    lines: list[str] = []
    for i in range(50):
        lines += _chain(f"reset{i}", "java.net.SocketException: reset")
    lines[25 * 3 : 25 * 3 + 3] = _chain("resetrare", "com.acme.RarelySeen")
    p = _write(tmp_path, "rare.log", lines)
    r = _run(p)
    names = _confirmed_names(r)
    assert "com.acme.RarelySeen" in names, \
        f"稀有根因类被同类簇的样例挤掉了，实得 {names}"
    # 主因必须是次数多的那个，而不是「谁恰好当了样例谁赢」。
    # 注意是 49 不是 50：上面那行是**替换**掉第 25 条链（slicing 赋值），
    # 所以实为 49 条 SocketException + 1 条 RarelySeen。
    c = next(c for c in r.clusters
             if c.root_cause_confidence == CONF_CONFIRMED)
    assert c.root_cause_variants[0].count == 49, c.root_cause_variants
    assert "SocketException" in c.root_cause_variants[0].reason
    assert "主因 49/50 次" in c.root_cause_reason, c.root_cause_reason


def test_two_rare_classes_both_survive(tmp_path: Path) -> None:
    """同簇内两个不同内层因，两个都要被点名。"""
    lines = _chain("a", "com.acme.First") + [""] + _chain("b", "com.acme.Second")
    p = _write(tmp_path, "two_rare.log", lines)
    r = _run(p)
    names = _confirmed_names(r)
    assert {"com.acme.First", "com.acme.Second"} <= names, \
        f"同簇内两个稀有类只报了一个，实得 {names}"


def test_single_cause_cluster_keeps_legacy_reason_text(tmp_path: Path) -> None:
    """只有一种内层因时，措辞与 v2.6.0 **逐字一致**（不做无谓的口径漂移）。"""
    p = _write(tmp_path, "one.log", _chain("a", "com.acme.First"))
    r = _run(p)
    c = r.clusters[0]
    assert c.root_cause_reason == \
        "Caused-by 因果链直连，根因：com.acme.First: root of it", \
        c.root_cause_reason
    assert len(c.root_cause_variants) == 1, c.root_cause_variants


def test_multi_cause_reason_states_the_split(tmp_path: Path) -> None:
    """多因时 reason 必须自报「几种/主因占比」，不能让人以为它是唯一根因。"""
    lines = _chain("a", "com.acme.First") + [""] + _chain("b", "com.acme.Second")
    p = _write(tmp_path, "multi.log", lines)
    c = _run(p).clusters[0]
    assert "本簇 2 种内层因" in c.root_cause_reason, c.root_cause_reason
    assert "主因 1/2 次" in c.root_cause_reason, c.root_cause_reason


def test_counts_cover_full_input_not_retained_instances(tmp_path: Path) -> None:
    """计数必须覆盖**全量**输入，不能只数保留下来的前 N 个实例详情。

    这正是 v2.6.0 缺陷的成因：靠 instances 反推只能拿到每簇前 200 条
    （MAX_CLUSTER_INSTANCES_DETAILED）的有偏样本。这里造 250 条同类链，
    远超单簇实例详情上限，若计数仍报 200 就说明退回了老路径。
    """
    lines: list[str] = []
    for i in range(250):
        lines += _chain(f"bulk{i}", "com.acme.BulkRoot")
    p = _write(tmp_path, "bulk.log", lines)
    r = _run(p)
    c = next(c for c in r.clusters
             if c.root_cause_confidence == CONF_CONFIRMED)
    assert c.cause_counts.get("com.acme.BulkRoot: root of it") == 250, \
        f"计数只覆盖了实例保留上限内的样本：{c.cause_counts}"
    # 只有一种内层因，所以 reason 仍是单因旧措辞（多因才加占比后缀）
    assert c.root_cause_reason.endswith("com.acme.BulkRoot: root of it"), \
        c.root_cause_reason


def test_each_variant_carries_its_own_verifiable_evidence(tmp_path: Path) -> None:
    """每种内层因都要有自己的原文引用，且必须回读校验通过。

    这是本项目最不能退的一档：一条指向别处的"证据"比没有证据更危险 ——
    排查的人会照着它去核对日志，结果核不到，整份报告的可信度一起塌。
    """
    lines = _chain("a", "com.acme.First") + [""] + _chain("b", "com.acme.Second")
    p = _write(tmp_path, "ev.log", lines)
    r = _run(p)
    c = r.clusters[0]
    refs = [v.evidence for v in c.root_cause_variants]
    assert all(refs), f"有变体没拿到原文引用：{c.root_cause_variants}"
    assert refs[0] is not refs[1], "两个变体共用同一条引用等于没指向各自原文"
    # 引用必须落在各自的链上：First 在 L1-L2，Second 在 L5-L6
    assert (refs[0].line_start, refs[0].line_end) == (1, 2), refs[0]
    assert (refs[1].line_start, refs[1].line_end) == (5, 6), refs[1]
    report = verify_refs([e for cl in r.clusters for e in cl.evidence])
    assert report.ok, f"变体证据回读失败：{report.as_dict()}"


def test_overflow_is_accounted_not_silently_dropped(tmp_path: Path) -> None:
    """超过单簇内层因上限时，多出来的必须**记账**，不能静默丢弃。"""
    n = MAX_CAUSES_PER_CLUSTER + 5
    lines: list[str] = []
    for i in range(n):
        lines += _chain(f"of{i}", f"com.acme.Of{i:02d}")
    p = _write(tmp_path, "of.log", lines)
    r = _run(p)
    c = next(c for c in r.clusters
             if c.root_cause_confidence == CONF_CONFIRMED)
    assert len(c.cause_counts) == MAX_CAUSES_PER_CLUSTER, len(c.cause_counts)
    assert c.cause_overflow == 5, c.cause_overflow
    tail = [v for v in c.root_cause_variants if v.reason.startswith("（")]
    assert tail and "另有 5 次" in tail[0].reason, c.root_cause_variants


def test_variant_ranking_is_deterministic(tmp_path: Path) -> None:
    """同输入两次分析，变体顺序必须一致（参与确定性指纹的那类字段）。"""
    lines = _chain("a", "com.acme.First") + [""] + _chain("b", "com.acme.Second")
    p = _write(tmp_path, "det.log", lines)
    a = _run(p).clusters[0]
    b = _run(p).clusters[0]
    assert [v.reason for v in a.root_cause_variants] == \
           [v.reason for v in b.root_cause_variants]
    assert a.root_cause_reason == b.root_cause_reason


def test_confirmed_survives_without_ingest_accumulator(tmp_path: Path) -> None:
    """簇不是由 pipeline 建出来时（直接构造 ErrorCluster），CONFIRMED 不能丢。

    这是本项目吃过一次的亏形态：结论依赖某个「应该被填过」的字段，
    字段一没填就**静默**降级成 INSUFFICIENT，而调用方看到的是「证据不足」，
    根本猜不到是哪个上游没填。分析层必须能自己从样例回退。
    """
    from tests.test_analysis import make_cluster, make_result
    carrier = make_cluster(
        1, "handle failed", first_line=2, last_line=5,
        stack=["java.lang.RuntimeException: query failed",
               "Caused by: java.sql.SQLTimeoutException: lock timeout"],
    )
    r = make_result([carrier])
    assert carrier.cause_counts == {}, "前置条件：手工簇没有摄入期累计"
    analyze_clusters(r)
    assert carrier.root_cause_confidence == CONF_CONFIRMED, \
        carrier.root_cause_confidence
    assert "SQLTimeoutException" in carrier.root_cause_reason


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
