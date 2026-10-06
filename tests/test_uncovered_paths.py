# -*- coding: utf-8 -*-
"""补齐「工作正常但零测试覆盖」的路径。

为什么单独开这个文件
--------------------
覆盖率报告里 265 行未覆盖，混着三类完全不同的东西：

  1. 入口编排（serve() 真起 uvicorn、占端口）—— 测它慢且易 flaky，不划算
  2. 防御性分支（构造出来才触发的 re.error）—— 测它等于测 mock
  3. **工作正常、但生产天天走、却一次没被测过的路径** ← 本文件的目标

第 3 类最危险：它没坏，但也没人知道它没坏。本文件把这几条钉死。
（附带一条反面教材记录：把覆盖率从 92% 刷到 100% 并不能发现「根因因果
链搞反」那个 bug —— 那是靠独立构造地面真相对账才发现的。覆盖率回答不了
「结论对不对」。）
"""
from __future__ import annotations

import gzip
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from log_ai_compressor.core.parser import LogParser, TimestampParser
from log_ai_compressor.core.pipeline import _sample_log_file, analyze_text
from log_ai_compressor.rules.engine import RuleSetError, load_ruleset

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A. 时区后缀时间戳 —— 任何用 ISO 8601 + Z/+08:00 的生产日志都走这里
# ---------------------------------------------------------------------------
class TestTimezoneSuffix:
    """断言「同一时刻的不同写法归一到同一 epoch」，而不是去手算偏移量。

    踩过的坑：最初写成 `got - naive == expect_off`，以为 naive 是 UTC。
    实际 naive 是**本地时间**（本机 UTC+8），于是 Z 会被算出 +8h 的差、
    +08:00 差 0，看起来像全错，其实全对。手算偏移量把断言绑死在了
    运行机器的时区上 —— 换个时区的 CI 就会假红。
    """

    def test_z_and_plus8_agree(self):
        assert (TimestampParser().parse("2024-01-01T10:00:00Z")
                == TimestampParser().parse("2024-01-01T18:00:00+08:00"))

    def test_plus0800_compact_agrees_with_colon_form(self):
        assert (TimestampParser().parse("2024-01-01T18:00:00+0800")
                == TimestampParser().parse("2024-01-01T18:00:00+08:00"))

    def test_negative_offset_agrees(self):
        assert (TimestampParser().parse("2024-01-01T10:00:00-05:00")
                == TimestampParser().parse("2024-01-01T15:00:00Z"))

    def test_suffix_actually_affects_result(self):
        """带后缀与不带后缀必须不同 —— 否则等于后缀被当装饰忽略了。"""
        a = TimestampParser().parse("2024-01-01T10:00:00Z")
        b = TimestampParser().parse("2024-01-01T10:00:00")
        assert a is not None and b is not None
        assert a != b, "时区后缀未生效"

    def test_comma_millis_with_z(self):
        got = TimestampParser().parse("2024-01-01 10:00:00,123Z")
        assert got is not None
        assert abs(got % 1 - 0.123) < 1e-6

    def test_bad_input_returns_none_not_raise(self):
        """解析不了就返回 None，绝不能抛异常打断整份日志。"""
        assert TimestampParser().parse("not-a-timestamp") is None
        assert TimestampParser().parse("") is None
        assert TimestampParser().parse(None) is None


# ---------------------------------------------------------------------------
# B. 消息头部令牌归一化（模块名/级别被写进 message 里的情况）
# ---------------------------------------------------------------------------
class TestHeadTokenNormalisation:
    @pytest.fixture()
    def parser(self):
        return LogParser(load_ruleset("generic"))

    def _one(self, parser, line):
        e = parser.feed(line, 1)
        return e if e is not None else parser.flush()

    def test_module_prefix_stripped_from_message(self, parser):
        """`[ts] auth - login failed` -> 模块 auth，消息只剩 login failed。"""
        e = self._one(parser, "[2026-09-04T06:12:12.244Z] auth - login failed")
        assert e is not None
        assert e.module == "auth"
        assert e.message == "login failed"

    def test_level_prefix_recognised(self, parser):
        """`[ts] ERROR | disk full` -> 级别 ERROR，消息只剩 disk full。

        注意分隔符是 `|`：bracket_iso 的级别组要求 `:` 或 `-`，
        所以这里级别组落空，靠头部令牌补写。
        """
        e = self._one(parser, "[2026-09-04T06:12:12.244Z] ERROR | disk full")
        assert e is not None
        assert e.level == "ERROR"
        assert e.message == "disk full"


# ---------------------------------------------------------------------------
# C. 堆栈行先于任何条目（截断日志 / 日志开头就是堆栈）
# ---------------------------------------------------------------------------
class TestOrphanStackLine:
    def test_stack_at_file_head_becomes_own_entry(self):
        text = ("\tat com.foo.Bar.run(Bar.java:10)\n"
                "\tat com.foo.Baz.run(Baz.java:20)\n")
        r = analyze_text(text)
        assert r.stats.entry_lines >= 1, "开头的堆栈行不应被静默丢弃"
        assert any(c.sample and c.sample.entry
                   and c.sample.entry.has_stack for c in r.clusters) or \
            r.stats.entry_lines >= 1


# ---------------------------------------------------------------------------
# D. 大文件分层采样（头 50 + 中 50 + 尾 50）
# ---------------------------------------------------------------------------
class TestLayeredSampling:
    def _big(self, tmp_path, n=60000, width=90):
        p = tmp_path / "big.log"
        with p.open("w", encoding="utf-8") as fh:
            for i in range(n):
                fh.write(f"{i:08d} " + "x" * width + "\n")
        return p

    def test_samples_head_middle_tail(self, tmp_path):
        """大文件不能整读：取头/中/尾三段，且三段都要有内容。"""
        p = self._big(tmp_path)
        lines = _sample_log_file(p, "utf-8", total=150)
        assert len(lines) >= 100, f"采样只拿到 {len(lines)} 行"
        firsts = {ln.split()[0] for ln in lines if ln.split()}
        lo, hi = min(firsts), max(firsts)
        assert lo == "00000000", "头部没被采到"
        assert int(hi) > 59000, f"尾部没被采到（最大 {hi}）"

    def test_small_file_degrades_to_head_only(self, tmp_path):
        """小文件走早退分支，不做无谓的 seek。"""
        p = tmp_path / "small.log"
        p.write_text("\n".join(f"line {i}" for i in range(10)) + "\n",
                     encoding="utf-8")
        assert len(_sample_log_file(p, "utf-8", total=150)) == 10

    def test_compressed_path_uses_streaming_sampler(self, tmp_path):
        """gzip 走流式跳读分支（无法 seek）。"""
        p = tmp_path / "big.log.gz"
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            for i in range(60000):
                fh.write(f"{i:08d} " + "x" * 90 + "\n")
        lines = _sample_log_file(p, "utf-8", total=150)
        assert len(lines) >= 100, f"压缩包采样只拿到 {len(lines)} 行"
        assert lines[0].startswith("00000000")


# ---------------------------------------------------------------------------
# E. 从 YAML 文件加载规则集
# ---------------------------------------------------------------------------
class TestLoadRulesetFromFile:
    def test_valid_yaml(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text(
            "name: custom\n"
            "patterns:\n"
            "  - name: p\n"
            "    regex: '^CUSTOM (?P<level>{LEVEL}) (?P<message>.*)$'\n",
            encoding="utf-8")
        rs = load_ruleset(str(p))
        assert rs.name == "custom"
        assert len(rs.patterns) == 1
        m = rs.match_line("CUSTOM ERROR boom")
        assert m is not None and m.group("level") == "ERROR"

    def test_malformed_yaml_raises_clear_error(self, tmp_path):
        """坏 YAML 要报「解析失败」而不是把底层异常糊到用户脸上。"""
        p = tmp_path / "bad.yaml"
        p.write_text("name: x\npatterns: [unclosed\n", encoding="utf-8")
        with pytest.raises(RuleSetError, match="YAML 解析失败"):
            load_ruleset(str(p))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_ruleset(str(tmp_path / "nope.yaml"))


# ---------------------------------------------------------------------------
# F. python -m log_ai_compressor（打包后的真实入口）
# ---------------------------------------------------------------------------
class TestModuleEntryPoint:
    def test_dash_version(self):
        p = subprocess.run([sys.executable, "-m", "log_ai_compressor",
                            "--version"],
                           capture_output=True, timeout=60, text=True,
                           encoding="utf-8", errors="replace")
        assert p.returncode == 0, p.stderr
        assert "log-ai-compressor" in p.stdout

    def test_bare_invocation_shows_usage(self):
        """裸跑必须给出用法并以非 0 退出，而不是静默挂住。"""
        p = subprocess.run([sys.executable, "-m", "log_ai_compressor"],
                           capture_output=True, timeout=60, text=True,
                           encoding="utf-8", errors="replace")
        assert p.returncode != 0
        assert "usage" in (p.stdout + p.stderr).lower()


# ---------------------------------------------------------------------------
# G. AI 提示词里的变量分布 / 共现段
# ---------------------------------------------------------------------------
class TestAiPromptSections:
    def test_variable_distribution_rendered(self):
        """同类错误的变量取值分布要进提示词 —— 这是判断「哪里不一样」的关键。"""
        from log_ai_compressor.ai.prompts import build_cluster_prompt
        ctx = {"cluster": {
            "id": 1, "summary": "db timeout", "count": 12, "level": "ERROR",
            "sample": {"entry": {"message": "db timeout", "line_no": 5,
                                 "stack": []}},
            "variables": [
                {"name": "路径", "values": [("/a", 8), ("/b", 4)]},
                {"name": "端口", "values": [("5432", 12)]},
            ],
        }}
        text = build_cluster_prompt(ctx, "为什么会超时？")
        assert "变量取值分布" in text
        assert "/a×8" in text and "端口" in text
        assert "为什么会超时？" in text

    def test_cluster_without_variables_still_renders(self):
        from log_ai_compressor.ai.prompts import build_cluster_prompt
        ctx = {"cluster": {"id": 1, "summary": "plain", "count": 1,
                           "level": "ERROR",
                           "sample": {"entry": {"message": "plain",
                                                "line_no": 1, "stack": []}}}}
        assert "plain" in build_cluster_prompt(ctx)


# ---------------------------------------------------------------------------
# H. 服务参数校验 —— 出错要给出可操作的提示，而不是抛裸异常
# ---------------------------------------------------------------------------
class TestParamValidation:
    def test_bad_top_n(self):
        from log_ai_compressor import service
        with pytest.raises(service.ServiceError, match="top_n 必须是整数"):
            service.analyze(paths=[__file__], params={"top_n": "abc"})

    def test_bad_max_lines(self):
        from log_ai_compressor import service
        with pytest.raises(service.ServiceError, match="max_lines 必须是整数"):
            service.analyze(paths=[__file__], params={"max_lines": "abc"})

    def test_bad_encoding(self):
        from log_ai_compressor import service
        with pytest.raises(service.ServiceError, match="不支持的编码"):
            service.analyze(paths=[__file__], params={"encoding": "bogus"})

    def test_no_input_at_all(self):
        from log_ai_compressor import service
        with pytest.raises(service.ServiceError):
            service.analyze(paths=[], text=None, params={})


# ---------------------------------------------------------------------------
# I. MCP 工具的错误路径 —— Agent 传错参不能让会话崩
#
# MCP 是本项目与竞品对比时剩下的少数真实差异之一，而 Agent 调用工具时
# 传错参数是常态（路径写错、类型给成字符串、文件不存在）。每个工具都
# 必须把异常收敛成 {"ok": false, "error": ...} 返回，而不是把栈糊到
# 协议流里把会话带崩。
# ---------------------------------------------------------------------------
# 注意：不能用模块顶层的 pytest.importorskip("mcp")。
# 那会在 import 阶段就跳过**整个文件** —— 而本文件里不只有 MCP 测试，
# 时区 / 采样 / 参数校验那些与 mcp 无关，在 Python 3.9 上会跟着一起被跳过
# （3.9 不装 mcp，因为 pyproject 的 extra 带 python_version >= '3.10' 门）。
# 结果就是「3.9 通过」其实是「什么都没跑」。
#
# 这里用 find_spec 探测 + 类级 skipif，只跳过 MCP 那一类。
_MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


def _call(tool: str, **kwargs):
    import asyncio
    import json
    from log_ai_compressor.mcp.server import server

    async def run():
        res = await server.call_tool(tool, kwargs)
        if isinstance(res, (list, tuple)):
            first = res[0]
            return first[0].text if isinstance(first, (list, tuple)) else first.text
        parts = getattr(res, "content", None) or []
        return parts[0].text if parts else str(res)

    text = asyncio.run(run())
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": str(text)}


@pytest.mark.skipif(not _MCP_AVAILABLE, reason="需要 mcp SDK")
class TestMcpToolErrorPaths:
    """只测**真能到达**我们异常处理器的输入。

    踩过的坑：最初写 `top_n="abc"` 期待走进 `except ServiceError`，结果
    直接被 MCP SDK 的 pydantic 层挡在门外抛 ToolError —— 类型错误在
    **协议层**就被拒了，根本到不了工具函数。
    这不是缺口，是正确的分层：协议层管类型，工具层管语义。
    工具层真正要兜的是「路径不存在」「文本为空」这类语义错误。
    """

    def test_sdk_rejects_type_errors_before_our_handler(self):
        """类型错误由协议层拦截（记录这个分层事实，避免以后重复踩）。"""
        import pytest as _pt
        from mcp.server.mcpserver.exceptions import ToolError
        with _pt.raises(ToolError):
            _call("analyze_log_file",
                  paths=[str(ROOT / "examples" / "sample_system.log")],
                  top_n="abc")

    def test_analyze_log_file_missing_path(self):
        r = _call("analyze_log_file", paths=[str(ROOT / "no-such-file.log")])
        assert r["ok"] is False
        assert r["error"]

    def test_analyze_log_file_empty_paths(self):
        r = _call("analyze_log_file", paths=[])
        assert r["ok"] is False

    def test_analyze_log_text_empty_text(self):
        r = _call("analyze_log_text", text="")
        assert r["ok"] is False

    def test_export_report_bad_format(self):
        r = _call("export_report",
                  paths=[str(ROOT / "examples" / "sample_system.log")],
                  format="nope")
        assert r["ok"] is False

    def test_export_report_missing_path(self):
        r = _call("export_report",
                  paths=[str(ROOT / "no-such-file.log")], format="md")
        assert r["ok"] is False
        assert r["error"]

    def test_get_cluster_detail_missing_path(self):
        r = _call("get_cluster_detail",
                  path=str(ROOT / "no-such-file.log"), cluster_id=0)
        assert r["ok"] is False

    def test_list_rules_survives_broken_preset(self, monkeypatch):
        """某个内置规则集坏了，要在该条目上报错，不能让整个列表调用失败。"""
        from log_ai_compressor.rules import engine as eng

        real = eng.load_ruleset

        def boom(name):
            if name == "generic":
                raise RuntimeError("规则集损坏（模拟）")
            return real(name)

        monkeypatch.setattr(eng, "load_ruleset", boom)
        r = _call("list_rules")
        assert r["ok"] is True, "一个坏规则集不该让整个调用失败"
        broken = [x for x in r["rules"] if "error" in x]
        assert broken, "应逐条报告损坏的规则集"


# ---------------------------------------------------------------------------
# J. 后台任务的异常兜底 —— 线程里静默死掉是最难查的一类故障
# ---------------------------------------------------------------------------
class TestJobErrorHandling:
    @staticmethod
    def _wait(job, timeout=30.0):
        """Job 不是 Thread，用它的 done 事件轮询等待（别假设有 join）。"""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job.done:
                return
            time.sleep(0.02)
        raise AssertionError(f"任务 {timeout}s 内未结束，可能线程静默死了")

    def test_validation_error_is_4xx_style_no_traceback(self, tmp_path):
        """参数问题不该打完整 traceback 吓人，只回一句可修正的提示。"""
        from log_ai_compressor.web import jobs as J
        job = J.start_job("file", {"paths": [str(tmp_path / "x.log")],
                                   "params": {"top_n": "abc"}})
        self._wait(job)
        assert job.error and "Traceback" not in job.error

    def test_internal_error_is_captured_with_traceback(self, tmp_path,
                                                      monkeypatch):
        """非预期异常必须被捕获并存 traceback —— 否则线程静默死，任务永远 pending。"""
        from log_ai_compressor.web import jobs as J

        def boom(*a, **k):
            raise RuntimeError("模拟内部故障")

        monkeypatch.setattr(J.S, "analyze", boom)
        job = J.start_job("file", {"paths": [str(tmp_path / "x.log")],
                                   "params": {}})
        self._wait(job)
        assert job.error and "模拟内部故障" in job.error
        assert job.traceback and "RuntimeError" in job.traceback

    def test_sse_stream_times_out_instead_of_hanging(self, tmp_path):
        """任务迟迟不完成时 SSE 要返回超时帧，不能无限挂着占连接。"""
        from log_ai_compressor.web import jobs as J
        job = J.Job(mode="file", payload={})
        frames = list(J.stream_events(job, max_wait=0.6))
        assert frames, "超时也必须给一帧"
        assert frames[0]["event"] == "error"
        assert "timeout" in frames[0]["data"]


# ---------------------------------------------------------------------------
# K. 文件浏览的根目录限制
#
# 注意：这不是安全边界。/api/analyze 本来就接受任意路径，服务也只听
# 127.0.0.1（见 server.py 里的说明）。它只影响浏览器的 can_go_up，
# 但逻辑本身要正确：设了根就不该让用户「往上」走出根目录。
# ---------------------------------------------------------------------------
class TestFsRoot:
    def test_no_root_allows_everything(self):
        from log_ai_compressor.web import server as W
        assert W._within_root(Path("/")) is True

    def test_root_contains_child(self, tmp_path, monkeypatch):
        from log_ai_compressor.web import server as W
        monkeypatch.setattr(W, "FS_ROOT", str(tmp_path))
        assert W._within_root(tmp_path / "a" / "b") is True

    def test_root_rejects_escape(self, tmp_path, monkeypatch):
        from log_ai_compressor.web import server as W
        root = tmp_path / "root"
        root.mkdir()
        monkeypatch.setattr(W, "FS_ROOT", str(root))
        assert W._within_root(tmp_path / "outside") is False


# ---------------------------------------------------------------------------
# L. serve() 的编排逻辑（uvicorn 被替换，不真的起服务）
# ---------------------------------------------------------------------------
class TestServeOrchestration:
    def test_serve_delegates_to_uvicorn_with_app(self, monkeypatch, capsys):
        """serve 必须把 create_app() 交给 uvicorn.run —— 这是唯一的接线点。"""
        import uvicorn
        from log_ai_compressor.web import server as W

        seen = {}

        def fake_run(app, **kw):
            seen["app"] = app
            seen.update(kw)

        monkeypatch.setattr(uvicorn, "run", fake_run)
        port = W.serve(host="127.0.0.1", port=8765, open_browser=False,
                       auto_port=False)
        assert port == 8765
        assert seen["app"] is not None
        assert seen["host"] == "127.0.0.1"
        out = capsys.readouterr().out
        assert "数据不出网" in out, "启动横幅要明确告知本地运行"

    def test_pick_port_skips_occupied(self):
        """端口被占时必须往后顺延 —— 双击启动常撞上上一次残留进程。"""
        import socket
        from log_ai_compressor.web import server as W
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            got = W.pick_port("127.0.0.1", busy, tries=5)
        assert got != busy, "不应返回已被占用的端口"
        assert busy < got <= busy + 5

