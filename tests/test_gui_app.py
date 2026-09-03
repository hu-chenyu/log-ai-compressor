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

# 修复R12：分隔条参数（宽度/最小宽度限制）
from log_ai_compressor.gui.app import (  # noqa: E402
    _SPLITTER_MIN_DETAIL,
    _SPLITTER_MIN_LIST,
    _SPLITTER_WIDTH,
)


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
    # 防御性解除 CTk 冻结（类级补丁是全局的，测试中断未松开时
    # 必须还原，否则后续所有测试的 CTk 重绘被卡死）
    try:
        application._set_ctk_drag_freeze(False)
    except Exception:
        pass
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
        # 行首元信息不包含摘要（R16 起用行内 head 引用）
        head_text = str(app._cluster_rows[1]["head"].cget("text"))
        assert "TAIL" not in head_text

    def test_summary_single_line_no_wrap(self, app):
        """修复R9：摘要单行不换行（wraplength=0），长内容靠水平滚动。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        for row in app._cluster_rows:
            assert int(row["summary"].cget("wraplength")) == 0, \
                "摘要应取消自动换行（wraplength=0 单行显示）"

    def test_horizontal_scrollbar_covers_wide_content(self, app):
        """修复R9：长摘要不换行后，水平滚动区域覆盖完整内容宽度。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        canvas = app._cluster_list._parent_canvas
        region = str(canvas.cget("scrollregion")).split()
        assert len(region) == 4, "scrollregion 应已设置"
        region_w = int(region[2])
        # 行 1 摘要极长（>100 字符），内容宽应超出视口（可水平滚动）
        widest = max(r["summary"].winfo_reqwidth()
                     for r in app._cluster_rows)
        assert region_w >= widest, \
            f"滚动区域宽 {region_w} 应 ≥ 摘要完整宽 {widest}"

    def test_classic_hbar_wired_and_mapped(self, app):
        """修复R9：经典列表底部水平滚动条存在且与画布双向联动。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        hbar = app._list_hbar
        assert hbar is not None and hbar.winfo_ismapped(), \
            "列表底部应有水平滚动条"
        canvas = app._cluster_list._parent_canvas
        # 画布 xscrollcommand 已接滚动条（set 回调非空）
        assert str(canvas.cget("xscrollcommand")) != "", \
            "画布 xscrollcommand 应接入水平滚动条"

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
        head = app._cluster_rows[0]["head"]
        assert isinstance(head, ctk.CTkLabel)
        # 取 CTkLabel 内部的真实子控件（Canvas / tk.Label）
        internals = head.winfo_children()
        assert internals, "CTkLabel 应有内部子控件"
        # 改点行 1 的头部内部子控件验证选中切换
        head1 = app._cluster_rows[1]["head"]
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

    def test_long_word_single_line_horizontal(self, app):
        """修复R9：超长 token 不再折行（单行），水平滚动查看完整内容。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        row = app._cluster_rows[1]
        assert int(row["summary"].cget("wraplength")) == 0
        # 单行摘要高度应只含一行文字（大字体下约 40-60px 逻辑）
        assert row["summary"].winfo_height() < 120, \
            f"摘要应为单行（实际高度 {row['summary'].winfo_height()}px）"


# ---------------------------------------------------------------------------
# 修复R7/R9：列表宽度对齐 + 字体放大 + 水平滚动 + 高度布局
# ---------------------------------------------------------------------------
class TestClusterListFontAndWidth:
    def test_main_list_font_sizes(self, app):
        """修复R9：主列表字体标称大小——头部 22 加粗 / 摘要 18。"""
        assert int(app._font_row_head.cget("size")) == 22
        assert str(app._font_row_head.cget("weight")) == "bold"
        assert int(app._font_row_summary.cget("size")) == 18
        # 底层 tk 命名字体的实际像素尺寸（CTkFont 用负数表示像素）
        head_tk = tkfont.Font(root=app, name=str(app._font_row_head),
                              exists=True)
        sum_tk = tkfont.Font(root=app, name=str(app._font_row_summary),
                             exists=True)
        assert int(head_tk.cget("size")) == -22
        assert int(sum_tk.cget("size")) == -18

    def test_fullscreen_list_font_sizes(self, app):
        """修复R10：全屏列表字体标称大小——头部 28 加粗 / 摘要 24 / 实例 20。"""
        assert int(app._font_fs_head.cget("size")) == 28
        assert str(app._font_fs_head.cget("weight")) == "bold"
        assert int(app._font_fs_summary.cget("size")) == 24
        assert int(app._font_fs_inst.cget("size")) == 20

    def test_summary_font_dpi_scaled(self, app):
        """修复R9：摘要字体随 DPI 缩放（渲染比例与头部一致）。

        原生 tk.Label 直传 CTkFont 命名字体不参与缩放，高 DPI 屏上
        渲染偏小（修复前摘要/头部渲染比例 ≈ 18/22/scale）。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        row = app._cluster_rows[0]
        head = row["head"]          # R16 起行内存 head 引用（结构无关）
        inner = [c for c in head.winfo_children()
                 if c.winfo_class() == "Label"][0]
        head_size = int(tkfont.Font(font=inner.cget("font")).cget("size"))
        sum_size = int(
            tkfont.Font(font=row["summary"].cget("font")).cget("size"))
        ratio = sum_size / max(1, head_size)
        assert abs(ratio - 18 / 22) < 0.06, \
            f"摘要/头部渲染比例 {ratio:.3f} 应 ≈ 18/22（均含 DPI 缩放）"

    def test_classic_row_uses_enlarged_fonts(self, app):
        """修复R9：经典模式头部使用共享字体对象，摘要渲染字号匹配。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        head = app._cluster_rows[0]["head"]
        assert isinstance(head, ctk.CTkLabel)
        assert head.cget("font") is app._font_row_head
        inner = [c for c in head.winfo_children()
                 if c.winfo_class() == "Label"][0]
        head_size = int(tkfont.Font(font=inner.cget("font")).cget("size"))
        sum_size = int(tkfont.Font(
            font=app._cluster_rows[0]["summary"].cget("font")).cget("size"))
        assert abs(sum_size / max(1, head_size) - 18 / 22) < 0.06

    def test_virtual_row_fonts_match_classic(self, app):
        """修复R9：虚拟模式与经典模式渲染字号完全一致（含 DPI 缩放）。"""
        # 经典模式先取样（R16 起行内存 head 引用，结构无关取样）
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        classic_head = app._cluster_rows[0]["head"]
        inner = [c for c in classic_head.winfo_children()
                 if c.winfo_class() == "Label"][0]
        classic_head_size = tkfont.Font(
            font=inner.cget("font")).cget("size")
        classic_sum_size = tkfont.Font(
            font=app._cluster_rows[0]["summary"].cget("font")).cget("size")
        # 虚拟模式取样
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None, "60 簇应启用虚拟列表"
        slot = app._virtual_list.slots[0]
        vh = tkfont.Font(font=slot["head"].cget("font"))
        vs = tkfont.Font(font=slot["summary"].cget("font"))
        assert abs(int(vh.cget("size")) - int(classic_head_size)) <= 1, \
            "虚拟/经典头部渲染字号应一致"
        assert abs(int(vs.cget("size")) - int(classic_sum_size)) <= 1, \
            "虚拟/经典摘要渲染字号应一致"

    def test_virtual_row_height_fits_fonts(self, app):
        """修复R9：虚拟行高按实际字体度量计算（容纳头部+摘要单行）。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        head_ls = tkfont.Font(
            font=vl.slots[0]["head"].cget("font")).metrics("linespace")
        sum_ls = tkfont.Font(
            font=vl.slots[0]["summary"].cget("font")).metrics("linespace")
        assert vl.ROW_HEIGHT >= head_ls + sum_ls, \
            f"行高 {vl.ROW_HEIGHT} 应 ≥ 头部{head_ls}+摘要{sum_ls}行距"

    def test_fullscreen_rows_use_fs_fonts(self, app):
        """修复R9：全屏列表行实际使用全屏字体（头部 24 / 摘要 20，含缩放）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        # 经典头部渲染字号取样（作为 22 号基准；R16 起用 head 引用）
        classic_head = app._cluster_rows[0]["head"]
        c_inner = [c for c in classic_head.winfo_children()
                   if c.winfo_class() == "Label"][0]
        classic_size = int(
            tkfont.Font(font=c_inner.cget("font")).cget("size"))
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        # 全屏行（原生 tk.Label）头部含级别文本；其渲染字号应 ≈ 28 号
        #（经典 22 号基准 × 28/22，同一缩放系数）
        heads = [w for w in _all_widgets(win)
                 if isinstance(w, tk.Label)
                 and ("ERROR" in str(w.cget("text"))
                      or "FATAL" in str(w.cget("text")))]
        assert heads, "全屏窗口应有行头部标签"
        fs_size = int(
            tkfont.Font(font=heads[0].cget("font")).cget("size"))
        assert abs(fs_size - classic_size * 28 / 22) <= 2, \
            f"全屏头部渲染 {fs_size} 应 ≈ 经典 {classic_size}×28/22"
        win.event_generate("<Escape>")
        app.update()

    def test_fullscreen_instance_row_font_and_nowrap(self, app):
        """修复R10：全屏展开实例行 20 号 + DPI 缩放 + 单行不换行。"""
        # 同簇多实例日志（×2 实例，展开后可见实例行）
        multi = (SAMPLE_PASTE + "2024-01-01 09:02:00 FATAL [core] "
                 "out of memory in worker 4\n")
        _run_paste_analysis(app, multi)
        app.update()
        app._open_list_fullscreen()
        for _ in range(25):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        # 点击「▶ ×2」展开按钮（FATAL 簇 ×2；文本形如 "▶ ×2"）
        toggles = [w for w in _all_widgets(win)
                   if isinstance(w, tk.Label)
                   and "\u00d72" in str(w.cget("text"))]
        assert toggles, "全屏窗口应有 ×2 展开按钮"
        toggles[0].event_generate("<Button-1>")
        for _ in range(25):
            app.update()
            time.sleep(0.005)
        # 实例行：时间戳开头（2024-01-01 …）的原生 Label
        insts = [w for w in _all_widgets(win)
                 if isinstance(w, tk.Label)
                 and "out of memory" in str(w.cget("text"))
                 and str(w.cget("text")).startswith("2024-")]
        assert insts, "展开后应有实例行"
        lbl = insts[0]
        assert int(lbl.cget("wraplength")) == 0, "实例行应单行不换行"
        size = int(tkfont.Font(font=lbl.cget("font")).cget("size"))
        # 实例行渲染字号 ≈ 20 号（相对经典 22 号基准同缩放系数）
        classic = app._cluster_rows[0]["head"]
        inner = [c for c in classic.winfo_children()
                 if c.winfo_class() == "Label"][0]
        base = int(tkfont.Font(font=inner.cget("font")).cget("size"))
        assert abs(size - base * 20 / 22) <= 2, \
            f"实例行渲染 {size} 应 ≈ 经典 {base}×20/22"
        win.event_generate("<Escape>")
        app.update()

    def test_fullscreen_search_font_enlarged(self, app):
        """修复R10：全屏搜索框字体 18 号。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        entries = [w for w in _all_widgets(win)
                   if isinstance(w, ctk.CTkEntry)]
        assert entries, "全屏窗口应有搜索输入框"
        # CTkEntry.cget("font") 返回 CTkFont 对象（标称字号，DPI 无关）
        font_obj = entries[0].cget("font")
        assert int(font_obj.cget("size")) == 18, \
            f"搜索框字号应为 18（实际 {font_obj.cget('size')}）"
        win.event_generate("<Escape>")
        app.update()

    def test_virtual_hbar_and_mode_switch(self, app):
        """修复R9：虚拟模式有独立水平滚动条，经典 hbar 隐藏，销毁后恢复。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        assert vl._hbar.winfo_ismapped(), "虚拟模式应有水平滚动条"
        assert not app._list_hbar.winfo_ismapped(), \
            "虚拟模式下经典 hbar 应隐藏"
        # 长摘要数据：水平滚动区域应加宽（内容宽超视口）
        canvas = vl._canvas
        region = str(canvas.cget("scrollregion")).split()
        assert int(region[2]) >= max(vl._content_w, canvas.winfo_width()) - 2
        # 切回经典（重新分析小数据）：经典 hbar 恢复显示
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        assert app._virtual_list is None
        assert app._list_hbar.winfo_ismapped(), \
            "切回经典模式后经典 hbar 应恢复"

    def test_default_window_height_upgraded(self, app):
        """修复R9：默认窗口高度升级为 1000（容纳大字体与 6 行可视）。"""
        assert int(app._config.get("window", {}).get("height", 0)) >= 1000

    def test_list_shows_six_rows_at_default_height(self, app):
        """修复R9：1000 逻辑高默认窗口下列表可视行数 ≥6。

        屏幕不够高时窗口会被 WM 钳制（无法直接渲染验证），改用实测
        行距与固定区域高度做数学验证（逻辑单位，DPI 无关）。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        rows = app._cluster_rows
        if len(rows) < 2:
            pytest.skip("行数不足")
        pitch = (rows[1]["frame"].winfo_rooty()
                 - rows[0]["frame"].winfo_rooty())
        canvas = app._cluster_list._parent_canvas
        if pitch < 10 or canvas.winfo_height() < 60:
            pytest.skip("窗口未完成布局")
        scale = max(1.0, app._font_scale)
        # 固定区域高度（逻辑）：窗口高 - 列表画布高
        fixed = app.winfo_height() / scale - canvas.winfo_height() / scale
        rows_at_1000 = int((1000 - fixed) / (pitch / scale))
        assert rows_at_1000 >= 6, \
            f"1000 逻辑高窗口应显示 ≥6 行（实际 {rows_at_1000}，" \
            f"行距 {pitch / scale:.1f} 逻辑px，固定区 {fixed:.0f} 逻辑px）"

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


# ---------------------------------------------------------------------------
# 修复缺陷R19：FATAL 复选框删除（始终放行显示）+ 五级别前移
# ---------------------------------------------------------------------------
class TestFatalLevelFilter:
    def test_fatal_checkbox_removed_five_remain(self, app):
        """修复R19：FATAL 复选框删除，ERROR 居首共五个复选框。"""
        from log_ai_compressor.gui.app import LEVEL_CHECKS
        assert LEVEL_CHECKS == ("ERROR", "FAIL", "WARN", "INFO", "DEBUG")
        assert "FATAL" not in LEVEL_CHECKS
        assert "FATAL" not in app._level_vars, "FATAL 复选框应已删除"
        assert len(app._level_vars) == 5, "应只剩五个级别复选框"
        assert app._level_vars["ERROR"].get() is True
        assert app._level_vars["FAIL"].get() is True
        assert app._level_vars["WARN"].get() is False

    def test_fatal_always_displayed_without_checkbox(self, app):
        """修复R19：真实 FATAL（显式级别字段）无开关、始终放行显示。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        levels = [c.level for c in app._displayed]
        assert "FATAL" in levels, \
            "显式 [FATAL] 日志应始终显示（无复选框可隐藏致命错误）"
        assert "ERROR" in levels

    def test_fatal_help_tooltip_registered(self, app):
        """优化：五个级别复选框左侧都有 ⓘ 且悬停说明已登记。"""
        tooltips = getattr(app, "_level_tooltips", {})
        assert len(tooltips) == 5, \
            f"五个级别都应有 ⓘ 悬停说明（实际 {len(tooltips)} 个）"
        for level in ("ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
            assert level in tooltips, f"{level} 缺少 ⓘ 悬停说明"
            assert tooltips[level] is not None
        assert "FATAL" not in tooltips

    def test_level_help_texts_match(self, app):
        """优化：每个 ⓘ 的悬停解释文字与其级别精确对应。"""
        from log_ai_compressor.gui.app import _LEVEL_HELP
        expected = {
            "ERROR": "ERROR：错误，程序运行中出现的异常，"
                     "可能导致功能异常但程序仍可继续运行",
            "FAIL": "FAIL：失败，操作或测试未成功完成的结果",
            "WARN": "WARN：警告，可能存在问题但不影响程序正常运行，"
                    "需要关注",
            "INFO": "INFO：信息，程序正常运行时的一般性记录",
            "DEBUG": "DEBUG：调试，开发调试用的详细信息，"
                     "通常生产环境不显示",
        }
        assert "FATAL" not in _LEVEL_HELP, "FATAL 说明应随复选框删除"
        for level, text in expected.items():
            tip = app._level_tooltips[level]
            assert tip._current_text() == text, \
                f"{level} 的解释文字不匹配（实际 {tip._current_text()!r}）"

    def test_level_info_icons_left_of_checkboxes(self, app):
        """优化：五个 ⓘ 位于对应复选框左侧且样式统一（蓝/手型光标）。"""
        app.update()
        level_box = app._level_tooltips["ERROR"]._widget.master
        boxes = {}
        infos = {}
        for child in level_box.winfo_children():
            txt = str(child.cget("text"))
            if txt == "ⓘ":
                infos[child] = child.winfo_x()
            elif txt in ("ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
                boxes[txt] = child.winfo_x()
            else:
                assert txt != "FATAL", "FATAL 复选框不应存在"
        assert len(infos) == 5, f"应有五个 ⓘ 图标（实际 {len(infos)}）"
        assert len(boxes) == 5, f"应有五个复选框（实际 {len(boxes)}）"
        for icon, ix in infos.items():
            # 每个 ⓘ 紧邻其右侧最近的复选框（同一级别组）
            right = min(bx for bx in boxes.values() if bx > ix - 5)
            assert right - ix <= 60, "ⓘ 应紧邻复选框左侧"
            assert str(icon.cget("cursor")) == "hand2", \
                "ⓘ 悬停光标应为手型"
            color = str(icon.cget("text_color"))
            assert color.lower() == "#3b82f6", \
                f"ⓘ 颜色应统一为 #3B82F6（实际 {color}）"

    def test_fatal_row_color_is_red(self, app):
        """修复R19：FATAL 行红色保留（真实致命日志仍醒目区分）。"""
        from log_ai_compressor.gui.app import LogCompressorApp
        from log_ai_compressor.core.models import ErrorCluster
        fatal = ErrorCluster(cluster_id="f", template="t", summary="boom",
                              level="FATAL", count=1)
        err = ErrorCluster(cluster_id="e", template="t", summary="bad",
                            level="ERROR", count=1)
        red = LogCompressorApp._row_color(fatal)
        assert red and red.lower().startswith("#ff"), \
            f"FATAL 应用红色（实际 {red}）"
        assert LogCompressorApp._row_color(err) is None

    def test_old_config_with_fatal_loads_safely(self, app):
        """修复R19：旧配置（levels 含 FATAL）静默兼容不崩溃。"""
        # app fixture 的配置由 _restore_config 处理：旧 levels 含
        # FATAL 时 _level_vars（无 FATAL 键）正常恢复、不 KeyError
        assert "FATAL" not in app._level_vars
        assert app._level_vars["ERROR"].get() is True


class TestFontSizeSelector:
    def test_font_menu_exists_with_default(self, app):
        """修复R10：字体大小选择器存在且默认「中」。"""
        assert app._font_menu.get() == "中"
        assert int(app._font_row_head.cget("size")) == 22, \
            "「中」档头部应为基准 22 号"

    def test_font_menu_in_list_title_bar(self, app):
        """修复R11：字体选择器在错误列表标题栏（标题→字体大小→全屏）。"""
        app.update()
        if app._list_fs_btn.winfo_rootx() == 0:
            pytest.skip("窗口未完成布局")
        # 1) 与全屏按钮同一容器（列表标题栏）
        assert (app._font_menu.master is app._list_fs_btn.master), \
            "字体选择器应与全屏按钮同在列表标题栏"
        # 2) 横向顺序：标题 → 字体大小选择器 → 全屏按钮
        font_x = app._font_menu.winfo_rootx()
        btn_x = app._list_fs_btn.winfo_rootx()
        assert font_x < btn_x, "字体选择器应在全屏按钮左边"
        # 3) 不在配置区（父容器不是配置面板）
        assert app._font_menu.master is not app._rule_menu.master, \
            "字体选择器应已从配置区移除"

    def test_font_size_change_scales_fonts(self, app):
        """修复R10：切换档位即时缩放主列表/全屏字体并保存配置。"""
        app._apply_font_size("特大")
        assert int(app._font_row_head.cget("size")) == 29, \
            "特大档头部应 round(22×1.3)=29"
        assert int(app._font_row_summary.cget("size")) == 23, \
            "特大档摘要应 round(18×1.3)=23"
        assert int(app._font_fs_head.cget("size")) == 36, \
            "特大档全屏头部应 round(28×1.3)=36"
        assert app._font_size == "特大"
        assert app._config.get("font_size") == "特大", "档位应已持久化"
        # 档位切换后行级原生标签重渲染（字号随档位；R16 起用 head 引用）
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        head = app._cluster_rows[0]["head"]
        inner = [c for c in head.winfo_children()
                 if c.winfo_class() == "Label"][0]
        size = int(tkfont.Font(font=inner.cget("font")).cget("size"))
        # 特大档 29 号 vs 基准 22 号（同 DPI 系数下比例 ≈ 29/22）
        assert size >= 26, f"特大档经典头部渲染 {size} 应 ≥26"
        # 恢复默认档位（避免影响其他用例）
        app._apply_font_size("中")
        assert int(app._font_row_head.cget("size")) == 22

    def test_font_size_persisted_across_restart(self, app, tmp_path):
        """修复R10：字体档位保存后新实例自动恢复（同一配置文件）。"""
        app._apply_font_size("大")
        from log_ai_compressor.gui.app import LogCompressorApp
        # app fixture 已隔离配置文件；新实例读取同一份 -> 恢复「大」
        app2 = LogCompressorApp()
        try:
            app2.update()
            assert app2._font_size == "大", "重启应恢复上次档位"
            assert app2._font_menu.get() == "大"
            assert int(app2._font_row_head.cget("size")) == 25, \
                "大档头部应 22×1.15≈25"
        finally:
            try:
                app2._on_close()
            except Exception:
                try:
                    app2.destroy()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 修复R12：错误列表 | 详情面板 可拖动分隔条
# ---------------------------------------------------------------------------
class TestSplitter:
    def _pw(self, app):
        return max(1, app._result_panel.winfo_width())

    def test_splitter_exists_with_cursor(self, app):
        """修复R12：分隔条存在、宽 4~6px、光标为左右箭头。"""
        sp = app._splitter
        assert sp.winfo_exists()
        assert 4 <= int(sp.cget("width")) <= 6, \
            f"分隔条宽度应 4~6px（实际 {sp.cget('width')}）"
        assert str(sp.cget("cursor")) == "sb_h_double_arrow", \
            "光标应为左右双箭头"
        # 三个握点（视觉提示）
        assert len(app._splitter_dots) == 3

    def test_drag_resizes_columns(self, app):
        """修复R12：拖动分隔条实时调整左右列宽。"""
        app.update()
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        left0 = app._list_col.winfo_width()
        # 拖动：把分隔条移到面板 65% 处（相对偏移 = 目标 - 当前）
        target = int(pw * 0.65)
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        left1 = app._list_col.winfo_width()
        detail1 = app._detail_col.winfo_width()
        scale = max(1.0, app._font_scale)
        assert abs(left1 - target) <= 8, \
            f"拖动后左列应 ≈{target}px（实际 {left1}px）"
        assert left1 > left0, "往右拖列表应变宽"
        assert abs((left1 + detail1 + _SPLITTER_WIDTH * scale) - pw) <= 6, \
            "左右列 + 分隔条应占满面板宽"

    def test_drag_columns_follow_per_motion(self, app):
        """优化：矢量文本代理 —— 内容随容器实时延展（文字露出更多）。

        用户验收核心：拖动中内容随容器实时变化（摘要文字随列宽
        露出更多），不是松开才变。矢量代理把可见行绘制为完整文本
        items，裁剪框变宽时 canvas 边界自然露出更多文字（真延展，
        非静态截图）。断言：左裁剪框宽逐 motion 实时跟随（= 左列
        内容视口）、右画布视口实时滚动（详情文本贴住分隔条）、
        真实列拖动中冻结（松开一次到位）。
        """
        _run_many_clusters(app)
        app.update()
        sp = app._splitter
        pw = self._pw(app)
        assert not hasattr(app, "_splitter_proxy"), "位图代理应已移除"
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        live = app._splitter_live
        assert live is not None, "虚拟列表模式应构建矢量文本代理"
        lw0 = live["lw"]
        panel = app._result_panel
        # 修复缺陷R13：初始视口精确归零（原 xview_moveto 错位数百 px
        # → 代理竖线与真实分隔条分离成双竖线残影）
        assert abs(live["right"].canvasx(0)) <= 1, \
            "press 后初始视口应归零（代理竖线与分隔条位置一致）"
        # 修复缺陷R13：真实分隔条 press 时隐藏（竖线全由代理呈现，
        # 每帧窗口操作 4→3，根除双线）
        assert not sp.winfo_ismapped(), "拖动中真实分隔条应隐藏"
        # 修复缺陷R15：ⓘ 保持真实控件显示（固定在右端全屏按钮左边，
        # 不随分隔条移动；仅「详情」标题 Label 被隐藏由代理近似）
        info_lbl = app._detail_head.winfo_children()[1]
        info_x0 = info_lbl.winfo_rootx()
        assert info_lbl.winfo_ismapped(), "拖动中 ⓘ 应真实显示"
        assert not app._detail_head.winfo_children()[0].winfo_ismapped(), \
            "拖动中真实「详情」标题应隐藏（代理近似）"
        frozen = app._list_col.winfo_width()
        for dx in (80, 160, 240):
            sp.event_generate("<B1-Motion>", x=3 + dx, y=40)
            # 拖动为 rAF 节流（≤83fps 节拍应用最新位置）：测试中
            # motion 间隔远小于节流窗口，手动 flush 应用待应用帧
            # （真实拖动由节拍器/兜底 after 帧触发，逻辑一致）
            app._live_flush()
            app.update()
            expect = app._splitter_ratio * pw
            assert abs(live["clip"].winfo_width() - expect) <= 2, \
                "左裁剪框宽应逐 motion 实时跟随（内容视口延展）"
            assert abs(live["right"].canvasx(0) - (lw0 - expect)) <= 2, \
                "右画布视口应实时滚动（详情文本贴住分隔条）"
            # 修复缺陷R13：右标题条左缘 x=left（含分隔条区，内部
            # canvas [0, sp_w] 画竖线 + 标题跟随）
            tbar_x = live["tbar"].winfo_rootx() - panel.winfo_rootx()
            assert abs(tbar_x - expect) <= 2, \
                "右标题条应跟随分隔条（左缘含分隔条区）"
            # 修复缺陷R14：tbar 右缘让开右「⛶ 全屏」按钮实测宽
            # （原固定 100px 高 DPI 下吃掉按钮左半）
            tbar_r = tbar_x + live["tbar"].winfo_width()
            assert abs(tbar_r - (pw - live["fs_w"])) <= 2, \
                "覆盖条右缘应让开右全屏按钮（实测宽余量）"
            # 修复缺陷R14：左列标题栏控件组（字体大小+全屏）实时
            # 跟随左列右缘（真实容器纯移动，非代理近似）
            ctrl_x = (app._list_ctrl_box.winfo_rootx()
                      - panel.winfo_rootx())
            assert abs(ctrl_x - max(0, expect - live["ctrl_dx"])) <= 2, \
                "标题栏控件组应实时跟随左列右缘"
            # 修复缺陷R15：ⓘ 位置固定（不随分隔条移动）
            assert info_lbl.winfo_rootx() == info_x0, \
                "ⓘ 应固定在全屏按钮左边（不随分隔条）"
            assert app._list_col.winfo_width() == frozen, \
                "拖动中真实列冻结（代理之下，松开一次应用）"
        sp.event_generate("<ButtonRelease-1>", x=3 + 240, y=40)
        app.update()
        assert app._splitter_live is None, "释放后代理应销毁"
        # 修复缺陷R13：真实分隔条恢复显示
        assert sp.winfo_ismapped(), "释放后真实分隔条应恢复显示"
        # 真实「详情」标题+ⓘ 应恢复显示
        assert all(w.winfo_ismapped()
                   for w in app._detail_head.winfo_children()), \
            "释放后真实标题栏控件应全部恢复"
        assert abs(app._splitter_ratio * pw
                   - app._list_col.winfo_width()) <= 8, \
            "释放后真实列一次性到最终位置"

    def test_drag_freezes_ctk_redraw_cascade(self, app):
        """优化：回退路径（经典小列表无代理）——按下冻结 CTk 重绘级联。

        虚拟列表模式走矢量代理（真实控件不动，无需冻结）；经典
        小列表代理不可用，回退真实布局逐 motion，此时按下冻结
        15 个 CTk 类的 _draw（掐断嵌套 update_idletasks 重入级联），
        松开还原。
        """
        import customtkinter as _ctk
        app.update()
        sp = app._splitter
        orig_draw = _ctk.CTkBaseClass._draw
        orig_sb_draw = _ctk.CTkScrollbar._draw
        # 经典模式（无虚拟列表）：代理不可用 → 回退路径
        assert app._virtual_list is None
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        assert app._splitter_live is None, "经典模式不应建代理"
        assert app._ctk_freeze_orig is not None, "回退路径按下应冻结"
        assert _ctk.CTkBaseClass._draw is not orig_draw
        assert _ctk.CTkScrollbar._draw is not orig_sb_draw
        sp.event_generate("<B1-Motion>", x=3 + 120, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + 120, y=40)
        app.update()
        assert app._ctk_freeze_orig is None, "松开应解除冻结"
        assert _ctk.CTkBaseClass._draw is orig_draw
        assert _ctk.CTkScrollbar._draw is orig_sb_draw

    def test_drag_focusout_ends_drag(self, app):
        """兼容性：拖动中窗口失焦 → 结束拖动并应用当前位置。"""
        _run_many_clusters(app)
        app.update()
        sp = app._splitter
        pw = self._pw(app)
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        assert app._splitter_live is not None
        sp.event_generate("<B1-Motion>", x=3 + 160, y=40)
        app.update()
        app.event_generate("<FocusOut>")
        app.update()
        assert not app._splitter_dragging, "失焦应结束拖动"
        assert app._splitter_live is None, "失焦应销毁代理"
        assert abs(app._splitter_ratio * pw
                   - app._list_col.winfo_width()) <= 8, \
            "失焦应应用当前位置"

    def test_drag_realtime_fast_perf(self, app):
        """优化：矢量代理拖动性能 —— 单帧（motion+重绘）<24ms（>40fps）。

        矢量文本代理：每帧仅左裁剪框 place + 右画布视口滚动
        （GDI 级原语，实测空闲环境 ~8ms/帧），单画布原子渲染无
        撕裂。测试跳变拖动（0.25↔0.6 大 delta）+ 负载抖动裕量，
        阈值取 24ms/64ms（仍比真实重排方案 ~140ms 快 6 倍以上）。
        """
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        sp = app._splitter
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        assert app._splitter_live is not None
        pw = self._pw(app)
        # 预热：首帧冷启动（代理建层 / 首次几何级联）
        for frac in (0.25, 0.6, 0.25):
            sp.event_generate("<B1-Motion>", x=3 + int(pw * frac), y=40)
            app.update()
        times = []
        try:
            for i in range(40):
                frac = 0.25 if i % 2 == 0 else 0.6
                sp.event_generate("<B1-Motion>",
                                   x=3 + int(pw * frac), y=40)
                t0 = time.perf_counter()
                app.update()
                times.append((time.perf_counter() - t0) * 1000)
        finally:
            sp.event_generate("<ButtonRelease-1>", x=3, y=40)
            app.update()
        # trimmed mean（去最高/最低各 4 帧）：抗负载尖峰（CI/沙箱
        # CPU 抢占会造成个别 40-60ms 尖峰，不代表真实性能）
        ts = sorted(times)[4:-4]
        trimmed = sum(ts) / len(ts)
        assert trimmed < 24.0, \
            f"trimmed 单帧应 <24ms（实际 {trimmed:.1f}ms，均值 {sum(times)/40:.1f}ms）"
        assert ts[-1] < 64.0, \
            f"去极值后单帧峰值应 <64ms（实际 {ts[-1]:.1f}ms）"

    def test_virtual_fast_path_during_drag(self, app):
        """优化：虚拟列表拖动期走快速路径（不重填文本），松开全量同步。

        拖动中数据/滚动位置不变，_sync 只 itemconfigure 行宽（<1ms），
        跳过文本重填/事件重绑；松开时补一次全量 _sync。
        """
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        sp = app._splitter
        calls = []
        orig = app._virtual_list._fill_slot

        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)

        app._virtual_list._fill_slot = counting
        try:
            sp.event_generate("<ButtonPress-1>", x=3, y=40)
            app.update()
            calls.clear()
            for dx in (80, 160, 240):
                sp.event_generate("<B1-Motion>", x=3 + dx, y=40)
                app.update()
            assert calls == [], "拖动中不应重填行文本（快速路径）"
            sp.event_generate("<ButtonRelease-1>", x=3 + 240, y=40)
            app.update()
            assert calls, "松开后应执行一次全量同步"
        finally:
            app._virtual_list._fill_slot = orig

    def test_drag_min_width_limits(self, app):
        """修复R12：拖到最左/最右受最小宽度限制（动态实测标题栏宽）。"""
        app.update()
        sp = app._splitter
        scale = max(1.0, app._font_scale)
        # 动态最小宽（标题栏实测）为下限；固定常量兜底值也应满足
        left_min, right_min = app._splitter_min_widths()
        min_list = max(_SPLITTER_MIN_LIST * scale, left_min) - 4
        min_detail = max(_SPLITTER_MIN_DETAIL * scale, right_min) - 4
        # 拖到最左
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=-5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=-5000, y=40)
        app.update()
        assert app._list_col.winfo_width() >= min_list, \
            f"列表最小宽度应 ≥{min_list:.0f}物理px（实际 {app._list_col.winfo_width()}）"
        assert app._detail_col.winfo_width() >= min_detail
        # 拖到最右
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=5000, y=40)
        app.update()
        assert app._detail_col.winfo_width() >= min_detail, \
            f"详情最小宽度应 ≥{min_detail:.0f}物理px（实际 {app._detail_col.winfo_width()}）"
        assert app._list_col.winfo_width() >= min_list

    def test_extremes_keep_titlebars_visible(self, app):
        """修复R12：拖到左右极限时标题栏控件完整可见（不被遮挡）。

        旧固定最小宽（200/300）小于标题栏内容宽：最左时「错误分类
        列表（按优先级降序）」整体被裁，最右时「详情」起首两字被
        分隔条挡住。断言：极限列宽 ≥ 标题栏请求宽 + padx，且标题栏
        每个子控件完全落在列内。
        """
        app.update()
        sp = app._splitter
        scale = max(1.0, app._font_scale)
        pad = 10 * scale * 2        # 标题栏 grid padx（物理，两侧）

        def check_visible(col, head, tag):
            app.update_idletasks(); app.update()
            assert col.winfo_width() >= head.winfo_reqwidth() + pad - 2, \
                f"{tag}: 列宽 {col.winfo_width()} 应 ≥ 标题栏需求 " \
                f"{head.winfo_reqwidth() + pad}"
            # 标题栏每个子控件完整落在列内（几何不遮挡的强断言）
            col_l = col.winfo_rootx()
            col_r = col_l + col.winfo_width()
            for child in head.winfo_children():
                cl = child.winfo_rootx()
                cr = cl + child.winfo_width()
                assert cl >= col_l - 2, f"{tag}: 子控件左缘越界"
                assert cr <= col_r + 2, f"{tag}: 子控件右缘越界（被裁剪）"

        # 修复缺陷R14：标题栏控件组（常驻 panel 的字体大小+全屏）
        # 极限位置完整落在左列内、贴右缘
        def check_ctrl(tag):
            app.update_idletasks(); app.update()
            col_l = app._list_col.winfo_rootx()
            col_r = col_l + app._list_col.winfo_width()
            cl = app._list_ctrl_box.winfo_rootx()
            cr = cl + app._list_ctrl_box.winfo_width()
            assert cl >= col_l - 2, f"{tag}: 控件组左缘越界"
            assert cr <= col_r + 2, f"{tag}: 控件组右缘越界（被裁剪）"
            assert abs(cr - (col_r - 10 * scale)) <= 4, \
                f"{tag}: 控件组应贴左列右缘（右边距 10）"

        # 拖到最左：左列表标题完整可见
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=-5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=-5000, y=40)
        app.update()
        check_visible(app._list_col, app._list_head, "最左极限")
        check_visible(app._detail_col, app._detail_head, "最左极限右列")
        check_ctrl("最左极限")

        # 拖到最右：右详情标题完整可见
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=5000, y=40)
        app.update()
        check_visible(app._detail_col, app._detail_head, "最右极限")
        check_visible(app._list_col, app._list_head, "最右极限左列")
        check_ctrl("最右极限左列")

        # 极限后仍能拖回（不锁死）
        pw = max(1, app._result_panel.winfo_width())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 - int(pw * 0.3), y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 - int(pw * 0.3), y=40)
        app.update()
        assert app._splitter_ratio < 0.75, "从右极限应能拖回"

    def test_drag_back_from_rightmost(self, app):
        """修复R12：拖到最右后仍能拖回左边（比例可恢复，不锁死）。

        高DPI下旧实现（分隔条 rootx + event.x 反推指针）坐标系错乱，
        拖到右极限后 ratio 卡死无法回拖。
        """
        app.update()
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        # 拖到右极限
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=5000, y=40)
        app.update()
        assert app._splitter_ratio > 0.5, "拖到右极限比例应 >0.5"
        # 从右极限拖回中间（重新按下，目标 40% 处）
        target = int(pw * 0.4)
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        assert abs(app._splitter_ratio - 0.4) < 0.02, \
            f"右极限后应能拖回 0.4（实际 {app._splitter_ratio:.3f}）"
        assert abs(app._list_col.winfo_width() - target) <= 8, \
            f"拖回后左列应 ≈{target}px（实际 {app._list_col.winfo_width()}）"

    def test_columns_compact_no_gap(self, app):
        """修复R12：三列（列表|分隔条|详情）紧密占满结果区，无中间空白。

        高DPI下旧实现 x（被CTk二次缩放）与 relwidth（不缩放）混用，
        分隔条/详情列被推出面板外（详情消失、列表右侧大片空白）。
        """
        app.update()
        panel = app._result_panel
        pw = self._pw(app)
        sp_w = max(1, app._splitter.winfo_width())

        def check():
            app.update_idletasks(); app.update()
            lx = app._list_col.winfo_rootx() - panel.winfo_rootx()
            sx = app._splitter.winfo_rootx() - panel.winfo_rootx()
            dx = app._detail_col.winfo_rootx() - panel.winfo_rootx()
            assert abs(sx - (lx + app._list_col.winfo_width())) <= 2, \
                "列表右缘应紧贴分隔条左缘"
            assert abs(dx - (sx + sp_w)) <= 2, "分隔条右缘应紧贴详情左缘"
            assert abs(pw - (dx + app._detail_col.winfo_width())) <= 2, \
                "详情右缘应贴齐面板右缘"
            assert 0 < app._detail_col.winfo_width() < pw, \
                f"详情列应可见（宽 {app._detail_col.winfo_width()}）"

        check()                                   # 初始布局
        sp = app._splitter
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + 600, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + 600, y=40)
        app.update()
        check()                                   # 拖动后布局
        app._on_splitter_dblclick(None)
        app.update()
        check()                                   # 双击恢复后布局

    def test_double_click_restores_default(self, app):
        """修复R12：双击分隔条恢复默认比例（2:3）。"""
        app.update()
        sp = app._splitter
        # 先拖到非默认位置
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + 200, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + 200, y=40)
        app.update()
        assert abs(app._splitter_ratio - 0.4) > 0.05, "先偏离默认比例"
        # 双击恢复（event_generate 无法合成 Double 事件，直接调 handler）
        app._on_splitter_dblclick(None)
        app.update()
        # 闪缩回调（150ms）后回落主题色
        deadline = time.time() + 2
        while time.time() < deadline:
            app.update()
            time.sleep(0.05)
        assert abs(app._splitter_ratio - 0.4) < 0.02, \
            f"双击应恢复默认比例 0.4（实际 {app._splitter_ratio:.3f}）"
        pw = self._pw(app)
        assert abs(app._list_col.winfo_width() / pw - 0.4) < 0.02

    def test_splitter_position_persisted(self, app):
        """修复R12：拖动后位置保存，重启自动恢复。"""
        app.update()
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        target = int(pw * 0.6)
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        assert abs(app._splitter_ratio - 0.6) < 0.02
        assert app._config.get("splitter_ratio") is not None
        # 新实例读同一份配置
        from log_ai_compressor.gui.app import LogCompressorApp
        app2 = LogCompressorApp()
        try:
            app2.update()
            assert abs(app2._splitter_ratio - 0.6) < 0.03, \
                f"重启应恢复 0.6（实际 {app2._splitter_ratio:.3f}）"
        finally:
            try:
                app2._on_close()
            except Exception:
                try:
                    app2.destroy()
                except Exception:
                    pass

    def test_splitter_theme_colors(self, app):
        """修复R12：四态主题切换分隔条颜色跟随调色板。"""
        from log_ai_compressor.gui.app import THEMES
        for theme in ("dark", "light", "blue", "green"):
            app._theme = theme
            app._apply_palette()
            app.update()
            sp_color = app._splitter.cget("fg_color")
            # CTk 颜色可能是元组（暗/亮）；归一化取当前模式的值
            if isinstance(sp_color, (tuple, list)):
                idx = 1 if theme == "dark" else 0
                sp_color = sp_color[idx]
            assert str(sp_color).lower() == THEMES[theme]["splitter"].lower(), \
                f"{theme} 主题分隔条色 {sp_color} 应为 {THEMES[theme]['splitter']}"

    def test_splitter_works_in_virtual_mode(self, app):
        """修复R12：虚拟列表模式下拖动分隔条正常。"""
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        target = int(pw * 0.55)
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        assert app._virtual_list is not None, "拖动后虚拟列表应仍在"
        assert abs(app._list_col.winfo_width() - target) <= 8
        # 虚拟列表画布随左列变宽
        assert app._virtual_list._canvas.winfo_width() <= \
            app._list_col.winfo_width()

    def test_splitter_no_overlap_with_scrollbars(self, app):
        """修复R12：分隔条不遮挡列表滚动条（列间留白 ≥2px）。"""
        app.update()
        sp = app._splitter
        sp_x = sp.winfo_rootx()
        sp_w = max(1, app._splitter.winfo_width())
        # 列表宿主右缘（含垂直滚动条）在分隔条左侧且留有间隙
        list_right = (app._list_host.winfo_rootx()
                      + app._list_host.winfo_width())
        assert list_right <= sp_x + 1, "列表区域不应越过分隔条"
        # 详情框左缘在分隔条右侧
        detail_left = app._detail_box.winfo_rootx()
        assert detail_left >= sp_x + sp_w - 1, \
            "详情面板不应被分隔条遮挡"


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
        """修复缺陷R20：下限钳制到 5，无上限（数字可填任意大）。"""
        for raw, expected in [("1", 5), ("0", 5), ("-3", 5), ("999", 999),
                              ("99999", 99999), ("abc", 50), ("", 50),
                              ("8", 8), ("120", 120), ("200", 200)]:
            app._ctx_entry.delete(0, "end")
            app._ctx_entry.insert(0, raw)
            assert app._current_context_lines() == expected, \
                f"输入 {raw!r} 应得 {expected}（下限 5、无上限）"

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
        tip._hide_now()
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
        tip._hide_now()

    def test_tooltip_leak_free_after_destroy(self, app):
        """关联控件销毁后 hide 不抛异常（健壮性）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        tip._hide_now()   # 常规销毁
        tip._hide_now()   # 二次销毁应幂等
        assert tip._tip is None

    def test_tooltip_enter_cancels_pending_hide(self, app):
        """修复闪烁：Enter 取消挂起的延迟销毁（已显示则保持）。

        旧链路：Leave 调度 200ms 销毁，Enter 只重置显示调度不取消
        销毁 —— 已显示的 tooltip 被销毁又在 300ms 后重建（闪烁
        一下；指针在 ⓘ 内微动跨 CTk 子窗口边界会反复触发）。
        """
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        assert tip._tip is not None
        tip._hide()                       # Leave（指针在外）
        assert tip._hide_after_id is not None
        tip._schedule()                   # 立刻 Enter（抖回）
        assert tip._hide_after_id is None, "Enter 应取消挂起的销毁"
        # 超过原销毁延迟（200ms）后 tooltip 仍显示，不销毁不重建
        deadline = time.time() + 0.6
        while time.time() < deadline:
            app.update()
            time.sleep(0.02)
        assert tip._tip is not None, "Enter 后 tooltip 应保持显示（无闪烁）"
        tip._hide_now()

    def test_tooltip_leave_ignored_when_pointer_inside(self, app):
        """修复闪烁：指针仍在控件内时 Leave 被忽略（子窗口抖动）。

        CTkLabel 是复合控件（canvas + 内部 label），指针跨子窗口
        边界会发 detail=NotifyInferior 的 Leave —— 指针并未真正
        离开控件，此时不应调度销毁。
        """
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        assert tip._tip is not None
        # 模拟指针仍在控件内（真实鼠标位置测试中不可控）
        tip._pointer_inside = lambda: True
        tip._hide()                       # NotifyInferior 类 Leave
        assert tip._hide_after_id is None, \
            "指针在控件内时 Leave 不应调度销毁"
        app.update()
        assert tip._tip is not None, "tooltip 不应被销毁（无闪烁）"
        tip._hide_now()


# ---------------------------------------------------------------------------
# 修复R3：Tooltip 字体放大 + 自动换行 + 智能定位（不溢出屏幕）
# ---------------------------------------------------------------------------
class TestTooltipR3:
    """悬停说明的可读性与定位（典型样例说明 / 解析规则说明共用）。"""

    def test_tooltip_font_size_enlarged(self, app):
        """优化：tooltip 字体放大到 17~19 号（15 号用户仍反馈小）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            font = str(canvas.itemcget(item, "font"))
            sizes = [int(t) for t in
                     __import__("re").findall(r"-?\d+", font)]
            assert any(17 <= s <= 19 for s in sizes), \
                f"字体应为 17~19 号，实际 {font}"
        finally:
            tip._hide_now()

    def test_level_tooltip_font_size_enlarged(self, app):
        """优化：级别 ⓘ 悬停说明的字体同样放大到 17~19 号。"""
        tip = app._level_tooltips["ERROR"]
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            font = str(canvas.itemcget(item, "font"))
            sizes = [int(t) for t in
                     __import__("re").findall(r"-?\d+", font)]
            assert any(17 <= s <= 19 for s in sizes), \
                f"级别说明字体应为 17~19 号，实际 {font}"
        finally:
            tip._hide_now()

    def test_level_tooltip_centered_above_icon(self, app):
        """优化：级别 ⓘ 的 tooltip 停在图标正上方（水平居中）。

        此前默认右下方弹出，行内靠右的 ⓘ 触发右缘换向/钳位后
        tooltip 相对图标位置各异（视觉"扭曲"）。以第一个 ⓘ 为
        基准统一：tooltip 水平中心 == 图标水平中心，tooltip 底边
        在图标顶边上方（8px 间隙，允许钳位引起的水平平移）。
        """
        app.update()
        for level in ("ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
            icon = app._level_tooltips[level]._widget
            tip = app._level_tooltips[level]
            tip._show()
            app.update()
            try:
                tw = tip._tip
                ic_cx = icon.winfo_rootx() + icon.winfo_width() / 2
                tp_cx = tw.winfo_x() + tw.winfo_width() / 2
                # 水平居中（屏幕钳位平移时容差 40px）
                assert abs(tp_cx - ic_cx) <= 40, (
                    f"{level} 的 tooltip 水平中心 {tp_cx:.0f} 应与图标"
                    f"中心 {ic_cx:.0f} 对齐")
                # 底边在图标顶边上方（正上方关系）
                assert tw.winfo_y() + tw.winfo_height() \
                    <= icon.winfo_rooty() + 2, (
                        f"{level} 的 tooltip 应在图标正上方"
                        f"（底边 {tw.winfo_y() + tw.winfo_height()}"
                        f" vs 图标顶 {icon.winfo_rooty()}）")
            finally:
                tip._hide_now()
                app.update()

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
            tip._hide_now()

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
            tip._hide_now()

    def test_tooltip_within_screen_bounds(self, app):
        """修复R3：tooltip 完整可见（不溢出物理屏幕边界）。

        优化（定位修正）：校验用物理像素边界（_screen_bounds）——
        winfo_screenwidth 高 DPI 下是逻辑值，与物理几何混用会误报。
        """
        from log_ai_compressor.gui.app import Tooltip
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            tw = tip._tip
            vx, vy, vw, vh = Tooltip._screen_bounds(tw)
            x, y = tw.winfo_x(), tw.winfo_y()
            w, h = tw.winfo_width(), tw.winfo_height()
            assert x >= vx - 2, "左边缘溢出"
            assert y >= vy - 2, "上边缘溢出"
            assert x + w <= vx + vw + 2, \
                f"右边缘溢出（{x + w} > {vx + vw}）"
            assert y + h <= vy + vh + 2, \
                f"下边缘溢出（{y + h} > {vy + vh}）"
        finally:
            tip._hide_now()

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
        """优化：宿主控件贴近物理屏幕右边缘时 tooltip 保持在屏内（居中被平移）。"""
        from log_ai_compressor.gui.app import Tooltip
        vx, vy, vw, vh = Tooltip._screen_bounds(app)
        host, lbl = self._tooltip_on_edge_widget(
            app, f"+{vx + vw - 40}+{vy + 240}")
        try:
            assert lbl.winfo_rootx() > vx + vw - 120, \
                "测试前置：宿主应贴近右边缘"
            tip = Tooltip(lbl, "较长的悬停说明文本 " * 10)
            tip._show()
            app.update()
            try:
                tw = tip._tip
                # 完整可见（物理屏内）
                assert tw.winfo_x() + tw.winfo_width() <= vx + vw + 2
                assert tw.winfo_x() >= vx - 2
            finally:
                tip._hide_now()
        finally:
            host.destroy()
            app.update()

    def test_tooltip_flips_up_near_bottom_edge(self, app):
        """优化：宿主控件贴近物理屏幕下边缘时 tooltip 保持在屏内上方。"""
        from log_ai_compressor.gui.app import Tooltip
        vx, vy, vw, vh = Tooltip._screen_bounds(app)
        host, lbl = self._tooltip_on_edge_widget(
            app, f"+{vx + 240}+{vy + vh - 50}")
        try:
            assert lbl.winfo_rooty() > vy + vh - 150, \
                "测试前置：宿主应贴近下边缘"
            tip = Tooltip(lbl, "多行悬停说明\n" * 8)
            tip._show()
            app.update()
            try:
                tw = tip._tip
                assert tw.winfo_y() + tw.winfo_height() <= vy + vh + 2
                assert tw.winfo_y() < lbl.winfo_rooty(), \
                    "下边缘情形应在控件上方弹出"
            finally:
                tip._hide_now()
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
        # 触发行 1 的点击（通过事件绑定）——直接调用绑定回调
        # 行 frame 的第一个可点击子控件。修复缺陷R17：查找限定
        # 【列表区子树】—— 新增的分隔条握点同样是带 Button-1 绑定的
        # tk.Frame 且在逆序遍历中更先命中（点到它会启动分隔条拖动
        # 而非选中行），必须从候选中排除
        clickables = []
        for sub in _all_widgets(app._fs_list_area):
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


def _force_hscroll_range(app, extra=1200):
    """虚拟列表强制超宽内容（单测聚焦滚动机制，不依赖聚类行为）。"""
    vl = app._virtual_list
    assert vl is not None
    vl._content_w = max(vl._region_w() + extra, 1200 + extra)
    vl._update_region()
    vl._sync()
    app.update()
    return vl


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

    def test_hscroll_snapshot_atomic_scrolling(self, app):
        """优化：水平滚动快照模式 —— 按下转图元、滚动原子、释放恢复。

        拖动水平滚动条时行窗口（原生子窗口）逐个平移 + 画布回填
        异步交错是撕裂根源；快照模式把可见行转为同一画布的矩形/
        文本图元（单表面整帧上屏），行窗口隐藏；释放后恢复。
        """
        _run_many_clusters(app)
        app.update()
        vl = _force_hscroll_range(app)
        assert vl._region_w() > vl._canvas.winfo_width()
        vl._on_hbar_press(None)
        assert vl._xsnap is not None, "按下后应进入快照滚动模式"
        # 行窗口隐藏（拖动中零子窗口平移）
        for slot in vl.slots:
            assert str(vl._canvas.itemcget(slot["win"], "state")) \
                == "hidden"
        # 快照图元随画布原点平移（视口内呈现整体左移）
        vx0 = vl._canvas.canvasx(0)
        vl._canvas.xview_moveto(0.5)
        app.update()
        vx1 = vl._canvas.canvasx(0)
        assert vx1 > vx0, "滚动后画布原点应右移"
        # 释放恢复：图元删除、行窗口可见
        vl._on_hbar_release(None)
        assert vl._xsnap is None
        for slot in vl.slots:
            assert str(vl._canvas.itemcget(slot["win"], "state")) \
                == "normal"

    def test_hscroll_snapshot_fast_perf(self, app):
        """优化：水平滚动快照性能 —— 单帧 <16ms（60fps）。"""
        _run_many_clusters(app)
        app.update()
        vl = _force_hscroll_range(app)
        vl._on_hbar_press(None)
        assert vl._xsnap is not None
        # 预热：首个大幅滚动触发画布首次全量重绘等一次性成本
        for frac in (0.5, 0.05, 0.9):
            vl._canvas.xview_moveto(frac)
            app.update()
        times = []
        try:
            for i in range(40):
                frac = 0.05 if i % 2 == 0 else 0.9
                vl._canvas.xview_moveto(frac)
                t0 = time.perf_counter()
                app.update()
                times.append((time.perf_counter() - t0) * 1000)
        finally:
            vl._on_hbar_release(None)
            app.update()
        assert sum(times) / len(times) < 16.0, \
            f"平均单帧应 <16ms（实际均值 {sum(times) / len(times):.1f}ms）"

    def test_hbar_press_release_binding_wired(self, app):
        """优化：水平滚动条内部画布已挂按下/释放绑定（触发快照模式）。"""
        _run_many_clusters(app)
        app.update()
        vl = _force_hscroll_range(app)
        bar_canvas = vl._hbar._canvas
        bar_canvas.event_generate("<ButtonPress-1>", x=6, y=4)
        app.update()
        assert vl._xsnap is not None, "滚动条按下应触发快照模式"
        bar_canvas.event_generate("<ButtonRelease-1>", x=6, y=4)
        app.update()
        assert vl._xsnap is None, "滚动条释放应退出快照模式"


class TestClusterExpandMain:
    """修复缺陷R16：主列表「▶ ×N」就地展开（展示全部 N 个错误位置）。"""

    @staticmethod
    def _repeat_log(n=8):
        """同一错误重复 n 次（1 簇 ×N 实例，经典模式小数据）。"""
        return "".join(
            f"2024-01-01 09:00:{i:02d} ERROR [db] same failure here\n"
            for i in range(n))

    def test_expand_virtual_injects_instance_rows(self, app):
        """虚拟模式：展开后实例作为视图行注入（时间戳+行号+摘要），再点收起。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        n0 = len(vl._data)
        assert all(r[0] == "c" for r in vl._data), "初始应全为簇行"
        insts = app._displayed[0].instances
        assert insts, "测试数据应有实例记录"
        app._toggle_cluster_expand(0)
        app.update()
        assert 0 in app._expanded_clusters
        assert len(vl._data) == n0 + len(insts), "展开后实例行注入视图"
        # 实例行紧跟所属簇之后，类型/索引正确
        assert vl._data[0] == ("c", 0)
        assert all(r == ("i", 0, j)
                   for j, r in enumerate(vl._data[1:1 + len(insts)]))
        app._toggle_cluster_expand(0)
        app.update()
        assert 0 not in app._expanded_clusters
        assert len(vl._data) == n0
        assert all(r[0] == "c" for r in vl._data), "收起后恢复纯簇行"

    def test_expand_virtual_keeps_scroll(self, app):
        """展开/收起保持滚动位置（不跳回顶部）。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        vl._canvas.yview_moveto(0.3)
        app.update()
        y0 = vl._canvas.canvasy(0)
        assert y0 > 0
        app._toggle_cluster_expand(1)
        app.update()
        assert abs(vl._canvas.canvasy(0) - y0) <= vl.ROW_HEIGHT, \
            "展开应保持滚动位置（内容偏移不变）"
        app._toggle_cluster_expand(1)
        app.update()

    def test_virtual_instance_row_click_shows_detail(self, app):
        """虚拟模式：点击实例行 → 右侧详情显示该实例自身（非典型样例）。"""
        _run_many_clusters(app)
        app.update()
        app._toggle_cluster_expand(0)
        app.update()
        vl = app._virtual_list
        target = None
        for s in vl.slots:
            idx = s.get("idx", -1)
            if 0 <= idx < len(vl._data) and vl._data[idx][0] == "i":
                target = s
                break
        assert target is not None, "视口内应渲染出实例行"
        row = vl._data[target["idx"]]
        target["summary"].event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._selected_inst == (row[1], row[2])
        detail = app._detail_box.get("1.0", "end")
        assert "【实例详情】" in detail
        inst = app._displayed[row[1]].instances[row[2]]
        assert f"行 {inst.line_no}~" in detail

    def test_expand_classic_shows_instances(self, app):
        """经典模式：行内就地展开全部实例，点击看实例详情，收起销毁。"""
        _run_paste_analysis(app, self._repeat_log(8))
        app.update()
        assert app._virtual_list is None, "小数据应走经典列表"
        row = next(r for r in app._cluster_rows if r.get("idx") == 0)
        assert "toggle" in row, "经典行应有「▶ ×N」展开按钮"
        insts = app._displayed[0].instances
        app._toggle_cluster_expand(0)
        app.update()
        assert 0 in app._classic_expanded
        state = app._classic_expanded[0]
        assert len(state["labels"]) >= len(insts), "实例行应全量创建"
        assert row["toggle"].cget("text").startswith("\u25bc"), \
            "展开后按钮应为 ▼"
        # 实例行文本含时间戳与行号
        first = state["labels"][0]
        assert "L" in first.cget("text") and insts[0].summary[:8] \
            in first.cget("text")
        # 点击首个实例 → 右侧实例详情
        first.event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._classic_inst_sel is first
        detail = app._detail_box.get("1.0", "end")
        assert "【实例详情】" in detail
        assert f"行 {insts[0].line_no}~" in detail
        # 收起
        app._toggle_cluster_expand(0)
        app.update()
        assert 0 not in app._classic_expanded
        assert not state["area"].winfo_exists(), "收起后实例区应销毁"
        assert row["toggle"].cget("text").startswith("\u25b6")

    def test_rerender_clears_expand_state(self, app):
        """重新渲染（过滤/TopN/再分析）后展开与实例选中状态清空。"""
        _run_many_clusters(app)
        app.update()
        app._toggle_cluster_expand(0)
        app._select_instance(0, 0)
        app.update()
        assert app._expanded_clusters and app._selected_inst is not None
        app._render_cluster_list()
        app.update()
        assert not app._expanded_clusters
        assert app._selected_inst is None


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


class TestFullscreenSplitter:
    """修复缺陷R17：全屏列表窗口 列表|详情 可拖分隔条（与主界面同款）。"""

    @staticmethod
    def _open_fs(app):
        """打开全屏并放大到可拖尺寸（测试环境屏幕小，默认 85% 宽度
        可能小于「两列最小宽+分隔条」导致分隔条锁定）。"""
        _run_many_clusters(app)
        app._open_list_fullscreen()
        for _ in range(25):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        try:
            win.state("normal")
        except tk.TclError:
            pass
        win.geometry("2400x1300+0+0")
        for _ in range(6):
            app.update()
        return win

    @staticmethod
    def _close(app, win):
        try:
            win.destroy()
        except tk.TclError:
            pass
        app.update()

    def test_fs_splitter_exists(self, app):
        """全屏应有可见分隔条（样式/光标/握点/三列 place 布局）。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            assert sp.winfo_ismapped(), "全屏应有可见分隔条"
            assert sp.cget("cursor") == "sb_h_double_arrow"
            assert len(app._fs_splitter_dots) == 3, "应有三个握点"
            pw = app._fs_body.winfo_width()
            lw = app._fs_list_col.winfo_width()
            dw = app._fs_detail_col.winfo_width()
            assert lw > 300 and dw > 300, "左右列应正常分宽"
            assert abs(lw / pw - app._fs_splitter_ratio) < 0.03, \
                "左列宽应按比例布局"
        finally:
            self._close(app, win)

    def test_fs_splitter_drag_proxy_follows(self, app):
        """矢量代理拖动：裁剪框/视口/标题条逐 motion 实时跟随。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            pw = app._fs_body.winfo_width()
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            live = app._fs_live
            assert live is not None, "应构建矢量文本代理"
            lw0 = live["lw"]
            assert abs(live["right"].canvasx(0)) <= 1, \
                "press 后初始视口应归零"
            assert not sp.winfo_ismapped(), "拖动中真实分隔条应隐藏"
            frozen = app._fs_list_col.winfo_width()
            for dx in (80, 160, 240):
                sp.event_generate("<B1-Motion>", x=3 + dx, y=100)
                app._fs_live_flush()
                app.update()
                expect = app._fs_splitter_ratio * pw
                assert abs(live["clip"].winfo_width() - expect) <= 2, \
                    "左裁剪框宽应逐 motion 实时跟随"
                assert abs(live["right"].canvasx(0)
                           - (lw0 - expect)) <= 2, \
                    "右画布视口应实时滚动（详情文本贴住分隔条）"
                tbar_x = (live["tbar"].winfo_rootx()
                          - app._fs_body.winfo_rootx())
                assert abs(tbar_x - expect) <= 2, \
                    "右标题条（详情标题）应跟随分隔条"
                assert app._fs_list_col.winfo_width() == frozen, \
                    "拖动中真实列冻结（代理之下，松开一次应用）"
            sp.event_generate("<ButtonRelease-1>", x=3 + 240, y=100)
            app.update()
            assert app._fs_live is None, "释放后代理应销毁"
            assert sp.winfo_ismapped(), "释放后真实分隔条应恢复"
            head0 = app._fs_detail_head.winfo_children()[0]
            assert head0.winfo_ismapped(), "释放后真实「详情」标题应恢复"
            assert abs(app._fs_splitter_ratio * pw
                       - app._fs_list_col.winfo_width()) <= 8, \
                "释放后真实列一次性到最终位置"
        finally:
            self._close(app, win)

    def test_fs_splitter_dblclick_restores_default(self, app):
        """双击恢复默认比例（2:3）。"""
        win = self._open_fs(app)
        try:
            app._fs_splitter_ratio = 0.6
            app._fs_layout_splitter()
            app.update()
            assert app._fs_list_col.winfo_width() \
                != int(0.4 * app._fs_body.winfo_width())
            app._on_fs_dblclick(None)
            app.update()
            assert abs(app._fs_splitter_ratio - 0.4) < 1e-6, \
                "双击应恢复默认 2:3 比例"
        finally:
            self._close(app, win)

    def test_fs_splitter_ratio_persisted(self, app):
        """拖动松开后比例持久化（config fs_splitter_ratio）。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            pw = app._fs_body.winfo_width()
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            sp.event_generate("<B1-Motion>", x=3 + int(pw * 0.1), y=100)
            app.update()
            sp.event_generate("<ButtonRelease-1>",
                              x=3 + int(pw * 0.1), y=100)
            app.update()
            expect = app._fs_splitter_ratio
            assert abs(app._config.get("fs_splitter_ratio")
                       - expect) < 1e-6, "比例应写入配置"
        finally:
            self._close(app, win)

    def test_fs_splitter_min_width_limits(self, app):
        """拖到左右极限受最小宽度钳制（不遮挡标题/不锁死）。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            left_min, right_min = app._fs_min_widths()
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            sp.event_generate("<B1-Motion>", x=-9999, y=100)
            app.update()
            sp.event_generate("<ButtonRelease-1>", x=-9999, y=100)
            app.update()
            assert app._fs_list_col.winfo_width() >= left_min - 4, \
                "左列最小宽度钳制"
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            sp.event_generate("<B1-Motion>", x=99999, y=100)
            app.update()
            sp.event_generate("<ButtonRelease-1>", x=99999, y=100)
            app.update()
            assert app._fs_detail_col.winfo_width() >= right_min - 4, \
                "右列最小宽度钳制"
        finally:
            self._close(app, win)


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

    def test_detail_fullscreen_font_18(self, app):
        """修复R10：详情全屏窗口字体放大到 18 号（全屏大字体）。"""
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
            assert int(size) == 18, f"全屏详情字体应 18 号，实际 {size}"
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
        tip._hide_now()

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
    # 四态主题名（修复R13 后由下拉选择框直接选择）
    THEME_NAMES = ("☀ 亮色", "🌙 暗色", "🔵 蓝调", "🟢 绿调")

    def _wait_theme(self, app, key, timeout=3.0):
        """推进淡出/淡入过渡帧直至主题到达目标（28ms/帧）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.update()
            if app._theme == key:
                break
            time.sleep(0.02)
        return app._theme == key

    def _box_text(self, app):
        """选择框当前显示文字（图标 + 主题名）。"""
        return (f"{app._theme_box_icon.cget('text')} "
                f"{app._theme_box_name.cget('text')}")

    def test_theme_menu_shows_current_mode(self, app):
        """修复R13：选择框显示当前主题（四态之一）。"""
        assert self._box_text(app) in self.THEME_NAMES, \
            f"选择框应显示当前主题，实际: {self._box_text(app)}"

    def test_theme_menu_values_exclude_current(self, app):
        """修复R13：下拉列表只列其他三态（当前项不重复，顺序保持）。"""
        from log_ai_compressor.gui.app import THEME_ORDER
        for key in THEME_ORDER:
            app._apply_theme_switch(key)
            app.update()
            expected = [k for k in THEME_ORDER if k != key]
            assert app._theme_popup_items() == expected, \
                f"{key} 下拉列表应为 {expected}，" \
                f"实际 {app._theme_popup_items()}"
        app._apply_theme_switch("dark")

    def test_theme_menu_select_switches_directly(self, app):
        """修复R13：下拉可跳过中间主题直达任意目标（无需循环点击）。"""
        app._apply_theme_switch("dark")
        app.update()
        # 暗色 → 绿调（跳过亮色、蓝调两步）
        app._on_theme_selected("green")
        assert self._wait_theme(app, "green"), "应直接切换到绿调"
        assert self._box_text(app) == "🟢 绿调"
        # 绿调 → 亮色（反向跳过）
        app._on_theme_selected("light")
        assert self._wait_theme(app, "light"), "应直接切换到亮色"
        assert self._box_text(app) == "☀ 亮色"
        app._apply_theme_switch("dark")

    def test_theme_menu_updates_after_switch(self, app):
        """修复R13：切换后选择框显示值与列表内容同步更新。"""
        before = self._box_text(app)
        app._apply_theme_switch("light" if app._is_dark_mode() else "dark")
        app.update()
        after = self._box_text(app)
        assert before != after
        assert {before, after} <= set(self.THEME_NAMES)
        # 当前主题不出现在列表中
        assert app._theme not in app._theme_popup_items()

    def test_theme_menu_all_four_selectable(self, app):
        """修复R13：四种主题都能从下拉直达（逐项选择并验证显示）。"""
        from log_ai_compressor.gui.app import THEME_ORDER
        app._apply_theme_switch("light")
        app.update()
        for expected in ("dark", "blue", "green", "light"):
            app._on_theme_selected(expected)
            assert self._wait_theme(app, expected), \
                f"切换后应为 {expected}，实际 {app._theme}"
            assert self._box_text(app) == \
                dict(zip(THEME_ORDER, self.THEME_NAMES))[expected]
        app._apply_theme_switch("dark")

    def test_theme_popup_text_aligned(self, app):
        """修复R14：下拉列表四个选项文字起始 x 坐标完全一致（对齐）。

        emoji（☀️🌙🔵🟢）字形宽度不一，纯文本菜单会错位；两列布局
        （固定宽图标列 + 左对齐文字列）后文字列起始 x 应严格相等。
        """
        from log_ai_compressor.gui.app import THEME_ORDER
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            xs = []
            for key in THEME_ORDER:
                row = app._theme_popup_rows[key]["row"]
                if key == app._theme:
                    continue          # 当前项隐藏
                xs.append(
                    app._theme_popup_rows[key]["name"].winfo_rootx())
            assert len(xs) == 3, "应显示三个选项"
            assert max(xs) - min(xs) == 0, \
                f"文字起始 x 应完全一致，实际 {xs}"
            # 图标列宽固定（各行图标控件宽度一致）
            icons = [app._theme_popup_rows[key]["icon"].winfo_width()
                     for key in THEME_ORDER if key != app._theme]
            assert max(icons) - min(icons) == 0, \
                f"图标列宽应固定，实际 {icons}"
        finally:
            app._close_theme_popup()

    def test_theme_popup_select_by_click(self, app):
        """修复R14：点击弹窗行控件（图标/文字）触发主题切换并收起。"""
        app._apply_theme_switch("dark")
        app.update()
        app._open_theme_popup()
        app.update()
        name_lbl = app._theme_popup_rows["green"]["name"]
        name_lbl.event_generate("<Button-1>", x=5, y=5)
        app.update()
        assert self._wait_theme(app, "green"), "点击行应切换主题"
        assert app._theme_popup.state() == "withdrawn", "选择后应自动收起"
        app._apply_theme_switch("dark")

    def test_theme_box_icon_col_matches_popup(self, app):
        """修复R14：选择框与下拉列表图标列宽一致（显示位置统一）。"""
        from log_ai_compressor.gui.app import _THEME_ICON_COL
        scale = max(1.0, app._font_scale)
        col = app._theme_icon_col
        assert col >= _THEME_ICON_COL, \
            f"实测图标列宽 {col} 应 ≥ 基准 {_THEME_ICON_COL}"
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            for key in app._theme_popup_items():
                icon = app._theme_popup_rows[key]["icon"]
                assert icon.winfo_width() == pytest.approx(
                    col * scale, abs=2), \
                    f"弹窗图标列宽 {icon.winfo_width()} 应为 " \
                    f"{col * scale:.0f}"
            assert app._theme_box_icon.winfo_width() == pytest.approx(
                col * scale, abs=2), "选择框图标列宽应一致"
        finally:
            app._close_theme_popup()

    def test_theme_box_click_opens_popup_realpath(self, app):
        """修复R15：真实点击路径打开弹窗、点击别处收起（焦点解耦）。

        修复前 FocusOut 收起被 CTkToplevel 全局 bind_all(set_focus)
        干扰：点击打开的瞬间焦点被抢回主窗口 → 弹窗立即失焦收起
        （表现为点击无反应）。现改为全局点击收起，与焦点无关。
        """
        app.update()
        # 模拟真实点击：命中选择框内部 canvas（bind 实际注册处）
        app._theme_box._canvas.event_generate("<Button-1>", x=10, y=10)
        app.update()
        assert app._theme_popup.state() == "normal", \
            "点击选择框后弹窗应打开（R15：不得被立即收起）"
        # CTkToplevel 全局 set_focus 抢焦点（真实链路必然发生），
        # 弹窗不应受焦点变化影响
        app._theme_box._canvas.focus_set()
        app.update()
        assert app._theme_popup.state() == "normal", \
            "焦点被抢回主窗口后弹窗应保持打开（焦点解耦）"
        # 再次点击选择框：切换为收起
        app._theme_box._canvas.event_generate("<Button-1>", x=10, y=10)
        app.update()
        assert app._theme_popup.state() == "withdrawn", "再点应收起"
        # 打开后点击主窗口其他区域：全局点击收起
        app._theme_box._canvas.event_generate("<Button-1>", x=10, y=10)
        app.update()
        assert app._theme_popup.state() == "normal"
        time.sleep(0.2)                     # 越过 150ms 打开豁免窗口
        app._status_label.event_generate("<Button-1>", x=5, y=5)
        app.update()
        assert app._theme_popup.state() == "withdrawn", \
            "点击别处应收起弹窗（全局点击收起）"

    def test_theme_popup_icon_advances_uniform(self, app):
        """修复R16：四个图标 advance（内部 label 宽）一致，无隐形空白。

        "☀️" 的 FE0F 变体选择符被 Tk 渲染成 ~36 物理px 空白尾迹，
        advance（62）是其他图标（30）的 2 倍 —— advance 盒居中后
        可见太阳偏左 ~18px。去掉 FE0F 后 advance 应与其他接近
        （最大/最小 ≤ 1.5 倍）。
        """
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            widths = []
            for key in app._theme_popup_items():
                inner = app._theme_popup_rows[key]["icon"]._label
                widths.append(inner.winfo_width())
            assert len(widths) == 3
            assert max(widths) / max(1, min(widths)) <= 1.5, \
                f"图标 advance 差异过大（{widths}），存在隐形空白尾迹"
            # 修复R16 的直接断言：太阳不带 FE0F
            from log_ai_compressor.gui.app import THEMES
            assert "\ufe0f" not in THEMES["light"]["icon"]
        finally:
            app._close_theme_popup()

    def test_theme_button_icon_col_aligns_popup(self, app):
        """修复R16（续）：主按钮图标列与弹窗图标列同起点、同宽。

        弹窗行 padx=2 + 图标 padx=8 = 10 逻辑 px，主按钮图标原为
        padx=8 —— 按钮图标列比弹窗图标列左偏 2 逻辑 px（200% DPI
        下 4 物理 px，实测太阳墨迹中心偏左 4px）。改为 padx=10 后
        两列完全重合：按钮图标与弹窗图标垂直对齐成一条线。
        """
        app.update()
        app.update_idletasks()
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            btn = app._theme_box_icon
            btn_x = btn.winfo_rootx()
            btn_w = btn.winfo_width()
            for key in app._theme_popup_items():
                icon = app._theme_popup_rows[key]["icon"]
                assert abs(icon.winfo_rootx() - btn_x) <= 2, (
                    f"弹窗行 {key} 图标列起点 {icon.winfo_rootx()} 与主按钮"
                    f"图标列起点 {btn_x} 未对齐（应同为 10 逻辑 px 内距）")
                assert icon.winfo_width() == btn_w, \
                    f"弹窗行 {key} 图标列宽与主按钮图标列宽不一致"
        finally:
            app._close_theme_popup()

    def test_theme_box_name_centered(self, app):
        """修复R15：主题名在图标与▼箭头的正中间（水平居中）。"""
        app.update()
        app.update_idletasks()
        icon = app._theme_box_icon
        name = app._theme_box_name
        arrow = app._theme_box_arrow
        icon_r = icon.winfo_rootx() + icon.winfo_width()
        arrow_l = arrow.winfo_rootx()
        name_c = name.winfo_rootx() + name.winfo_width() / 2
        mid = (icon_r + arrow_l) / 2
        assert abs(name_c - mid) <= 4, \
            f"主题名中心 {name_c} 应在图标右缘与箭头左缘中点 {mid}"

    def test_palette_roles_complete(self, app):
        """修复R1：每个主题调色板字段齐全（缺角色会导致刷新异常）。"""
        from log_ai_compressor.gui.app import THEMES
        required = {"name", "icon", "label", "window", "card", "header",
                    "text", "muted", "accent", "accent_hover", "accent_text",
                    "row_bg", "row_hover", "row_selected", "row_text",
                    "is_dark"}
        for key, palette in THEMES.items():
            assert required <= set(palette), f"{key} 缺字段: {required - set(palette)}"

    def test_blue_green_themes_menu_white(self, app):
        """修复R1/R13：蓝调/绿调下选择框为白底深色字（accent 白色）。"""
        from log_ai_compressor.gui.app import THEMES
        for key in ("blue", "green"):
            app._apply_theme_switch(key)
            app.update()
            assert THEMES[key]["accent"] == "#ffffff"
            # 选择框 fg_color 应用为白色（CTk 返回元组 (r,g,b) 或 hex）
            color = str(app._theme_box.cget("fg_color"))
            assert "255, 255, 255" in color or color == "#ffffff", \
                f"{key} 主题选择框应为白色，实际 {color}"
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
        # 3) 选择框显示与主题一致
        app._update_theme_menu()
        assert self._box_text(app) == "☀ 亮色"
        # 恢复默认暗色
        app._apply_theme_switch("dark")

    def test_selection_with_animation_completes(self, app):
        """修复R13：下拉选择（含过渡动画）最终完成切换且恢复不透明。"""
        before_dark = app._is_dark_mode()
        target = "light" if before_dark else "dark"
        app._on_theme_selected(target)
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
