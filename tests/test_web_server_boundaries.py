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

import importlib
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


@pytest.fixture()
def fs_root_env(tmp_path, monkeypatch):
    """把 LOG_AI_FS_ROOT **真的塞进环境变量**再重新加载模块。

    为什么不用 ``monkeypatch.setattr(S, "FS_ROOT", ...)``（本文件其它用例
    就是那么写的）：setattr 只测「FS_ROOT 为真值时闸门生效」，把
    `server.py:68` 那根 ``os.environ.get("LOG_AI_FS_ROOT")`` 接线整个绕过去
    了 —— 哪天环境变量被改名、或那行被删，setattr 的用例照样全绿。
    reload 让这根接线也落进被守范围（下面那句 assert 就是它的守门）。

    为什么 reload 是安全的：``importlib.reload`` **复用同一个 module
    对象**（只重新执行代码），别处持有的 S 引用不会失效成旧壳；
    而 FS_ROOT 会在 teardown 里再 reload 一次还原，避免污染后续用例。
    """
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "inside.log").write_text("TOPSECRET-CONTENT-MARKER\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.log").write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setenv("LOG_AI_FS_ROOT", str(root))
    importlib.reload(S)
    # 这条断言本身就是被守对象：环境变量没能变成 FS_ROOT，下面的
    # 400 断言就变成了「没设限根时的行为」，测试会给出误导性的绿。
    assert S.FS_ROOT == str(root), "LOG_AI_FS_ROOT 没有变成 FS_ROOT"
    try:
        yield root
    finally:
        monkeypatch.undo()          # 先撤环境变量
        importlib.reload(S)          # 再让 FS_ROOT 回到环境的真实值


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


def test_fs_file_blocks_escape(fs_root_env, client) -> None:
    """/api/fs/file 也必须受 LOG_AI_FS_ROOT 约束 —— 旋钮不能只管一个接口。

    此前这个接口根本不调 `_within_root`，于是设了根目录之后，
    /api/fs/list 挡住越界、/api/fs/file 照样把根外文件的元数据端出去。
    已知威胁模型下不可利用（只绑 127.0.0.1，且只回元数据不回内容），
    但「提供了限根旋钮」与「旋钮名不副实」是两回事。

    用 `fs_root_env`（真设环境变量 + reload）而不是 monkeypatch FS_ROOT：
    这里要连 `server.py:68` 那根环境变量接线一起守住。
    """
    outside = fs_root_env.parent / "outside" / "secret.log"
    bad = client.get("/api/fs/file", params={"path": str(outside)})
    assert bad.status_code == 400, bad.text
    detail = bad.json()["detail"]
    assert str(fs_root_env) in detail and "只能访问" in detail, detail


def test_fs_file_inside_root_still_works(fs_root_env, client) -> None:
    """闸门不能紧到把功能也关掉：根内的文件必须照常返回元数据。

    与上一条成对（AGENTS.md §6.3）：只测「该拒的拒了」，闸门写成
    「恒返回 400」测试照样是绿的。
    """
    ok = client.get("/api/fs/file",
                    params={"path": str(fs_root_env / "inside.log")})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["is_dir"] is False
    assert body["size"] > 0
    assert body["big"] is False
    # 文件内容里埋了标记：元数据接口不得把正文带出来
    assert "TOPSECRET-CONTENT-MARKER" not in ok.text, \
        "本接口只回元数据，不该带出文件内容"


def test_fs_file_escape_not_leaking_existence(fs_root_env, client) -> None:
    """根外的**不存在**路径也必须 400，不能退化成 404。

    顺序反了（先 is_file 再查根）的话，404 与 400 的差别就成了一个
    存在性探测器：这个接口在本机就能问出任意路径存不存在。
    """
    ghost = fs_root_env.parent / "outside" / "definitely-not-here.log"
    r = client.get("/api/fs/file", params={"path": str(ghost)})
    assert r.status_code == 400, r.text


def test_fs_file_dotdot_escape_blocked(fs_root_env, client) -> None:
    """`..` 形式同样要拦住，且它指向的文件必须真实存在。

    没有「文件真的存在」这个前提，400 可能只是因为 404 —— 那这条测试
    什么都没证明。
    """
    inside = client.get("/api/fs/file",
                        params={"path": str(fs_root_env / "sub" / ".." / ".."
                                            / "outside" / "secret.log")})
    assert inside.status_code == 400, inside.text


def test_can_go_up_reflects_fs_root(fs_root_env, client) -> None:
    """限根后 can_go_up 必须跟着根变 —— 这是修 fs_file 时最容易顺手改坏的一行。

    `root` 的父目录在根外 → 不能往上；`root/sub` 的父目录就是 root →
    可以往上。写反了会出现「越界按钮照样能点」或「根目录里寸步难行」。
    """
    at_root = client.get("/api/fs/list", params={"path": str(fs_root_env)})
    assert at_root.status_code == 200, at_root.text
    assert at_root.json()["can_go_up"] is False, \
        "根目录的父目录在根外，can_go_up 应为 False"

    in_sub = client.get("/api/fs/list", params={"path": str(fs_root_env / "sub")})
    assert in_sub.status_code == 200, in_sub.text
    assert in_sub.json()["can_go_up"] is True, \
        "子目录的父目录仍在根内，can_go_up 应为 True"


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
