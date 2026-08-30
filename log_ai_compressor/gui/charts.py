# -*- coding: utf-8 -*-
"""Matplotlib 统计图表：错误时间趋势 / 级别占比 / 模块分布。

设计说明：
- 使用 Figure（非 pyplot）+ FigureCanvasTkAgg 嵌入 Tk，避免 pyplot
  全局状态与多窗口问题；
- 三图支持 pick 事件联动：点击饼图区块（级别）或柱状图（模块）
  回调跳转错误列表；
- 趋势图自动标注集中爆发时间点（与智能分析口径一致）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional, Tuple

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from log_ai_compressor.core.models import AnalysisResult

# 配色（暗色主题友好）
_LEVEL_COLORS = {
    "FATAL": "#e53935",
    "ERROR": "#ef5350",
    "FAIL": "#ff7043",
    "WARN": "#ffb300",
    "INFO": "#4dd0e1",
    "DEBUG": "#78909c",
    "TRACE": "#90a4ae",
}
_ACCENT = "#26c6da"


def _fmt_time(t: float) -> str:
    """横轴刻度：epoch -> 时:分；相对秒 -> 原值。"""
    if t >= 1e9:
        return datetime.fromtimestamp(t).strftime("%H:%M:%S")
    return f"{t:.0f}s"


class ChartsPanel:
    """三联统计图表面板（错误趋势折线 / 级别占比饼图 / 模块分布柱状图）。"""

    def __init__(self, parent, result: AnalysisResult,
                 on_select_level: Optional[Callable[[str], None]] = None,
                 on_select_module: Optional[Callable[[str], None]] = None):
        self._result = result
        self._on_level = on_select_level
        self._on_module = on_select_module

        self.figure = Figure(figsize=(11, 3.6), dpi=96,
                              facecolor="#2b2b30")
        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)
        self._build()
        self.canvas.get_tk_widget().pack(fill="both", expand=True,
                                         padx=4, pady=4)
        self.canvas.mpl_connect("pick_event", self._on_pick)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        gs = self.figure.add_gridspec(1, 3, wspace=0.35,
                                      left=0.07, right=0.97,
                                      top=0.86, bottom=0.18)
        self._ax_trend = self.figure.add_subplot(gs[0, 0])
        self._ax_pie = self.figure.add_subplot(gs[0, 1])
        self._ax_bar = self.figure.add_subplot(gs[0, 2])
        self._draw_trend()
        self._draw_pie()
        self._draw_bar()
        self.figure.suptitle("错误统计面板（点击饼图/柱状图可联动错误列表）",
                             color="#e0e0e0", fontsize=11)

    def _style_axes(self, ax) -> None:
        ax.set_facecolor("#2b2b30")
        for spine in ax.spines.values():
            spine.set_color("#55555c")
        ax.tick_params(colors="#bdbdbd", labelsize=8)
        ax.title.set_color("#e0e0e0")
        ax.title.set_fontsize(10)

    # ------------------------------------------------------------------
    def _draw_trend(self) -> None:
        """错误时间趋势折线图（单位时间错误数）+ 爆发点标注。"""
        ax = self._ax_trend
        self._style_axes(ax)
        series = self._result.global_hist.series()
        ax.set_title("错误时间趋势")
        if not series:
            ax.text(0.5, 0.5, "无时间戳数据", ha="center", va="center",
                    color="#9e9e9e", transform=ax.transAxes)
            return
        xs = [_fmt_time(t) for t, _ in series]
        ys = [c for _, c in series]
        ax.plot(range(len(xs)), ys, color=_ACCENT, linewidth=1.4,
                marker="o", markersize=3)
        step = max(1, len(xs) // 6)
        ax.set_xticks(range(0, len(xs), step))
        ax.set_xticklabels(xs[::step], rotation=30, ha="right")
        ax.set_ylabel("错误数/单位时间", fontsize=8)
        # 集中爆发点标注（红色竖虚线）
        for t, _ in self._result.global_hist.burst_buckets():
            idx = next((i for i, (bt, _) in enumerate(series)
                        if bt <= t < bt + self._result.global_hist.width), None)
            if idx is not None:
                ax.axvline(idx, color="#e53935", linestyle="--",
                           linewidth=1, alpha=0.7)
        ax.grid(axis="y", color="#3a3a40", linewidth=0.6)

    def _draw_pie(self) -> None:
        """错误级别占比饼图（按簇次数加权）。"""
        ax = self._ax_pie
        self._style_axes(ax)
        ax.set_title("错误级别占比")
        counts: List[Tuple[str, int]] = []
        for level in ("FATAL", "ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
            total = sum(c.count for c in
                        self._result.clusters_of_level(level))
            if total:
                counts.append((level, total))
        if not counts:
            ax.text(0.5, 0.5, "无错误数据", ha="center", va="center",
                    color="#9e9e9e", transform=ax.transAxes)
            return
        labels = [f"{lv} ({v})" for lv, v in counts]
        colors = [_LEVEL_COLORS.get(lv, "#78909c") for lv, _ in counts]
        wedges, texts = ax.pie(
            [v for _, v in counts], labels=labels, colors=colors,
            textprops={"color": "#e0e0e0", "fontsize": 8},
            startangle=90, counterclock=False,
            wedgeprops={"linewidth": 1, "edgecolor": "#2b2b30",
                        "picker": True})
        for wedge, (lv, _) in zip(wedges, counts):
            wedge.set_gid(f"level:{lv}")

    def _draw_bar(self) -> None:
        """模块错误分布柱状图（Top 10，按次数加权）。"""
        ax = self._ax_bar
        self._style_axes(ax)
        ax.set_title("模块错误分布（Top 10）")
        module_counts = {}
        for c in self._result.clusters:
            key = c.module or "(未知)"
            module_counts[key] = module_counts.get(key, 0) + c.count
        items = sorted(module_counts.items(), key=lambda kv: kv[1],
                       reverse=True)[:10]
        if not items:
            ax.text(0.5, 0.5, "无模块数据", ha="center", va="center",
                    color="#9e9e9e", transform=ax.transAxes)
            return
        names = [k[:14] for k, _ in items]
        values = [v for _, v in items]
        bars = ax.barh(range(len(items)), values, color=_ACCENT,
                       height=0.6, picker=True)
        ax.set_yticks(range(len(items)))
        ax.set_yticklabels(names, fontsize=8)
        ax.invert_yaxis()
        for bar, (name, _) in zip(bars, items):
            bar.set_gid(f"module:{name}")
        ax.grid(axis="x", color="#3a3a40", linewidth=0.6)

    # ------------------------------------------------------------------
    def _on_pick(self, event) -> None:
        """点击联动：饼图 -> 级别过滤；柱状图 -> 模块定位。"""
        artist = event.artist
        gid = getattr(artist, "get_gid", lambda: None)()
        if not gid:
            return
        kind, _, value = gid.partition(":")
        if kind == "level" and self._on_level:
            self._on_level(value)
        elif kind == "module" and self._on_module:
            self._on_module(value)

    def refresh(self) -> None:
        self.canvas.draw_idle()
