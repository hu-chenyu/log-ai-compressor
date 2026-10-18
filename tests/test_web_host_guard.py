# -*- coding: utf-8 -*-
"""Web 服务必须校验 Host 头。

背景
----
`server.py` 的模块 docstring 曾经论证「没有鉴权是有意的：它等价于
『本机进程』」，理由是「浏览器同源策略也阻止远端页面打这个端口」。

**那条理由是假的。** 同源策略挡不住 DNS rebinding：攻击者把自己控制的
`evil.test` 解析到 `127.0.0.1`，浏览器发出的就是**同源**请求，同源策略因此
放行；可 Host 头是 `evil.test`，服务端若不校验就照样应答。

审计实测（2026-10-18，真实 uvicorn + 真实 TCP）：

    无伪造 Host                /api/health -> 200  完整负载
    Host:evil.attacker.example /api/health -> 200  完整负载
    Host:localhost.evil.test:9999 /api/health -> 200  完整负载

所以边界是**两道独立的门**：「回环绑定」+「Host 校验」。少一道，
「只有本机能访问」这个推论就不成立。这组测试守住第二道。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from log_ai_compressor.web import server as W


def _app(**kw):
    return TestClient(W.create_app(**kw))


# ---------------------------------------------------------------------------
# 拒绝：伪造的 Host
# ---------------------------------------------------------------------------
class TestForgedHostIsRejected:
    def test_rebinding_domain_is_rejected(self):
        """攻击者控制的域名 —— 正是 DNS rebinding 的形状。"""
        with _app() as c:
            r = c.get("/api/health", headers={"Host": "evil.attacker.example"})
        assert r.status_code == 403, (
            f"伪造 Host 被放行了，HTTP {r.status_code}；"
            "这正是 DNS rebinding 用来绕过「只绑回环」的那一步")
        assert r.json()["ok"] is False

    def test_lookalike_loopback_domain_is_rejected(self):
        """`localhost.evil.test` 以 localhost 开头，但控制权在攻击者手里。"""
        with _app() as c:
            r = c.get("/api/health",
                      headers={"Host": "localhost.evil.test:9999"})
        assert r.status_code == 403, "前缀像回环不等于就是回环"

    def test_arbitrary_public_domain_is_rejected(self):
        with _app() as c:
            r = c.get("/api/health", headers={"Host": "example.com"})
        assert r.status_code == 403

    @pytest.mark.parametrize("path", ["/api/fs/list", "/api/fs/file", "/api/docs"])
    def test_guard_covers_the_whole_app(self, path):
        """守卫挂在**整个应用**上，不能只挡 /api/health。"""
        with _app() as c:
            r = c.get(path, headers={"Host": "evil.attacker.example"})
        assert r.status_code == 403, f"{path} 绕过了 Host 守卫"

    def test_error_body_does_not_leak_internals(self):
        with _app() as c:
            r = c.get("/api/health", headers={"Host": "evil.attacker.example"})
        assert "Traceback" not in r.text
        assert "127.0.0.1" not in r.json()["error"], (
            "回显 Host 时别把绑定地址也一起带出去")


# ---------------------------------------------------------------------------
# 放行：真正的本机访问
# ---------------------------------------------------------------------------
class TestLoopbackHostIsAccepted:
    @pytest.mark.parametrize("host", [
        "127.0.0.1:8765",
        "127.0.0.1",
        "localhost:8765",
        "localhost",
        "[::1]:8765",
        "LOCALHOST:1234",
        "127.0.0.1:65535",
    ])
    def test_loopback_forms_pass(self, host):
        with _app() as c:
            r = c.get("/api/health", headers={"Host": host})
        assert r.status_code == 200, f"本机 Host {host} 被误拒"

    @pytest.mark.parametrize("raw", [None, ""])
    def test_missing_host_header_is_allowed(self, raw):
        """HTTP/1.0 客户端可能不带 Host；没有可伪造的来源，不拦。"""
        assert W._host_allowed(raw, W._LOOPBACK_HOSTS) is True


# ---------------------------------------------------------------------------
# --allow-remote 的显式让步，且只在非回环时生效
# ---------------------------------------------------------------------------
class TestAllowRemoteOptOut:
    def test_wildcard_disables_the_guard(self):
        """用户显式 --allow-remote 后再拦 Host，只会让他够不着自己的服务。"""
        with _app(allowed_hosts=("*",)) as c:
            r = c.get("/api/health", headers={"Host": "192.168.1.50:8765"})
        assert r.status_code == 200

    @pytest.mark.parametrize("bind,want_forged_host_accepted", [
        ("127.0.0.1", False),
        ("localhost", False),
        ("127.0.0.5", False),
        ("0.0.0.0", True),
        ("::", True),
    ])
    def test_serve_relaxes_the_guard_only_for_non_loopback_bind(
            self, monkeypatch, bind, want_forged_host_accepted):
        """知情同意的落点就在这里：绑回环就守，绑全网就放。

        判据不是「用户加了什么参数」，而是**实际绑在哪儿** —— 免得哪天
        有人把默认值改成 0.0.0.0 时守卫跟着一起消失。
        """
        import uvicorn

        captured = {}

        def _fake_run(app, host, port, log_level):  # noqa: ANN001
            captured["app"] = app

        monkeypatch.setattr(uvicorn, "run", _fake_run)
        W.serve(bind, port=9999, open_browser=False, auto_port=False)

        with TestClient(captured["app"]) as c:
            r = c.get("/api/health", headers={"Host": "evil.attacker.example"})
        got = r.status_code == 200
        assert got is want_forged_host_accepted, (
            f"绑 {bind} 时，伪造 Host 被接受={got}，期望 {want_forged_host_accepted}")


# ---------------------------------------------------------------------------
# 主机名解析本身（守卫写错就等于没守）
# ---------------------------------------------------------------------------
class TestHostNameParsing:
    @pytest.mark.parametrize("raw,expect", [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost:1234", "localhost"),
        ("[::1]:8765", "::1"),
        ("::1", "::1"),
        ("  localhost:80  ", "localhost"),
    ])
    def test_strips_port_correctly(self, raw, expect):
        assert W._host_name(raw) == expect

    def test_ipv6_bracketed_keeps_both_colons(self):
        """`[::1]:8765` 里有 2 个冒号，不能按「剥端口」那套切。"""
        assert W._host_name("[::1]:8765") == "::1"
