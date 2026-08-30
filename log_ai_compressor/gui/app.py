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
from log_ai_compressor.constants import DEFAULT_TOP_N, HUMAN_NAME
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
from log_ai_compressor.gui.charts import ChartsPanel
from log_ai_compressor.gui.config_store import ConfigStore

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

_KW_DEFAULT = ("ERROR", "FAIL", "FATAL", "Caused by", "Exception",
               "Traceback")


def _rate_text(lps: float) -> str:
    if lps >= 10000:
        return f"{lps / 10000:.1f} 万行/秒"
    return f"{lps:.0f} 行/秒"


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
        self._queue: "queue.Queue" = queue.Queue()
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

        ctk.CTkLabel(panel, text="解析规则").grid(row=0, column=6, padx=(6, 2),
                                                  sticky="e")
        self._rule_menu = ctk.CTkOptionMenu(panel, values=list(RULE_NAMES),
                                            width=130)
        self._rule_menu.grid(row=0, column=7, padx=(2, 12), sticky="w")

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

        ctk.CTkLabel(panel, text="错误分类列表（按优先级降序）",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=10, pady=(8, 2), sticky="w")
        ctk.CTkLabel(panel, text="详情（典型样例 · 上下文 · 降噪堆栈）",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, padx=10, pady=(8, 2), sticky="w")

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
            self._detail_box.tag_config("kw", foreground="#ff6b6b")
            self._detail_box.tag_config("bstack", foreground="#ffd54f")
            self._detail_box.tag_config("meta", foreground="#8fa4b8")
            self._detail_box.tag_config("header", foreground="#4dd0e1")
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
            context_lines=5,
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
        self._cancel_event.set()
        self._progress_label.configure(text="正在取消…")
        self._cancel_btn.configure(state="disabled")

    def _poll_queue(self) -> None:
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
        self.after(80, self._poll_queue)

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
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(1.0)
        self._result = result
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
        """左侧错误列表：Top N 行（图标 + 优先级 + 次数 + 摘要）。"""
        assert self._result is not None
        n = self._current_top_n()
        self._displayed = self._result.clusters[:n]
        for child in self._cluster_list.winfo_children():
            child.destroy()
        if not self._displayed:
            ctk.CTkLabel(self._cluster_list, text="未发现符合条件的错误",
                         text_color="#8fa4b8").pack(pady=20)
            return
        for idx, cluster in enumerate(self._displayed):
            row = ctk.CTkButton(
                self._cluster_list, anchor="w", height=30, corner_radius=4,
                text=self._row_text(cluster),
                text_color=self._row_color(cluster),
                font=ctk.CTkFont(family="Consolas", size=12),
                fg_color=("gray86", "gray22"),
                hover_color=("gray78", "gray30"),
                command=lambda i=idx: self._select_cluster(i))
            row.pack(fill="x", padx=4, pady=1)
        total = len(self._result.clusters)
        if total > n:
            ctk.CTkLabel(self._cluster_list,
                         text=f"…… 其余 {total - n} 种错误可通过调大 Top N 查看",
                         text_color="#8fa4b8", font=ctk.CTkFont(size=11)
                         ).pack(pady=6)

    @staticmethod
    def _row_text(cluster: ErrorCluster) -> str:
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
        return (f"{icon} {cluster.priority_label} {cluster.level:<5} "
                f"\u00d7{cluster.count:<4} "
                f"{LogCompressorApp._clip(cluster.summary, 52)}")

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
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(1.0)
        self._progress_label.configure(text="对比完成")
        self._compare_results = results
        self._result = None

        box = self._detail_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", "【多文件对比结果】\n", "header")
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
        box.insert("end", "\n提示：点击「导出报告」保存完整对比差异报告\n", "meta")
        box.configure(state="disabled")
        for child in self._cluster_list.winfo_children():
            child.destroy()
        ctk.CTkLabel(self._cluster_list, text="对比模式：差异摘要见右侧详情",
                     text_color="#8fa4b8").pack(pady=20)
        self._status_label.configure(
            text=f"对比完成：{' vs '.join([cmp.other_name for cmp in results])}")
        self._save_config()

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
        if self._result is None:
            return
        if self._chart_window is not None and self._chart_window.winfo_exists():
            self._chart_window.destroy()
        self._chart_window = ctk.CTkToplevel(self)
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
    # 状态切换 / 退出
    # ==================================================================
    def _set_running(self, running: bool) -> None:
        """按钮状态机：运行中禁用全部；完成后按是否有结果分级启用。"""
        if running:
            for btn in (self._start_btn, self._cancel_btn, self._export_btn,
                        self._copy_btn, self._chart_btn):
                btn.configure(state="disabled")
            self._cancel_btn.configure(state="normal")
            return
        self._start_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        has_result = self._result is not None
        has_any = has_result or bool(self._compare_results)
        self._export_btn.configure(
            state="normal" if has_any else "disabled")
        self._copy_btn.configure(
            state="normal" if has_result else "disabled")
        self._chart_btn.configure(
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
