# -*- coding: utf-8 -*-
"""scripts/audit_published_versions.py 的 https 守卫。

**为什么单独一个测试文件**
------------------------
SAST 基线里 `scripts/audit_published_versions.py` 的 B310（urllib 不校验
scheme）写的是「有 `_require_https()` 挡着」。这句话如果没人验，它就是一句
注释 —— 而注释不会因为守卫被删掉而变红。

所以：删掉守卫 → 这个测试红 → 基线那条理由立刻变成假话。

这一条的判据与 tests/test_fetch_datasets_scheme_guard.py 完全同构：
**守卫必须挂在 urlopen 之前**，不是事后检查。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "audit_published_versions", ROOT / "scripts" / "audit_published_versions.py")
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
sys.modules["audit_published_versions"] = mod
_SPEC.loader.exec_module(mod)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x.tgz",
    "http://pypi.org/pypi/log-ai-compressor/json",   # 降级到明文
    "gopher://example.com",
    "//pypi.org/x",            # 无 scheme
    "",
])
def test_non_https_is_rejected(url):
    with pytest.raises(ValueError):
        mod._require_https(url)


@pytest.mark.parametrize("url", [
    "https://pypi.org/pypi/log-ai-compressor/json",
    "HTTPS://pypi.org/x",
])
def test_https_is_allowed(url):
    mod._require_https(url)          # 不抛即通过


def test_guard_runs_before_any_network_call(monkeypatch):
    """守卫必须在 urlopen **之前**跑，不是事后检查。

    判据认的是「打开之前就已经拒绝」，不是「最终报了个错」。
    """
    called = []

    def fake_urlopen(*a, **kw):        # pragma: no cover - 必须不被调用
        called.append(a)
        raise AssertionError("非 https 的 URL 竟然发起了网络请求")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError):
        mod.get_json("file:///etc/passwd")
    assert called == []


def test_fetch_also_goes_through_the_guard(monkeypatch, tmp_path):
    """fetch 是下载那一路，也必须自己过守卫 —— 不能靠调用方记得调。"""
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("不该发起请求")))
    with pytest.raises(ValueError):
        mod.fetch("http://example.com/x.tgz", tmp_path / "x.tgz", 1)
