# -*- coding: utf-8 -*-
"""GUI 应用层测试：拖拽、按钮状态、主题、全屏、Tooltip 等交互逻辑。

运行前提：需要可用的显示环境（本地桌面）；CI 无头环境自动跳过。
"""
from __future__ import annotations

import time
import tkinter as tk
from types import SimpleNamespace

import pytest

ctk = pytest.importorskip("customtkinter")


def _display_available() -> bool:
    """探测能否创建 Tk 窗口（无头 CI 返回 False）。"""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _display_available(),
                                reason="无可用显示环境（无头 CI 跳过 GUI 测试）")


@pytest.fixture()
def app(monkeypatch, tmp_path):
    """创建主窗口实例，隔离用户配置文件，测试后销毁。"""
    monkeypatch.setattr("log_ai_compressor.gui.config_store.CONFIG_FILE",
                        tmp_path / "config.json")
    from log_ai_compressor.gui.app import LogCompressorApp
    application = LogCompressorApp()
    application.update()
    yield application
    try:
        application._on_close()
    except tk.TclError:
        application.destroy()


class TestDragAndDrop:
    """修复2：拖拽文件导入（整窗注册 + Tab 路由）。"""

    def test_tkinterdnd2_imported_and_root_registered(self, app):
        from log_ai_compressor.gui import app as app_module
        assert app_module._HAS_DND, "tkinterdnd2 应已安装并启用"
        # 根窗口已具备 DnD 能力（tkdnd 已加载）
        assert getattr(app, "TkdndVersion", None) is not None
        assert hasattr(app, "drop_target_register")

    def test_drop_single_file_fills_file_entry(self, app):
        event = SimpleNamespace(data="{D:/logs/app.log}")
        app._on_drop_file(event)
        assert app._file_entry.get() == "D:/logs/app.log"
        assert "已拖入文件" in app._status_label.cget("text")

    def test_drop_plain_path_without_braces(self, app):
        event = SimpleNamespace(data="D:/logs/plain.log")
        app._on_drop_file(event)
        assert app._file_entry.get() == "D:/logs/plain.log"

    def test_drop_multiple_files_prefills_compare(self, app):
        event = SimpleNamespace(
            data="{D:/logs/a.log} {D:/logs/b.log} {D:/logs/c.log}")
        app._on_drop_file(event)
        # 首个进入文件导入框，其余进入对比区
        assert app._file_entry.get() == "D:/logs/a.log"
        assert app._compare_entries[0].get() == "D:/logs/b.log"
        assert app._compare_entries[1].get() == "D:/logs/c.log"

    def test_drop_in_compare_tab_fills_ab(self, app):
        app._tabview.set("多文件对比")
        app.update()
        event = SimpleNamespace(data="{D:/v1.log} {D:/v2.log}")
        app._on_drop_file(event)
        assert app._compare_entries[0].get() == "D:/v1.log"
        assert app._compare_entries[1].get() == "D:/v2.log"
        assert "对比模式" in app._status_label.cget("text")

    def test_drop_empty_event_no_crash(self, app):
        app._on_drop_file(SimpleNamespace(data=""))
        app._on_drop_file(SimpleNamespace(data=None))
        assert app._file_entry.get() == ""

    def test_requirements_declares_tkinterdnd2(self):
        from pathlib import Path
        content = (Path(__file__).resolve().parent.parent
                   / "requirements.txt").read_text(encoding="utf-8")
        assert "tkinterdnd2" in content


# ---------------------------------------------------------------------------
# 修复3：按钮状态机
# ---------------------------------------------------------------------------
SAMPLE_PASTE = """\
2024-01-01 09:00:00 INFO [auth] start
2024-01-01 09:00:05 ERROR [db] connection refused to db-primary:5432
java.net.ConnectException: Connection refused
\tat com.app.db.Pool.init(Pool.java:42)
\tat java.base/java.net.Socket.connect(Socket.java:1)
2024-01-01 09:01:00 FATAL [core] out of memory in worker 3
"""


def _run_paste_analysis(app, text=SAMPLE_PASTE):
    """执行一次文本粘贴分析并等待完成。"""
    app._tabview.set("文本粘贴")
    app._paste_box.delete("1.0", "end")
    app._paste_box.insert("1.0", text)
    app._on_start()
    deadline = time.time() + 20
    while time.time() < deadline:
        app.update()
        if app._result is not None:
            break
        time.sleep(0.02)
    assert app._result is not None, "分析未完成"


class TestButtonStates:
    ACTION_BUTTONS = ("_cancel_btn", "_export_btn", "_copy_btn", "_chart_btn")

    def test_initial_state_all_disabled(self, app):
        # 未开始分析：四个操作按钮全部置灰
        for name in self.ACTION_BUTTONS:
            assert app.__getattribute__(name).cget("state") == "disabled", name

    def test_after_analysis_all_enabled(self, app):
        # 分析完成后：四个操作按钮全部可点击
        _run_paste_analysis(app)
        for name in self.ACTION_BUTTONS:
            assert app.__getattribute__(name).cget("state") == "normal", name
        assert app._start_btn.cget("state") == "normal"

    def test_cancel_without_task_is_safe(self, app):
        # 完成后点击取消（无进行中任务）：仅提示，不崩溃
        _run_paste_analysis(app)
        app._on_cancel()
        assert "没有进行中的分析任务" in app._status_label.cget("text")

    def test_start_disabled_while_running(self, app, monkeypatch):
        # 分析进行中：开始按钮置灰（monkeypatch 慢速分析消除竞态）
        import log_ai_compressor.gui.app as app_mod
        from log_ai_compressor.core.models import RunStats, AnalysisResult

        def slow_analyze(text, **kwargs):
            cancel = kwargs.get("cancel_event")
            for _ in range(200):
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.02)
            # 取消后返回一个最小结果（与真实管线行为一致）
            stats = RunStats(source="<粘贴文本>", total_lines=1)
            return AnalysisResult(stats=stats, clusters=[])

        monkeypatch.setattr(app_mod, "analyze_text", slow_analyze)
        app._tabview.set("文本粘贴")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        app._on_start()
        app.update()
        try:
            assert app._start_btn.cget("state") == "disabled"
            assert app._cancel_btn.cget("state") == "normal"
        finally:
            # 取消任务并等待收尾，避免影响后续测试
            app._on_cancel()
            deadline = time.time() + 15
            while time.time() < deadline:
                app.update()
                if app._result is not None:
                    break
                time.sleep(0.02)
        assert app._result is not None
        assert app._start_btn.cget("state") == "normal"
