# -*- coding: utf-8 -*-
"""确定性指纹必须覆盖**每一条会出现在报告里、且影响判断**的字段。

为什么这组用例存在：v2.7.0 的招牌改动（簇内全部内层因）**没有**进指纹。
后果不是「指纹算错了」，而是**指纹看不见那次改动** —— 有人把
`root_cause_variants` 改回「只报主因」，指纹逐字节不变，指纹一致性
100% 的门禁照样全绿，而对外承诺的「同输入同结论」已经悄悄破了一个角。

指纹最大的作用不是「同输入不出错」（那是回归测试的活），而是**对外的
可验证承诺**：谁都能把同一份日志跑两遍比对。既然是承诺，它就必须覆盖
结论的全部内容；漏一个字段，等于给那种悄悄改回旧行为的回归发通行证。

所以这里的写法是**逐字段反向证明**：每个字段都故意改坏一次，断言指纹
一定会变。新增结论字段却忘了加进指纹时，本文件会直接红。
"""
from __future__ import annotations

import copy

import pytest

from log_ai_compressor import service
from log_ai_compressor.core.evidence import _FINGERPRINT_CLUSTER_FIELDS
from log_ai_compressor.core.evidence import result_fingerprint
from log_ai_compressor.core.models import RootCauseVariant

HEAD = "2024-01-15 10:00:00"


def _two_cause_log() -> str:
    """两条外层消息一词之差 → 聚成同簇，内层因不同 → variants 非空。"""
    return "\n".join([
        f"{HEAD} ERROR java.lang.RuntimeException: a failed",
        "Caused by: com.acme.First: root of it",
        f"{HEAD} INFO chatter a",
        "",
        f"{HEAD} ERROR java.lang.RuntimeException: b failed",
        "Caused by: com.acme.Second: root of it",
        f"{HEAD} INFO chatter b",
    ]) + "\n"


def _analyze(tmp_path):
    """走真实文件路径而不是粘贴文本 —— 证据引用要指向可回读的文件。"""
    p = tmp_path / "two.log"
    p.write_text(_two_cause_log(), encoding="utf-8")
    return service.analyze(paths=[str(p)], params={"top_n": 0,
                                                   "context_lines": 0})


def _confirmed_cluster(result):
    return next(c for c in result.clusters if c.root_cause_confidence == "CONFIRMED")


# ---------------------------------------------------------------------------
# 一、前置：这份语料真的造出了两个内层因（否则下面的断言全是空转）
# ---------------------------------------------------------------------------


def test_corpus_really_produces_two_variants(tmp_path) -> None:
    r = _analyze(tmp_path)
    c = _confirmed_cluster(r)
    assert len(c.cause_counts) == 2, c.cause_counts
    assert len(c.root_cause_variants) == 2, c.root_cause_variants


# ---------------------------------------------------------------------------
# 二、逐字段反向证明：改坏它，指纹必须变
# ---------------------------------------------------------------------------


def _mutations(c):
    """(字段名, 破坏动作)。每个破坏动作都必须让指纹改变。"""
    return [
        ("root_cause_variants",
         lambda x: setattr(x, "root_cause_variants", x.root_cause_variants[:1])),
        ("cause_overflow",
         lambda x: setattr(x, "cause_overflow", x.cause_overflow + 1)),
        ("root_timeline",
         lambda x: setattr(x, "root_timeline", (x.root_timeline or "") + "tampered")),
        ("related_clusters",
         lambda x: setattr(x, "related_clusters", list(x.related_clusters) + [999])),
        ("traces",
         lambda x: setattr(x, "traces", list(x.traces) + ["tampered-trace"])),
        ("root_cause_reason",
         lambda x: setattr(x, "root_cause_reason", "tampered reason")),
        ("root_cause_confidence",
         lambda x: setattr(x, "root_cause_confidence", "INSUFFICIENT")),
        ("anomaly",
         lambda x: setattr(x, "anomaly", "burst")),
        ("change_context",
         lambda x: setattr(x, "change_context", "tampered change window")),
        ("priority",
         lambda x: setattr(x, "priority", round(x.priority + 1, 1))),
        ("count",
         lambda x: setattr(x, "count", x.count + 1)),
        ("summary",
         lambda x: setattr(x, "summary", "tampered summary")),
        ("evidence",
         lambda x: setattr(x, "evidence", list(x.evidence)[1:])),
    ]


@pytest.mark.parametrize("field", [name for name, _ in _mutations(None)])
def test_mutating_each_conclusion_field_changes_fingerprint(
        tmp_path, field: str) -> None:
    """改坏任一结论字段，指纹必须变。

    v2.6.0 只有 change_context 一个字段有这条保护（见
    `test_change_events.py`）。v2.7.0 起把它扩成一张表 —— 新增结论字段
    却忘了进指纹时，本用例会以「参数化里没有这个字段」的方式提醒，
    而真正拦住回归的是下面那条白名单一致性测试。
    """
    baseline = _analyze(tmp_path)
    mutated = _analyze(tmp_path)
    before = result_fingerprint(baseline)
    mutate = dict(_mutations(_confirmed_cluster(mutated)))[field]
    mutate(_confirmed_cluster(mutated))
    assert result_fingerprint(mutated) != before, \
        f"篡改 {field} 后指纹纹丝不动 —— 这个结论字段不在指纹覆盖范围内"


def test_fingerprint_covers_every_field_in_the_whitelist() -> None:
    """白名单里的每个字段，都必须有对应的反向用例。

    防止「加了白名单条目但忘了写保护」，也防止「白名单与实现漂移」。
    两侧都对着同一份 `_mutations` 名单，缺一边就红。
    """
    covered = {name for name, _ in _mutations(None)}
    missing = set(_FINGERPRINT_CLUSTER_FIELDS) - covered - {
        # 这几个字段由 evidence_ids / 时间戳等既有用例间接覆盖，
        # 且它们不是「结论内容」而是定位信息。
        "cluster_id", "level", "module", "priority_label", "is_root_cause",
        "first_seen", "last_seen",
    }
    assert not missing, f"白名单里有字段没有反向用例保护：{sorted(missing)}"


# ---------------------------------------------------------------------------
# 三、加了集合型字段之后，确定性承诺还成立吗？
# ---------------------------------------------------------------------------


def test_fingerprint_still_deterministic_with_set_like_fields(tmp_path) -> None:
    """related_clusters / traces 语义上是集合：顺序不该让指纹漂移。

    这条是加入集合型字段的**代价检查**。如果它们以列表原序入指纹，
    任何依赖字典/集合遍历顺序的地方都会让「同输入同结论」悄悄失效。
    """
    a = _analyze(tmp_path)
    b = _analyze(tmp_path)
    assert result_fingerprint(a) == result_fingerprint(b)

    # 人为打乱顺序，指纹必须不变（集合语义）
    for r in (a, b):
        for c in r.clusters:
            c.related_clusters = list(reversed(c.related_clusters))
            c.traces = list(reversed(c.traces))
    assert result_fingerprint(a) == result_fingerprint(b), \
        "集合型字段换了顺序就改变指纹 —— 它们被当成有序序列入哈希了"


def test_variant_evidence_id_participates(tmp_path) -> None:
    """变体自带的首处引用也要进指纹（换了出处就是换了证据链）。"""
    r = _analyze(tmp_path)
    before = result_fingerprint(r)
    c = _confirmed_cluster(r)
    c.root_cause_variants = [
        RootCauseVariant(reason=v.reason, count=v.count,
                         evidence=None)      # 丢掉原引用
        for v in c.root_cause_variants
    ]
    assert result_fingerprint(r) != before


def test_copying_result_keeps_fingerprint(tmp_path) -> None:
    """深拷贝后指纹不变 —— 证明 payload 只由值组成、不依赖对象身份。"""
    r = _analyze(tmp_path)
    assert result_fingerprint(r) == result_fingerprint(copy.deepcopy(r))


# ---------------------------------------------------------------------------
# 四、路径不是结论（v2.11.0）
#
# 修之前的实测：同一份逐字节相同的日志，绝对路径得 856b49a8…、相对路径得
# 2adae7d0…。两处来源：`stats.source` 与 `evidence_id`（它由 source 派生）。
#
# 这条不只影响指纹的**数值**，它让对外那句话直接不成立：
# README 第一屏写的是「同一份日志跑两遍，结论指纹逐字节相同」，
# 而「跑两遍」完全可以是「一次 cd 到目录里、一次用绝对路径」。
# CI 的确定性门禁用**同一个字面路径**连跑三次，结构上就看不到这一维。
# ---------------------------------------------------------------------------


def _analyze_by(spelling, tmp_path, monkeypatch):
    """同一份日志，用给定的路径写法分析一次。"""
    p = tmp_path / "two.log"
    p.write_text(_two_cause_log(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return service.analyze(paths=[spelling], params={"top_n": 0,
                                                     "context_lines": 0})


def test_fingerprint_does_not_depend_on_how_the_path_was_spelled(
        tmp_path, monkeypatch) -> None:
    """绝对路径 / 相对路径 / `./` 前缀 —— 指纹必须逐字节相同。"""
    p = tmp_path / "two.log"
    a = _analyze_by(str(p), tmp_path, monkeypatch)
    b = _analyze_by("two.log", tmp_path, monkeypatch)
    c = _analyze_by("./two.log", tmp_path, monkeypatch)
    # 先证真：分析本身确实被调用到了，且内容一致
    assert len(a.clusters) == len(b.clusters) == len(c.clusters) > 0
    assert a.stats.source != b.stats.source, "两种写法没真的产生不同的 source"
    assert result_fingerprint(a) == result_fingerprint(b) == result_fingerprint(c), \
        "同一份日志换个路径写法就换了指纹 —— 对外那句承诺不成立"


def test_changing_only_the_path_does_not_change_the_fingerprint(
        tmp_path, monkeypatch) -> None:
    """把 `stats.source` 与每条引用的 source 一起改掉，指纹必须不动。

    这是上面那条的**加强版**：它不依赖「怎么把路径写成相对」，
    而是直接把路径字段改坏。如果哪天有人把 `source` 加回指纹，
    这里立刻红 —— 而不会等到有人从别的目录跑一次才发现。
    """
    r = _analyze(tmp_path)
    before = result_fingerprint(r)
    r.stats.source = "C:\\somewhere\\else\\完全不同的名字.log"
    for c in r.clusters:
        for e in c.evidence:
            object.__setattr__(e, "source", "C:\\somewhere\\else\\x.log")
        for v in (c.root_cause_variants or ()):
            if v.evidence is not None:
                object.__setattr__(v.evidence, "source", "C:\\elsewhere\\y.log")
    assert result_fingerprint(r) == before, \
        "只改路径就改变了指纹 —— 路径不是结论，它不该进来"


@pytest.mark.parametrize("field, value", [
    ("line_start", 999),
    ("line_end", 999),
    ("kind", "tampered_kind"),
    ("digest", "0" * 64),
])
def test_path_free_evidence_identity_still_discriminates(
        tmp_path, field, value) -> None:
    """去掉路径之后，证据身份**仍然**有判别力。

    这是修复的代价检查：换成「起止行 + 类型 + 摘要」之后，
    改动证据定位或原文都必须让指纹变 —— 否则等于用一个更弱的身份
    换掉了原来的 `evidence_id`，把「看不见回归」的门禁又装回去了。
    """
    r = _analyze(tmp_path)
    before = result_fingerprint(r)
    c = _confirmed_cluster(r)
    ref = c.evidence[0]
    object.__setattr__(ref, field, value)
    assert result_fingerprint(r) != before, \
        f"篡改证据的 {field} 后指纹纹丝不动 —— 证据身份没有判别力了"
