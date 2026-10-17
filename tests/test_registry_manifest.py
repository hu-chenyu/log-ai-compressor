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
import os
import subprocess
import sys
import tomllib
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


class TestRulesListTellsYouWhyItFailed:
    """`rules list` 曾只打「(加载失败)」且照常退出 0。

    两个问题叠在一起：

    1. **没有原因**。用户看到「加载失败」既没法判断是自己的环境坏了还是
       这个内置模板坏了，也没法把问题报上来 —— 一句没有任何信息的话，
       等于没打印。
    2. **退出码是 0**。内置模板加载不了是**我们**的缺陷，不是用户的用法错误；
       照常退出 0 等于把缺陷藏进一次「成功」里。而 MCP 侧同一个概念是
       `{"ok": False, "error": ...}`，两边对同一个错误的表态不一致。
    """

    @staticmethod
    def _args(action="list", name=None):
        import argparse

        return argparse.Namespace(action=action, name=name)

    def test_broken_preset_is_reported_with_reason_and_fails(self,
                                                             monkeypatch,
                                                             capsys):
        from log_ai_compressor import cli

        import log_ai_compressor.rules.engine as engine

        def boom(name):
            raise ValueError("unknown field 'patern'")

        monkeypatch.setattr(engine, "load_ruleset", boom)
        rc = cli.cmd_rules(self._args())
        err = capsys.readouterr().err
        assert rc == cli.EXIT_ERROR, (
            "内置模板加载失败却退出 0 —— 缺陷被藏进一次「成功」里")
        assert "unknown field 'patern'" in err, (
            f"必须打印失败原因，实际 stderr：{err!r}")
        assert "ValueError" in err, "异常类型也要打：YAML 语法错与文件缺失只看 message 分不清"
        assert "Traceback" not in err, "面向用户的是一句话，不是裸栈"

    def test_all_presets_load_cleanly_in_this_repo(self, capsys):
        """先证真：本仓库内置模板全部可加载，所以本用例红就是真回归。"""
        from log_ai_compressor import cli

        rc = cli.cmd_rules(self._args())
        out, err = capsys.readouterr()
        assert rc == cli.EXIT_OK, f"本仓库内置模板不该有加载失败：{err}"
        assert "加载失败" not in out and "加载失败" not in err

    def test_single_preset_failure_still_lists_the_rest(self, monkeypatch,
                                                        capsys):
        """一个坏的不该把整张表吞掉 —— 坏哪个要指名道姓。"""
        from log_ai_compressor import cli

        import log_ai_compressor.rules.engine as engine

        real = engine.load_ruleset

        def flaky(name):
            if name == "generic":
                raise OSError("cannot read rules/generic.yaml")
            return real(name)

        monkeypatch.setattr(engine, "load_ruleset", flaky)
        rc = cli.cmd_rules(self._args())
        out, err = capsys.readouterr()
        assert rc == cli.EXIT_ERROR
        assert "cannot read rules/generic.yaml" in err, err
        assert "generic" in err, "坏的是哪一个必须指名道姓"
        assert "jenkins" in out, "其余模板必须照常列出，不能一个坏了就整张吞掉"

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
        # 显式给子进程设编码（v2.8.1 契约）：否则本地绿、cp1252 机器上乱码。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("PYTHONUTF8", None)
        p = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, timeout=30, env=env,
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

    def test_transport_is_stdio(self):
        """Registry 只能拉起 stdio 服务器。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        pkg = data["packages"][0]
        assert pkg["transport"]["type"] == "stdio"

    def test_version_matches_the_shipped_package(self):
        """version 必须与**实际发布的那个包**一致，否则验证抓不到包。

        此前这里只断言「同一文件里两个字段互相相等」（`data["version"] ==
        pkg["version"]`），docstring 却写着「与 PyPI 上真实存在的版本一致」。
        断言比承诺弱，于是 2.0.1 这个错值从 v2.1.0 一路绿灯放到 v2.7.0 ——
        测试在跑、名字很像、实际上什么都没守住。
        **断言要比 docstring 强，否则它只是一个仪式。**
        """
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        declared = tomllib.loads(
            (BASE / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        assert data["version"] == declared, (
            f"server.json 写 {data['version']}，包版本是 {declared}；"
            "Registry 会按清单里的版本去抓包，抓不到就收不了录")
        assert data["packages"][0]["version"] == declared

    def test_declared_version_matches_installed_package(self):
        """再钉一层：清单里的版本必须等于运行时 import 到的版本。

        前两条比的都是**文本文件**，万一有人改了 pyproject 却没重新构建，
        文本仍自洽但发出去的包是旧的。这条把「装的到底是不是这个版本」
        变成可执行事实。
        """
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        import log_ai_compressor
        assert data["version"] == log_ai_compressor.__version__

    def test_tool_list_matches_the_registered_tools(self):
        """publisher-provided.tools 必须与实际注册的工具**完全一致**。

        漏一个，用户在 Registry 页面上就看不到它存在 —— 而这恰恰发生在
        `verify_evidence` 上：它是本项目唯一能让 Agent 自证「日志没被改过」
        的工具，却因为不在清单里而在 Registry 上隐身了。
        """
        pytest.importorskip("mcp", reason="需要 mcp SDK")
        import importlib
        mod = importlib.import_module("log_ai_compressor.mcp.server")
        registered = {t.name for t in mod.server._tool_manager.list_tools()}
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        declared = {t["name"] for t in
                    data["_meta"]["io.modelcontextprotocol.registry/publisher-provided"]["tools"]}
        assert registered, "没取到已注册工具，本用例就失去意义了"
        assert declared == registered, (
            f"清单缺 {sorted(registered - declared)}；"
            f"清单多 {sorted(declared - registered)}")

    def test_verify_evidence_is_declared(self):
        """单独钉住这个工具 —— 它是对外信任的锚点，不该是隐形功能。"""
        data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
        names = {t["name"] for t in
                 data["_meta"]["io.modelcontextprotocol.registry/publisher-provided"]["tools"]}
        assert "verify_evidence" in names, (
            "verify_evidence 是唯一能让调用方自证日志未被篡改的工具，"
            "不登记等于对用户隐藏它存在")

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
