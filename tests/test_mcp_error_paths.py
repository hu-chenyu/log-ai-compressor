# -*- coding: utf-8 -*-
"""MCP 错误路径：内部异常**绝不能**泄漏 traceback 给 Agent。

Agent 通过 MCP 拿到的是一段文本。如果工具在异常时把 Python traceback
直接抛出去，Agent 会把栈里的文件路径、内部函数名当成「分析结论」读进去 ——
比报错更糟，因为它看起来像内容。

所以这里逐个工具验证同一件事：底层抛非 ServiceError 的异常时，
返回的必须是 ``{"ok": False, "error": 一行可读的话}``。

（写法跟 tests/test_mcp.py 对齐：用 asyncio.run 而非 pytest.mark.asyncio；
模块用 importlib 取，因为 log_ai_compressor.mcp 包把 server 对象导出成了属性，
``import a.b.c as x`` 会拿到那个对象而不是模块。）
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

srv = importlib.import_module("log_ai_compressor.mcp.server")

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"
V1 = BASE / "examples" / "app_v1.log"
V2 = BASE / "examples" / "app_v2.log"

# 泄漏到 Agent 侧就等于内容污染：这几样出现即判定失败
LEAK_MARKERS = ("Traceback", '  File "', "site-packages", "log_ai_compressor/")


async def _call(tool: str, **kwargs) -> dict:
    res = await srv.server.call_tool(tool, kwargs)
    if isinstance(res, (list, tuple)):
        first = res[0]
        text = first[0].text if isinstance(first, (list, tuple)) else first.text
    else:
        parts = getattr(res, "content", None) or []
        text = parts[0].text if parts else str(res)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"_raw": text}


class Boom(RuntimeError):
    """刻意不是 ServiceError —— 走 except Exception 那条分支。"""


@pytest.fixture
def blow_up(monkeypatch):
    def _apply(target: str):
        def _boom(*a, **k):
            raise Boom("底层炸了")
        monkeypatch.setattr(srv.S, target, _boom)
    return _apply


def _assert_clean_failure(got: dict, where: str) -> None:
    assert "_raw" not in got, f"{where}: 返回的不是 JSON，traceback 泄漏了 -> {got}"
    assert got.get("ok") is False, f"{where}: 应报失败，实际 {got}"
    err = got.get("error") or ""
    assert err, f"{where}: 没有 error 字段"
    for bad in LEAK_MARKERS:
        assert bad not in err, f"{where}: error 里混进了 {bad!r} -> {err[:200]}"


def test_analyze_log_file_swallows_internal_error(blow_up) -> None:
    blow_up("analyze")
    _assert_clean_failure(
        asyncio.run(_call("analyze_log_file", paths=[str(SAMPLE)])),
        "analyze_log_file")


def test_analyze_log_text_swallows_internal_error(blow_up) -> None:
    blow_up("analyze")
    _assert_clean_failure(
        asyncio.run(_call("analyze_log_text", text="whatever")),
        "analyze_log_text")


def test_compare_log_files_swallows_internal_error(blow_up) -> None:
    blow_up("compare")
    _assert_clean_failure(
        asyncio.run(_call("compare_log_files", paths=[str(V1), str(V2)])),
        "compare_log_files")


def test_export_report_swallows_internal_error(blow_up) -> None:
    blow_up("export_text")
    _assert_clean_failure(
        asyncio.run(_call("export_report", paths=[str(SAMPLE)], format="json")),
        "export_report")


def test_get_cluster_detail_swallows_internal_error(blow_up) -> None:
    blow_up("analyze")
    _assert_clean_failure(
        asyncio.run(_call("get_cluster_detail", path=str(SAMPLE), cluster_id=0)),
        "get_cluster_detail")


def test_verify_evidence_swallows_internal_error(blow_up, tmp_path) -> None:
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"clusters": []}), encoding="utf-8")
    blow_up("result_to_dict")
    _assert_clean_failure(
        asyncio.run(_call("verify_evidence", report_path=str(p))),
        "verify_evidence")


def test_export_report_missing_file_is_clean_error() -> None:
    got = asyncio.run(
        _call("export_report", paths=["Z:/no/such/file.log"], format="json"))
    _assert_clean_failure(got, "export_report 缺文件")


def test_verify_evidence_rejects_report_without_refs(tmp_path) -> None:
    """报告里没有证据引用时要说清楚，而不是给一个空成功。"""
    p = tmp_path / "no_refs.json"
    p.write_text(json.dumps({"clusters": []}), encoding="utf-8")
    got = asyncio.run(_call("verify_evidence", report_path=str(p)))
    assert got.get("ok") is False, got
    assert "证据" in (got.get("error") or ""), got


def test_err_formatter_has_no_traceback() -> None:
    """_err 只输出一行摘要：带类型名和消息，不带栈。"""
    try:
        raise Boom("根因在这")
    except Boom as exc:
        s = srv._err(exc)
    assert "Boom" in s and "根因在这" in s
    for bad in LEAK_MARKERS:
        assert bad not in s, s


def test_check_environment_declares_no_data_egress() -> None:
    """环境自检必须明确声明「不出网」，且本身不发起任何网络调用。

    刻意**不**去源码里搜 'httpx'/'requests' 这类词 —— check_environment 的
    docstring 和「未安装可选依赖 httpx」的提示串里就有这些字，但那是**说明**
    不是调用。按词搜索会得出「它触网」的错误结论（写这条测试时真踩过）。
    """
    import asyncio as _a
    got = _a.run(_call("check_environment"))
    assert got.get("ok") is True, got
    assert "不出网" in str(got.get("data_egress", "")), got
    # 结构自洽：版本号与包一致，说明确实在报告本机环境
    import log_ai_compressor as _pkg
    assert got.get("version") == _pkg.__version__, got
