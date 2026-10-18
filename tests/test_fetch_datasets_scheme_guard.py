# -*- coding: utf-8 -*-
"""scripts/fetch_datasets.py 的 https 白名单守卫。

**为什么单独一个测试文件**
------------------------
SAST 基线里 `scripts/fetch_datasets.py` 的两条 B310（urllib 不校验
scheme）写的是「已修过一半：`_require_https()` 挡着」。这句话如果没人验，
它就是一句注释 —— 而注释不会因为守卫被删掉而变红。

所以：删掉守卫 → 这个测试红 → 基线那条理由立刻变成假话，
下一个人会以为「反正基线里已经豁免了」。这正是本项目记过多次的形状：
**判据落在被守的东西的邻居上，而不是被守的东西本身。**
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "fetch_datasets", ROOT / "scripts" / "fetch_datasets.py")
assert _SPEC and _SPEC.loader
fetch = importlib.util.module_from_spec(_SPEC)
sys.modules["fetch_datasets"] = fetch
_SPEC.loader.exec_module(fetch)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x.zip",
    "http://zenodo.org/api/records/1/files/x/content",   # 降级到明文
    "gopher://example.com",
    "//zenodo.org/x",            # 无 scheme
    "",
])
def test_non_https_is_rejected(url):
    with pytest.raises(ValueError):
        fetch._require_https(url)


@pytest.mark.parametrize("url", [
    "https://zenodo.org/api/records/8275861/files/Proxifier.zip/content",
    "HTTPS://zenodo.org/x",
])
def test_https_is_allowed(url):
    fetch._require_https(url)          # 不抛即通过


def test_every_declared_dataset_url_is_https():
    """表里拼出来的 URL 必须全是 https —— 守卫不能只对测试样例有效。"""
    for record, filename, _sub, _desc in fetch.LOGS_2_0:
        fetch._require_https(fetch.url_for(record, filename))
    for entry in fetch.EXTRA:
        record, filename = entry[0], entry[1]
        fetch._require_https(fetch.url_for(record, filename))


def test_guard_is_called_before_any_network_call(monkeypatch):
    """守卫必须在 urlopen **之前**跑，不是事后检查。

    判据认的是「打开之前就已经拒绝」，不是「最终报了个错」。
    """
    called = []

    def fake_urlopen(*a, **kw):        # pragma: no cover - 必须不被调用
        called.append(a)
        raise AssertionError("非 https 的 URL 竟然发起了网络请求")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError):
        fetch.total_size("file:///etc/passwd")
    assert called == []


def test_total_size_calls_the_guard(monkeypatch):
    """total_size 是 urlopen 的第一站，守卫必须挂在它里面而不是靠调用方记得调。"""
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("不该发起请求")))
    with pytest.raises(ValueError):
        fetch.total_size("http://zenodo.org/x")
