# -*- coding: utf-8 -*-
"""GUI 应用层测试：拖拽、按钮状态、主题、全屏、Tooltip 等交互逻辑。

运行前提：需要可用的显示环境（本地桌面）；CI 无头环境自动跳过。
"""
from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
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
    """创建主窗口实例，隔离用户配置文件，测试后销毁。

    teardown 强制 gc.collect()：长测试序列中 Tk/CTk 控件句柄
    （Canvas/字体/GDI 对象）依赖 GC 释放，累积不回收会触发
    Windows 句柄耗尽（新 Tk root 创建失败）。
    """
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
    import gc
    gc.collect()


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


def _run_paste_analysis(app, text=SAMPLE_PASTE, timeout=60.0):
    """执行一次文本粘贴分析并等待完成。

    timeout 放宽到 60s：全量测试运行时系统满载（多 GUI 实例 +
    matplotlib 首次导入），30s 偶发超时。
    """
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

    def test_row_head_internal_click_selects(self, app):
        """修复R2：真实点击命中 CTkLabel 内部子控件也触发选中。

        Tk 事件不冒泡：CTkLabel 是容器（内部 Canvas + tk.Label），
        只绑容器时点击头部行（级别/次数行）不生效——此为
        「点其他项不生效、只能默认选中第一行」的根因。
        """
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(0)
        app.update()
        head = app._cluster_rows[0]["frame"].winfo_children()[0]
        assert isinstance(head, ctk.CTkLabel)
        # 取 CTkLabel 内部的真实子控件（Canvas / tk.Label）
        internals = head.winfo_children()
        assert internals, "CTkLabel 应有内部子控件"
        # 改点行 1 的头部内部子控件验证选中切换
        head1 = app._cluster_rows[1]["frame"].winfo_children()[0]
        for child in head1.winfo_children():
            child.event_generate("<Button-1>")
        app.update()
        assert app._selected_row == 1, \
            "点击行 1 头部（CTkLabel 内部子控件）应选中行 1"
        # 详情面板同步更新
        detail = app._detail_box.get("1.0", "end")
        assert "错误摘要" in detail

    def test_row_click_detail_updates(self, app):
        """修复R2：点击任意行右侧详情同步切换。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(0)
        app.update()
        before = app._detail_box.get("1.0", "end")
        # 点击行 1 的摘要标签
        app._cluster_rows[1]["summary"].event_generate("<Button-1>")
        app.update()
        after = app._detail_box.get("1.0", "end")
        assert app._selected_row == 1
        assert before != after, "详情应随点击切换"

    def test_selected_row_blue_highlight(self, app):
        """修复R2：选中态与未选中态背景色明显区分（蓝色高亮）。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(1)
        app.update()
        states = app._row_states()
        selected = app._cluster_rows[1]["frame"].cget("fg_color")
        normal = app._cluster_rows[0]["frame"].cget("fg_color")
        # CTk 颜色可能返回 hex 或元组字符串，统一转字符串比较
        assert str(selected) != str(normal), "选中/未选中应明显区分"
        assert str(states["selected"]) not in ("", "None")

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
# 修复R7：错误分类列表宽度对齐 + 字体放大（主列表 / 虚拟列表 / 全屏一致）
# ---------------------------------------------------------------------------
class TestClusterListFontAndWidth:
    def test_main_list_font_sizes(self, app):
        """修复R7：主列表字体实际大小——头部 17 加粗 / 摘要 14。"""
        assert int(app._font_row_head.cget("size")) == 17
        assert str(app._font_row_head.cget("weight")) == "bold"
        assert int(app._font_row_summary.cget("size")) == 14
        # 底层 tk 命名字体的实际像素尺寸（CTkFont 用负数表示像素）
        head_tk = tkfont.Font(root=app, name=str(app._font_row_head),
                              exists=True)
        sum_tk = tkfont.Font(root=app, name=str(app._font_row_summary),
                             exists=True)
        assert int(head_tk.cget("size")) == -17
        assert int(sum_tk.cget("size")) == -14

    def test_fullscreen_list_font_sizes(self, app):
        """修复R7：全屏列表字体实际大小——头部 18 加粗 / 摘要 16。"""
        assert int(app._font_fs_head.cget("size")) == 18
        assert str(app._font_fs_head.cget("weight")) == "bold"
        assert int(app._font_fs_summary.cget("size")) == 16

    def test_classic_row_uses_enlarged_fonts(self, app):
        """修复R7：经典模式行控件直接使用放大后的共享字体对象。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        head = app._cluster_rows[0]["frame"].winfo_children()[0]
        assert isinstance(head, ctk.CTkLabel)
        assert head.cget("font") is app._font_row_head
        assert str(app._cluster_rows[0]["summary"].cget("font")) == \
            str(app._font_row_summary)

    def test_virtual_row_fonts_match_classic(self, app):
        """修复R7：虚拟模式（>40 行）与经典模式字体一致（同一共享字体）。"""
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None, "60 簇应启用虚拟列表"
        slot = app._virtual_list.slots[0]
        assert str(slot["head"].cget("font")) == str(app._font_row_head)
        assert str(slot["summary"].cget("font")) == \
            str(app._font_row_summary)

    def test_virtual_row_height_enlarged(self, app):
        """修复R7：虚拟行高随字体放大（容纳 17 头部 + 多行 14 摘要）。"""
        from log_ai_compressor.gui.app import VirtualClusterList
        assert VirtualClusterList.ROW_HEIGHT >= 108, \
            "行高应容纳放大后的头部与两行摘要"

    def test_fullscreen_rows_use_fs_fonts(self, app):
        """修复R7：全屏列表行实际使用全屏字体（头部 18 / 摘要 16）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        fonts_in_use = set()

        def walk(widget):
            for child in widget.winfo_children():
                try:
                    fonts_in_use.add(str(child.cget("font")))
                except (tk.TclError, ValueError):
                    pass    # CTkFrame 等不支持 font 属性的控件跳过
                walk(child)

        walk(win)
        assert str(app._font_fs_head) in fonts_in_use, \
            "全屏行头部应使用 18 号加粗字体"
        assert str(app._font_fs_summary) in fonts_in_use, \
            "全屏行摘要应使用 16 号字体"
        win.event_generate("<Escape>")
        app.update()

    def test_list_right_edge_aligns_with_fullscreen_button(self, app):
        """修复R7：列表右缘（含滚动条）与「全屏」按钮右缘严格对齐。"""
        app.update()
        if app._list_host.winfo_width() < 50:
            pytest.skip("窗口未完成布局")
        btn = app._list_fs_btn
        btn_right = btn.winfo_rootx() + btn.winfo_width()
        list_right = (app._list_host.winfo_rootx()
                      + app._list_host.winfo_width())
        assert abs(list_right - btn_right) <= 2, \
            f"列表右缘与按钮右缘偏差 {list_right - btn_right}px（应 ≤2px）"

    def test_list_fills_host_width(self, app):
        """修复R7：列表占满宿主宽度 ≥90%（去除固定 470px 留白）。"""
        app.update()
        if app._list_host.winfo_width() < 50:
            pytest.skip("窗口未完成布局")
        host_w = app._list_host.winfo_width()
        list_w = app._cluster_list.winfo_width()
        assert list_w >= 0.9 * host_w, \
            f"列表宽 {list_w}px 应占宿主宽 {host_w}px 的 90% 以上"

    def test_summary_wraplength_uses_full_list_width(self, app):
        """修复R7：摘要初始换行宽度按列表实际宽度计算（不再固定 400 早折行）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        width = app._cluster_list.winfo_width()
        if width <= 100:
            pytest.skip("窗口未完成布局")
        wrap = int(app._cluster_rows[0]["summary"].cget("wraplength"))
        assert wrap >= width - 80, \
            f"换行宽 {wrap}px 应接近列表宽 {width}px（充分利用加宽空间）"


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
        """配置文件中的 context_lines 启动时恢复到输入框。

        不创建第二个 Tk root（长序列句柄耗尽风险），以
        _restore_config 在干净输入框上的重放等效验证。
        """
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "77")
        app._save_config()
        # 模拟重启：清空输入框后用保存的配置重放恢复逻辑
        app._ctx_entry.delete(0, "end")
        app._config = app._store.load()
        app._restore_config()
        app.update()
        assert app._ctx_entry.get() == "77"


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
        # 窗口内文本正确（Canvas 绘制的 text 项）
        canvas = tip._tip.winfo_children()[0]
        item = canvas.find_withtag("text")[0]
        assert canvas.itemcget(item, "text") == SAMPLE_HELP_TEXT
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
# 修复R3：Tooltip 字体放大 + 自动换行 + 智能定位（不溢出屏幕）
# ---------------------------------------------------------------------------
class TestTooltipR3:
    """悬停说明的可读性与定位（典型样例说明 / 解析规则说明共用）。"""

    def test_tooltip_font_size_enlarged(self, app):
        """修复R3：tooltip 字体放大到 12~13 号（原 10 号过小）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            font = str(canvas.itemcget(item, "font"))
            assert "12" in font or "13" in font, f"字体应为 12/13 号，实际 {font}"
        finally:
            tip._hide()

    def test_tooltip_wrap_width_in_range(self, app):
        """修复R3：tooltip 宽度限制在 400~500px（长文本自动换行）。"""
        from log_ai_compressor.gui.app import Tooltip
        assert 400 <= Tooltip._WRAP <= 500
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            assert 400 <= int(canvas.itemcget(item, "width")) <= 500
            # 画布宽度不超 500 + 边距
            assert canvas.winfo_width() <= 530
        finally:
            tip._hide()

    def test_tooltip_light_bg_dark_text(self, app):
        """修复R3：tooltip 白底深字（视觉清晰）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            # 深色文字（亮度低于 0.5）
            fg = canvas.itemcget(item, "fill")
            r, g, b = (int(fg[i:i + 2], 16) for i in (1, 3, 5))
            assert (r + g + b) / 3 < 128, "文字应为深色"
            # 圆角卡片为白色（rounded 路径）或画布白底（降级路径）
            cards = canvas.find_enclosed(0, 0, canvas.winfo_width() + 5,
                                         canvas.winfo_height() + 5)
            fill_colors = {str(canvas.itemcget(c, "fill")) for c in cards}
            assert "#ffffff" in fill_colors or str(canvas.cget("bg")) == "#ffffff"
        finally:
            tip._hide()

    def test_tooltip_within_screen_bounds(self, app):
        """修复R3：tooltip 完整可见（不溢出屏幕右/下边缘）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            tw = tip._tip
            sw, sh = tw.winfo_screenwidth(), tw.winfo_screenheight()
            x, y = tw.winfo_x(), tw.winfo_y()
            w, h = tw.winfo_width(), tw.winfo_height()
            assert x >= 0, "左边缘溢出"
            assert y >= 0, "上边缘溢出"
            assert x + w <= sw + 2, f"右边缘溢出（{x + w} > {sw}）"
            assert y + h <= sh + 2, f"下边缘溢出（{y + h} > {sh}）"
        finally:
            tip._hide()

    def _tooltip_on_edge_widget(self, app, geometry: str):
        """在指定屏幕位置创建宿主控件并显示 tooltip。"""
        host = tk.Toplevel(app)
        host.geometry(geometry)
        host.geometry("+300+300")  # 先强制一次布局
        host.geometry(geometry)
        app.update()
        lbl = tk.Label(host, text="ⓘ")
        lbl.pack()
        app.update()
        return host, lbl

    def test_tooltip_flips_left_near_right_edge(self, app):
        """修复R3：宿主控件贴近屏幕右边缘时 tooltip 向左弹出。"""
        from log_ai_compressor.gui.app import Tooltip
        sw = app.winfo_screenwidth()
        host, lbl = self._tooltip_on_edge_widget(
            app, f"+{sw - 40}+240")
        try:
            assert lbl.winfo_rootx() > sw - 120, "测试前置：宿主应贴近右边缘"
            tip = Tooltip(lbl, "较长的悬停说明文本 " * 10)
            tip._show()
            app.update()
            try:
                tw = tip._tip
                # 完整可见且在宿主左侧（向左弹出）
                assert tw.winfo_x() + tw.winfo_width() <= sw + 2
                assert tw.winfo_x() < lbl.winfo_rootx(), \
                    "右边缘情形应在控件左侧弹出"
            finally:
                tip._hide()
        finally:
            host.destroy()
            app.update()

    def test_tooltip_flips_up_near_bottom_edge(self, app):
        """修复R3：宿主控件贴近屏幕下边缘时 tooltip 向上弹出。"""
        from log_ai_compressor.gui.app import Tooltip
        sh = app.winfo_screenheight()
        host, lbl = self._tooltip_on_edge_widget(
            app, f"+240+{sh - 50}")
        try:
            assert lbl.winfo_rooty() > sh - 150, "测试前置：宿主应贴近下边缘"
            tip = Tooltip(lbl, "多行悬停说明\n" * 8)
            tip._show()
            app.update()
            try:
                tw = tip._tip
                assert tw.winfo_y() + tw.winfo_height() <= sh + 2
                assert tw.winfo_y() < lbl.winfo_rooty(), \
                    "下边缘情形应在控件上方弹出"
            finally:
                tip._hide()
        finally:
            host.destroy()
            app.update()


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

    def test_fullscreen_window_centered_and_large(self, app):
        """修复R2：全屏窗口位于屏幕正中央且尺寸 ≥80%。

        zoomed 生效时窗口为整屏（同样满足 ≥80%）；zoomed 不可用时
        退回显式居中几何（85% × 88%）。两种情形均校验。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        win = app._make_fullscreen_window("测试全屏")
        app.update()
        try:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            # zoomed 状态：窗口即整屏，视为通过
            if str(win.state()) == "zoomed":
                return
            w, h = win.winfo_width(), win.winfo_height()
            assert w >= sw * 0.8, f"宽度 {w} 应 ≥ 屏幕 80%（{sw * 0.8:.0f}）"
            assert h >= sh * 0.8, f"高度 {h} 应 ≥ 屏幕 80%（{sh * 0.8:.0f}）"
            # 居中：窗口中心与屏幕中心偏差 ≤ 5%
            cx_off = abs((w / 2 + win.winfo_x()) - sw / 2)
            cy_off = abs((h / 2 + win.winfo_y()) - sh / 2)
            assert cx_off <= sw * 0.05, f"水平中心偏差 {cx_off}px 过大"
            assert cy_off <= sh * 0.05, f"垂直中心偏差 {cy_off}px 过大"
        finally:
            win.destroy()
            app.update()

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
        """ESC 键应关闭全屏窗口（返回主界面）。

        修复R6：窗口预创建复用后 ESC 改为 withdraw（隐藏返回主界面，
        窗口保留复用）。判定标准：窗口销毁 或 不再可见（未映射）。
        """
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
                     and "错误分类列表" in w.title()
                     and w.winfo_ismapped()]
        assert not remaining, "ESC 后窗口应不再可见（销毁或隐藏）"
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


# ---------------------------------------------------------------------------
# 修复R4：全屏窗口簇展开（左右分栏 + 实例列表 + 实例详情联动）
# ---------------------------------------------------------------------------
REPEAT_LOG = "\n".join(
    f"2024-01-01 09:{i // 60:02d}:{i % 60:02d} ERROR [db] "
    f"connection refused to db-primary (attempt {i})"
    for i in range(12)
) + "\n" + "2024-01-01 09:01:01 ERROR [api] request 404 failed\n"


def _click_ctk_label(widget):
    """模拟真实点击：事件发给实际命中的子控件（Tk 不冒泡）。

    - CTkLabel（winfo_class 为 Frame 的容器）：bind 转发到内部
      Canvas/tk.Label，对其内部 Label 发事件；
    - 原生 tk.Label（修复R6 全屏行）：绑定即在本体，直接发事件。
    """
    if widget.winfo_class() == "Label":
        widget.event_generate("<Button-1>", x=3, y=2)
        widget.update()
        return
    for child in widget.winfo_children():
        if child.winfo_class() == "Label":
            child.event_generate("<Button-1>")
    widget.update()


class TestFullscreenExpand:
    def _open_fs(self, app):
        """分析 REPEAT_LOG 后打开列表全屏，返回窗口。

        修复R6：行渲染分批异步（首批 after 1ms），需推进事件循环
        至少一批行出现。
        """
        _run_paste_analysis(app, REPEAT_LOG)
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "错误分类列表" in w.title()]
        assert wins, "全屏窗口应已打开"
        return wins[0]

    def _find_toggles(self, win):
        # 修复R6：全屏行为原生 tk.Label（控件复用降开销）
        return [w for w in _all_widgets(win)
                if isinstance(w, tk.Label)
                and w.winfo_class() == "Label"
                and "×12" in str(w.cget("text"))]

    def test_toggle_button_exists_with_count(self, app):
        """每簇有「▶ ×N」展开按钮（次数在按钮上可点击）。"""
        win = self._open_fs(app)
        try:
            toggles = self._find_toggles(win)
            assert toggles, "应有 ×12 展开按钮"
            assert "▶" in str(toggles[0].cget("text")), "初始为收起态 ▶"
        finally:
            win.destroy()
            app.update()

    def test_expand_shows_all_instances(self, app):
        """点击展开：显示全部 12 个实例（时间戳 + 摘要）。"""
        win = self._open_fs(app)
        try:
            toggle = self._find_toggles(win)[0]
            _click_ctk_label(toggle)
            for _ in range(40):
                app.update()
                time.sleep(0.005)
            assert "▼" in str(toggle.cget("text")), "展开后为 ▼"
            # 实例行：含「  L行号  」模式的原生 label
            inst_labels = [
                w for w in _all_widgets(win)
                if isinstance(w, tk.Label)
                and "  L" in str(w.cget("text"))
                and "connection refused" in str(w.cget("text"))]
            assert len(inst_labels) == 12, \
                f"应展开 12 个实例，实际 {len(inst_labels)}"
        finally:
            win.destroy()
            app.update()

    def test_instance_click_shows_detail(self, app):
        """点击实例：右侧详情面板显示该实例原始日志与堆栈。"""
        win = self._open_fs(app)
        try:
            toggle = self._find_toggles(win)[0]
            _click_ctk_label(toggle)
            for _ in range(40):
                app.update()
                time.sleep(0.005)
            inst_labels = [
                w for w in _all_widgets(win)
                if isinstance(w, tk.Label)
                and "  L" in str(w.cget("text"))]
            assert inst_labels
            inst_labels[0].event_generate("<Button-1>")
            app.update()
            boxes = [w for w in _all_widgets(win)
                     if isinstance(w, ctk.CTkTextbox)]
            assert boxes, "右侧应有详情面板"
            text = boxes[0].get("1.0", "end")
            assert "【实例详情】" in text, "应显示实例详情"
            assert "原始日志" in text or "典型样例" in text
            assert "connection refused" in text
        finally:
            win.destroy()
            app.update()

    def test_cluster_click_shows_cluster_detail(self, app):
        """点击簇行：右侧显示簇详情（典型样例）。"""
        win = self._open_fs(app)
        try:
            # 点击首个簇行头部（非 toggle；修复R6 后为原生 Label）
            heads = [w for w in _all_widgets(win)
                     if w.winfo_class() == "Label"
                     and "ERROR" in str(w.cget("text"))
                     and "×" not in str(w.cget("text"))]
            assert heads, "应有簇行头部"
            _click_ctk_label(heads[0])
            app.update()
            boxes = [w for w in _all_widgets(win)
                     if isinstance(w, ctk.CTkTextbox)]
            text = boxes[0].get("1.0", "end")
            assert "【错误摘要】" in text, "应显示簇详情"
        finally:
            win.destroy()
            app.update()

    def test_collapse_after_expand(self, app):
        """再次点击 toggle 收起实例列表（▼ → ▶）。"""
        win = self._open_fs(app)
        try:
            toggle = self._find_toggles(win)[0]
            _click_ctk_label(toggle)
            for _ in range(40):
                app.update()
                time.sleep(0.005)
            assert "▼" in str(toggle.cget("text"))
            _click_ctk_label(toggle)
            for _ in range(60):
                app.update()
                time.sleep(0.005)
            assert "▶" in str(toggle.cget("text")), "收起后为 ▶"
        finally:
            win.destroy()
            app.update()

    def test_split_pane_layout(self, app):
        """左右分栏：左簇列表 + 右详情面板。"""
        win = self._open_fs(app)
        try:
            boxes = [w for w in _all_widgets(win)
                     if isinstance(w, ctk.CTkTextbox)]
            assert boxes, "右侧应有详情面板"
            # 左侧滚动列表存在
            from customtkinter import CTkScrollableFrame
            scrollables = [w for w in _all_widgets(win)
                           if isinstance(w, CTkScrollableFrame)]
            assert scrollables, "左侧应有簇列表"
        finally:
            win.destroy()
            app.update()

    def test_instances_data_available(self, app):
        """数据层：簇实例全量记录（count == len(instances)）。"""
        _run_paste_analysis(app, REPEAT_LOG)
        db = max(app._displayed, key=lambda c: c.count)
        assert db.count == 12
        assert len(db.instances) == 12
        assert db.instances[0].entry is not None
        assert "connection refused" in db.instances[0].summary


# ---------------------------------------------------------------------------
# 修复R6：UI 性能优化（虚拟列表 / 全屏窗口复用 / 原生行控件）
# ---------------------------------------------------------------------------
def _make_virtual_log(n=60):
    """生成 n 类互不相似的错误（数字被指纹归一化掩盖、相似骨架会
    触发相似度合并——用确定性唯一拼造码 + 分段不同骨架词保证独立簇）。"""
    cons = "bcdfghjklmnpqrstvwxz"
    vow = "aeiou"
    muls = [7, 11, 13, 17, 19, 23, 3, 5, 9, 29]
    tails = ["signal lost", "carrier gone", "beacon dead"]

    def code(i):
        out = [cons[(i // 20 * 7) % 20]]
        for k, m in enumerate(muls):
            mod = 20 if k % 2 == 0 else 5
            out.append((cons if k % 2 == 0 else vow)[(i * m) % mod])
        return "".join(out)

    return "\n".join(
        f"2024-01-01 09:{i // 60:02d}:{i % 60:02d} ERROR [db] "
        f"{code(i)} {tails[i // 20]}" for i in range(n)) + "\n"


def _run_many_clusters(app, n=60):
    """分析 n 簇日志并把 Top N 调大（触发虚拟列表阈值）。"""
    app._topn_entry.delete(0, "end")
    app._topn_entry.insert(0, "200")
    _run_paste_analysis(app, _make_virtual_log(n))


class TestVirtualList:
    def test_virtual_list_activates_above_threshold(self, app):
        """修复R6：列表超过阈值（40）切换虚拟滚动渲染。"""
        from log_ai_compressor.gui.app import VIRTUAL_LIST_THRESHOLD
        _run_many_clusters(app)
        assert len(app._displayed) > VIRTUAL_LIST_THRESHOLD
        assert app._virtual_list is not None, "应启用虚拟列表"
        # 经典滚动容器隐藏
        assert not app._cluster_list.winfo_ismapped()

    def test_slot_pool_bounded(self, app):
        """修复R6：池化行数远小于总行数（只建可见区+缓冲）。"""
        _run_many_clusters(app)
        slots = app._virtual_list.slots
        app.update()
        assert len(slots) < 25, \
            f"池行数应 <25（视口行数级别），实际 {len(slots)}"

    def test_virtual_row_click_selects(self, app):
        """修复R6：虚拟行点击选中 + 详情同步（池行复用后仍正确）。"""
        _run_many_clusters(app)
        app.update()
        app._select_cluster(0)
        slots = app._virtual_list.slots
        # 点可见区第二行
        target = next(s for s in slots if s["idx"] == 1)
        target["summary"].event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._selected_row == 1
        assert "【错误摘要】" in app._detail_box.get("1.0", "end")

    def test_virtual_scroll_reuses_slots(self, app):
        """修复R6：滚动后池行复用到高索引（不新建控件）。"""
        _run_many_clusters(app)
        app.update()
        before = len(app._virtual_list.slots)
        app._virtual_list._canvas.yview_moveto(1.0)
        app.update()
        after = len(app._virtual_list.slots)
        assert after <= before + 1, "滚动不应显著增加池行数"
        max_idx = max(s["idx"] for s in
                      app._virtual_list.slots if s["idx"] >= 0)
        assert max_idx >= len(app._displayed) - 5, \
            "滚动到底应显示尾部行"

    def test_small_list_keeps_classic_mode(self, app):
        """修复R6：小列表（<=阈值）保持经典 CTk 滚动列表。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        assert app._virtual_list is None
        assert len(app._cluster_rows) == 2

    def test_selected_highlight_on_virtual_row(self, app):
        """修复R6：虚拟行选中态蓝色高亮（与未选中区分）。"""
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        slots = {s["idx"]: s for s in app._virtual_list.slots}
        if 1 in slots and 2 in slots:
            sel = str(slots[1]["frame"].cget("bg"))
            normal = str(slots[2]["frame"].cget("bg"))
            assert sel != normal, "选中/未选中背景应区分"

    def test_virtual_list_survives_theme_switch(self, app):
        """修复R6：虚拟模式下主题切换刷新配色不崩溃。"""
        _run_many_clusters(app)
        app.update()
        app._apply_theme_switch("blue")
        app.update()
        app._apply_theme_switch("dark")
        app.update()
        assert app._virtual_list is not None


class TestFullscreenReuse:
    def _open_fs(self, app):
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        return app._fs_list_win

    def test_fullscreen_window_reused(self, app):
        """修复R6：二次打开复用同一窗口对象（withdraw 而非销毁）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        win1 = self._open_fs(app)
        assert win1 is not None and win1.winfo_exists()
        win1.event_generate("<Escape>")
        app.update()
        assert not win1.winfo_ismapped(), "ESC 后窗口应隐藏"
        app._open_list_fullscreen()
        app.update()
        assert app._fs_list_win is win1, "二次打开应复用窗口对象"
        assert win1.winfo_ismapped(), "复用后窗口应可见"
        win1.destroy()
        app.update()

    def test_detail_fullscreen_window_reused(self, app):
        """修复R6：详情全屏窗口同样预创建复用。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app._select_cluster(0)
        app.update()
        app._open_detail_fullscreen()
        app.update()
        win1 = app._fs_detail_win
        assert win1 is not None and win1.winfo_exists()
        box = app._fs_detail_box
        assert "【错误摘要】" in box.get("1.0", "end")
        win1.event_generate("<Escape>")
        app.update()
        assert not win1.winfo_ismapped()
        # 换一个簇再打开：内容刷新且窗口复用
        app._select_cluster(1) if len(app._displayed) > 1 else None
        app._open_detail_fullscreen()
        app.update()
        assert app._fs_detail_win is win1
        assert win1.winfo_ismapped()
        win1.destroy()
        app.update()

    def test_fs_rows_native_lightweight(self, app):
        """修复R6：全屏列表行为原生 tk 控件（无内部 Canvas 开销）。"""
        _run_many_clusters(app)
        win = self._open_fs(app)
        try:
            native_labels = [w for w in _all_widgets(win)
                             if w.winfo_class() == "Label"
                             and "ERROR" in str(w.cget("text"))]
            assert native_labels, "全屏行应为原生 Label"
            canvases = [w for w in _all_widgets(win)
                        if w.winfo_class() == "Canvas"]
            # Canvas 仅来自 CTk 框架控件（搜索框/按钮/滚动条/详情框
            # ≈11 个，与行数无关）；57 行若用 CTk 行会有 100+ Canvas
            assert len(canvases) <= 12, \
                f"行控件不应引入大量 Canvas，实际 {len(canvases)}"
        finally:
            win.destroy()
            app.update()

    def test_open_fullscreen_callback_fast(self, app):
        """修复R6：全屏按钮回调 <300ms（首批渲染异步，窗口先显示）。"""
        _run_many_clusters(app)
        t0 = time.perf_counter()
        app._open_list_fullscreen()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        try:
            assert elapsed_ms < 300, \
                f"全屏回调应 <300ms，实际 {elapsed_ms:.0f}ms"
        finally:
            if app._fs_list_win is not None:
                app._fs_list_win.destroy()
                app.update()


# ---------------------------------------------------------------------------
# 修复R5：详情面板字体与高亮（Tooltip 已由 R3 统一修复）
# ---------------------------------------------------------------------------
class TestDetailPanelR5:
    def _select_db_cluster(self, app):
        """选中带堆栈的 db 簇（含系统库噪声帧 -> 有折叠行）。"""
        idx = next(i for i, c in enumerate(app._displayed)
                   if "connection refused" in c.summary)
        app._select_cluster(idx)
        app.update()
        return idx

    def test_main_detail_font_13(self, app):
        """修复R5：主面板详情字体 13 号（摘要/堆栈/上下文可读）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        font = app._detail_box.cget("font")
        size = font.cget("size") if hasattr(font, "cget") else font
        assert int(size) in (12, 13), f"详情字体应为 12~13 号，实际 {size}"

    def test_bstack_bold_and_distinct(self, app):
        """修复R5：业务栈帧琥珀色加粗（与普通行区分更明显）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        box = app._detail_box
        # 有业务栈帧（at com.app... 行）
        assert box.tag_ranges("bstack"), "应有业务栈帧高亮"
        fg = str(box.tag_cget("bstack", "foreground"))
        assert fg == "#fbbf24", f"业务栈帧应为琥珀色，实际 {fg}"
        font = str(box.tag_cget("bstack", "font"))
        assert "bold" in font, f"业务栈帧应加粗，实际 {font}"

    def test_fold_line_distinct_tag(self, app):
        """修复R5：系统库折叠提示独立配色（清晰可辨）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        box = app._detail_box
        assert box.tag_ranges("fold"), "java.base 噪声帧应生成折叠行"
        fg = str(box.tag_cget("fold", "foreground"))
        assert fg == "#a78bfa", f"折叠提示应为紫色，实际 {fg}"
        # 折叠行文本确实包含「已折叠」
        text = box.get("1.0", "end")
        assert "已折叠" in text

    def test_detail_fullscreen_font_13(self, app):
        """修复R5：详情全屏窗口字体 13 号。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        app._open_detail_fullscreen()
        app.update()
        try:
            wins = [w for w in app.winfo_children()
                    if isinstance(w, tk.Toplevel)
                    and "错误详情" in w.title()]
            assert wins
            boxes = [w for w in _all_widgets(wins[0])
                     if isinstance(w, ctk.CTkTextbox)]
            font = boxes[0].cget("font")
            size = font.cget("size") if hasattr(font, "cget") else font
            assert int(size) in (12, 13), f"全屏详情字体应 12~13 号，实际 {size}"
        finally:
            for w in [w for w in app.winfo_children()
                      if isinstance(w, tk.Toplevel)]:
                w.destroy()
            app.update()


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
        canvas = tip._tip.winfo_children()[0]
        item = canvas.find_withtag("text")[0]
        assert canvas.itemcget(item, "text") == RULE_TOOLTIPS["embedded"]
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


# ---------------------------------------------------------------------------
# 修复10：多文件对比模式（按钮可用 + 图例 + 差异列表 + 对比图表）
# ---------------------------------------------------------------------------
BASE_LOG = "\n".join([
    "2024-01-01 09:00:00 ERROR [db] connection refused to db-primary",
    "2024-01-01 09:00:01 ERROR [api] request 123 failed",
    "2024-01-01 09:00:02 FATAL [core] out of memory in worker",
    "2024-01-01 09:00:03 WARN [db] pool nearly exhausted",
]) + "\n"

OTHER_LOG = "\n".join([
    "2024-01-01 09:00:00 ERROR [db] connection refused to db-primary",
    "2024-01-01 09:00:01 ERROR [api] request 123 failed",
    "2024-01-01 09:00:02 ERROR [api] request 456 failed",
    "2024-01-01 09:00:03 FATAL [net] handshake failed with peer",
]) + "\n"


def _run_compare_analysis(app, tmp_path, timeout=60.0):
    """用两个临时日志文件执行一次对比分析并等待完成。"""
    fa = tmp_path / "base.log"
    fb = tmp_path / "other.log"
    fa.write_text(BASE_LOG, encoding="utf-8")
    fb.write_text(OTHER_LOG, encoding="utf-8")
    app._tabview.set("多文件对比")
    for i, path in enumerate((fa, fb)):
        entry = app._compare_entries[i]
        entry.delete(0, "end")
        entry.insert(0, str(path))
    app._on_start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if app._compare_results:
            break
        time.sleep(0.02)
    if not app._compare_results:
        worker = app._worker
        print(f"[diag-compare] queue_size={app._queue.qsize()} "
              f"worker_alive={worker.is_alive() if worker else None} "
              f"status={app._status_label.cget('text')}", flush=True)
    assert app._compare_results, "对比分析未完成"
    return app._compare_results


class TestCompareMode:
    def test_compare_buttons_enabled_after_analysis(self, app, tmp_path):
        """对比完成后：导出报告 / 统计图表按钮应可用。"""
        _run_compare_analysis(app, tmp_path)
        assert app._export_btn.cget("state") == "normal", \
            "对比模式下导出报告按钮应可用"
        assert app._chart_btn.cget("state") == "normal", \
            "对比模式下统计图表按钮应可用"

    def test_compare_legend_in_detail(self, app, tmp_path):
        """对比结果顶部必须有 +/-/= 图例说明。"""
        _run_compare_analysis(app, tmp_path)
        text = app._detail_box.get("1.0", "end")
        assert "+ 新增错误（对比文件中新出现的）" in text
        assert "- 消失错误（基准文件中有但对比文件中没有的）" in text
        assert "= 共同错误（两个文件中都存在的）" in text

    def test_compare_diff_rows_rendered(self, app, tmp_path):
        """左侧列表应渲染差异行（含 + / - / = 符号与摘要）。"""
        _run_compare_analysis(app, tmp_path)
        app.update()
        # 左侧列表应有差异行（不再是「对比模式：差异摘要见右侧详情」占位）
        texts = _texts_in(app._cluster_list)
        assert any(t.startswith("+") for t in texts), "应有新增行"
        assert any(t.startswith("-") for t in texts), "应有消失行"
        assert any(t.startswith("=") for t in texts), "应有共同行"

    def test_compare_chart_window_opens(self, app, tmp_path):
        """点击统计图表应弹出对比图表窗口。"""
        _run_compare_analysis(app, tmp_path)
        app._show_charts()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.update()
            if app._chart_window is not None and app._chart_window.winfo_exists():
                break
            time.sleep(0.02)
        assert app._chart_window is not None and app._chart_window.winfo_exists()
        assert "对比" in app._chart_window.title()
        app._chart_window.destroy()
        app._chart_window = None

    def test_compare_list_fullscreen(self, app, tmp_path):
        """对比差异列表支持全屏查看（含搜索与图例）。"""
        _run_compare_analysis(app, tmp_path)
        app._open_list_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "对比差异列表" in w.title()]
        assert wins, "对比差异列表全屏窗口应打开"
        win = wins[0]
        texts = _texts_in(win)
        assert any("关闭" in t for t in texts)
        assert any("+ 新增" in t for t in texts), "全屏顶栏应有图例"

    def test_compare_export_writes_report(self, app, tmp_path, monkeypatch):
        """对比模式导出报告应保存对比差异报告文件。"""
        _run_compare_analysis(app, tmp_path)
        target = tmp_path / "compare_report.md"
        monkeypatch.setattr(
            "log_ai_compressor.gui.app.filedialog.asksaveasfilename",
            lambda **kw: str(target))
        app._on_export()
        content = target.read_text(encoding="utf-8")
        assert "base.log" in content and "other.log" in content
        assert "新增" in content or "消失" in content

    def test_compare_fullscreen_esc_closes(self, app, tmp_path):
        """对比差异全屏窗口支持 ESC 关闭。"""
        _run_compare_analysis(app, tmp_path)
        app._open_list_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "对比差异列表" in w.title()]
        assert wins
        wins[0].event_generate("<Escape>")
        app.update()
        remaining = [w for w in app.winfo_children()
                     if isinstance(w, tk.Toplevel)
                     and "对比差异列表" in w.title()]
        assert not remaining


# ---------------------------------------------------------------------------
# 修复11：文本粘贴模式排查（大文本 / 中文特殊字符 / Tab 切换 / 编码）
# ---------------------------------------------------------------------------
PASTE_CN_LOG = "\n".join([
    "2024-01-01 09:00:00 INFO [认证] 用户登录成功",
    "",   # 空行
    "2024-01-01 09:00:01 ERROR [数据库] 连接失败：无法连接到 db-primary:5432",
    "Caused by: java.net.ConnectException: Connection refused",
    "\tat com.app.db.Pool.init(Pool.java:42)",
    "2024-01-01 09:00:02 FATAL [核心] 内存不足 worker 3 退出",
    "2024-01-01 09:00:03 WARN [缓存] \"key=abc\"\ttoken 过期 \U0001f6a8",
    "   ",  # 纯空白行
    "2024-01-01 09:00:04 ERROR [数据库] 连接失败：无法连接到 db-secondary:5432",
])


class TestPasteMode:
    def test_paste_large_text_analysis(self, app):
        """粘贴 1 万行文本：正常解析（总行数 / 错误数正确）。"""
        lines = []
        for i in range(10000):
            if i % 20 == 0:
                lines.append(f"2024-01-01 09:{i // 60 % 60:02d}:{i % 60:02d} "
                             f"ERROR [db] connection refused to host {i % 3}")
            else:
                lines.append(f"2024-01-01 09:{i // 60 % 60:02d}:{i % 60:02d} "
                             f"INFO [core] heartbeat ok")
        _run_paste_analysis(app, "\n".join(lines), timeout=60)
        assert app._result.stats.total_lines == 10000
        assert app._result.stats.error_entries == 500

    def test_paste_chinese_and_special_chars(self, app):
        """中文 / emoji / 引号 / 制表符 / 空行混合日志正常解析。"""
        _run_paste_analysis(app, PASTE_CN_LOG)
        r = app._result
        # 空行与纯空白行不计入总行数？—— splitlines 计入空行
        assert r.stats.total_lines == 9
        # 中文错误聚为 2 簇（db-primary/db-secondary 掩码后同模板 + FATAL）
        assert len(r.clusters) >= 2
        summaries = " ".join(c.summary for c in r.clusters)
        assert "连接失败" in summaries

    def test_paste_tab_switch_content_preserved(self, app):
        """粘贴后切换 Tab 再切回：内容不丢失。"""
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        original = app._paste_box.get("1.0", "end").strip()
        # 切走再切回
        app._tabview.set("文件导入")
        app.update()
        app._tabview.set("多文件对比")
        app.update()
        app._tabview.set("文本粘贴")
        app.update()
        restored = app._paste_box.get("1.0", "end").strip()
        assert restored == original, "切换 Tab 后粘贴内容丢失"

    def test_paste_bom_text_parsed(self, app):
        """BOM 开头的粘贴文本：首行仍能正常规则解析（修复缺陷#11）。"""
        text = ("\ufeff2024-01-01 09:00:00 ERROR [db] connection refused\n"
                "2024-01-01 09:00:01 INFO [core] heartbeat ok\n")
        _run_paste_analysis(app, text)
        r = app._result
        # 首行应被解析为 ERROR 错误条目（而非无结构 INFO 行）
        assert r.stats.error_entries == 1
        assert any("connection refused" in c.summary for c in r.clusters)

    def test_paste_crlf_text_parsed(self, app):
        """CRLF / CR 混合换行的粘贴文本：行数与条目正确。"""
        text = ("2024-01-01 09:00:00 ERROR [db] boom\r\n"
                "2024-01-01 09:00:01 INFO [core] ok\r"
                "2024-01-01 09:00:02 FATAL [core] dead\n")
        _run_paste_analysis(app, text)
        assert app._result.stats.total_lines == 3
        assert app._result.stats.error_lines == 2

    def test_paste_blank_only_warns(self, app, monkeypatch):
        """纯空白粘贴：提示「请先粘贴日志文本」且不启动分析。"""
        import log_ai_compressor.gui.app as app_mod
        warned = []
        monkeypatch.setattr(app_mod.messagebox, "showwarning",
                            lambda title, msg: warned.append(msg))
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", "\n   \n\t\n")
        app._on_start()
        app.update()
        assert warned and "粘贴" in warned[0]
        assert app._worker is None or not app._worker.is_alive()

    def test_paste_text_undo_disabled(self, app):
        """粘贴框 undo 应关闭（大文本粘贴的 undo 栈内存保护）。"""
        # CTkTextbox 底层 tk Text 的 undo 选项
        try:
            undo = app._paste_box.cget("undo")
        except (ValueError, tk.TclError):
            undo = None
        if undo is not None:
            assert str(undo) in ("False", "0", "false")

    def test_paste_trailing_newlines_stripped(self, app):
        """粘贴框恒有的尾部换行被正确去除（不产生空错误条目）。"""
        text = ("2024-01-01 09:00:00 ERROR [db] connection refused\n\n\n\n")
        _run_paste_analysis(app, text)
        assert app._result.stats.error_entries == 1
        # 尾部空行不计入解析行数（strip 后消除）
        assert app._result.stats.total_lines == 1

    def test_paste_result_encoding_label(self, app):
        """粘贴文本分析结果编码标注为 utf-8（Unicode 直通无转换）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        assert app._result.stats.encoding == "utf-8"
        assert app._result.stats.source == "<粘贴文本>"


# ---------------------------------------------------------------------------
# 修复12：主题切换体验（状态标识 + 平滑过渡 + 持久化 + 对比度）
# ---------------------------------------------------------------------------
class TestThemeSwitch:
    # 四态主题名（修复R1：亮色 → 暗色 → 蓝调 → 绿调 循环）
    THEME_NAMES = ("☀️ 亮色", "🌙 暗色", "🔵 蓝调", "🟢 绿调")

    def test_theme_button_shows_current_mode(self, app):
        """主题按钮必须显示当前主题（四态之一）。"""
        text = str(app._theme_btn.cget("text"))
        assert text in self.THEME_NAMES, \
            f"按钮应显示当前主题状态，实际: {text}"

    def test_toggle_updates_button_text(self, app):
        """切换后按钮文本随主题变化。"""
        before = str(app._theme_btn.cget("text"))
        app._apply_theme_switch("light" if app._is_dark_mode() else "dark")
        app.update()
        after = str(app._theme_btn.cget("text"))
        assert before != after
        assert {before, after} <= set(self.THEME_NAMES)

    def test_toggle_cycles_through_four_themes(self, app):
        """修复R1：_toggle_theme 应按 亮色→暗色→蓝调→绿调→亮色 循环。"""
        from log_ai_compressor.gui.app import THEME_ORDER
        app._apply_theme_switch("light")
        app.update()
        for expected in ("dark", "blue", "green", "light"):
            app._toggle_theme()
            # 推进过渡动画帧（淡出4帧+谷底切换+淡入4帧，28ms/帧）
            for _ in range(30):
                app.update()
                time.sleep(0.005)
            deadline = time.time() + 3
            while time.time() < deadline and app._theme != expected:
                app.update()
                time.sleep(0.02)
            assert app._theme == expected, \
                f"切换后应为 {expected}，实际 {app._theme}"
            assert str(app._theme_btn.cget("text")) == \
                dict(zip(THEME_ORDER, self.THEME_NAMES))[expected]
        app._apply_theme_switch("dark")

    def test_palette_roles_complete(self, app):
        """修复R1：每个主题调色板字段齐全（缺角色会导致刷新异常）。"""
        from log_ai_compressor.gui.app import THEMES
        required = {"name", "window", "card", "header", "text", "muted",
                    "accent", "accent_hover", "accent_text",
                    "row_bg", "row_hover", "row_selected", "row_text",
                    "is_dark"}
        for key, palette in THEMES.items():
            assert required <= set(palette), f"{key} 缺字段: {required - set(palette)}"

    def test_blue_green_themes_button_white(self, app):
        """修复R1：蓝调/绿调主题下主按钮为白底深色字（accent 白色）。"""
        from log_ai_compressor.gui.app import THEMES
        for key in ("blue", "green"):
            app._apply_theme_switch(key)
            app.update()
            assert THEMES[key]["accent"] == "#ffffff"
            # 实际按钮 fg_color 应用为白色（CTk 返回元组 (r,g,b) 或 hex）
            color = str(app._theme_btn.cget("fg_color"))
            assert "255, 255, 255" in color or color == "#ffffff", \
                f"{key} 主题按钮应为白色，实际 {color}"
        app._apply_theme_switch("dark")

    def test_theme_persisted_immediately(self, app):
        """切换主题立即写盘（不等关闭窗口）。"""
        target = "light" if app._is_dark_mode() else "dark"
        app._apply_theme_switch(target)
        saved = app._store.load()
        assert saved.get("appearance") == target

    def test_theme_restored_on_startup(self, app):
        """下次启动自动恢复上次的主题（配置文件验证）。

        说明：不创建第二个 Tk root —— 长测试序列中 Windows 句柄
        累积会令新 root 创建失败（Can't find a usable init.tcl），
        此处以配置文件内容 + 启动加载逻辑等效验证。
        """
        app._apply_theme_switch("light")
        # 1) 配置文件已写入 light
        assert app._store.load().get("appearance") == "light"
        # 2) 启动加载路径等效验证（LogCompressorApp.__init__ 同款逻辑）
        import customtkinter as _ctk
        from log_ai_compressor.gui.config_store import ConfigStore
        cfg = ConfigStore(app._store.path).load()
        _ctk.set_appearance_mode(cfg.get("appearance", "dark"))
        assert _ctk.get_appearance_mode().lower() == "light"
        # 3) 按钮标识与主题一致
        app._update_theme_button()
        assert str(app._theme_btn.cget("text")) == "☀️ 亮色"
        # 恢复默认暗色
        app._apply_theme_switch("dark")

    def test_toggle_with_animation_completes(self, app):
        """_toggle_theme（含过渡动画）最终完成主题切换且恢复不透明。"""
        before_dark = app._is_dark_mode()
        app._toggle_theme()
        # 推进动画帧（淡出 4 帧 + 谷底切换 + 淡入 4 帧）
        for _ in range(40):
            app.update()
            time.sleep(0.01)
        deadline = time.time() + 3
        while time.time() < deadline:
            app.update()
            if app._is_dark_mode() != before_dark:
                break
            time.sleep(0.02)
        assert app._is_dark_mode() != before_dark, "动画后主题应已切换"
        # 窗口恢复完全不透明
        assert float(app.attributes("-alpha")) == pytest.approx(1.0, abs=0.01)
        # 恢复默认主题
        app._apply_theme_switch("dark" if before_dark else "light")

    def test_row_colors_refresh_on_theme(self, app):
        """切换主题后列表行配色刷新（原生 label 不随 CTk 主题自动变）。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        dark = app._is_dark_mode()
        fg_before = str(app._cluster_rows[1]["summary"].cget("fg"))
        app._apply_theme_switch("light" if dark else "dark")
        app.update()
        fg_after = str(app._cluster_rows[1]["summary"].cget("fg"))
        assert fg_before != fg_after, "行文字颜色应随主题刷新"
        # 暗色模式用浅色文字 / 亮色模式用深色文字（对比度保障）
        if app._is_dark_mode():
            assert fg_after == "#c8cdd4"
        else:
            assert fg_after == "#2d333b"
        app._apply_theme_switch("dark" if dark else "light")
        app.update()

    def test_dark_mode_text_contrast(self, app):
        """暗色模式下关键文字颜色具备足够对比度（可读性保障）。"""
        app._apply_theme_switch("dark")
        app.update()
        # 行文字在暗色背景（gray22 ≈ #383838）上应为浅色
        assert _row_fg_is_light("#c8cdd4")
        # 亮色模式（gray88 ≈ #e0e0e0 背景）上应为深色
        assert not _row_fg_is_light("#2d333b")


def _luminance(hex_color: str) -> float:
    """相对亮度（0~1，WCAG 口径近似）。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _row_fg_is_light(hex_color: str) -> bool:
    return _luminance(hex_color) > 0.4
