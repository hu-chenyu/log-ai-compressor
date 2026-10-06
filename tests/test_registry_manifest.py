# -*- coding: utf-8 -*-
"""独立 MCP 入口 ``log-ai-compressor-mcp`` 与官方 Registry 清单的测试。

为什么这组测试重要
------------------
官方 MCP Registry 与各类 MCP 客户端按「**可执行名 = 包名**」的约定拉起
服务器（例如 ``uvx log-ai-compressor``）。而主 CLI 需要子命令，裸跑只会
打印 usage 并以退出码 2 结束 —— 客户端拿到的是一段帮助文本而不是 MCP
服务器，Registry 条目形同装饰。

所以「独立入口存在 + 缺依赖时不裸崩」不是锦上添花，是收录的前提条件。
另外 server.json 是对外的公开契约，改坏了只会在发布时被 Registry 拒绝，
所以 schema 校验也在这里钉住。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
SERVER_JSON = BASE / "server.json"

REGISTRY_NAME = "io.github.hu-chenyu/log-ai-compressor"


# ---------------------------------------------------------------------------
# 独立入口
# ---------------------------------------------------------------------------
class TestStandaloneMcpEntryPoint:
    def test_declared_in_pyproject(self):
        """pyproject 必须声明独立入口，否则 uvx 拉不起来。"""
        text = (BASE / "pyproject.toml").read_text(encoding="utf-8")
        assert "log-ai-compressor-mcp" in text
        assert "main_mcp" in text

    def test_main_mcp_is_importable_without_starting_server(self):
        from log_ai_compressor.cli import main_mcp
        assert callable(main_mcp)

    def test_degrades_gracefully_when_mcp_missing(self, monkeypatch, capsys):
        """没装 [mcp] extra 时要给出可执行提示，而不是裸 ImportError 栈。"""
        import builtins

        from log_ai_compressor import cli

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "log_ai_compressor.mcp.server":
                raise ImportError("No module named 'mcp'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        rc = cli.main_mcp()
        err = capsys.readouterr().err
        assert rc == cli.EXIT_ERROR
        assert "log-ai-compressor[mcp]" in err, "必须告诉用户装哪个 extra"
        assert "Traceback" not in err

    @pytest.mark.skipif(
        pytest.importorskip("mcp", reason="需要 mcp SDK") is None,
        reason="需要 mcp SDK")
    def test_starts_and_exits_clean_on_stdin_close(self):
        """stdio 传输下 stdin 关闭应干净退出，且诊断只走 stderr。

        stdout 是 JSON-RPC 协议通道，往里 print 任何东西都会污染协议流
        —— 这个 bug 在客户端里表现为"连上了但收不到 initialize 响应"。
        """
        code = ("from log_ai_compressor.cli import main_mcp; "
                "import sys; sys.exit(main_mcp())")
        p = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, timeout=30,
            stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace",
        )
        assert p.returncode == 0
        assert p.stdout.strip() == "", "stdout 必须干净 —— 那是协议通道"
        assert "MCP" in (p.stderr or "")


# ---------------------------------------------------------------------------
# Registry 清单
# ---------------------------------------------------------------------------
class TestServerJson:
    def test_exists(self):
        assert SERVER_JSON.is_file(), "缺少 server.json，Registry 无法收录"

    def test_is_valid_json(self):
        json.loads(SERVER_JSON.read_text(encoding="utf-8"))

    def test_name_matches_github_namespace(self):
        """namespace 必须是 io.github.<user>/<name>，且与认证方式一致。

        用 GitHub 登录却起 com.example/* 的名字，会报
        "Your authentication method doesn't match your server's namespace"。
        """
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        assert data["name"] == REGISTRY_NAME
        assert data["name"].count("/") == 1, "必须且只能有一个斜杠"
        assert data["name"].startswith("io.github.")

    def test_description_within_100_chars(self):
        """schema 的 maxLength=100。这是最容易踩的一条：多一个字符即失败。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        assert len(data["description"]) <= 100

    def test_schema_is_not_deprecated(self):
        """旧 schema 还能通过校验，但 CLI 会警告并要求迁移。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        assert data["$schema"].endswith("2025-12-11/server.schema.json")

    def test_pypi_package_points_at_official_registry(self):
        """私有镜像/自建源不被接受，registryBaseUrl 必须是 pypi.org。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        pkg = data["packages"][0]
        assert pkg["registryType"] == "pypi"
        assert pkg["registryBaseUrl"] == "https://pypi.org"
        assert pkg["identifier"] == "log-ai-compressor"

    def test_transport_is_stdio_and_version_matches_package(self):
        """version 必须与 PyPI 上真实存在的版本一致，否则验证抓不到包。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        pkg = data["packages"][0]
        assert pkg["transport"]["type"] == "stdio"
        assert data["version"] == pkg["version"]

    def test_runtime_arguments_are_objects_not_strings(self):
        """runtimeArguments 的元素必须是 Argument 对象。

        写成裸字符串数组时，jsonschema 与官方 CLI 会同时报错：
        "cannot unmarshal string into Go struct field Package.runtimeArguments"。
        """
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        args = data["packages"][0]["runtimeArguments"]
        assert args, "需要 runtimeHint + runtimeArguments 才能被客户端拉起"
        for a in args:
            assert isinstance(a, dict), f"参数必须是对象，实际是 {type(a)}"
            assert "type" in a

    def test_readme_carries_the_mcp_name_marker(self):
        """Registry 验证 PyPI 包的方式是抓该版本的 README 找这个标记。

        放进 HTML 注释等于没放 —— PyPI 渲染 Markdown 时会剥离注释。
        """
        text = (BASE / "README.md").read_text(encoding="utf-8")
        assert f"mcp-name: {REGISTRY_NAME}" in text
        line = next(l for l in text.splitlines() if l.startswith("mcp-name:"))
        assert not line.startswith("<!--"), "标记必须在可见正文里"

    def test_meta_within_4kb_limit(self):
        """publisher-provided 扩展上限 4096 字节，超了发布直接失败。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        meta = data.get("_meta", {})
        assert "io.modelcontextprotocol.registry/publisher-provided" in meta
        size = len(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        assert size <= 4096, f"_meta {size} 字节，超出 4096 上限"
