# -*- coding: utf-8 -*-
"""Web 接入层测试：REST 契约、SSE 帧格式、参数拦截、文件浏览边界。

用 FastAPI TestClient（httpx），不真起端口，因此可在 CI 无头环境跑。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from log_ai_compressor.web.server import create_app  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"
GBK = BASE / "examples" / "sample_gbk.log"
V1 = BASE / "examples" / "app_v1.log"
V2 = BASE / "examples" / "app_v2.log"
STATIC = BASE / "log_ai_compressor" / "web" / "static"


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def _wait(client, job_id, limit=30.0):
    """轮询任务到终态，返回状态快照。"""
    deadline = time.time() + limit
    snap = {}
    while time.time() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in ("done", "error"):
            return snap
        time.sleep(0.05)
    return snap


def _run(client, **body):
    """建任务并等到终态，返回 job_id。"""
    r = client.post("/api/analyze", json=body)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert _wait(client, job_id)["state"] == "done"
    return job_id


def _sse(client, job_id):
    """读取 SSE 全文并解析成 [(event, data)]，顺带校验每条 data 都是合法 JSON。"""
    with client.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
        body = "".join(resp.iter_text())
    frames = []
    for block in body.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if name and data is not None:
            # 关键：data 必须是合法 JSON，浏览器 JSON.parse 依赖这一点
            frames.append((name, json.loads(data)))
    return frames


# ---------------------------------------------------------------------------
# 元信息 / 静态资源
# ---------------------------------------------------------------------------
class TestMeta:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["app"] == "log-ai-compressor"
        assert set(body["rules"]) >= {"generic", "embedded", "jenkins"}
        assert "available" in body["ai"]

    @pytest.mark.parametrize("name,needle", [
        # needle 只是"文件内容确实被送达"的探针，不是品牌断言 —— 产品名从
        # 「日志AI压缩器」改为「本机日志取证层」时同步更新即可
        ("index.html", "本机日志取证层"),
        ("app.js", "State"),
        ("style.css", "--accent"),
    ])
    def test_static_assets(self, client, name, needle):
        """前端是零构建静态资源，必须随包分发，否则 pip 安装后界面 404。"""
        assert (STATIC / name).is_file(), f"缺少静态资源 {name}"
        r = client.get(f"/{name}")
        assert r.status_code == 200
        assert needle in r.text

    def test_no_external_cdn(self, client):
        """完全离线可用：页面不得从外部加载任何资源。

        只禁「带 scheme 的绝对地址」与「协议相对地址」；`app.js` 这类
        同源相对路径是本地静态资源，必须放行。
        """
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        stripped = re.sub(r'xmlns=["\']https?://[^"\']+["\']', "", html)
        assert "http://" not in stripped, "index.html 引用了外部 http 资源"
        assert "https://" not in stripped, "index.html 引用了外部 https 资源"

        url_re = re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']')
        for url in url_re.findall(html):
            if url.startswith(("data:", "#", "/")) or ":" not in url:
                continue                      # 内联 / 锚点 / 同源绝对路径 / 相对路径
            assert not url.startswith("//"), f"协议相对地址（外部资源）：{url}"
            assert False, f"外部资源：{url}"


# ---------------------------------------------------------------------------
# 文件浏览
# ---------------------------------------------------------------------------
class TestFileBrowser:
    def test_list_home(self, client):
        r = client.get("/api/fs/list?path=")
        assert r.status_code == 200
        assert "entries" in r.json()

    def test_list_examples_dir(self, client):
        r = client.get("/api/fs/list", params={"path": str(BASE / "examples")})
        assert r.status_code == 200
        names = {e["name"] for e in r.json()["entries"]}
        assert "sample_system.log" in names

    def test_file_info(self, client):
        r = client.get("/api/fs/file", params={"path": str(SAMPLE)})
        assert r.status_code == 200
        assert r.json()["size"] > 0
        assert r.json()["is_dir"] is False

    def test_missing_file_404(self, client):
        assert client.get("/api/fs/file",
                          params={"path": "C:/no/such.log"}).status_code == 404

    def test_missing_dir_404(self, client):
        r = client.get("/api/fs/list", params={"path": "/no/such/dir/at/all"})
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_relative_path_resolves_against_home(self, client):
        """相对路径按主目录解析 —— 行为必须跨平台一致。"""
        r = client.get("/api/fs/list", params={"path": "."})
        assert r.status_code == 200
        assert Path(r.json()["path"]).is_absolute()


# ---------------------------------------------------------------------------
# 分析任务
# ---------------------------------------------------------------------------
class TestAnalyzeJob:
    def test_file_job_completes(self, client):
        job_id = _run(client, mode="file", paths=[str(SAMPLE)],
                      params={"levels": ["ERROR", "FAIL"]})
        snap = client.get(f"/api/jobs/{job_id}").json()
        assert snap["state"] == "done"
        assert snap["has_result"] is True

    def test_text_job(self, client):
        job_id = _run(client, mode="text",
                      text="2024-01-01 10:00:00 ERROR [db] boom\n"
                           "2024-01-01 10:00:01 ERROR [db] boom\n")
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"

    def test_compare_job(self, client):
        job_id = _run(client, mode="compare", paths=[str(V1), str(V2)])
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"

    def test_gbk_job(self, client):
        job_id = _run(client, mode="file", paths=[str(GBK)])
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"

    def test_cancel(self, client):
        r = client.post("/api/analyze", json={"mode": "file",
                                              "paths": [str(SAMPLE)], "params": {}})
        assert client.post(f"/api/jobs/{r.json()['job_id']}/cancel"
                           ).json()["cancelled"] is True

    def test_unknown_job_404(self, client):
        assert client.get("/api/jobs/deadbeef").status_code == 404

    @pytest.mark.parametrize("body,label", [
        ({"mode": "file", "paths": [], "params": {}}, "空路径"),
        ({"mode": "text", "text": "   ", "params": {}}, "空文本"),
        ({"mode": "compare", "paths": [str(V1)], "params": {}}, "对比不足"),
        ({"mode": "nope", "paths": [str(SAMPLE)], "params": {}}, "未知模式"),
        ({"mode": "file", "paths": ["C:/no/such.log"], "params": {}}, "文件不存在"),
        ({"mode": "file", "paths": [str(SAMPLE)],
          "params": {"similarity": "bogus"}}, "非法相似度"),
        ({"mode": "file", "paths": [str(SAMPLE)],
          "params": {"analysis_mode": "x"}}, "非法分析模式"),
        ({"mode": "file", "paths": [str(SAMPLE)],
          "params": {"maxlines": "x"}}, "非法行数上限"),
        ({"mode": "file", "paths": [str(SAMPLE)],
          "params": {"encoding": "ebcdic"}}, "非法编码"),
        ({"mode": "file", "paths": [str(SAMPLE)],
          "params": {"use_regex": True, "include": "[bad"}}, "非法正则"),
    ])
    def test_bad_requests_rejected_fast(self, client, body, label):
        """非法输入必须在建任务时就 400，别让用户干等 SSE 才发现。"""
        r = client.post("/api/analyze", json=body)
        assert r.status_code == 400, f"{label} 期望 400，实得 {r.status_code}"
        assert r.json()["detail"]


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
class TestSSE:
    def test_frames_are_valid_json(self, client):
        """回归：EventSourceResponse 收到 dict 会 str() 成 Python 字面量，
        浏览器 JSON.parse 直接抛错。_sse() 里的 json.loads 就是守门。"""
        job_id = _run(client, mode="file", paths=[str(SAMPLE)])
        frames = _sse(client, job_id)
        assert frames, "没有任何 SSE 事件"
        assert all(isinstance(data, dict) for _, data in frames)

    def test_done_frame_carries_result(self, client):
        job_id = _run(client, mode="file", paths=[str(SAMPLE)])
        frames = _sse(client, job_id)
        done = [d for n, d in frames if n == "done"]
        assert done, "缺少 done 帧"
        assert "clusters" in done[-1]["result"]
        assert done[-1]["result"]["stats"]["total_lines"] > 0

    def test_unknown_job_404(self, client):
        assert client.get("/api/jobs/deadbeef/stream").status_code == 404


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
class TestVerifyEndpoint:
    """/api/verify：浏览器里当场复核结论出处。

    这不是给 Web 层加个便利按钮 —— 取证层最核心的主张是「每条结论都
    能被独立复核」，而如果用户只能在命令行跑 verify，那在浏览器里看到
    证据出处的人仍然无从判断它是否还对得上原文。主张必须在它出现的
    地方就能被验证。
    """

    def test_verifies_untouched_log(self, client, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("2026-10-14 10:00:00 ERROR db refused\n"
                     "2026-10-14 10:00:01 ERROR db refused\n",
                     encoding="utf-8")
        job_id = _run(client, mode="file", paths=[str(p)])
        r = client.post("/api/verify", json={"job_id": job_id})
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "VERIFIED"
        assert body["ok"] is True
        assert body["checked"] > 0
        assert body["matched"] == body["checked"]

    def test_detects_tampered_log(self, client, tmp_path):
        """分析之后日志被改 —— 必须报 TAMPERED。"""
        p = tmp_path / "b.log"
        original = ("2026-10-14 10:00:00 ERROR db refused to db-primary:5432\n"
                    "2026-10-14 10:00:01 ERROR db refused to db-primary:5432\n")
        p.write_text(original, encoding="utf-8")
        job_id = _run(client, mode="file", paths=[str(p)])
        p.write_text(original.replace("db-primary:5432", "db-replica:6432"),
                     encoding="utf-8")
        r = client.post("/api/verify", json={"job_id": job_id})
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] == "TAMPERED"
        assert body["ok"] is False
        assert body["mismatched"] >= 1
        assert any(d["status"] == "MISMATCH" for d in body["details"])

    def test_pasted_text_has_no_readable_source(self, client):
        """粘贴文本没有可回读源文件 —— 必须如实说，不谎称通过。"""
        job_id = _run(client, mode="text",
                      text="2026-10-14 10:00:00 ERROR db refused\n"
                           "2026-10-14 10:00:01 ERROR db refused\n")
        body = client.post("/api/verify", json={"job_id": job_id}).json()
        assert body["verdict"] == "TAMPERED"
        assert body["missing_source"] >= 1

    def test_unknown_job_404(self, client):
        assert client.post("/api/verify",
                           json={"job_id": "deadbeef"}).status_code == 404

    def test_missing_body_field_404(self, client):
        assert client.post("/api/verify", json={}).status_code == 404

    def test_no_partial_pass_verdict(self, client, tmp_path):
        """不能出现「大部分通过」这种判定 —— 掺假证据的报告不该放行。"""
        p = tmp_path / "c.log"
        original = "2026-10-14 10:00:00 ERROR boom %d\n" % 0
        lines = original + "".join(
            f"2026-10-14 10:00:0{i} ERROR other {i}\n" for i in range(1, 5))
        p.write_text(lines, encoding="utf-8")
        job_id = _run(client, mode="file", paths=[str(p)])
        p.write_text(lines.replace("ERROR other 1", "ERROR other 99"),
                     encoding="utf-8")
        body = client.post("/api/verify", json={"job_id": job_id}).json()
        assert body["verdict"] in ("VERIFIED", "TAMPERED")
        assert body["matched"] > 0 and body["mismatched"] > 0
        assert body["ok"] is False


class TestExport:
    @pytest.fixture(scope="class")
    def job_id(self, client):
        return _run(client, mode="file", paths=[str(SAMPLE)])

    @pytest.mark.parametrize("fmt,needle", [
        ("md", "日志AI压缩报告"),
        ("json", '"error_kinds"'),
        ("json_full", '"clusters"'),
        ("txt", "日志"),
        ("html", "<html"),
        ("summary", "日志分析摘要"),
    ])
    def test_formats(self, client, job_id, fmt, needle):
        r = client.post("/api/export", json={"job_id": job_id, "format": fmt})
        assert r.status_code == 200
        assert needle in r.text

    def test_download_headers(self, client, job_id):
        r = client.post("/api/export", json={"job_id": job_id, "format": "md"})
        assert "attachment" in r.headers.get("content-disposition", "")
        assert "markdown" in r.headers.get("content-type", "")

    def test_copy_endpoint(self, client, job_id):
        r = client.post("/api/export/copy",
                        json={"job_id": job_id, "format": "summary"})
        assert r.status_code == 200
        assert r.json()["length"] > 50

    def test_redact_flag(self, client, job_id):
        r = client.post("/api/export",
                        json={"job_id": job_id, "format": "md", "redact": True})
        assert r.status_code == 200

    def test_bad_format_400(self, client, job_id):
        assert client.post("/api/export",
                           json={"job_id": job_id, "format": "pdf"}
                           ).status_code == 400

    def test_unknown_job_404(self, client):
        assert client.post("/api/export",
                           json={"job_id": "deadbeef"}).status_code == 404

    def test_compare_export(self, client):
        job_id = _run(client, mode="compare", paths=[str(V1), str(V2)])
        r = client.post("/api/export", json={"job_id": job_id, "format": "md"})
        assert r.status_code == 200 and len(r.text) > 50


# ---------------------------------------------------------------------------
# AI 端点
# ---------------------------------------------------------------------------
class TestAiEndpoints:
    def test_status(self, client):
        r = client.get("/api/ai/status")
        assert r.status_code == 200
        body = r.json()
        assert "available" in body
        assert any(p["key"] == "ollama" for p in body["providers"])

    def test_status_is_get(self, client):
        """前端 api() 默认发 GET；这里曾误注册成 POST 导致 404。"""
        assert client.get("/api/ai/status").status_code == 200

    def test_config_save(self, client):
        r = client.post("/api/ai/config", json={"provider": "none"})
        assert r.status_code == 200
        assert r.json()["available"] is False

    def test_config_never_echoes_key(self, client):
        client.post("/api/ai/config", json={"provider": "deepseek",
                                            "api_key": "sk-secret-1234567890"})
        body = client.get("/api/ai/status").text
        assert "sk-secret-1234567890" not in body
        assert '"api_key"' not in body
        # 之后清回未启用，避免污染后续用例
        client.post("/api/ai/config", json={"provider": "none", "api_key": ""})

    def test_explain_without_config_is_4xx(self, client):
        job_id = _run(client, mode="file", paths=[str(SAMPLE)])
        r = client.post("/api/ai/explain", json={"job_id": job_id})
        assert r.status_code == 400, "未配置 AI 应是 4xx，不该 500"
        assert "AI" in r.json()["detail"]

    def test_explain_unknown_job(self, client):
        r = client.post("/api/ai/explain", json={"job_id": "deadbeef"})
        assert r.status_code == 400


class TestPasteLimit:
    def test_oversized_paste_rejected(self, client, monkeypatch):
        """修复缺陷#5：粘贴文本超上限必须 400 拒绝并指路文件导入。

        粘贴模式整段文本随请求体一次性进内存，不限长等于把本机服务
        白送给一个失控的请求体。
        """
        from log_ai_compressor.web import server as webserver
        monkeypatch.setattr(webserver, "MAX_PASTE_CHARS", 100)
        r = client.post("/api/analyze", json={"mode": "text", "text": "x" * 101})
        assert r.status_code == 400
        assert "文件导入" in r.json()["detail"]

    def test_paste_within_limit_ok(self, client, monkeypatch):
        from log_ai_compressor.web import server as webserver
        monkeypatch.setattr(webserver, "MAX_PASTE_CHARS", 100)
        job_id = _run(client, mode="text",
                      text="2026-10-14 10:00:00 ERROR [db] boom")
        assert job_id
