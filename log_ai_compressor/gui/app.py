# -*- coding: utf-8 -*-
"""GUI 主界面（CustomTkinter）：单窗口三 Tab 布局。

交互结构
--------
- 顶部 Tab：「文件导入」（主力）/「文本粘贴」（快捷）/「多文件对比」；
- 配置区：级别勾选（默认 ERROR+FAIL）、包含/排除关键字、Top N、解析规则；
- 进度区：实时行数 / 速率 / 错误种类数 + 进度条 + 取消按钮；
- 结果区：左侧错误分类列表（图标+次数+摘要+优先级），右侧完整
  堆栈与上下文详情（关键字自动高亮、业务栈帧高亮）；
- 图表窗口：趋势折线 / 级别占比 / 模块分布，点击联动错误列表。

线程模型：管线在后台线程运行，进度经 queue 传递，UI 更新全部在
主线程 after 轮询中完成（Tk 线程安全约束）。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk

from log_ai_compressor import __version__
from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_TOP_N,
    HUMAN_NAME,
    MAX_CONTEXT_LINES,
    MIN_CONTEXT_LINES,
)
from log_ai_compressor.core.analysis import simplify_stack
from log_ai_compressor.core.comparator import CompareResult, compare_files
from log_ai_compressor.core.models import AnalysisResult, ErrorCluster, format_timestamp
from log_ai_compressor.core.pipeline import analyze_file, analyze_text
from log_ai_compressor.export.reporters import (
    brief_summary,
    compare_to_markdown,
    to_json,
    to_markdown,
    to_text,
)
from log_ai_compressor.gui.config_store import ConfigStore

# 修复缺陷#9：matplotlib 导入约 0.4s，延迟到首次点击「统计图表」时
# 才加载（charts 模块不再在 GUI 启动路径上被导入）。

# 可选拖拽支持（tkinterdnd2 未安装时自动退化为点击选择）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False


def _make_app_base():
    """应用基类：有 tkinterdnd2 时混入拖拽能力，否则退化为纯 CTk。"""
    if _HAS_DND:

        class DnDApp(ctk.CTk, TkinterDnD.DnDWrapper):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.TkdndVersion = TkinterDnD._require(self)

        return DnDApp

    class PlainApp(ctk.CTk):
        pass

    return PlainApp

LEVEL_CHECKS = ("ERROR", "FAIL", "WARN", "INFO", "DEBUG")
RULE_NAMES = ("generic", "embedded", "jenkins")
_ANOMALY_LABELS = {"burst": "集中爆发", "rare": "罕见异常"}

# 错误行智能图标：▲ 根因 / ● 爆发 / ○ 稀有 / ◆ 致命
_CLUSTER_ICON = {"fatal": "\u25c6", "root": "\u25b2", "burst": "\u25cf",
                 "rare": "\u25cb", "normal": "\u2022"}

# 错误行背景（元组自动适配明暗主题）
_ROW_BG_DEFAULT = ("gray88", "gray22")
_ROW_BG_HOVER = ("gray80", "gray30")
_ROW_BG_SELECTED = ("gray74", "gray38")
# 摘要文字颜色（明 / 暗）
_ROW_TEXT_DARK = "#c8cdd4"
_ROW_TEXT_LIGHT = "#2d333b"

_KW_DEFAULT = ("ERROR", "FAIL", "FATAL", "Caused by", "Exception",
               "Traceback")

# 详情文本高亮标签配色（主面板与全屏窗口共用，修复缺陷#7）
_DETAIL_TAG_COLORS = {"kw": "#ff6b6b", "bstack": "#ffd54f",
                      "meta": "#8fa4b8", "header": "#4dd0e1"}

# 解析规则说明（悬停提示，修复缺陷#8）
RULE_DESCRIPTIONS = {
    "generic": "通用系统日志格式，适用于大多数标准应用日志、服务日志",
    "embedded": "嵌入式/UT测试日志格式，适用于嵌入式设备、单元测试输出、编译日志",
    "jenkins": "Jenkins控制台输出格式，适用于CI/CD流水线日志、构建日志",
}


def _rate_text(lps: float) -> str:
    if lps >= 10000:
        return f"{lps / 10000:.1f} 万行/秒"
    return f"{lps:.0f} 行/秒"


class Tooltip:
    """鼠标悬停提示（CustomTkinter / 原生控件通用）。

    修复缺陷#6/#8：GUI 关键选项与标题缺少解释性说明，用户无法
    理解「典型样例」「解析规则」等术语的含义。

    实现：Enter 后延迟显示无边框 Toplevel（不抢焦点、不挡操作），
    Leave / 按下时立即销毁；配色随明暗主题自适应。
    """

    def __init__(self, widget, text, delay: int = 400,
                 wrap: int = 380):
        """text 支持静态字符串或返回字符串的可调用对象（动态提示）。

        修复缺陷#8：解析规则说明需跟随当前选中规则动态变化。
        """
        self._widget = widget
        self._text = text
        self._delay = delay
        self._wrap = wrap
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    # ------------------------------------------------------------------
    def _current_text(self) -> str:
        if callable(self._text):
            try:
                return str(self._text() or "")
            except Exception:
                return ""
        return self._text or ""

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self._widget.after(self._delay, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass  # 控件已销毁
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None:
            return
        text = self._current_text()
        if not text:
            return
        try:
            x = self._widget.winfo_rootx() + 12
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        except tk.TclError:
            return  # 控件已销毁
        dark = LogCompressorApp._is_dark_mode() if hasattr(
            LogCompressorApp, "_is_dark_mode") else True
        bg = "#2b2b30" if dark else "#fffdf5"
        fg = "#e8e8ec" if dark else "#2d333b"
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)   # 无边框
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=text, justify="left", wraplength=self._wrap,
            bg=bg, fg=fg, relief="solid", borderwidth=1,
            font=("Microsoft YaHei UI", 10), padx=10, pady=6
        ).pack(fill="both", expand=True)

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class LogCompressorApp(_make_app_base()):
    """日志AI压缩器主窗口。"""

    def __init__(self):
        self._store = ConfigStore()
        self._config = self._store.load()
        ctk.set_appearance_mode(self._config.get("appearance", "dark"))
        super().__init__()

        window = self._config.get("window", {})
        self.title(f"{HUMAN_NAME}  v{__version__}")
        width = window.get("width", 1280)
        height = window.get("height", 840)
        self.geometry(f"{width}x{height}")
        self.minsize(1000, 680)

        # 运行状态
        self._result: Optional[AnalysisResult] = None
        self._compare_results: List[CompareResult] = []
        self._displayed: List[ErrorCluster] = []
        self._cluster_rows: List[dict] = []
        self._selected_row: int = -1
        self._queue: "queue.Queue" = queue.Queue()
        # 共享字体：行级字体必须复用（每行新建 CTkFont 会被 GC 在
        # 任意线程析构，tkinter.Font.__del__ 跨线程调用 Tk 造成死锁）
        self._font_row_head = ctk.CTkFont(family="Consolas", size=12)
        self._font_row_summary = ctk.CTkFont(size=11)
        self._font_hint = ctk.CTkFont(size=11)
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._chart_window: Optional[ctk.CTkToplevel] = None

        self._build_ui()
        self._setup_drag_and_drop()
        self._restore_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_queue)

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        self._build_header()
        self._build_tabs()
        self._build_config_panel()
        self._build_action_panel()
        self._build_result_panel()
        self._build_status_bar()

    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray90", "gray17"))
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        title = ctk.CTkLabel(
            bar, text=f"{HUMAN_NAME}  v{__version__}",
            font=ctk.CTkFont(size=17, weight="bold"))
        title.grid(row=0, column=0, padx=14, pady=8, sticky="w")
        subtitle = ctk.CTkLabel(
            bar, text="海量日志压缩投喂大模型 · 快速故障排查",
            text_color="#8fa4b8")
        subtitle.grid(row=0, column=1, padx=6, sticky="w")
        theme_btn = ctk.CTkButton(bar, text="主题", width=56,
                                  command=self._toggle_theme)
        theme_btn.grid(row=0, column=2, padx=10)

    # ------------------------------------------------------------------
    def _build_tabs(self) -> None:
        self._tabview = ctk.CTkTabview(self)
        self._tabview.grid(row=1, column=0, sticky="ew", padx=10, pady=(8, 2))
        for name in ("文件导入", "文本粘贴", "多文件对比"):
            self._tabview.add(name)
        self._build_file_tab()
        self._build_text_tab()
        self._build_compare_tab()

    def _build_file_tab(self) -> None:
        tab = self._tabview.tab("文件导入")
        tab.grid_columnconfigure(0, weight=1)
        hint_text = ("选择或将日志文件拖入窗口任意位置（支持超大文件、"
                     "UTF-8/GBK 自动适配）")
        if not _HAS_DND:
            hint_text += "  |  拖拽未启用：pip install tkinterdnd2 后重启"
        hint = ctk.CTkLabel(tab, text=hint_text, text_color="#8fa4b8")
        hint.grid(row=0, column=0, sticky="w", pady=(2, 4))
        self._file_entry = ctk.CTkEntry(tab, placeholder_text="日志文件路径…")
        self._file_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        browse = ctk.CTkButton(tab, text="选择文件", width=90,
                               command=self._browse_file)
        browse.grid(row=1, column=1)

    def _build_text_tab(self) -> None:
        tab = self._tabview.tab("文本粘贴")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        hint = ctk.CTkLabel(
            tab, text="直接粘贴日志片段（适合几万行以内的快速排查，无需存为文件）",
            text_color="#8fa4b8")
        hint.grid(row=0, column=0, sticky="w", pady=(2, 4))
        self._paste_box = ctk.CTkTextbox(tab, height=170,
                                         font=ctk.CTkFont(family="Consolas",
                                                          size=12))
        self._paste_box.grid(row=1, column=0, sticky="ew")

    def _build_compare_tab(self) -> None:
        tab = self._tabview.tab("多文件对比")
        tab.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="2~3 个日志文件对比（第一个为基准；适配版本对比 / 修复前后对比）",
                     text_color="#8fa4b8").grid(row=0, column=0, columnspan=3,
                                                sticky="w", pady=(2, 6))
        self._compare_entries = []
        for i, label in enumerate(("基准文件 A", "对比文件 B", "对比文件 C（可选）")):
            ctk.CTkLabel(tab, text=label, width=110, anchor="w").grid(
                row=i + 1, column=0, padx=(0, 6), pady=3, sticky="w")
            entry = ctk.CTkEntry(tab, placeholder_text="日志文件路径…")
            entry.grid(row=i + 1, column=1, sticky="ew", padx=(0, 6))
            btn = ctk.CTkButton(tab, text="选择", width=64,
                                command=lambda e=entry: self._browse_file(e))
            btn.grid(row=i + 1, column=2)
            self._compare_entries.append(entry)

    # ------------------------------------------------------------------
    def _build_config_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        for col in range(6):
            panel.grid_columnconfigure(col, weight=1)

        ctk.CTkLabel(panel, text="级别过滤", font=ctk.CTkFont(weight="bold")
                     ).grid(row=0, column=0, padx=(12, 4), sticky="w")
        self._level_vars: Dict[str, tk.BooleanVar] = {}
        for i, level in enumerate(LEVEL_CHECKS):
            var = tk.BooleanVar(value=level in ("ERROR", "FAIL"))
            self._level_vars[level] = var
            ctk.CTkCheckBox(panel, text=level, variable=var,
                            checkbox_width=18, checkbox_height=18).grid(
                row=0, column=1 + i, padx=6, sticky="w")

        self._include_entry = ctk.CTkEntry(panel, width=200,
                                           placeholder_text="包含关键字（逗号分隔）")
        self._include_entry.grid(row=1, column=0, columnspan=2, padx=(12, 6),
                                 pady=(6, 8), sticky="ew")
        self._exclude_entry = ctk.CTkEntry(panel, width=200,
                                           placeholder_text="排除关键字（逗号分隔）")
        self._exclude_entry.grid(row=1, column=2, columnspan=2, padx=6,
                                 pady=(6, 8), sticky="ew")
        ctk.CTkLabel(panel, text="Top N").grid(row=1, column=4, padx=(6, 2),
                                               sticky="e")
        self._topn_entry = ctk.CTkEntry(panel, width=60)
        self._topn_entry.insert(0, str(DEFAULT_TOP_N))
        self._topn_entry.grid(row=1, column=5, padx=(2, 12), pady=(6, 8),
                              sticky="w")

        # 修复缺陷#5：上下文行数可调节输入框（5~200，默认 50）
        ctk.CTkLabel(panel, text="上下文行数").grid(
            row=2, column=0, padx=(12, 2), pady=(0, 8), sticky="e")
        self._ctx_entry = ctk.CTkEntry(panel, width=60)
        self._ctx_entry.insert(0, str(DEFAULT_CONTEXT_LINES))
        self._ctx_entry.grid(row=2, column=1, padx=(2, 6), pady=(0, 8),
                             sticky="w")
        ctx_hint = ctk.CTkLabel(
            panel, text="典型样例前后各保留的上下文行数（5~200）",
            text_color="#8fa4b8")
        ctx_hint.grid(row=2, column=2, columnspan=2, padx=(2, 6),
                      pady=(0, 8), sticky="w")

        ctk.CTkLabel(panel, text="解析规则").grid(row=0, column=6, padx=(6, 2),
                                                  sticky="e")
        self._rule_menu = ctk.CTkOptionMenu(panel, values=list(RULE_NAMES),
                                            width=130,
                                            command=self._on_rule_changed)
        self._rule_menu.grid(row=0, column=7, padx=(2, 0), sticky="w")
        # 修复缺陷#8：解析规则悬停说明（跟随当前选中规则动态变化）
        rule_help = ctk.CTkLabel(
            panel, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        rule_help.grid(row=0, column=8, padx=(4, 12), sticky="w")
        self._rule_help_tooltip = Tooltip(
            rule_help,
            lambda: RULE_DESCRIPTIONS.get(self._rule_menu.get(), ""))

    def _build_action_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=3, column=0, sticky="ew", padx=10, pady=4)
        for col in range(7):
            panel.grid_columnconfigure(col, weight=1)

        self._start_btn = ctk.CTkButton(
            panel, text="开始分析", font=ctk.CTkFont(weight="bold"),
            command=self._on_start)
        self._start_btn.grid(row=0, column=0, padx=6, pady=8, sticky="ew")
        self._cancel_btn = ctk.CTkButton(
            panel, text="取消", state="disabled", fg_color="#7b3535",
            hover_color="#94424a", command=self._on_cancel)
        self._cancel_btn.grid(row=0, column=1, padx=6, sticky="ew")
        self._export_btn = ctk.CTkButton(panel, text="导出报告",
                                         state="disabled",
                                         command=self._on_export)
        self._export_btn.grid(row=0, column=2, padx=6, sticky="ew")
        self._copy_btn = ctk.CTkButton(panel, text="复制摘要", state="disabled",
                                       command=self._on_copy_summary)
        self._copy_btn.grid(row=0, column=3, padx=6, sticky="ew")
        self._chart_btn = ctk.CTkButton(panel, text="统计图表", state="disabled",
                                        command=self._show_charts)
        self._chart_btn.grid(row=0, column=4, padx=6, sticky="ew")

        progress_frame = ctk.CTkFrame(panel, fg_color="transparent")
        progress_frame.grid(row=0, column=5, columnspan=2, padx=10,
                            sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        self._progress_label = ctk.CTkLabel(progress_frame, text="就绪",
                                            text_color="#8fa4b8",
                                            anchor="w")
        self._progress_label.grid(row=0, column=0, sticky="ew")
        self._progress_bar = ctk.CTkProgressBar(progress_frame)
        self._progress_bar.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self._progress_bar.set(0)

    def _build_result_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        panel.grid(row=4, column=0, sticky="nsew", padx=10, pady=(4, 2))
        panel.grid_columnconfigure(0, weight=2)
        panel.grid_columnconfigure(1, weight=3)
        panel.grid_rowconfigure(1, weight=1)

        # 修复缺陷#7：列表 / 详情标题行增加「全屏」按钮（独立最大化窗口）
        list_head = ctk.CTkFrame(panel, fg_color="transparent")
        list_head.grid(row=0, column=0, padx=10, pady=(8, 2), sticky="ew")
        list_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(list_head, text="错误分类列表（按优先级降序）",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w")
        self._list_fs_btn = ctk.CTkButton(list_head, text="⛶ 全屏", width=84,
                                          height=26,
                                          command=self._open_list_fullscreen)
        self._list_fs_btn.grid(row=0, column=1, padx=(6, 0), sticky="e")

        # 修复缺陷#6：「典型样例」术语加悬停说明（ⓘ 图标触发）
        detail_head = ctk.CTkFrame(panel, fg_color="transparent")
        detail_head.grid(row=0, column=1, padx=10, pady=(8, 2), sticky="ew")
        detail_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(detail_head, text="详情（典型样例 · 上下文 · 降噪堆栈）",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, sticky="w")
        sample_help = ctk.CTkLabel(
            detail_head, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        sample_help.grid(row=0, column=1, padx=(4, 0), sticky="w")
        self._sample_help_tooltip = Tooltip(
            sample_help,
            "该错误类型的代表性日志样例，包含完整的错误信息、堆栈跟踪"
            "和前后上下文，用于快速定位问题")
        self._detail_fs_btn = ctk.CTkButton(detail_head, text="⛶ 全屏", width=84,
                                            height=26,
                                            command=self._open_detail_fullscreen)
        self._detail_fs_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")

        self._cluster_list = ctk.CTkScrollableFrame(panel, width=430)
        self._cluster_list.grid(row=1, column=0, sticky="nsw", padx=(10, 4),
                                pady=(2, 8))
        self._detail_box = ctk.CTkTextbox(
            panel, font=ctk.CTkFont(family="Consolas", size=12), wrap="none")
        self._detail_box.grid(row=1, column=1, sticky="nsew", padx=(4, 10),
                              pady=(2, 8))
        self._setup_detail_tags()

    def _setup_detail_tags(self) -> None:
        """详情文本高亮标签：关键字 / 业务栈帧 / 元信息 / 段落标题。"""
        try:
            for tag, color in _DETAIL_TAG_COLORS.items():
                self._detail_box.tag_config(tag, foreground=color)
        except (tk.TclError, AttributeError):
            pass  # 标签配置失败时降级为无高亮

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray90", "gray17"))
        bar.grid(row=5, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self._status_label = ctk.CTkLabel(bar, text="就绪 · 支持文件导入 / 文本粘贴 / 多文件对比",
                                          anchor="w", text_color="#8fa4b8")
        self._status_label.grid(row=0, column=0, padx=12, pady=5, sticky="w")

    # ==================================================================
    # 配置持久化
    # ==================================================================
    def _restore_config(self) -> None:
        cfg = self._config
        for level, var in self._level_vars.items():
            var.set(level in (cfg.get("levels") or ["ERROR", "FAIL"]))
        if cfg.get("include"):
            self._include_entry.insert(0, ",".join(cfg["include"]))
        if cfg.get("exclude"):
            self._exclude_entry.insert(0, ",".join(cfg["exclude"]))
        if isinstance(cfg.get("top_n"), int):
            self._topn_entry.delete(0, "end")
            self._topn_entry.insert(0, str(cfg["top_n"]))
        # 修复缺陷#5：恢复上次设置的上下文行数
        if isinstance(cfg.get("context_lines"), int):
            self._ctx_entry.delete(0, "end")
            self._ctx_entry.insert(0, str(cfg["context_lines"]))
        if cfg.get("rule") in RULE_NAMES:
            self._rule_menu.set(cfg["rule"])
        last = cfg.get("last_files") or []
        if last and self._file_entry.get() == "":
            self._file_entry.insert(0, str(last[0]))
        for entry, path in zip(self._compare_entries, last[1:]):
            if path:
                entry.insert(0, str(path))

    def _current_config_dict(self) -> dict:
        return {
            "levels": [lv for lv, var in self._level_vars.items() if var.get()],
            "include": self._split_keywords(self._include_entry.get()),
            "exclude": self._split_keywords(self._exclude_entry.get()),
            "top_n": self._current_top_n(),
            "context_lines": self._current_context_lines(),
            "rule": self._rule_menu.get(),
            "appearance": self._config.get("appearance", "dark"),
            "window": {"width": self.winfo_width(),
                       "height": self.winfo_height()},
            "last_files": [self._file_entry.get()] +
                          [e.get() for e in self._compare_entries],
        }

    def _save_config(self) -> None:
        self._config = self._current_config_dict()
        self._store.save(self._config)

    @staticmethod
    def _split_keywords(raw: str) -> List[str]:
        return [k.strip() for k in raw.split(",") if k and k.strip()] if raw else []

    def _current_top_n(self) -> int:
        try:
            return max(1, int(self._topn_entry.get() or DEFAULT_TOP_N))
        except ValueError:
            return DEFAULT_TOP_N

    def _current_context_lines(self) -> int:
        """读取上下文行数（非法/越界输入自动钳制到 5~200）。

        修复缺陷#5：原硬编码 5 行，现为 GUI 可调节项。
        """
        try:
            value = int(self._ctx_entry.get() or DEFAULT_CONTEXT_LINES)
        except ValueError:
            return DEFAULT_CONTEXT_LINES
        # 钳制到合法范围（上限防极端值拖慢流式解析）
        return max(MIN_CONTEXT_LINES, min(MAX_CONTEXT_LINES, value))

    def _on_rule_changed(self, choice: str) -> None:
        """解析规则切换：状态栏即时展示该规则的适用场景说明。

        修复缺陷#8：与悬停 tooltip 互补——切换规则时无需悬停
        即可看到当前规则的含义。
        """
        desc = RULE_DESCRIPTIONS.get(choice)
        if desc:
            self._status_label.configure(text=f"解析规则 {choice}：{desc}")

    # ==================================================================
    # 文件选择 / 拖拽
    # ==================================================================
    def _setup_drag_and_drop(self) -> None:
        """拖拽初始化：注册整窗为拖放目标。

        说明：注册在根窗口（而非单个输入框）上，任何位置松手均可接收，
        规避 CTk 控件级注册的兼容性问题；tkinterdnd2 缺失时自动降级
        为纯点击选择并给出安装提示。
        """
        if not _HAS_DND:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop_file)
        except tk.TclError:
            # tkdnd 二进制与当前环境不兼容时静默降级
            pass

    def _browse_file(self, entry: Optional[ctk.CTkEntry] = None) -> None:
        target = entry or self._file_entry
        path = filedialog.askopenfilename(
            title="选择日志文件",
            filetypes=[("日志文件", "*.log *.txt *.out"), ("所有文件", "*.*")])
        if path:
            target.delete(0, "end")
            target.insert(0, path)

    def _on_drop_file(self, event) -> None:
        """拖拽导入：整窗接收，按当前 Tab 路由填充文件路径。"""
        try:
            paths = [str(p) for p in self.tk.splitlist(event.data)]
        except (tk.TclError, AttributeError, TypeError):
            raw = getattr(event, "data", "") or ""
            paths = [raw.strip("{}")] if raw else []
        paths = [p for p in paths if p]
        if not paths:
            return

        if self._tabview.get() == "多文件对比":
            # 对比模式：按 A/B/C 顺序填充
            for entry, path in zip(self._compare_entries, paths[:3]):
                entry.delete(0, "end")
                entry.insert(0, path)
            self._status_label.configure(
                text=f"已拖入 {min(len(paths), 3)} 个文件（对比模式，点击开始分析）")
        else:
            # 常规模式：首个文件进导入框，其余预填对比区
            self._file_entry.delete(0, "end")
            self._file_entry.insert(0, paths[0])
            if len(paths) > 1:
                for entry, path in zip(self._compare_entries, paths[1:3]):
                    entry.delete(0, "end")
                    entry.insert(0, path)
                self._status_label.configure(
                    text=f"已拖入 {len(paths)} 个文件：首个进入文件导入，"
                         f"其余已填入多文件对比区（可切换 Tab 对比）")
            else:
                self._status_label.configure(
                    text=f"已拖入文件：{paths[0]}（点击开始分析）")

    def _toggle_theme(self) -> None:
        current = ctk.get_appearance_mode().lower()
        mode = "light" if current == "dark" else "dark"
        ctk.set_appearance_mode(mode)
        self._config["appearance"] = mode

    # ==================================================================
    # 任务调度（后台线程 + 队列轮询）
    # ==================================================================
    def _on_start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        mode = self._tabview.get()

        payload: dict = {"mode": mode}
        # 全部 UI 状态必须在主线程采集（Tk 控件禁止跨线程访问）
        payload["common"] = dict(
            levels=[lv for lv, var in self._level_vars.items() if var.get()],
            include=self._split_keywords(self._include_entry.get()),
            exclude=self._split_keywords(self._exclude_entry.get()),
            top_n=self._current_top_n(),
            context_lines=self._current_context_lines(),
            rule=self._rule_menu.get(),
        )
        if mode == "文件导入":
            path = self._file_entry.get().strip()
            if not path:
                messagebox.showwarning("提示", "请先选择日志文件")
                return
            payload["file"] = path
        elif mode == "文本粘贴":
            text = self._paste_box.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning("提示", "请先粘贴日志文本")
                return
            payload["text"] = text
        else:
            paths = [e.get().strip() for e in self._compare_entries
                     if e.get().strip()]
            if len(paths) < 2:
                messagebox.showwarning("提示", "对比分析至少需要 2 个日志文件")
                return
            payload["files"] = paths

        # 重置状态并启动
        self._result = None
        self._compare_results = []
        self._displayed = []
        for child in self._cluster_list.winfo_children():
            child.destroy()
        self._detail_box.configure(state="normal")
        self._detail_box.delete("1.0", "end")
        self._detail_box.configure(state="disabled")

        self._cancel_event.clear()
        self._set_running(True)
        self._progress_label.configure(text="解析中…")
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start()

        self._worker = threading.Thread(target=self._run_worker,
                                        args=(payload,), daemon=True)
        self._worker.start()

    def _run_worker(self, payload: dict) -> None:
        """后台线程：调用核心管线（UI 更新一律经 queue 回主线程）。"""
        common = payload["common"]
        progress = lambda d: self._queue.put(("progress", d))  # noqa: E731
        try:
            if payload["mode"] == "多文件对比":
                results = compare_files(payload["files"], **common)
                self._queue.put(("compare_done", results))
            elif payload["mode"] == "文件导入":
                result = analyze_file(payload["file"], analyze=True,
                                      progress_cb=progress,
                                      cancel_event=self._cancel_event, **common)
                self._queue.put(("done", result))
            else:
                result = analyze_text(payload["text"], analyze=True,
                                      progress_cb=progress,
                                      cancel_event=self._cancel_event, **common)
                self._queue.put(("done", result))
        except FileNotFoundError as exc:
            self._queue.put(("error", f"文件不存在：{exc}"))
        except Exception as exc:  # 规则文件错误等
            self._queue.put(("error", f"处理失败：{exc}"))

    def _on_cancel(self) -> None:
        # 完成后「取消」按钮保持可点击（规范要求），空任务时仅提示
        if not (self._worker and self._worker.is_alive()):
            self._status_label.configure(text="当前没有进行中的分析任务")
            return
        self._cancel_event.set()
        self._progress_label.configure(text="正在取消…")
        self._cancel_btn.configure(state="disabled")

    def _poll_queue(self) -> None:
        """队列轮询（80ms 周期）。

        健壮性设计：先重调度再处理事件——任何处理器异常都不会
        终止轮询循环（否则 GUI 将永久失去响应）；异常降级为
        状态栏提示并打印到 stderr 便于定位。
        """
        self.after(80, self._poll_queue)
        try:
            while True:
                kind, data = self._queue.get_nowait()
                if kind == "progress":
                    self._update_progress(data)
                elif kind == "done":
                    self._on_result(data)
                elif kind == "compare_done":
                    self._on_compare_result(data)
                elif kind == "error":
                    self._on_error(data)
        except queue.Empty:
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self._status_label.configure(text="内部错误（详情见控制台），轮询已继续")

    def _update_progress(self, data: dict) -> None:
        if data.get("phase") == "parsing":
            self._progress_label.configure(
                text=f"已处理 {data['lines']:,} 行 | "
                     f"{_rate_text(data['lps'])} | "
                     f"错误种类 {data['clusters']} | 耗时 {data['elapsed']:.1f}s")

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(0)
        self._progress_label.configure(text="失败")
        self._status_label.configure(text=message)
        messagebox.showerror("错误", message)

    # ==================================================================
    # 结果渲染
    # ==================================================================
    def _on_result(self, result: AnalysisResult) -> None:
        # 先赋值再切换按钮状态：状态机依据 self._result 判断可用性
        self._result = result
        self._compare_results = []
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(1.0)
        s = result.stats
        suffix = "（已取消，增量结果）" if s.truncated else ""
        self._progress_label.configure(
            text=f"完成：{s.total_lines:,} 行 | 错误 {s.error_lines:,} 行 | "
                 f"{len(result.clusters)} 种 | {_rate_text(s.lines_per_second)}"
                 f"{suffix}")
        self._status_label.configure(
            text=f"{s.source} | 编码 {s.encoding} | 规则 {s.rule_name} | "
                 f"时间范围 {format_timestamp(s.time_start)} ~ "
                 f"{format_timestamp(s.time_end)} | 智能分析耗时 "
                 f"{s.analysis_cost * 1000:.0f}ms")
        self._render_cluster_list()
        self._save_config()
        if self._displayed:
            self._select_cluster(0)

    def _render_cluster_list(self) -> None:
        """左侧错误列表：Top N 行（图标/优先级/次数行 + 自动换行摘要行）。

        修复缺陷：原单行 CTkButton 长文本溢出右侧且无横向滚动能力，
        改为摘要自动换行（wraplength 随列表宽度自适应），内容完整可见。
        """
        assert self._result is not None
        n = self._current_top_n()
        self._displayed = self._result.clusters[:n]
        for child in self._cluster_list.winfo_children():
            child.destroy()
        self._cluster_rows = []
        self._selected_row = -1
        if not self._displayed:
            ctk.CTkLabel(self._cluster_list, text="未发现符合条件的错误",
                         text_color="#8fa4b8").pack(pady=20)
            return
        for idx, cluster in enumerate(self._displayed):
            self._make_cluster_row(self._cluster_list, idx, cluster)
        total = len(self._result.clusters)
        if total > n:
            ctk.CTkLabel(self._cluster_list,
                         text=f"…… 其余 {total - n} 种错误可通过调大 Top N 查看",
                         text_color="#8fa4b8",
                         font=self._font_hint).pack(pady=6)
        # 列表宽度变化时刷新换行宽度
        self._cluster_list.unbind("<Configure>")
        self._cluster_list.bind("<Configure>", self._on_list_resize)

    def _make_cluster_row(self, parent, idx: int, cluster: ErrorCluster,
                          register: bool = True,
                          on_select=None, on_hover=None) -> dict:
        """构建单条错误行（主列表与全屏列表复用，修复缺陷#7）。

        摘要使用原生 tk.Label：wraplength 超限即折行（含超长
        单词的字符级折行），长 token（超长路径 / 哈希串）完整可见。

        参数：
            register: 登记进 self._cluster_rows（主列表选中态管理）
            on_select / on_hover: 自定义回调（全屏窗口联动高亮用）
        """
        frame = ctk.CTkFrame(parent, corner_radius=4,
                             fg_color=_ROW_BG_DEFAULT)
        frame.pack(fill="x", padx=4, pady=1)
        head = ctk.CTkLabel(
            frame, text=self._row_text(cluster), anchor="w",
            text_color=self._row_color(cluster) or None,
            font=self._font_row_head)
        head.pack(fill="x", padx=(8, 8), pady=(3, 0))
        summary = tk.Label(
            frame, text=cluster.summary, anchor="w", justify="left",
            wraplength=360,
            font=self._font_row_summary,
            bg=self._resolve_row_color(_ROW_BG_DEFAULT),
            fg=_ROW_TEXT_DARK if self._is_dark_mode() else _ROW_TEXT_LIGHT)
        summary.pack(fill="x", padx=(8, 2), pady=(0, 4))
        select_cb = on_select or (lambda: self._select_cluster(idx))
        hover_cb = on_hover or (lambda hovered: self._hover_row(idx, hovered))
        for widget in (frame, head, summary):
            widget.bind("<Button-1>", lambda e: select_cb())
            widget.bind("<Enter>", lambda e: hover_cb(True))
            widget.bind("<Leave>", lambda e: hover_cb(False))
        row = {"frame": frame, "summary": summary, "idx": idx}
        if register:
            self._cluster_rows.append(row)
        return row

    @staticmethod
    def _is_dark_mode() -> bool:
        return ctk.get_appearance_mode().lower() == "dark"

    @staticmethod
    def _resolve_row_color(color) -> str:
        """主题色元组 -> 当前模式下的实际颜色值。"""
        if isinstance(color, (tuple, list)):
            return color[1] if LogCompressorApp._is_dark_mode() else color[0]
        return color

    def _apply_row_bg(self, idx: int, color) -> None:
        """统一更新行背景（CTkFrame + 原生摘要标签同步）。"""
        row = self._cluster_rows[idx]
        row["frame"].configure(fg_color=color)
        row["summary"].configure(bg=self._resolve_row_color(color))

    def _on_list_resize(self, event) -> None:
        """列表宽度变化 -> 自适应摘要换行宽度（保持内容完整可见）。"""
        wrap = max(240, event.width - 60)
        for row in getattr(self, "_cluster_rows", ()):
            row["summary"].configure(wraplength=wrap)

    def _hover_row(self, idx: int, hovered: bool) -> None:
        """行悬停高亮（选中行保持选中色）。"""
        if not (0 <= idx < len(self._cluster_rows)):
            return
        if idx == self._selected_row:
            return
        self._apply_row_bg(
            idx, _ROW_BG_HOVER if hovered else _ROW_BG_DEFAULT)

    def _mark_selected_row(self, idx: int) -> None:
        """更新选中行高亮（清除旧选中，标记新选中）。"""
        previous = getattr(self, "_selected_row", -1)
        if 0 <= previous < len(self._cluster_rows):
            self._apply_row_bg(previous, _ROW_BG_DEFAULT)
        if 0 <= idx < len(self._cluster_rows):
            self._apply_row_bg(idx, _ROW_BG_SELECTED)
        self._selected_row = idx

    @staticmethod
    def _row_text(cluster: ErrorCluster) -> str:
        """行首元信息：图标 + 优先级 + 级别 + 次数 + 模块（不含摘要）。"""
        if cluster.level == "FATAL":
            icon = _CLUSTER_ICON["fatal"]
        elif cluster.is_root_cause:
            icon = _CLUSTER_ICON["root"]
        elif cluster.anomaly == "burst":
            icon = _CLUSTER_ICON["burst"]
        elif cluster.anomaly == "rare":
            icon = _CLUSTER_ICON["rare"]
        else:
            icon = _CLUSTER_ICON["normal"]
        module = f"  {cluster.module}" if cluster.module else ""
        return (f"{icon} {cluster.priority_label} {cluster.level:<5} "
                f"\u00d7{cluster.count:<4}{module}")

    @staticmethod
    def _clip(text: str, width: int) -> str:
        return text if len(text) <= width else text[:width - 1] + "…"

    @staticmethod
    def _row_color(cluster: ErrorCluster) -> Optional[str]:
        if cluster.level == "FATAL":
            return "#ff5252"
        if cluster.is_root_cause:
            return "#ffb74d"
        return None

    # ------------------------------------------------------------------
    # 详情渲染
    # ------------------------------------------------------------------
    def _select_cluster(self, idx: int) -> None:
        if not (0 <= idx < len(self._displayed)):
            return
        self._mark_selected_row(idx)
        self._show_cluster_detail(self._displayed[idx])

    def _show_cluster_detail(self, cluster: ErrorCluster) -> None:
        box = self._detail_box
        box.configure(state="normal")
        box.delete("1.0", "end")

        def header(text: str) -> None:
            box.insert("end", text + "\n", "header")

        def meta(text: str) -> None:
            box.insert("end", text + "\n", "meta")

        def plain(text: str = "") -> None:
            box.insert("end", text + "\n")

        header(f"【错误摘要】{cluster.summary}")
        meta(f"出现 {cluster.count} 次 | 级别 {cluster.level} | "
             f"模块 {cluster.module or '-'} | "
             f"行 {cluster.first_line}~{cluster.last_line}")
        meta(f"时间范围 {format_timestamp(cluster.first_seen)} ~ "
             f"{format_timestamp(cluster.last_seen)} | "
             f"优先级 {cluster.priority_label}（{cluster.priority:.0f}）")
        notes = []
        if cluster.is_root_cause:
            notes.append(f"根因：{cluster.root_cause_reason}")
        elif cluster.root_cause_reason:
            notes.append(cluster.root_cause_reason)
        if cluster.anomaly:
            notes.append(f"异常：{_ANOMALY_LABELS.get(cluster.anomaly, cluster.anomaly)}")
        meta(("【智能分析】" + "；".join(notes)) if notes else "【智能分析】无特殊标记")

        sample = cluster.sample
        if sample is None:
            plain()
            plain("（无典型样例）")
            box.configure(state="disabled")
            return

        entry = sample.entry
        if sample.before:
            plain()
            header("──── 前上下文 ────")
            for line in sample.before:
                plain(line)
        plain()
        header("──── 典型样例 ────")
        plain(entry.raw)
        for extra in entry.message_extra:
            plain(extra)

        if entry.stack:
            simplified = simplify_stack(entry.stack)
            plain()
            header(f"──── 堆栈（业务帧高亮，折叠噪声帧 {simplified.noise_count} 行）────")
            for line in simplified.lines:
                if "已折叠" in line:
                    meta(line)
                else:
                    box.insert("end", line + "\n", "bstack")

        if sample.after:
            plain()
            header("──── 后上下文 ────")
            for line in sample.after:
                plain(line)
        box.configure(state="disabled")
        self._highlight_keywords(box)

    def _highlight_keywords(self, box: ctk.CTkTextbox) -> None:
        """关键字自动高亮（包含关键字 + 常见错误特征词）。"""
        text = box.get("1.0", "end")
        keywords = set(self._split_keywords(self._include_entry.get()))
        keywords.update(_KW_DEFAULT)
        for kw in keywords:
            if not kw:
                continue
            start = 0
            lowered = text.lower()
            kw_low = kw.lower()
            while True:
                pos = lowered.find(kw_low, start)
                if pos < 0:
                    break
                before = pos > 0 and text[pos - 1].isalnum()
                after = (pos + len(kw) < len(text)
                         and text[pos + len(kw)].isalnum())
                if not before and not after:
                    line, col = self._index_of_offset(box, pos)
                    if line:
                        box.tag_add("kw", f"{line}.{col}",
                                    f"{line}.{col + len(kw)}")
                start = pos + len(kw)

    @staticmethod
    def _index_of_offset(box: ctk.CTkTextbox, offset: int):
        """字符偏移 -> (行, 列)（逐行累计，避免行尾符计数差异）。"""
        line = 1
        remaining = offset
        while line < 100000:
            line_len = len(box.get(f"{line}.0", f"{line}.end")) + 1
            if remaining < line_len:
                return line, remaining
            remaining -= line_len
            line += 1
        return None, 0

    # ------------------------------------------------------------------
    # 对比结果渲染
    # ------------------------------------------------------------------
    def _on_compare_result(self, results: List[CompareResult]) -> None:
        # 先赋值再切换按钮状态：状态机依据结果判断可用性
        self._compare_results = results
        self._result = None
        self._displayed = []
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(1.0)
        self._progress_label.configure(text="对比完成")

        box = self._detail_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", "【多文件对比结果】\n", "header")
        # 修复缺陷#10：对比符号图例说明（顶部一次性展示）
        box.insert("end", "图例：", "header")
        box.insert("end", "+ 新增错误（对比文件中新出现的）\n")
        box.insert("end", "        - 消失错误（基准文件中有但对比文件中没有的）\n")
        box.insert("end", "        = 共同错误（两个文件中都存在的）\n", "meta")
        for cmp in results:
            box.insert("end", f"\n基准 {cmp.base_name} vs {cmp.other_name}\n",
                       "header")
            box.insert(
                "end",
                f"新增 {len(cmp.new_items)} 种（{sum(i.count_b for i in cmp.new_items)} 次） | "
                f"消失 {len(cmp.gone_items)} 种 | "
                f"共同 {len(cmp.common_items)} 种\n", "meta")
            for i in cmp.new_items[:10]:
                box.insert("end", f"  + [{i.level} ×{i.count_b}] {i.summary}\n")
            for i in cmp.gone_items[:10]:
                box.insert("end", f"  - [{i.level} ×{i.count_a}] {i.summary}\n")
            for i in cmp.common_items[:10]:
                box.insert("end",
                           f"  = [{i.level} {i.count_a}->{i.count_b} "
                           f"{i.change_text}] {i.summary}\n")
        box.insert("end", "\n提示：点击「导出报告」保存完整对比差异报告；"
                           "「统计图表」查看两文件错误对比图\n", "meta")
        box.configure(state="disabled")
        # 修复缺陷#10：左侧渲染对比差异列表（支持全屏查看）
        self._render_compare_list()
        self._status_label.configure(
            text=f"对比完成：{' vs '.join([cmp.other_name for cmp in results])}")
        self._save_config()

    # 对比行符号与配色
    _CMP_SYMBOL = {"new": "+", "gone": "-", "common": "="}
    _CMP_COLOR = {"new": "#66bb6a", "gone": "#ef5350", "common": "#4dd0e1"}

    def _iter_compare_rows(self):
        """按「新增 → 消失 → 共同」顺序产出 (kind, 对比对, 差异项)。"""
        for cmp in self._compare_results:
            for i in cmp.new_items:
                yield "new", cmp, i
            for i in cmp.gone_items:
                yield "gone", cmp, i
            for i in cmp.common_items:
                yield "common", cmp, i

    def _make_compare_row(self, parent, kind: str, cmp: CompareResult,
                          item) -> dict:
        """构建单条对比差异行：符号 + 级别 + 次数变化 + 摘要（自动换行）。"""
        frame = ctk.CTkFrame(parent, corner_radius=4,
                             fg_color=_ROW_BG_DEFAULT)
        frame.pack(fill="x", padx=4, pady=1)
        if kind == "new":
            count_text = f"×{item.count_b} 次"
        elif kind == "gone":
            count_text = f"×{item.count_a} 次"
        else:
            count_text = f"{item.count_a} -> {item.count_b}（{item.change_text}）"
        head = ctk.CTkLabel(
            frame,
            text=(f"{self._CMP_SYMBOL[kind]} [{item.level}] {count_text}"
                  f"   {cmp.base_name} vs {cmp.other_name}"),
            anchor="w", text_color=self._CMP_COLOR[kind],
            font=self._font_row_head)
        head.pack(fill="x", padx=(8, 8), pady=(3, 0))
        summary = tk.Label(
            frame, text=item.summary, anchor="w", justify="left",
            wraplength=360, font=self._font_row_summary,
            bg=self._resolve_row_color(_ROW_BG_DEFAULT),
            fg=_ROW_TEXT_DARK if self._is_dark_mode() else _ROW_TEXT_LIGHT)
        summary.pack(fill="x", padx=(8, 2), pady=(0, 4))
        return {"frame": frame, "summary": summary, "kind": kind,
                "item": item, "text": f"{item.summary} {item.level} "
                                      f"{self._CMP_SYMBOL[kind]}"}

    def _render_compare_list(self) -> None:
        """左侧对比差异列表（新增/消失/共同 全量渲染）。"""
        for child in self._cluster_list.winfo_children():
            child.destroy()
        self._cluster_rows = []
        self._selected_row = -1
        if not self._compare_results:
            ctk.CTkLabel(self._cluster_list,
                         text="对比模式：差异摘要见右侧详情",
                         text_color="#8fa4b8").pack(pady=20)
            return
        rows = list(self._iter_compare_rows())
        if not rows:
            ctk.CTkLabel(self._cluster_list,
                         text="两文件错误完全一致（无差异项）",
                         text_color="#8fa4b8").pack(pady=20)
            return
        for kind, cmp, item in rows[:200]:   # 上限保护（超长列表性能）
            self._make_compare_row(self._cluster_list, kind, cmp, item)

    # ==================================================================
    # 导出 / 复制 / 图表
    # ==================================================================
    def _on_export(self) -> None:
        if self._result is None and not self._compare_results:
            return
        path = filedialog.asksaveasfilename(
            title="导出报告",
            defaultextension=".md",
            filetypes=[("Markdown 报告", "*.md"), ("JSON 数据", "*.json"),
                       ("纯文本", "*.txt")])
        if not path:
            return
        try:
            if self._result is not None:
                top_n = self._current_top_n()
                if path.lower().endswith(".json"):
                    content = to_json(self._result, top_n=top_n)
                elif path.lower().endswith(".txt"):
                    content = to_text(self._result, top_n=top_n)
                else:
                    content = to_markdown(self._result, top_n=top_n)
            else:
                content = compare_to_markdown(self._compare_results)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._status_label.configure(text=f"报告已导出：{path}")
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))

    def _on_copy_summary(self) -> None:
        if self._result is None:
            return
        summary = brief_summary(self._result, top_n=self._current_top_n())
        self.clipboard_clear()
        self.clipboard_append(summary)
        self._status_label.configure(
            text="摘要已复制到剪贴板，可直接粘贴投喂 AI 助手")

    def _show_charts(self) -> None:
        if self._result is None and not self._compare_results:
            return
        if self._chart_window is not None and self._chart_window.winfo_exists():
            self._chart_window.destroy()
        # 懒加载：首次点击图表时才导入 matplotlib（修复缺陷#9）
        from log_ai_compressor.gui.charts import (
            ChartsPanel,
            CompareChartsPanel,
        )
        self._chart_window = ctk.CTkToplevel(self)
        if self._compare_results:
            # 修复缺陷#10：对比模式展示两文件错误对比图表
            self._chart_window.title("错误对比图表")
            self._chart_window.geometry("1150x460")
            CompareChartsPanel(self._chart_window, self._compare_results)
        else:
            self._chart_window.title("错误统计图表")
            self._chart_window.geometry("1150x430")
            ChartsPanel(
                self._chart_window, self._result,
                on_select_level=self._select_by_level,
                on_select_module=self._select_by_module)

    def _select_by_level(self, level: str) -> None:
        for i, c in enumerate(self._displayed):
            if c.level == level:
                self._select_cluster(i)
                return

    def _select_by_module(self, module: str) -> None:
        for i, c in enumerate(self._displayed):
            if c.module == module or (module == "(未知)" and not c.module):
                self._select_cluster(i)
                return

    # ==================================================================
    # 全屏查看（修复缺陷#7：列表 / 详情独立最大化窗口）
    # ==================================================================
    def _make_fullscreen_window(self, title: str) -> ctk.CTkToplevel:
        """创建最大化全屏窗口（ESC 键 / 关闭按钮返回主界面）。"""
        win = ctk.CTkToplevel(self)
        win.title(f"{title}（全屏 · ESC 返回）")
        try:
            win.state("zoomed")            # Windows / macOS
        except tk.TclError:
            win.attributes("-fullscreen", True)   # Linux 回退
        win.bind("<Escape>", lambda e: win.destroy())
        win.after(60, win.focus_set)       # 抢焦点以接收 ESC
        return win

    def _open_list_fullscreen(self) -> None:
        """错误分类列表全屏：搜索过滤 + 滚动，点击行联动主界面详情。

        修复缺陷#10：对比模式下展示差异列表（+ 新增 / - 消失 / = 共同）。
        """
        compare_mode = bool(self._compare_results) and not self._displayed
        if not self._displayed and not compare_mode:
            messagebox.showinfo("提示", "请先完成一次分析再使用全屏")
            return
        win = self._make_fullscreen_window("错误分类列表" if not compare_mode
                                           else "对比差异列表")
        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text="搜索：").pack(side="left", padx=(12, 4))
        search_var = tk.StringVar()
        search = ctk.CTkEntry(bar, textvariable=search_var,
                              placeholder_text="按摘要 / 模块 / 级别过滤…")
        search.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=8)
        count_label = ctk.CTkLabel(bar, text="", text_color="#8fa4b8")
        count_label.pack(side="right", padx=12)
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=win.destroy).pack(side="right", padx=(0, 12))
        if compare_mode:
            # 图例常驻顶栏（对比模式）
            ctk.CTkLabel(bar,
                         text="+ 新增  - 消失  = 共同",
                         text_color="#8fa4b8").pack(side="right", padx=12)
        list_area = ctk.CTkScrollableFrame(win)
        list_area.pack(fill="both", expand=True)
        fs_rows: List[dict] = []

        def paint_row(idx: int, color) -> None:
            for row in fs_rows:
                if row["idx"] == idx:
                    row["frame"].configure(fg_color=color)
                    row["summary"].configure(
                        bg=self._resolve_row_color(color))

        def fs_select(idx: int) -> None:
            # 联动主界面详情 + 全屏窗口内高亮
            self._select_cluster(idx)
            for row in fs_rows:
                color = (_ROW_BG_SELECTED if row["idx"] == idx
                         else _ROW_BG_DEFAULT)
                row["frame"].configure(fg_color=color)
                row["summary"].configure(
                    bg=self._resolve_row_color(color))

        def render(keyword: str = "") -> None:
            kw = keyword.strip().lower()
            for child in list_area.winfo_children():
                child.destroy()
            fs_rows.clear()
            shown = 0
            if compare_mode:
                total = 0
                for kind, cmp, item in self._iter_compare_rows():
                    total += 1
                    hay = (f"{item.summary} {item.level} {item.module} "
                           f"{self._CMP_SYMBOL[kind]}").lower()
                    if kw and kw not in hay:
                        continue
                    if total > 500:
                        continue
                    shown += 1
                    fs_rows.append(
                        self._make_compare_row(list_area, kind, cmp, item))
                count_label.configure(
                    text=f"显示 {shown} / {total} 条")
                return
            for idx, cluster in enumerate(self._displayed):
                hay = (f"{cluster.summary} {cluster.module} "
                       f"{cluster.level} {cluster.priority_label}").lower()
                if kw and kw not in hay:
                    continue
                shown += 1
                fs_rows.append(
                    self._make_cluster_row(
                        list_area, idx, cluster, register=False,
                        on_select=lambda i=idx: fs_select(i),
                        on_hover=lambda hovered, i=idx: paint_row(
                            i, _ROW_BG_HOVER if hovered else _ROW_BG_DEFAULT)))
            count_label.configure(
                text=f"显示 {shown} / {len(self._displayed)} 条")

        def on_resize(event) -> None:
            wrap = max(240, event.width - 60)
            for row in fs_rows:
                row["summary"].configure(wraplength=wrap)

        # 文本变化即过滤（trace 不依赖键盘事件，无焦点也可靠触发）
        search_var.trace_add("write", lambda *a: render(search_var.get()))
        list_area.bind("<Configure>", on_resize)
        render()

    def _open_detail_fullscreen(self) -> None:
        """详情面板全屏：完整详情 + 上下左右滚动（高亮一并复制）。"""
        content = self._detail_box.get("1.0", "end").rstrip("\n")
        if not content:
            messagebox.showinfo("提示", "请先选择一个错误查看详情")
            return
        win = self._make_fullscreen_window("错误详情")
        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text="错误详情（典型样例 · 上下文 · 降噪堆栈 · "
                               "支持上下左右滚动）").pack(side="left", padx=12)
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=win.destroy).pack(side="right", padx=12, pady=8)
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas",
                                                   size=12), wrap="none")
        # 水平滚动条（垂直滚动条 CTkTextbox 自带）
        xbar = tk.Scrollbar(win, orient="horizontal", command=box.xview)
        box.configure(xscrollcommand=xbar.set)
        xbar.pack(side="bottom", fill="x")
        box.pack(fill="both", expand=True, pady=(0, 4))
        box.insert("1.0", content)
        # 复制主面板的高亮标签配置与范围（内容一致 -> 索引一致）
        for tag, color in _DETAIL_TAG_COLORS.items():
            try:
                box.tag_config(tag, foreground=color)
                ranges = self._detail_box.tag_ranges(tag)
                for i in range(0, len(ranges) - 1, 2):
                    box.tag_add(tag, str(ranges[i]), str(ranges[i + 1]))
            except (tk.TclError, AttributeError):
                pass
        box.configure(state="disabled")

    # ==================================================================
    # 状态切换 / 退出
    # ==================================================================
    def _set_running(self, running: bool) -> None:
        """按钮状态机（调用前提：self._result / _compare_results 已就位）。

        - 未开始分析：四个操作按钮全部置灰；
        - 分析进行中：仅「取消」可点击，其余三个置灰；
        - 分析完成后：「取消 / 导出报告 / 复制摘要 / 统图表」全部可点击。
        """
        if running:
            self._start_btn.configure(state="disabled")
            for btn in (self._export_btn, self._copy_btn, self._chart_btn):
                btn.configure(state="disabled")
            self._cancel_btn.configure(state="normal")
            return
        self._start_btn.configure(state="normal")
        has_result = self._result is not None
        has_any = has_result or bool(self._compare_results)
        self._cancel_btn.configure(
            state="normal" if has_any else "disabled")
        self._export_btn.configure(
            state="normal" if has_any else "disabled")
        # 修复缺陷#10：统计图表在对比模式下也可用（展示两文件错误对比图）
        self._chart_btn.configure(
            state="normal" if has_any else "disabled")
        # 复制摘要面向单次分析结果（对比模式导出报告即可）
        self._copy_btn.configure(
            state="normal" if has_result else "disabled")

    def _on_close(self) -> None:
        self._save_config()
        self.destroy()


def main() -> None:
    """GUI 启动入口。"""
    app = LogCompressorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
