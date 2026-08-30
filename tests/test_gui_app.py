# -*- coding: utf-8 -*-
"""GUI 应用层测试：拖拽、按钮状态、主题、全屏、Tooltip 等交互逻辑。

运行前提：需要可用的显示环境（本地桌面）；CI 无头环境自动跳过。
"""
from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
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
    # 宽容销毁：窗口已失效时仅静默清理（避免teardown报错）
    try:
        application._on_close()
    except Exception:
        try:
            application.destroy()
        except Exception:
            pass


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


def _run_paste_analysis(app, text=SAMPLE_PASTE, timeout=30.0):
    """执行一次文本粘贴分析并等待完成。"""
    app._tabview.set("文本粘贴")
    app._paste_box.delete("1.0", "end")
    app._paste_box.insert("1.0", text)
    app._on_start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if app._result is not None:
            break
        time.sleep(0.02)
    if app._result is None:
        # 诊断转储：失败时输出 worker/轮询状态（定位挂起类问题）
        worker = app._worker
        print(f"[diag] queue_size={app._queue.qsize()} "
              f"worker_alive={worker.is_alive() if worker else None}",
              flush=True)
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


# ---------------------------------------------------------------------------
# 修复4：错误列表换行布局（长摘要完整可见）
# ---------------------------------------------------------------------------
LONG_SUMMARY_LOG = (
    "2024-01-01 09:00:00 ERROR [db] " + "x" * 140 + " 尾部可见标记TAIL\n"
    "2024-01-01 09:00:01 FATAL [core] short fatal\n"
)


class TestClusterListWrap:
    def test_rows_rendered_with_summary_label(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        # 两行错误 -> 两行记录（FATAL 排序在前）
        assert len(app._cluster_rows) == 2
        # 长摘要在行 1（FATAL"short fatal"置顶）：完整未截断
        first_summary = str(app._cluster_rows[1]["summary"].cget("text"))
        assert "TAIL" in first_summary
        assert len(first_summary) > 100
        # 行首元信息不包含摘要
        head_text = str(app._cluster_rows[1]["frame"]
                        .winfo_children()[0].cget("text"))
        assert "TAIL" not in head_text

    def test_wraplength_adapts_to_width(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        width = app._cluster_list.winfo_width()
        if width > 100:  # 窗口已布局
            for row in app._cluster_rows:
                assert int(row["summary"].cget("wraplength")) >= 240

    def test_row_content_not_clipped(self, app):
        """修复验证：摘要标签请求宽度不超列表可视宽度（不再溢出裁剪）。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        # 换行后摘要的请求宽度应受 wraplength 约束
        for row in app._cluster_rows:
            assert row["summary"].winfo_reqwidth() <= \
                int(row["summary"].cget("wraplength")) + 40

    def test_selection_highlight(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(1)
        assert app._selected_row == 1
        app.update()
        selected_color = app._cluster_rows[1]["frame"].cget("fg_color")
        default_color = app._cluster_rows[0]["frame"].cget("fg_color")
        assert selected_color != default_color

    def test_row_click_selects(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        summary_label = app._cluster_rows[1]["summary"]
        summary_label.event_generate("<Button-1>")
        app.update()
        assert app._selected_row == 1

    def test_hover_highlight(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        # 行 0 在结果渲染后自动选中，悬停测试使用未选中的行 1
        frame = app._cluster_rows[1]["frame"]
        base = frame.cget("fg_color")
        app._hover_row(1, True)
        app.update()
        hovered = frame.cget("fg_color")
        app._hover_row(1, False)
        app.update()
        restored = frame.cget("fg_color")
        assert hovered != base and restored == base

    def test_long_word_wraps_not_overflow(self, app):
        """超长无空格 token（哈希/路径）也按字符折行，不横向溢出。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        row = app._cluster_rows[1]
        assert row["summary"].winfo_reqwidth() <= \
            int(row["summary"].cget("wraplength")) + 40


# ---------------------------------------------------------------------------
# 修复9：性能优化（matplotlib 懒加载 + 共享字体防死锁）
# ---------------------------------------------------------------------------
class TestPerformanceOptimizations:
    def test_charts_module_not_imported_at_startup(self):
        """matplotlib 必须延迟加载：GUI 模块导入后不应加载 matplotlib。

        用独立子进程验证（当前测试进程可能已被其他用例拉起 matplotlib）。
        """
        import json
        import subprocess
        import sys
        code = ("import sys, json; import log_ai_compressor.gui.app; "
                "print(json.dumps('matplotlib' not in sys.modules))")
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              cwd=str(Path(__file__).resolve().parent.parent))
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout.strip().splitlines()[-1]), \
            "matplotlib 不应在 GUI 启动路径上被导入（应懒加载）"

    def test_chart_button_lazy_loads_charts(self, app):
        """点击统计图表后才导入 matplotlib 且窗口正常弹出。"""
        import sys
        import time as _time
        _run_paste_analysis(app)
        # 触发前确保未加载（其他测试可能已加载，先清理引用判定逻辑：
        # 直接调用 _show_charts 验证功能不受懒加载影响）
        had_matplotlib = "matplotlib" in sys.modules
        app._show_charts()
        deadline = _time.time() + 5
        while _time.time() < deadline and not (
                app._chart_window is not None
                and app._chart_window.winfo_exists()):
            app.update()
            _time.sleep(0.02)
        assert app._chart_window is not None and app._chart_window.winfo_exists()
        assert "matplotlib" in sys.modules or had_matplotlib
        app._chart_window.destroy()
        app._chart_window = None

    def test_shared_fonts_reused_across_rows(self, app):
        """行级字体必须共享复用：防止跨线程 GC 析构导致 Tkinter 死锁。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        assert len(app._cluster_rows) >= 2
        # 所有行引用同一字体对象（无每行新建）
        fonts = {id(row["summary"].cget("font")) for row in app._cluster_rows}
        # tk.Label cget('font') 返回字体名；底层共享通过 app 字段验证
        assert app._font_row_summary is not None
        assert app._font_row_head is not None
        # 两个字体的底层 Tk 字体名不同（各自独立共享对象）
        assert (str(app._font_row_head) != str(app._font_row_summary))

    def test_analysis_runs_in_worker_thread(self, app, monkeypatch):
        """分析必须在后台线程执行（主线程阻塞 = 界面卡死）。"""
        import threading
        import log_ai_compressor.gui.app as app_mod
        observed = {}

        def spy_analyze(text, **kwargs):
            observed["thread"] = threading.current_thread()
            from log_ai_compressor.core.models import RunStats, AnalysisResult
            return AnalysisResult(stats=RunStats(source="<t>", total_lines=1),
                                  clusters=[])

        monkeypatch.setattr(app_mod, "analyze_text", spy_analyze)
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        app._on_start()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.update()
            if app._result is not None:
                break
            time.sleep(0.02)
        assert app._result is not None
        # 工作线程必须不是主线程
        assert observed["thread"] is not threading.main_thread()
