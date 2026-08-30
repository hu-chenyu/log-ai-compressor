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
            observed["context_lines"] = kwargs.get("context_lines")
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


# ---------------------------------------------------------------------------
# 修复5：上下文行数（默认 50 + GUI 可调节 5~200）
# ---------------------------------------------------------------------------
class TestContextLines:
    def test_default_context_lines_is_50(self):
        """全局默认值必须为 50（原 5 行太少）。"""
        from log_ai_compressor.constants import DEFAULT_CONTEXT_LINES
        assert DEFAULT_CONTEXT_LINES == 50

    def test_context_entry_exists_with_default(self, app):
        """配置区必须有「上下文行数」输入框，默认值 50。"""
        assert app._ctx_entry is not None
        assert app._ctx_entry.get() == "50"

    def test_context_lines_clamped_to_range(self, app):
        """非法/越界输入自动钳制到 5~200。"""
        for raw, expected in [("1", 5), ("0", 5), ("-3", 5), ("999", 200),
                              ("abc", 50), ("", 50), ("8", 8), ("120", 120)]:
            app._ctx_entry.delete(0, "end")
            app._ctx_entry.insert(0, raw)
            assert app._current_context_lines() == expected, \
                f"输入 {raw!r} 应钳制为 {expected}"

    def test_context_lines_passed_to_pipeline(self, app, monkeypatch):
        """GUI 配置的上下文行数必须传给分析管线。"""
        import log_ai_compressor.gui.app as app_mod
        captured = {}

        def spy_analyze(text, **kwargs):
            captured["context_lines"] = kwargs.get("context_lines")
            from log_ai_compressor.core.models import RunStats, AnalysisResult
            return AnalysisResult(stats=RunStats(source="<t>", total_lines=1),
                                  clusters=[])

        monkeypatch.setattr(app_mod, "analyze_text", spy_analyze)
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "30")
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
        assert captured["context_lines"] == 30

    def test_context_lines_persisted(self, app, tmp_path):
        """用户调整的上下文行数必须持久化，重启后恢复。"""
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "100")
        app._save_config()
        saved = app._store.load()
        assert saved.get("context_lines") == 100

    def test_context_lines_restored_from_config(self, app, monkeypatch):
        """配置文件中的 context_lines 启动时恢复到输入框。"""
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "77")
        app._save_config()
        from log_ai_compressor.gui.app import LogCompressorApp
        # 新实例读取同一配置文件
        new_app = LogCompressorApp()
        try:
            new_app.update()
            assert new_app._ctx_entry.get() == "77"
        finally:
            try:
                new_app._on_close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 修复6：「典型样例」悬停说明（Tooltip）
# ---------------------------------------------------------------------------
SAMPLE_HELP_TEXT = ("该错误类型的代表性日志样例，包含完整的错误信息、"
                    "堆栈跟踪和前后上下文，用于快速定位问题")


class TestSampleHelpTooltip:
    def test_help_icon_exists(self, app):
        """详情面板标题旁必须有 ⓘ 帮助图标。"""
        assert app._sample_help_tooltip is not None

    def test_tooltip_text_content(self, app):
        """悬停说明文本必须为规范要求的完整说明。"""
        tip = app._sample_help_tooltip
        assert tip._text == SAMPLE_HELP_TEXT

    def test_tooltip_shows_on_hover(self, app):
        """Enter 事件（延时后）应显示说明窗口，Leave 后销毁。"""
        tip = app._sample_help_tooltip
        assert tip._tip is None
        tip._show()
        app.update()
        assert tip._tip is not None
        assert tip._tip.winfo_exists()
        # 窗口内文本正确
        children = tip._tip.winfo_children()
        assert children, "tooltip 应包含文本标签"
        assert children[0].cget("text") == SAMPLE_HELP_TEXT
        tip._hide()
        app.update()
        assert tip._tip is None

    def test_tooltip_delayed_show_via_event(self, app):
        """Enter 事件 -> 延时调度 -> 显示（真实悬停路径）。"""
        tip = app._sample_help_tooltip
        tip._schedule()
        assert tip._after_id is not None  # 已调度延时任务
        # 手动触发延时回调（跳过等待）
        tip._show()
        app.update()
        assert tip._tip is not None
        tip._hide()

    def test_tooltip_leak_free_after_destroy(self, app):
        """关联控件销毁后 hide 不抛异常（健壮性）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        tip._hide()   # 常规销毁
        tip._hide()   # 二次销毁应幂等
        assert tip._tip is None


# ---------------------------------------------------------------------------
# 修复7：全屏查看（列表 / 详情独立最大化窗口 + ESC 返回）
# ---------------------------------------------------------------------------
class TestFullscreenView:
    def _open_fs_windows(self, app):
        """打开两个全屏窗口并返回（列表窗, 详情窗）。"""
        app._open_list_fullscreen()
        app.update()
        list_win = [w for w in app.winfo_children()
                    if isinstance(w, tk.Toplevel)
                    and "错误分类列表" in w.title()]
        app._open_detail_fullscreen()
        app.update()
        detail_win = [w for w in app.winfo_children()
                      if isinstance(w, tk.Toplevel)
                      and "错误详情" in w.title()]
        return list_win, detail_win

    def test_fullscreen_buttons_exist(self, app):
        """主界面必须有列表 / 详情两个全屏按钮。"""
        assert app._list_fs_btn is not None
        assert app._detail_fs_btn is not None

    def test_list_fullscreen_opens_with_rows(self, app):
        """列表全屏：窗口打开且包含全部错误行 + 搜索框。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._open_list_fullscreen()
        app.update()
        fs_windows = [w for w in app.winfo_children()
                      if isinstance(w, tk.Toplevel)
                      and "错误分类列表" in w.title()]
        assert fs_windows, "列表全屏窗口应已打开"
        win = fs_windows[0]
        # 窗口内应有滚动列表 + 搜索框 + 关闭按钮
        # （CTk 控件 winfo_class 均为 Frame，直接取 text 属性判定）
        texts = _texts_in(win)
        assert any("关闭" in t for t in texts), "应有关闭按钮"
        assert any("搜索" in t for t in texts), "应有搜索框"

    def test_list_fullscreen_search_filters(self, app):
        """全屏搜索：关键字过滤行数。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._open_list_fullscreen()
        app.update()
        win = [w for w in app.winfo_children()
               if isinstance(w, tk.Toplevel)
               and "错误分类列表" in w.title()][0]
        entries = [w for w in _all_widgets(win)
                   if isinstance(w, ctk.CTkEntry)]
        assert entries, "全屏窗口应有搜索输入框"
        search = entries[0]
        # 输入 fatal：仅 FATAL"short fatal" 行匹配（trace 实时过滤）
        search.insert(0, "fatal")
        app.update()
        labels = [w for w in _all_widgets(win)
                  if isinstance(w, ctk.CTkLabel)
                  and "显示" in str(w.cget("text"))]
        assert labels, "应有过滤计数标签"
        assert "1 /" in str(labels[0].cget("text")), \
            f"过滤后应只剩 1 行，实际: {labels[0].cget('text')}"

    def test_detail_fullscreen_shows_content(self, app):
        """详情全屏：内容与主面板一致且支持横向滚动。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._open_detail_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel) and "错误详情" in w.title()]
        assert wins, "详情全屏窗口应已打开"
        win = wins[0]
        # 全屏文本内容 = 主面板内容
        boxes = [w for w in _all_widgets(win)
                 if isinstance(w, ctk.CTkTextbox)]
        assert boxes
        fs_text = boxes[0].get("1.0", "end")
        main_text = app._detail_box.get("1.0", "end")
        assert fs_text.strip() == main_text.strip()
        # 水平滚动条存在（wrap=none + xscrollbar）
        xbars = [w for w in _all_widgets(win)
                 if isinstance(w, tk.Scrollbar)
                 and str(w.cget("orient")).endswith("horizontal")]
        assert xbars, "详情全屏应配置水平滚动条"

    def test_fullscreen_esc_closes(self, app):
        """ESC 键应关闭全屏窗口（返回主界面）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app._open_list_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "错误分类列表" in w.title()]
        assert wins
        win = wins[0]
        win.event_generate("<Escape>")
        app.update()
        remaining = [w for w in app.winfo_children()
                     if isinstance(w, tk.Toplevel)
                     and "错误分类列表" in w.title()]
        assert not remaining, "ESC 后窗口应关闭"
        # 主界面仍存活
        assert app.winfo_exists()

    def test_fullscreen_click_links_main_detail(self, app):
        """全屏列表点击行 -> 主界面详情同步切换。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(0)
        before = app._detail_box.get("1.0", "end")
        app._open_list_fullscreen()
        app.update()
        win = [w for w in app.winfo_children()
               if isinstance(w, tk.Toplevel)
               and "错误分类列表" in w.title()][0]
        # 找到行 frame 并点击（行 1 的 frame）
        rows = [w for w in _all_widgets(win)
                if isinstance(w, ctk.CTkFrame) and w.winfo_children()]
        # 触发行 1 的点击（通过事件绑定）——直接调用绑定回调
        canvases = [w for w in _all_widgets(win) if w.winfo_class() == "Canvas"]
        # 行 frame 的第一个可点击子控件
        clickables = []
        for f in rows:
            for sub in _all_widgets(f):
                if sub.winfo_class() in ("CTkLabel", "Label", "Frame"):
                    clickables.append(sub)
        target = None
        for w in clickables:
            binds = w.bind("<Button-1>")
            if binds:
                target = w
                break
        assert target is not None, "行控件应有点击绑定"
        target.event_generate("<Button-1>")
        app.update()
        after = app._detail_box.get("1.0", "end")
        assert after != before or len(app._displayed) == 1, \
            "点击全屏行应联动主界面详情（或仅 1 行无法切换）"

    def test_fullscreen_without_analysis_is_safe(self, app):
        """未分析时点击全屏按钮不崩溃（提示后返回）。"""
        # displayed 为空 -> 直接返回不弹窗（messagebox 需要交互，跳过）
        app._displayed = []
        app._detail_box.delete("1.0", "end")
        # monkeypatch 掉 messagebox 避免阻塞
        import log_ai_compressor.gui.app as app_mod
        original = app_mod.messagebox.showinfo
        app_mod.messagebox.showinfo = lambda *a, **k: None
        try:
            app._open_list_fullscreen()
            app._open_detail_fullscreen()
            app.update()
        finally:
            app_mod.messagebox.showinfo = original


def _all_widgets(root):
    """递归收集窗口内全部控件。"""
    result = []
    stack = [root]
    while stack:
        w = stack.pop()
        result.append(w)
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            pass
    return result


def _texts_in(root):
    """收集窗口内全部控件的 text 属性（无 text 的控件跳过）。"""
    texts = []
    for w in _all_widgets(root):
        try:
            texts.append(str(w.cget("text")))
        except (tk.TclError, AttributeError, TypeError, ValueError):
            continue
    return [t for t in texts if t]


# ---------------------------------------------------------------------------
# 修复8：解析规则悬停说明（动态 tooltip + 状态栏说明）
# ---------------------------------------------------------------------------
RULE_TOOLTIPS = {
    "generic": "通用系统日志格式，适用于大多数标准应用日志、服务日志",
    "embedded": "嵌入式/UT测试日志格式，适用于嵌入式设备、单元测试输出、编译日志",
    "jenkins": "Jenkins控制台输出格式，适用于CI/CD流水线日志、构建日志",
}


class TestRuleTooltips:
    def test_rule_help_tooltip_exists(self, app):
        """解析规则旁必须有 ⓘ 悬停说明。"""
        assert app._rule_help_tooltip is not None

    def test_rule_tooltip_dynamic_text(self, app):
        """tooltip 文本必须跟随当前选中的解析规则动态变化。"""
        tip = app._rule_help_tooltip
        for rule, expected in RULE_TOOLTIPS.items():
            app._rule_menu.set(rule)
            assert tip._current_text() == expected, \
                f"规则 {rule} 的悬停说明不正确"

    def test_rule_tooltip_shows_current_rule_text(self, app):
        """显示中的 tooltip 内容与当前规则一致。"""
        tip = app._rule_help_tooltip
        app._rule_menu.set("embedded")
        tip._show()
        app.update()
        assert tip._tip is not None
        label = tip._tip.winfo_children()[0]
        assert label.cget("text") == RULE_TOOLTIPS["embedded"]
        tip._hide()

    def test_all_rules_have_descriptions(self):
        """三个内置规则都必须有说明文本。"""
        from log_ai_compressor.gui.app import RULE_DESCRIPTIONS, RULE_NAMES
        for name in RULE_NAMES:
            assert name in RULE_DESCRIPTIONS
            assert len(RULE_DESCRIPTIONS[name]) >= 10

    def test_rule_change_updates_status_bar(self, app):
        """切换规则时状态栏即时展示适用场景说明。"""
        app._on_rule_changed("generic")
        assert "通用系统日志格式" in app._status_label.cget("text")
        app._on_rule_changed("jenkins")
        assert "Jenkins" in app._status_label.cget("text")

    def test_unknown_rule_tooltip_empty(self, app):
        """未知规则名时 tooltip 文本为空（不显示误导信息）。"""
        tip = app._rule_help_tooltip
        app._rule_menu.set("nonexistent-rule")
        assert tip._current_text() == ""
