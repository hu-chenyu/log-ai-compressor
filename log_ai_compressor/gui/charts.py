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

import customtkinter as ctk

# 中文字体回退链（Windows/macOS/Linux 常见中文字体优先，缺字警告消除）
from matplotlib import rcParams
rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC",
                               "Noto Sans CJK SC", "WenQuanYi Micro Hei",
                               "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False   # 负号字形兼容

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


def _clip(text: str, width: int) -> str:
    """摘要截断（条形图 y 轴标签用，优化缺陷R63）。"""
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[:width - 1] + "…"


class ChartsPanel:
    """统计图表面板（优化缺陷R64：分页单图切换）。

    顶部 CTkSegmentedButton 切换「时间趋势 / 级别占比 / 种类
    Top10」，每张图独占整个窗口 —— 三图并排时饼图小扇区标签全
    部叠字、条形图 y 轴摘要挤在一起的布局缺陷彻底消除；
    饼图小扇区改右侧图例（级别+次数+百分比），扇内仅 ≥4% 显示
    百分比（99%/1% 同样清晰）。
    """

    TABS = ("时间趋势", "级别占比", "种类 Top 10")

    def __init__(self, parent, result: AnalysisResult,
                 on_select_level: Optional[Callable[[str], None]] = None,
                 on_select_cluster: Optional[Callable[[str], None]] = None,
                 dpi_scale: float = 1.0):
        self._result = result
        self._on_level = on_select_level
        self._on_cluster = on_select_cluster

        # 顶部切换栏（蓝色选中段，与应用主题一致）
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(10, 0))
        self._switch = ctk.CTkSegmentedButton(
            bar, values=list(self.TABS), command=self._on_tab)
        self._switch.set(self.TABS[0])
        self._switch.pack()

        self._body = ctk.CTkFrame(parent, fg_color="transparent")
        self._body.pack(fill="both", expand=True)

        dpi = int(round(96 * max(1.0, dpi_scale)))
        self.figure = Figure(figsize=(10.5, 4.6), dpi=dpi,
                              facecolor="#2b2b30")
        self.canvas = FigureCanvasTkAgg(self.figure, master=self._body)
        self.canvas.get_tk_widget().pack(fill="both", expand=True,
                                         padx=4, pady=4)
        self.canvas.mpl_connect("pick_event", self._on_pick)
        self._ax = None
        self._tab = None
        self._show_tab(self.TABS[0])

    # ------------------------------------------------------------------
    def _on_tab(self, name: str) -> None:
        self._show_tab(name)

    def _show_tab(self, name: str) -> None:
        """切换分页：清图重建单轴大图（画布复用，无重复建窗开销）。"""
        self._tab = name
        self.figure.clear()
        self._ax = self.figure.add_subplot(111)
        self._style_axes(self._ax)
        if name == "时间趋势":
            self.figure.subplots_adjust(
                left=0.08, right=0.97, top=0.90, bottom=0.15)
            self._draw_trend(self._ax)
        elif name == "级别占比":
            self.figure.subplots_adjust(
                left=0.02, right=0.72, top=0.92, bottom=0.06)
            self._draw_pie(self._ax)
        else:
            self.figure.subplots_adjust(
                left=0.30, right=0.95, top=0.90, bottom=0.10)
            self._draw_top_clusters(self._ax)
        self.canvas.draw_idle()

    def _style_axes(self, ax) -> None:
        ax.set_facecolor("#2b2b30")
        for spine in ax.spines.values():
            spine.set_color("#55555c")
        ax.tick_params(colors="#bdbdbd", labelsize=10)
        ax.title.set_color("#e0e0e0")
        ax.title.set_fontsize(13)

    # ------------------------------------------------------------------
    def _draw_trend(self, ax) -> None:
        """错误时间趋势折线图（单位时间错误数）+ 爆发点标注。"""
        series = self._result.global_hist.series()
        ax.set_title("错误时间趋势")
        if not series:
            ax.text(0.5, 0.5, "无时间戳数据", ha="center", va="center",
                    color="#9e9e9e", fontsize=12, transform=ax.transAxes)
            return
        xs = [_fmt_time(t) for t, _ in series]
        ys = [c for _, c in series]
        ax.plot(range(len(xs)), ys, color=_ACCENT, linewidth=1.8,
                marker="o", markersize=5)
        step = max(1, len(xs) // 10)
        ax.set_xticks(range(0, len(xs), step))
        ax.set_xticklabels(xs[::step], rotation=30, ha="right")
        ax.set_ylabel("错误数/单位时间", fontsize=10)
        # 集中爆发点标注（红色竖虚线）
        for t, _ in self._result.global_hist.burst_buckets():
            idx = next((i for i, (bt, _) in enumerate(series)
                        if bt <= t < bt + self._result.global_hist.width), None)
            if idx is not None:
                ax.axvline(idx, color="#e53935", linestyle="--",
                           linewidth=1, alpha=0.7)
        ax.grid(axis="y", color="#3a3a40", linewidth=0.6)

    def _draw_pie(self, ax) -> None:
        """错误级别占比饼图（按簇次数加权）。

        优化缺陷R64：99% 占比场景小扇区标签不再叠字 —— 扇内仅
        ≥4% 显示百分比，全部级别信息移至右侧图例（级别+次数+
        百分比，≥1% 保留一位小数）。
        """
        ax.set_title("错误级别占比", x=0.85)
        counts: List[Tuple[str, int]] = []
        for level in ("FATAL", "ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
            total = sum(c.count for c in
                        self._result.clusters_of_level(level))
            if total:
                counts.append((level, total))
        if not counts:
            ax.text(0.5, 0.5, "无错误数据", ha="center", va="center",
                    color="#9e9e9e", fontsize=12, transform=ax.transAxes)
            return
        values = [v for _, v in counts]
        total = sum(values)
        colors = [_LEVEL_COLORS.get(lv, "#78909c") for lv, _ in counts]
        wedges, _, autotexts = ax.pie(
            values, colors=colors,
            autopct=lambda p: f"{p:.0f}%" if p >= 4 else "",
            pctdistance=0.72,
            textprops={"color": "#e0e0e0", "fontsize": 10},
            startangle=90, counterclock=False, radius=1.25,
            wedgeprops={"linewidth": 1, "edgecolor": "#2b2b30",
                        "picker": True})
        for t in autotexts:
            t.set_color("#ffffff")
            t.set_fontsize(11)
        for wedge, (lv, _) in zip(wedges, counts):
            wedge.set_gid(f"level:{lv}")
        legend_labels = [f"{lv} ({v})   {v / total * 100:.1f}%"
                         for lv, v in counts]
        ax.legend(wedges, legend_labels, loc="center left",
                  bbox_to_anchor=(1.02, 0.5), fontsize=11, frameon=False,
                  labelcolor="#e0e0e0", title="级别（次数）占比",
                  title_fontsize=11)

    def _draw_top_clusters(self, ax) -> None:
        """错误种类 Top 10 横向条形（按次数降序、级别着色、点击定位）。

        优化缺陷R64：独占整页后左侧空间充足，摘要标签放宽到 40 字
        （不再截断挤叠）；条端次数标注；gid=cluster:<id> 点击联动。
        """
        ax.set_title("错误种类 Top 10（点击定位）")
        items = sorted(self._result.clusters, key=lambda c: c.count,
                       reverse=True)[:10]
        if not items:
            ax.text(0.5, 0.5, "无错误数据", ha="center", va="center",
                    color="#9e9e9e", fontsize=12, transform=ax.transAxes)
            return
        names = [_clip(c.summary, 40) for c in items]
        values = [c.count for c in items]
        colors = [_LEVEL_COLORS.get(c.level, "#78909c") for c in items]
        bars = ax.barh(range(len(items)), values, color=colors,
                       height=0.62, picker=True)
        ax.set_yticks(range(len(items)))
        ax.set_yticklabels(names, fontsize=10)
        ax.invert_yaxis()
        for bar, c in zip(bars, items):
            bar.set_gid(f"cluster:{c.cluster_id}")
        for rect, v in zip(bars, values):
            ax.annotate(str(v), xy=(rect.get_width(), rect.get_y()
                                    + rect.get_height() / 2),
                        xytext=(3, 0), textcoords="offset points",
                        va="center", fontsize=9, color="#e0e0e0")
        ax.grid(axis="x", color="#3a3a40", linewidth=0.6)

    # ------------------------------------------------------------------
    def _on_pick(self, event) -> None:
        """点击联动：饼图 -> 级别过滤；种类条形 -> 定位簇（R63）。"""
        artist = event.artist
        gid = getattr(artist, "get_gid", lambda: None)()
        if not gid:
            return
        kind, _, value = gid.partition(":")
        if kind == "level" and self._on_level:
            self._on_level(value)
        elif kind == "cluster" and self._on_cluster:
            self._on_cluster(value)

    def refresh(self) -> None:
        self.canvas.draw_idle()


class CompareChartsPanel:
    """多文件对比统计图（修复缺陷#10）。

    左图：差异概览柱状图（新增 / 消失 / 共同的种类数，按对比对分组）；
    右图：Top 差异项次数对比（横向双色条形，基准 A vs 对比 B）。
    """

    def __init__(self, parent, results: List, dpi_scale: float = 1.0):
        self._results = results
        # 优化缺陷R63：高 DPI 适配（与分析图表同口径）
        dpi = int(round(96 * max(1.0, dpi_scale)))
        self.figure = Figure(figsize=(11, 3.8), dpi=dpi,
                              facecolor="#2b2b30")
        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)
        self._build()
        self.canvas.get_tk_widget().pack(fill="both", expand=True,
                                         padx=4, pady=4)

    # ------------------------------------------------------------------
    def _style_axes(self, ax) -> None:
        ax.set_facecolor("#2b2b30")
        for spine in ax.spines.values():
            spine.set_color("#55555c")
        ax.tick_params(colors="#bdbdbd", labelsize=8)
        ax.title.set_color("#e0e0e0")
        ax.title.set_fontsize(10)

    def _build(self) -> None:
        gs = self.figure.add_gridspec(1, 2, wspace=0.30,
                                      left=0.10, right=0.97,
                                      top=0.86, bottom=0.22)
        self._ax_overview = self.figure.add_subplot(gs[0, 0])
        self._ax_items = self.figure.add_subplot(gs[0, 1])
        self._draw_overview()
        self._draw_items()
        self.figure.suptitle("多文件错误对比（新增 + / 消失 - / 共同 =）",
                             color="#e0e0e0", fontsize=11)

    def _draw_overview(self) -> None:
        """差异概览：每个对比对的新增/消失/共同错误种类数。"""
        ax = self._ax_overview
        self._style_axes(ax)
        ax.set_title("差异概览（错误种类数）")
        labels = [f"{r.base_name[:8]} vs\n{r.other_name[:8]}"
                  for r in self._results]
        new_counts = [len(r.new_items) for r in self._results]
        gone_counts = [len(r.gone_items) for r in self._results]
        common_counts = [len(r.common_items) for r in self._results]
        if not labels:
            ax.text(0.5, 0.5, "无对比数据", ha="center", va="center",
                    color="#9e9e9e", transform=ax.transAxes)
            return
        width = 0.27 if len(labels) > 1 else 0.5
        xs = range(len(labels))
        b1 = ax.bar([x - width for x in xs], new_counts, width,
                    color="#66bb6a", label="+ 新增")
        b2 = ax.bar(list(xs), gone_counts, width,
                    color="#ef5350", label="- 消失")
        b3 = ax.bar([x + width for x in xs], common_counts, width,
                    color="#78909c", label="= 共同")
        # 柱顶数值标注
        for bars in (b1, b2, b3):
            for rect in bars:
                h = rect.get_height()
                if h > 0:
                    ax.annotate(f"{int(h)}",
                                xy=(rect.get_x() + rect.get_width() / 2, h),
                                xytext=(0, 2), textcoords="offset points",
                                ha="center", fontsize=7, color="#e0e0e0")
        ax.set_xticks(list(xs))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_ylabel("错误种类数", fontsize=8)
        ax.legend(fontsize=8, labelcolor="#e0e0e0", framealpha=0.2)
        ax.grid(axis="y", color="#3a3a40", linewidth=0.6)

    def _draw_items(self) -> None:
        """Top 差异项次数对比：基准 A（橙） vs 对比 B（青）。"""
        ax = self._ax_items
        self._style_axes(ax)
        ax.set_title("Top 差异项错误次数（基准 vs 对比）")
        # 汇总第一个对比对的差异项（新增/消失/变化最大的共同项）
        rows = []
        if self._results:
            cmp = self._results[0]
            for i in cmp.new_items[:8]:
                rows.append((i.summary[:26], i.count_a, i.count_b, "+"))
            for i in cmp.gone_items[:8]:
                rows.append((i.summary[:26], i.count_a, i.count_b, "-"))
            # 共同项中变化最大的（按次数差排序）
            changed = sorted(cmp.common_items,
                             key=lambda i: abs(i.count_b - i.count_a),
                             reverse=True)[:8]
            for i in changed:
                rows.append((i.summary[:26], i.count_a, i.count_b, "="))
        if not rows:
            ax.text(0.5, 0.5, "无差异项", ha="center", va="center",
                    color="#9e9e9e", transform=ax.transAxes)
            return
        rows = rows[:12]  # 上限保护
        names = [f"{sym} {s}" for s, _, _, sym in rows]
        a_vals = [a for _, a, _, _ in rows]
        b_vals = [b for _, _, b, _ in rows]
        ys = range(len(rows))
        ax.barh([y + 0.19 for y in ys], a_vals, height=0.36,
                color="#ffa726", label=f"基准 {self._results[0].base_name[:10]}")
        ax.barh([y - 0.19 for y in ys], b_vals, height=0.36,
                color="#26c6da", label=f"对比 {self._results[0].other_name[:10]}")
        ax.set_yticks(list(ys))
        ax.set_yticklabels(names, fontsize=7)
        ax.invert_yaxis()
        ax.legend(fontsize=8, labelcolor="#e0e0e0", framealpha=0.2)
        ax.grid(axis="x", color="#3a3a40", linewidth=0.6)
