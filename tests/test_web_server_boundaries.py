# -*- coding: utf-8 -*-
"""Web 接入层的**边界**用例 —— 上一轮审查指出这里最薄（32 行未覆盖）。

为什么单独开这个文件
------------------
`web/` 与 `ai/` 是可选 extra，测试在没装 `fastapi` 的环境里整片被
`importorskip` 跳过 —— 于是它在覆盖率报告上是一块**没人看过的区域**，
而它恰好是唯一直接对外暴露 HTTP 的地方。

最要紧的一条在这里：**路径逃逸防护**（`web/server.py:106`）零测试覆盖。
一个没有测试的安全闸门，和没有闸门的区别只存在于「读者相信它存在」这件事上。

其余各条都是**可达但没人走过**的分支：OSError 兜底、403/404/500、
脱敏在 compare 模式下的生效点、配置非法、端口占满、浏览器自动打开。
它们不是「测 mock」，每一条都对应一次真实排障里会遇到的场景。
"""
from __future__ import annotations

import pathlib
import socket

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from log_ai_compressor.web import server as S  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(S.create_app()) as c:
        yield c


# ---------------------------------------------------------------------------
# 1) 路径逃逸防护（安全闸门，必须有测试）
# ---------------------------------------------------------------------------


def test_fs_root_blocks_escape(client, tmp_path, monkeypatch) -> None:
    """FS_ROOT 生效时，越界路径必须 400 —— 不能只靠读代码确认。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.log").write_text("hello\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.log").write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setattr(S, "FS_ROOT", str(root))

    ok = client.get("/api/fs/list", params={"path": str(root / "ok.log")})
    assert ok.status_code == 200, ok.text

    bad = client.get("/api/fs/list", params={"path": str(outside / "secret.log")})
    assert bad.status_code == 400, bad.text
    assert "root" in bad.text or "只能浏览" in bad.text, bad.text


def test_fs_root_blocks_dotdot_escape(client, tmp_path, monkeypatch) -> None:
    """`..` 形式的逃逸也要拦住 —— 只测绝对路径等于没测。

    绝对路径一眼就能看出越界，而 `../..` 是手写路径时最自然的形式，
    也正是历史上各类目录穿越漏洞的输入形状。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.log").write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setattr(S, "FS_ROOT", str(root))

    r = client.get("/api/fs/list", params={"path": f"{root / 'sub' / '..' / '..'}"})
    assert r.status_code == 400, r.text


def test_fs_root_allows_relative_under_root(client, tmp_path, monkeypatch) -> None:
    """限根之后，相对路径仍要能用 —— 否则闸门太紧把功能也关掉了。"""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.log").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(S, "FS_ROOT", str(root))
    r = client.get("/api/fs/list", params={"path": "sub"})
    assert r.status_code == 200, r.text
    assert "a.log" in r.text


def test_file_path_falls_back_to_parent_dir(client, tmp_path, monkeypatch) -> None:
    """给一个**文件**路径时应当回落到它所在目录，而不是报 400/404。

    前端会把用户选中的文件路径直接传回来，这条分支每天都在走。
    """
    monkeypatch.setattr(S, "FS_ROOT", None)
    f = tmp_path / "x.log"
    f.write_text("x\n", encoding="utf-8")
    r = client.get("/api/fs/list", params={"path": str(f)})
    assert r.status_code == 200, r.text
    assert "x.log" in r.text


def test_missing_path_is_404(client, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(S, "FS_ROOT", None)
    r = client.get("/api/fs/list", params={"path": str(tmp_path / "nope_zzz")})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 2) 文件列举的 OSError 兜底（文件浏览器最常见的真实故障）
# ---------------------------------------------------------------------------


def test_entry_survives_stat_failure(client, tmp_path, monkeypatch) -> None:
    """某个条目 stat 失败时，整个目录列举不能变成 500。

    场景很具体：枚举到一半那个文件被别的进程删了。旧实现对同一路径
    stat 两次，第二次抛异常会把整个目录列举打成 500 —— 用户看到的是
    「文件浏览器坏了」，真因只是少了一个文件。
    """
    monkeypatch.setattr(S, "FS_ROOT", None)
    d = tmp_path / "dir"
    d.mkdir()
    (d / "a.log").write_text("a\n", encoding="utf-8")
    (d / "b.log").write_text("b\n", encoding="utf-8")

    real_stat = pathlib.Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *a, **kw):
        # 只让**第一次** stat 失败：`_fs_entry` 的 except 分支会调 `is_dir()`，
        # 而 `is_dir()` 内部同样要 stat。两次都失败的话，抛出去的是
        # 「模拟得不像」，不是「条目刚好消失」——那测的就不是这条分支了。
        if self.name == "a.log":
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated: file vanished")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)
    r = client.get("/api/fs/list", params={"path": str(d)})
    assert r.status_code == 200, r.text
    assert "b.log" in r.text, "一个条目 stat 失败把整个列举带崩了"


def test_permission_error_is_403(client, tmp_path, monkeypatch) -> None:
    """目录不可读 → 403，不是 500。"""
    monkeypatch.setattr(S, "FS_ROOT", None)
    d = tmp_path / "locked"
    d.mkdir()

    def boom(self):
        raise PermissionError("simulated: denied")

    monkeypatch.setattr(pathlib.Path, "iterdir", boom)
    r = client.get("/api/fs/list", params={"path": str(d)})
    assert r.status_code == 403, r.text


def test_other_oserror_is_400(client, tmp_path, monkeypatch) -> None:
    """非权限类的 OSError → 400，消息里带上原因。

    与 403 分开是有意义的：403 告诉用户「换个目录或改权限」，400 告诉
    用户「这个路径本身有问题」。混成一个码，排查方向就丢了。
    """
    monkeypatch.setattr(S, "FS_ROOT", None)
    d = tmp_path / "weird"
    d.mkdir()

    def boom(self):
        raise OSError("simulated: filename too long")

    monkeypatch.setattr(pathlib.Path, "iterdir", boom)
    r = client.get("/api/fs/list", params={"path": str(d)})
    assert r.status_code == 400, r.text
    assert "filename too long" in r.text


def test_human_size_never_reaches_tb_fallback() -> None:
    """`_human_size` 的 TB 兜底 return 是**死代码**，这里把理由钉死。

    for 循环最后一轮 `unit == "TB"` 必然 return，所以最后那行
    `return f"{n:.1f} TB"` 永远走不到。与其留一行骗人的代码，
    不如用一条测试说明「这里不存在漏测的分支」—— 将来谁重构了
    `_human_size`，这条会立刻指出那个 return 到底是不是还需要。
    """
    assert S._human_size(0) == "0 B"
    assert S._human_size(1023) == "1023 B"
    assert S._human_size(1024) == "1.0 KB"
    assert S._human_size(1024 ** 2) == "1.0 MB"
    assert S._human_size(1024 ** 3) == "1.0 GB"
    assert S._human_size(1024 ** 4) == "1.0 TB"
    # 超过 1 PB 仍在 TB 收尾（这正是死代码那行本想处理的情形）
    assert S._human_size(1024 ** 5).endswith("TB")


# ---------------------------------------------------------------------------
# 3) 任务与复核的 404 / 500
# ---------------------------------------------------------------------------


def test_cancel_unknown_job_is_404(client) -> None:
    r = client.post("/api/jobs/does-not-exist/cancel")
    assert r.status_code == 404, r.text


def test_verify_unknown_job_is_404(client) -> None:
    r = client.post("/api/verify", json={"job_id": "nope"})
    assert r.status_code == 404, r.text


def test_verify_failure_becomes_500_with_message(client, monkeypatch) -> None:
    """复核抛异常 → 500 且带原因，不是一句 Internal Server Error。

    前端拿到空消息时没法区分「结果被淘汰」和「复核本身坏了」。
    """
    job = S.J.REGISTRY.create("text", {"text": "x"})
    job.result = object()
    import log_ai_compressor.service as svc
    monkeypatch.setattr(svc, "verify_result",
                        lambda r: (_ for _ in ()).throw(RuntimeError("simulated boom")))
    r = client.post("/api/verify", json={"job_id": job.id})
    assert r.status_code == 500, r.text
    assert "复核失败" in r.text or "boom" in r.text


# ---------------------------------------------------------------------------
# 4) 端口选择与自动开浏览器
# ---------------------------------------------------------------------------


def test_pick_port_finds_a_free_one() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert S.pick_port("127.0.0.1", port) == port


def test_pick_port_walks_forward_when_taken() -> None:
    """起始端口被真的在监听时必须顺延 —— 这正是它存在的理由。

    回归：旧实现用 bind + SO_REUSEADDR 探测，而 Windows 上那个选项
    允许绑到已在监听的端口，于是探测永远"成功"，被占端口原样返回。

    注意 socket 必须在**探测期间保持打开** —— 写在 `with` 块外面的话
    它已经被 close 了，端口当然是空的（这个坑我自己踩了一次）。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        got = S.pick_port("127.0.0.1", port, tries=5)
    assert got > port, f"被占端口 {port} 被原样返回了（探测失效）"


def test_port_in_use_detects_a_listening_socket() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert S._port_in_use("127.0.0.1", port) is True
    assert S._port_in_use("127.0.0.1", port) is False


def test_pick_port_gives_up_with_an_actionable_message(monkeypatch) -> None:
    """全被占完 → 抛出的消息必须告诉用户怎么办。"""
    monkeypatch.setattr(S, "_port_in_use", lambda host, port: True)
    with pytest.raises(OSError) as ei:
        S.pick_port("127.0.0.1", 50000, tries=3)
    assert "--port" in str(ei.value), str(ei.value)




def test_serve_opens_browser_only_when_asked(monkeypatch) -> None:
    """open_browser=False 时绝不能弹浏览器 —— 无头 CI 里弹了就是事故。"""
    calls = []
    monkeypatch.setattr(S, "pick_port", lambda host, port: port)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                        type("M", (), {"run": staticmethod(lambda *a, **k: calls.append("run"))}))
    monkeypatch.setattr(S.webbrowser if hasattr(S, "webbrowser") else __import__("webbrowser"),
                        "open", lambda url: calls.append("open"))
    S.serve(host="127.0.0.1", port=8799, open_browser=False)
    assert calls == ["run"], calls
