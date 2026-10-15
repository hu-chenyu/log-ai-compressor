# -*- coding: utf-8 -*-
"""簇内全部内层因（root_cause_variants）必须在**每一路出口**上都能看到。

为什么单独一组用例：v2.6.0 的「一簇只报一因」缺陷之所以能一路藏着，
是因为「只报一条」在每个出口上都**看不出异常** —— JSON 少个字段、报告
少一行、MCP 少一个键，谁都不会发现。只有当修复之后逐路确认都带上了，
才叫真的修好；将来有人重构导出层不小心把这个字段漏掉，这里会直接红。

四路出口：
1. `service.result_to_dict`（Web API / MCP 共同底层）
2. JSON 报告（`log-ai-compressor` 落盘的那份）
3. Markdown / 文本报告（人读的那份）
4. MCP 工具说明（Agent 读到的方法论，否则它会只报主因）

外加一条：**单一内层因**时不许出现「簇内全部内层因」行 —— 只有一个因也
列一遍是噪音，而噪音会让人忽略真正需要看的那几行。
"""
from __future__ import annotations

from pathlib import Path

from log_ai_compressor import service
from log_ai_compressor.export import reporters

HEAD = "2024-01-15 10:00:00"


def _two_cause_log(tmp_path: Path) -> Path:
    lines = [
        f"{HEAD} ERROR java.lang.RuntimeException: a failed",
        "Caused by: com.acme.First: root of it",
        f"{HEAD} INFO chatter a",
        "",
        f"{HEAD} ERROR java.lang.RuntimeException: b failed",
        "Caused by: com.acme.Second: root of it",
        f"{HEAD} INFO chatter b",
    ]
    p = tmp_path / "two.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _one_cause_log(tmp_path: Path) -> Path:
    lines = [
        f"{HEAD} ERROR java.lang.RuntimeException: solo failed",
        "Caused by: com.acme.Only: root of it",
        f"{HEAD} INFO chatter",
    ]
    p = tmp_path / "one.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _analyze(path: Path):
    return service.analyze(paths=[str(path)],
                           params={"top_n": 0, "context_lines": 0})


# ---------------------------------------------------------------------------
# 1) service.result_to_dict（Web API 与 MCP 的共同底层）
# ---------------------------------------------------------------------------


def test_service_dict_carries_every_variant(tmp_path: Path) -> None:
    r = _analyze(_two_cause_log(tmp_path))
    d = service.result_to_dict(r, top_n=0, with_samples=False)
    c = next(x for x in d["clusters"]
             if x.get("root_cause_variants"))
    variants = c["root_cause_variants"]
    assert len(variants) == 2, variants
    got = {v["reason"].split(":", 1)[0] for v in variants}
    assert got == {"com.acme.First", "com.acme.Second"}, got
    assert all(v["count"] == 1 for v in variants), variants
    # 每条变体都要能指回原文（这是本项目不可退的一档）
    assert all(v["citation"] for v in variants), variants


def test_service_dict_omits_empty_variants_for_single_cause(tmp_path: Path) -> None:
    """单一内层因：字段在（结构统一），但只有一条、不重复报。"""
    r = _analyze(_one_cause_log(tmp_path))
    d = service.result_to_dict(r, top_n=0, with_samples=False)
    c = next(x for x in d["clusters"] if x.get("root_cause_confidence"))
    assert len(c["root_cause_variants"]) == 1, c["root_cause_variants"]


# ---------------------------------------------------------------------------
# 2) JSON 报告
# ---------------------------------------------------------------------------


def test_json_report_carries_variants(tmp_path: Path) -> None:
    import json as _json
    r = _analyze(_two_cause_log(tmp_path))
    d = _json.loads(reporters.to_json(r, top_n=0))
    c = next(x for x in d["clusters"] if x.get("root_cause_variants"))
    reasons = [v["reason"] for v in c["root_cause_variants"]]
    assert any("First" in x for x in reasons), reasons
    assert any("Second" in x for x in reasons), reasons


# ---------------------------------------------------------------------------
# 3) Markdown / 文本报告
# ---------------------------------------------------------------------------


def test_markdown_report_lists_every_cause(tmp_path: Path) -> None:
    r = _analyze(_two_cause_log(tmp_path))
    text = reporters.to_markdown(r, top_n=0)
    assert "簇内全部内层因" in text, \
        "报告里没有「簇内全部内层因」这一行 —— 同簇的另一个因又丢了"
    assert "com.acme.First" in text and "com.acme.Second" in text, \
        "两个内层因都要出现在报告里"


def test_markdown_stays_quiet_for_single_cause(tmp_path: Path) -> None:
    """只有一个因时不要多列一行 —— 噪音会让真正该看的那几行被忽略。"""
    r = _analyze(_one_cause_log(tmp_path))
    text = reporters.to_markdown(r, top_n=0)
    assert "簇内全部内层因" not in text, \
        "单一内层因时仍列了变体行，属于噪音"


# ---------------------------------------------------------------------------
# 4) MCP 方法论：Agent 得知道这个字段怎么用
# ---------------------------------------------------------------------------


def test_mcp_instructions_teach_variants() -> None:
    # 注意：`log_ai_compressor.mcp.server` 这个名字在包级别被 MCPServer
    # 实例遮蔽了（mcp/__init__.py 里 server = Server(...)），所以
    # `from ... import server` 拿到的是实例而不是模块，必须 importlib。
    import importlib
    instructions = importlib.import_module(
        "log_ai_compressor.mcp.server").INSTRUCTIONS
    assert "root_cause_variants" in instructions, (
        "MCP 说明里没有 root_cause_variants —— Agent 会当噪声忽略，"
        "然后只转述主因，等于把刚修好的漏报在出口上原样放回去")
    assert "不要只报" in instructions, \
        "说明只介绍了字段、没讲该怎么用（要不要全报、按什么顺序报）"
