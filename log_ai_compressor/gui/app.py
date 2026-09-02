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
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk

from log_ai_compressor import __version__
from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_SELECTED_LEVELS,
    DEFAULT_TOP_N,
    HUMAN_NAME,
    MAX_CONTEXT_LINES,
    MIN_CONTEXT_LINES,
)
from log_ai_compressor.core.analysis import simplify_stack
from log_ai_compressor.core.comparator import CompareResult, compare_files
from log_ai_compressor.core.models import (
    AnalysisResult,
    ClusterInstance,
    ErrorCluster,
    format_timestamp,
)
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

# 修复缺陷R10：级别过滤增加 FATAL（最严重的级别放最前面）
LEVEL_CHECKS = ("FATAL", "ERROR", "FAIL", "WARN", "INFO", "DEBUG")
RULE_NAMES = ("generic", "embedded", "jenkins")
_ANOMALY_LABELS = {"burst": "集中爆发", "rare": "罕见异常"}

# 优化：六个级别复选框旁的 ⓘ 悬停说明（每个级别对应自己的解释）
_LEVEL_HELP = {
    "FATAL": "FATAL：致命错误，程序无法继续运行的严重故障，"
             "严重程度高于ERROR",
    "ERROR": "ERROR：错误，程序运行中出现的异常，"
             "可能导致功能异常但程序仍可继续运行",
    "FAIL": "FAIL：失败，操作或测试未成功完成的结果",
    "WARN": "WARN：警告，可能存在问题但不影响程序正常运行，"
            "需要关注",
    "INFO": "INFO：信息，程序正常运行时的一般性记录",
    "DEBUG": "DEBUG：调试，开发调试用的详细信息，"
             "通常生产环境不显示",
}

# 修复缺陷R10：字体大小档位（配置区「字体大小」选择器，持久化）
# - 「中」为基准档（主列表头部 22 加粗 / 摘要 18；全屏 28/24/20）
# - 其余档位按系数整体缩放（经典 / 虚拟 / 全屏 / 对比同步生效）
FONT_SIZE_OPTIONS = ("小", "中", "大", "特大")
FONT_SIZE_SCALE = {"小": 0.85, "中": 1.0, "大": 1.15, "特大": 1.3}

# 错误行智能图标：▲ 根因 / ● 爆发 / ○ 稀有 / ◆ 致命
_CLUSTER_ICON = {"fatal": "\u25c6", "root": "\u25b2", "burst": "\u25cf",
                 "rare": "\u25cb", "normal": "\u2022"}

# ---------------------------------------------------------------------------
# 修复缺陷R1：四态主题体系（亮色 → 暗色 → 蓝调 → 绿调 循环）
# ---------------------------------------------------------------------------
# 主色调 #3B82F6（舒适蓝）；蓝调/绿调为柔和浅色渐变近似（Tk 无原生渐变，
# 以「窗口层浅色 + 卡片层更浅」双层色营造层次）。
# - window: 根/边栏背景   card: 卡片/输入区背景   header: 标题栏
# - accent: 主按钮底色 / accent_text: 主按钮文字 / accent_hover: 悬停
# - muted: 次要文字（提示/说明）
# - row_*: 错误列表行 背景/悬停/选中/文字
# 修复缺陷R12：splitter/splitter_grip —— 错误列表与详情面板间的
# 可拖动分隔条底色与握点色（半透明观感的灰系，四态各自协调）
THEMES: Dict[str, Dict[str, str]] = {
    "light": {
        # 修复缺陷R16：icon 不带 FE0F —— Tk 会把变体选择符渲染成
        # 36 物理px 的空白尾迹，"☀️" 的 advance（62）远大于其他图标
        # （30），advance 盒居中后可见太阳被推到左半边（视觉偏左 18px）。
        # 去掉 FE0F 字形不变（同高 32px），advance 26 与其他一致，居中对齐
        "name": "☀ 亮色", "icon": "☀", "label": "亮色",
        "window": "#eef1f6", "card": "#ffffff",
        "header": "#ffffff", "text": "#1f2937", "muted": "#6b7280",
        "accent": "#3B82F6", "accent_hover": "#2563EB", "accent_text": "#ffffff",
        "row_bg": "#ffffff", "row_hover": "#eff6ff", "row_selected": "#bfdbfe",
        "row_text": "#2d333b", "is_dark": "0",
        "splitter": "#c3ccd9", "splitter_grip": "#8a97a8",
    },
    "dark": {
        "name": "🌙 暗色", "icon": "🌙", "label": "暗色",
        "window": "#111827", "card": "#1c2433",
        "header": "#161e2d", "text": "#e5e7eb", "muted": "#94a3b8",
        "accent": "#3B82F6", "accent_hover": "#60a5fa", "accent_text": "#ffffff",
        "row_bg": "#1c2433", "row_hover": "#2a3547", "row_selected": "#1d4ed8",
        "row_text": "#c8cdd4", "is_dark": "1",
        "splitter": "#3a485e", "splitter_grip": "#64758f",
    },
    "blue": {
        "name": "🔵 蓝调", "icon": "🔵", "label": "蓝调",
        "window": "#cfe3fa", "card": "#e8f1fd",
        "header": "#bcd6f5", "text": "#173a63", "muted": "#486e9c",
        "accent": "#ffffff", "accent_hover": "#f4f9ff", "accent_text": "#1d4ed8",
        "row_bg": "#e8f1fd", "row_hover": "#cfe0f5", "row_selected": "#8cbaf0",
        "row_text": "#173a63", "is_dark": "0",
        "splitter": "#a9c4e4", "splitter_grip": "#6d95c2",
    },
    "green": {
        "name": "🟢 绿调", "icon": "🟢", "label": "绿调",
        "window": "#cdeeda", "card": "#e6f7ec",
        "header": "#b7e3c8", "text": "#14432a", "muted": "#3f7d59",
        "accent": "#ffffff", "accent_hover": "#f2fbf6", "accent_text": "#15803d",
        "row_bg": "#e6f7ec", "row_hover": "#cdecd9", "row_selected": "#8fdcab",
        "row_text": "#14432a", "is_dark": "0",
        "splitter": "#a6d2b9", "splitter_grip": "#5f9c7c",
    },
}
# 主题顺序（下拉列表展示顺序；修复缺陷R13 后不再循环点击）
THEME_ORDER = ("light", "dark", "blue", "green")
# 修复缺陷R14：主题下拉图标列固定宽（逻辑 px，CTk 自动随 DPI 缩放）
# —— 四个 emoji（☀️🌙🔵🟢）字形宽度不一，固定图标列后文字列起始
# 位置完全一致（垂直对齐）；选择框与弹窗共用同一列宽
_THEME_ICON_COL = 28
# 兼容别名（旧配置 appearance 值）
_THEME_ALIASES = {"dark": "dark", "light": "light", "blue": "blue",
                  "green": "green"}

_KW_DEFAULT = ("ERROR", "FAIL", "FATAL", "Caused by", "Exception",
               "Traceback")

# 修复缺陷R6：主列表虚拟滚动阈值（超过则切换池化虚拟渲染）
VIRTUAL_LIST_THRESHOLD = 40

# 修复缺陷R12：错误列表 | 详情面板 可拖动分隔条参数
_SPLITTER_WIDTH = 6            # 分隔条宽度（像素）
# 列最小宽兜底值（逻辑像素）—— 实际以标题栏内容动态实测为准
# （_splitter_min_widths；200/300 旧值小于标题栏内容宽导致极限遮挡）
_SPLITTER_MIN_LIST = 420       # 左列兜底（实测标题栏 ~412 + 余量）
_SPLITTER_MIN_DETAIL = 360     # 右列兜底（实测标题栏 ~325 + padx + 余量）
_SPLITTER_DEFAULT_RATIO = 0.4  # 默认左右宽度比（列表:详情 = 2:3）

# 详情文本高亮标签配色（主面板与全屏窗口共用，修复缺陷#7/R5）
# 修复缺陷R5：业务栈帧提亮加粗更明显；系统库折叠提示独立配色更清晰
_DETAIL_TAG_COLORS = {"kw": "#ff6b6b", "bstack": "#fbbf24",
                      "meta": "#8fa4b8", "fold": "#a78bfa",
                      "header": "#4dd0e1"}

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


def _widget_alive(widget) -> bool:
    """控件是否仍存活（destroy 后登记表清理用）。"""
    try:
        return bool(widget.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


class Tooltip:
    """鼠标悬停提示（CustomTkinter / 原生控件通用）。

    修复缺陷#6/#8：GUI 关键选项与标题缺少解释性说明，用户无法
    理解「典型样例」「解析规则」等术语的含义。

    修复缺陷R3：原实现字体 10 号过小、定位固定在控件右下方，
    悬停主窗口右上角的 ⓘ 时文本框溢出屏幕右边界看不到完整内容。
    现改为：字体 12 号、宽 420px 自动换行、弹出位置智能判断
    （近右边缘向左弹 / 近下边缘向上弹）、白底深字 + 圆角阴影
    （Windows 平台 transparentcolor 实现真圆角，其他平台退化为
    白底描边矩形）。

    实现：Enter 后延迟显示无边框 Toplevel（不抢焦点、不挡操作），
    Leave / 按下时立即销毁。
    """

    # 优化：字号 18（12→15 用户仍反馈小）；宽度 420（不截断自动换行）
    _FONT = ("Microsoft YaHei UI", 18)
    _WRAP = 420
    _BG = "#ffffff"          # 浅色背景
    _FG = "#1f2937"          # 深色文字
    _BORDER = "#c9d3de"      # 描边
    _SHADOW1 = "#b9c3cf"     # 阴影内层
    _SHADOW2 = "#98a5b3"     # 阴影外层
    _CORNER = 10             # 圆角半径
    _MAGIC = "#ff00ff"       # 透明色（Windows -transparentcolor）
    _PAD = 14                # 文本内边距（指令：上下左右 12~16px）
    _MARGIN = 5              # 阴影外溢边距
    _DELAY_SHOW = 300        # 悬停显示延迟（ms）
    _DELAY_HIDE = 200        # 移出消失延迟（ms）

    def __init__(self, widget, text, delay: int = _DELAY_SHOW,
                 wrap: int = _WRAP):
        """text 支持静态字符串或返回字符串的可调用对象（动态提示）。

        修复缺陷#8：解析规则说明需跟随当前选中规则动态变化。
        优化：显示延迟 300ms（原 400）；移出后 200ms 延迟消失
        （原立即销毁，快速划过多个 ⓘ 时更连贯）。
        """
        self._widget = widget
        self._text = text
        self._delay = delay
        self._wrap = wrap
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        self._hide_after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide_now, add="+")

    # ------------------------------------------------------------------
    def _current_text(self) -> str:
        if callable(self._text):
            try:
                return str(self._text() or "")
            except Exception:
                return ""
        return self._text or ""

    def _schedule(self, _event=None) -> None:
        """Enter：调度显示（已显示则保持，绝不触发销毁/重建）。

        修复闪烁：CTkLabel 是复合控件（canvas + 内部 label），bind
        双注册到两层 —— 指针在图标内微动跨子窗口边界时会成对触发
        Leave/Enter（X 的 NotifyInferior 语义）。旧链路 Leave 先
        调度 200ms 延迟销毁、Enter 只重置显示调度不取消销毁 ——
        已显示的 tooltip 被销毁又在 300ms 后重建，往返一次即一次
        视觉闪烁。现在 Enter 时取消挂起的销毁；tooltip 已显示时
        直接保持（零销毁零重建）。
        """
        self._cancel()
        self._cancel_hide()
        if self._tip is not None:
            return          # 已显示：保持显示，无需重建
        self._after_id = self._widget.after(self._delay, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass  # 控件已销毁
            self._after_id = None

    @staticmethod
    def _round_rect(canvas: tk.Canvas, x1: float, y1: float,
                    x2: float, y2: float, r: float, **kw):
        """平滑多边形近似圆角矩形（smooth 插值）。"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return canvas.create_polygon(pts, smooth=True, **kw)

    def _show(self) -> None:
        if self._tip is not None:
            return
        # 优化：显示时取消挂起的延迟消失（快速划回同一 ⓘ）
        self._cancel_hide()
        text = self._current_text()
        if not text:
            return
        try:
            wx = self._widget.winfo_rootx()
            wy = self._widget.winfo_rooty()
            ww = self._widget.winfo_width()
            wh = self._widget.winfo_height()
        except tk.TclError:
            return  # 控件已销毁
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)   # 无边框
        tw.withdraw()                  # 先测量后定位再显示（无闪烁）
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        # 圆角支持：Windows 透明色（其他平台捕获异常后走描边矩形）
        rounded = self._enable_rounded(tw)
        canvas = tk.Canvas(
            tw, highlightthickness=0, bd=0,
            bg=self._MAGIC if rounded else self._BG)
        canvas.pack(fill="both", expand=True)
        # 1) 先创建文本测尺寸（tag "text" 便于测试取用）
        # 优化：15 号微软雅黑默认行高 ~1.33 倍（canvas text 无
        # spacing 选项；字号放大后行高自然舒展，多行不拥挤）
        tip_id = canvas.create_text(
            1 + self._PAD, 1 + self._PAD, text=text, anchor="nw",
            justify="left", width=self._wrap, tags="text",
            font=self._FONT, fill=self._FG)
        tw.update_idletasks()
        bx1, by1, bx2, by2 = canvas.bbox(tip_id)
        w = (bx2 - bx1) + 2 * self._PAD + 2
        h = (by2 - by1) + 2 * self._PAD + 2
        W, H = w + self._MARGIN, h + self._MARGIN
        # 2) 绘制圆角卡片 + 双层阴影（压到文本下方）
        if rounded:
            s2 = self._round_rect(canvas, 4, 4, w + 2, h + 2,
                                  self._CORNER, fill=self._SHADOW2,
                                  outline="")
            s1 = self._round_rect(canvas, 2.5, 2.5, w + 0.5, h + 0.5,
                                  self._CORNER, fill=self._SHADOW1,
                                  outline="")
            card = self._round_rect(canvas, 1, 1, w - 2, h - 2,
                                    self._CORNER, fill=self._BG,
                                    outline=self._BORDER)
            canvas.tag_lower(card)
            canvas.tag_lower(s1)
            canvas.tag_lower(s2)
        else:
            card = canvas.create_rectangle(
                1, 1, w - 1, h - 1, fill=self._BG,
                outline=self._BORDER)
            canvas.tag_lower(card)
        canvas.configure(width=W, height=H)
        # 3) 优化：默认显示在控件正上方（水平居中）—— 级别 ⓘ 基准。
        # 此前默认右下方弹出，行内靠右的 ⓘ 触发右缘换向/钳位后
        # tooltip 相对图标位置各异（用户视觉"扭曲"）。统一为：
        # 水平中心对齐控件中心，底边距控件顶边 8px；水平溢出仅
        # 左右平移（不改变"正上方"关系）；贴近屏幕上缘放不下时
        # 才回退到控件下方；最终钳制在物理屏幕内（任意方向不溢出）。
        vx, vy, vw, vh = self._screen_bounds(tw)
        x = wx + ww // 2 - W // 2      # 与控件水平居中
        y = wy - H - 8                 # 控件正上方（8px 间隙）
        if y < vy + 8:                 # 上方空间不足 → 下方
            y = wy + wh + 8
        # 最终钳制在屏幕内（水平钳制只平移，垂直关系保持）
        x = max(vx + 8, min(int(x), int(vx + vw - W - 8)))
        y = max(vy + 8, min(int(y), int(vy + vh - H - 8)))
        tw.wm_geometry(f"+{x}+{y}")
        tw.deiconify()

    def _enable_rounded(self, tw: tk.Toplevel) -> bool:
        """尝试启用透明色圆角（Windows），失败返回 False 走矩形降级。"""
        try:
            tw.configure(bg=self._MAGIC)
            tw.attributes("-transparentcolor", self._MAGIC)
            return True
        except tk.TclError:
            return False

    @staticmethod
    def _screen_bounds(tw: tk.Toplevel):
        """物理像素的屏幕边界（多显示器取虚拟屏），用于钳位。

        优化（定位修正）：winfo_screenwidth() 在高 DPI 下返回
        逻辑像素（200% 时为物理值的一半），而 tooltip 几何
        （wm_geometry / rootx / 控件宽）是物理像素 —— 两者混用
        时右侧控件的 tooltip 被钳制到「逻辑屏宽」内（实际只占
        物理屏左半），远离图标数百像素（视觉"扭曲"）。Windows
        下经 ctypes 取虚拟屏物理边界（DPI 感知进程返回物理值），
        其他平台回退 winfo（无 DPI 缩放时两者一致）。
        """
        try:
            import ctypes
            u = ctypes.windll.user32
            vx = u.GetSystemMetrics(76)      # 虚拟屏左上 x
            vy = u.GetSystemMetrics(77)      # 虚拟屏左上 y
            vw = u.GetSystemMetrics(78)      # 虚拟屏宽
            vh = u.GetSystemMetrics(79)      # 虚拟屏高
            if vw > 0 and vh > 0:
                return vx, vy, vw, vh
        except Exception:
            pass
        return 0, 0, tw.winfo_screenwidth(), tw.winfo_screenheight()

    def _hide(self, _event=None) -> None:
        """Leave：200ms 延迟消失；指针仍在控件内时忽略（防闪烁）。

        修复闪烁（第二道防线）：指针在控件内部子窗口（canvas/
        label）间微移时 Tk 会发 detail=NotifyInferior 的 Leave ——
        此时指针并未真正离开控件矩形，销毁再重建即视觉闪烁。
        Leave 到达时实测指针热点位置，仍在控件矩形内则直接忽略。
        """
        self._cancel()
        if self._pointer_inside():
            return          # 子窗口边界抖动：指针未真正离开控件
        if self._hide_after_id is not None:
            return
        self._hide_after_id = self._widget.after(
            self._DELAY_HIDE, self._destroy_tip)

    def _pointer_inside(self) -> bool:
        """指针热点是否仍在控件矩形内（屏幕物理坐标比较）。"""
        try:
            px = self._widget.winfo_pointerx()
            py = self._widget.winfo_pointery()
            x = self._widget.winfo_rootx()
            y = self._widget.winfo_rooty()
            w = self._widget.winfo_width()
            h = self._widget.winfo_height()
            return x <= px < x + w and y <= py < y + h
        except tk.TclError:
            return False    # 控件已销毁

    def _hide_now(self, _event=None) -> None:
        """按下：立即销毁（不遮挡点击）。"""
        self._cancel()
        self._cancel_hide()
        self._destroy_tip()

    def _cancel_hide(self) -> None:
        if self._hide_after_id is not None:
            try:
                self._widget.after_cancel(self._hide_after_id)
            except tk.TclError:
                pass
            self._hide_after_id = None

    def _destroy_tip(self) -> None:
        self._hide_after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class VirtualClusterList:
    """主错误列表虚拟滚动容器（修复缺陷R6）。

    背景：CTk 复合控件（Frame/Label 各含内部 Canvas）创建开销大，
    Top N 调大后一次性渲染数百行明显卡顿。

    设计：
    - 只为「可见区域 + 缓冲」创建行控件（池化），控件数量与列表
      长度无关；滚动时把池内行重新绑定到不同数据索引（复用控件）；
    - 行使用原生 tk 控件（无内部 Canvas，创建开销约为 CTk 的 1/5）；
    - 固定行高（修复缺陷R9：头部 22 加粗 + 摘要单行 18 号不换行），
      摘要超长截断（完整内容在右侧详情面板查看）；
    - 修复缺陷R9：水平滚动 —— 摘要单行完整显示，内容自然宽度
      超过视口时画布横向扩展（xscrollcommand 接底部水平滚动条，
      Shift+滚轮横向滚动）。
    """

    ROW_HEIGHT = 138       # 类默认（实际在 __init__ 按字体度量动态计算）
    BUFFER = 2             # 视口上下缓冲行数（快速滚动不露白）
    SUMMARY_CLIP = 100     # 虚拟模式摘要截断字符数

    def __init__(self, host, app):
        self._app = app
        self._host = host
        self._data: List[ErrorCluster] = []
        self._slots: List[dict] = []      # 行控件池
        self._hovered = -1
        self._content_w = 600             # 数据内容自然宽（水平滚动区域宽）
        # 修复缺陷R9：行高按实际渲染字体度量动态计算（DPI 无关，
        # 与经典模式行高一致：头部行距 + 摘要行距 + 行内边距/间隙）
        try:
            self._m_head = tkfont.Font(
                font=app._scaled_font(app._font_row_head))
            self._m_sum = tkfont.Font(
                font=app._scaled_font(app._font_row_summary))
            self.ROW_HEIGHT = (self._m_head.metrics("linespace")
                               + self._m_sum.metrics("linespace") + 44)
        except (tk.TclError, ValueError):
            self._m_head = app._font_row_head
            self._m_sum = app._font_row_summary
        p = app._palette()
        # 修复缺陷R9：滚动步进随行高走（每 2 单位 ≈ 1 行）
        self._canvas = tk.Canvas(host, highlightthickness=0,
                                 bg=p["window"],
                                 yscrollincrement=max(1, self.ROW_HEIGHT // 2),
                                 xscrollincrement=60,
                                 width=470, height=400)
        # 修复缺陷R9：垂直/水平滚动条统一用 CTkScrollbar（与整体 UI 风格一致）
        self._vbar = ctk.CTkScrollbar(host, orientation="vertical",
                                      command=self._scroll_cmd)
        self._hbar = ctk.CTkScrollbar(host, orientation="horizontal",
                                      height=14, command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=self._on_yscroll,
                               xscrollcommand=self._on_xscroll)
        # 优化（实时滚动防撕裂）：水平滚动条按下/释放 -> 快照滚动
        # 模式（可见行转画布图元，见 _on_hbar_press）
        self._xsnap = None
        try:
            self._hbar._canvas.bind("<ButtonPress-1>",
                                    self._on_hbar_press, add="+")
            self._hbar._canvas.bind("<ButtonRelease-1>",
                                    self._on_hbar_release, add="+")
        except (AttributeError, tk.TclError):
            pass
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vbar.grid(row=0, column=1, sticky="ns")
        self._hbar.grid(row=1, column=0, sticky="ew")
        # 修复缺陷R9：视口尺寸变化 -> 更新水平滚动区域并重对齐池行
        # （scrollregion 变更会经 yscrollcommand 回流 _on_yscroll ->
        # _sync，_sync 内不再改 scrollregion 且带重入护栏，防事件循环）
        self._in_sync = False
        # 优化（实时拖动快速路径）：分隔条拖动期间 _sync 只改行宽
        # （数据/滚动位置未变，跳过文本重填与事件重绑）
        self._dragging_splitter = False
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        # 修复缺陷R9（顺带修复既有缺陷）：滚轮绑定挂在画布与池内行控件
        # 自身而非 bind_all —— bind_all 会覆盖 CTkScrollableFrame 的全局
        # 滚轮处理，虚拟模式销毁后经典列表滚轮失效；控件级绑定随控件
        # 销毁自动解除，无此问题。Shift+滚轮横向滚动。
        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self._canvas.bind("<Shift-MouseWheel>", self._on_wheel_h)

    # ------------------------------------------------------------------
    # 数据 / 生命周期
    # ------------------------------------------------------------------
    def set_data(self, data: List[ErrorCluster]) -> None:
        """设置列表数据并回到顶部（含水平内容宽测量）。"""
        self._data = data
        self._hovered = -1
        # 数据更换时残留的快照图元一并清理（正常时序不会发生）
        if self._xsnap is not None:
            self._on_hbar_release(None)
        # 修复缺陷R9：内容自然宽（摘要单行不换行后的完整像素宽）
        self._content_w = self._measure_width(data)
        self._update_region()
        self._canvas.yview_moveto(0.0)
        self._canvas.xview_moveto(0.0)
        self._sync()

    def _measure_width(self, data: List[ErrorCluster]) -> int:
        """全部行的最大文本像素宽（按实际渲染的缩放字体度量）。"""
        need = 0
        for cluster in data:
            need = max(
                need,
                self._m_sum.measure(
                    self._app._clip(cluster.summary, self.SUMMARY_CLIP)),
                self._m_head.measure(self._app._row_text(cluster)))
        return need + 44          # 行内左右边距

    def _region_w(self) -> int:
        """水平滚动区域宽：内容自然宽与视口宽取大（整行高亮需占满视口）。"""
        try:
            view_w = self._canvas.winfo_width()
        except tk.TclError:
            view_w = 0
        return max(self._content_w, view_w, 200)

    def _update_region(self) -> None:
        self._canvas.configure(
            scrollregion=(0, 0, self._region_w(),
                          len(self._data) * self.ROW_HEIGHT))

    def destroy(self) -> None:
        """销毁虚拟列表（切回经典模式 / 重新分析时）。"""
        self._xsnap = None
        for slot in self._slots:
            try:
                slot["frame"].destroy()
            except tk.TclError:
                pass
        self._slots = []
        for w in (self._canvas, self._vbar, self._hbar):
            try:
                w.destroy()
            except tk.TclError:
                pass

    def apply_palette(self) -> None:
        """主题切换：画布底色 + 可见行重刷（fill 时取最新调色板）。"""
        try:
            self._canvas.configure(bg=self._app._palette()["window"])
        except (tk.TclError, ValueError):
            pass
        self._sync()

    @property
    def slots(self) -> List[dict]:
        return self._slots

    # ------------------------------------------------------------------
    # 滚动
    # ------------------------------------------------------------------
    def _scroll_cmd(self, *args) -> None:
        self._canvas.yview(*args)

    def _on_yscroll(self, first, last) -> None:
        # 优化（实时拖动禁内部重绘）：拖动中画布宽度变化也会回流
        # yscrollcommand（视口比例重算）→ vbar.set → CTkScrollbar
        # 重绘；拖动中跳过（高度未变，滑块位置无视觉差异，松开时
        # set_splitter_drag(False) 统一补齐）
        if self._dragging_splitter:
            return
        self._vbar.set(first, last)
        self._sync()

    def _on_xscroll(self, first, last) -> None:
        # 优化（实时拖动禁内部重绘）：分隔条拖动中画布宽度逐帧
        # 变化 → xview 范围变 → 本回调 → hbar.set → CTkScrollbar
        # 重绘（实测每帧最大头）。拖动中跳过（松开时
        # set_splitter_drag(False) 统一补 region/同步/滑块位置）
        if self._dragging_splitter:
            return
        self._hbar.set(first, last)

    def _on_canvas_resize(self, event) -> None:
        """视口尺寸变化：更新水平滚动区域宽（内容窄时区域=视口宽）。"""
        # 优化（实时拖动禁内部重绘）：拖动中不更新 scrollregion
        # （scrollregion 变更会回流 xscrollcommand 触发滚动条重绘
        # 级联）—— 只走行宽快速路径（_sync 内部判断），region 与
        # 全量同步延迟到松开时一次补齐
        if self._dragging_splitter:
            self._sync()
            return
        self._update_region()
        self._sync()

    def _on_wheel(self, event) -> None:
        steps = -int(event.delta / 120) * 2   # 每 2 单位 ≈ 1 行
        self._canvas.yview_scroll(steps, "units")

    def _on_wheel_h(self, event) -> None:
        # 修复缺陷R9：Shift+滚轮横向滚动（单行长摘要左右查看）
        self._canvas.xview_scroll(-int(event.delta / 120) * 3, "units")

    # ------------------------------------------------------------------
    # 水平滚动快照模式（实时滚动防撕裂）
    # ------------------------------------------------------------------
    def _on_hbar_press(self, _event) -> None:
        """水平滚动条按下：可见行转画布图元（单表面原子滚动）。

        优化（实时滚动防撕裂）：拖动水平滚动条时若照常滚动画布，
        每个行窗口（原生子窗口）逐个平移 + 画布底色回填异步交错
        上屏，快速拖动出现文字重影/重叠。按下时把当前可见行转画
        为同一画布上的矩形/文本图元并隐藏行窗口 —— 拖动中滚动只
        重绘单一画布（Tk 内部双缓冲整帧原子上屏），物理上不可能
        撕裂；且水平滚动中可见行集合不变（仅整体 X 平移），图元
        渲染内容与真实行完全一致。释放时删除图元恢复行窗口
        （行窗口坐标在隐藏期间已随画布同步平移，无需重排）。
        """
        if self._xsnap is not None or not self._data:
            return
        try:
            if self._canvas.winfo_width() >= self._region_w():
                return          # 无水平滚动范围
        except tk.TclError:
            return
        app = self._app
        p = app._palette()
        states = app._row_states()
        width = self._region_w()
        # 只绘制视口相交行（水平滚动不改变纵向可见集合；池内
        # 含上下缓冲行，全部绘制会成倍增加每帧重绘图元数）
        try:
            top = self._canvas.canvasy(0)
            vh = self._canvas.winfo_height()
        except tk.TclError:
            return
        items = []
        try:
            for slot in self._slots:
                idx = slot.get("idx", -1)
                if idx < 0 or idx >= len(self._data):
                    continue
                y = idx * self.ROW_HEIGHT
                if y + self.ROW_HEIGHT <= top or y >= top + vh:
                    continue
                cluster = self._data[idx]
                if idx == app._selected_row:
                    bg = states["selected"]
                elif idx == self._hovered:
                    bg = states["hover"]
                else:
                    bg = states["bg"]
                items.append(self._canvas.create_rectangle(
                    0, y, width, y + self.ROW_HEIGHT, fill=bg, width=0))
                head_color = app._row_color(cluster) or p["row_text"]
                items.append(self._canvas.create_text(
                    10, y + 7, anchor="nw", font=self._m_head,
                    fill=head_color, text=app._row_text(cluster)))
                # 摘要 y = 头部行距 + 头部上边距 7 + 间隙 2（与
                # _make_slot 的 pack pady 一致）
                sum_y = y + 7 + self._m_head.metrics("linespace") + 2
                items.append(self._canvas.create_text(
                    10, sum_y, anchor="nw", font=self._m_sum,
                    fill=p["row_text"],
                    text=app._clip(cluster.summary, self.SUMMARY_CLIP)))
            for slot in self._slots:
                try:
                    self._canvas.itemconfigure(slot["win"], state="hidden")
                except tk.TclError:
                    pass
        except (tk.TclError, ValueError):
            for it in items:
                try:
                    self._canvas.delete(it)
                except tk.TclError:
                    pass
            return
        self._xsnap = items

    def _on_hbar_release(self, _event) -> None:
        """水平滚动条释放：删除快照图元，恢复真实行窗口。"""
        items, self._xsnap = self._xsnap, None
        if items is None:
            return
        for it in items:
            try:
                self._canvas.delete(it)
            except tk.TclError:
                pass
        for slot in self._slots:
            try:
                self._canvas.itemconfigure(slot["win"], state="normal")
            except tk.TclError:
                pass

    def _bind_row_wheel(self, *widgets) -> None:
        """池内行控件挂滚轮绑定（Tk 事件不冒泡）。"""
        for w in widgets:
            w.bind("<MouseWheel>", self._on_wheel)
            w.bind("<Shift-MouseWheel>", self._on_wheel_h)

    # ------------------------------------------------------------------
    # 池化渲染核心
    # ------------------------------------------------------------------
    def _sync(self) -> None:
        """把池内行对齐到当前可见索引区间（滚动/resize 时复用）。

        修复缺陷R9：带重入护栏 —— _on_yscroll 会回调本方法，若本方法
        再触发滚动回调（scrollregion/yview 变化）会形成事件循环死锁。
        """
        if self._in_sync or not self._data:
            return
        if self._xsnap is not None:
            return          # 快照滚动中：行窗口已隐藏，拖动结束后恢复
        height = self._canvas.winfo_height()
        if height < 10:                       # 布局未完成
            return
        self._in_sync = True
        try:
            # 修复缺陷R9：行宽 = 水平滚动区域宽（内容超宽时行随之加宽，
            # 整行高亮与点击区域覆盖全部内容）
            width = self._region_w()
            if self._dragging_splitter:
                # 优化（实时拖动快速路径）：拖动中数据/滚动位置不变、
                # 仅列宽变化 —— 只把池行窗口宽度对齐新列宽（整行高亮
                # 实时跟随），跳过文本重填/事件重绑（每帧仅 ~15 次
                # itemconfigure，<1ms）；松开时 set_splitter_drag(False)
                # 触发一次全量 _sync 补齐。行文本/颜色本就正确（数据
                # 没变），无视觉差异。
                for slot in self._slots:
                    try:
                        self._canvas.itemconfigure(slot["win"], width=width)
                    except tk.TclError:
                        pass
                return
            top = self._canvas.canvasy(0)
            first = max(0, int(top // self.ROW_HEIGHT) - self.BUFFER)
            need = int(height // self.ROW_HEIGHT) + 2 * self.BUFFER + 1
            last = min(len(self._data), first + need)
            # 池按需增长（只增不减，控件全程复用）
            while len(self._slots) < (last - first):
                self._slots.append(self._make_slot(width))
            for i, slot in enumerate(self._slots):
                idx = first + i
                if idx < last:
                    self._fill_slot(slot, idx, width)
                else:
                    try:
                        self._canvas.itemconfigure(slot["win"], state="hidden")
                    except tk.TclError:
                        pass
        finally:
            self._in_sync = False

    def set_splitter_drag(self, active: bool) -> None:
        """分隔条拖动模式开关：拖动中 _sync 走快速路径（只改行宽）。

        优化（实时拖动）：active=False 时补齐拖动期间跳过的全部
        内部重排 —— scrollregion（列宽变化后水平滚动范围）+ 一次
        全量 _sync（文本填充/事件重绑）+ 滚动条滑块位置（画布
        xview 比例随宽度变化已偏移，set 让 hbar 重新对齐）。
        """
        self._dragging_splitter = active
        if not active and self._data:
            self._update_region()
            self._sync()
            try:
                # 触发一次 xscrollcommand 回调，刷新滑块到真实位置
                self._canvas.xview_moveto(self._canvas.xview()[0])
            except tk.TclError:
                pass

    def _make_slot(self, width: int) -> dict:
        """创建一个池化行（原生 tk 控件，创建后长期复用）。"""
        p = self._app._palette()
        frame = tk.Frame(self._canvas, bg=p["row_bg"], bd=0,
                         highlightthickness=0)
        # 修复缺陷R9：头部/摘要字体均施加与经典模式 CTkLabel 一致的
        # DPI 缩放（原生 tk.Label 不缩放命名字体，直接传会偏小/不一致）
        head = tk.Label(frame, anchor="w",
                        font=self._app._scaled_font(self._app._font_row_head),
                        bg=p["row_bg"], fg=p["row_text"])
        head.pack(fill="x", padx=(10, 10), pady=(7, 2))
        # 修复缺陷R9：摘要单行不换行（wraplength=0），长摘要靠水平滚动查看
        summary = tk.Label(frame, anchor="w", justify="left",
                           font=self._app._scaled_font(
                               self._app._font_row_summary),
                           wraplength=0,
                           bg=p["row_bg"], fg=p["row_text"])
        summary.pack(fill="x", padx=(10, 4), pady=(2, 6))
        # 窗口项高度锁 ROW_HEIGHT：整行高亮覆盖完整行（无残留缝隙）
        win = self._canvas.create_window(0, 0, window=frame,
                                         anchor="nw", width=width,
                                         height=self.ROW_HEIGHT)
        self._bind_row_wheel(frame, head, summary)
        return {"frame": frame, "head": head, "summary": summary,
                "win": win, "idx": -1, "virtual": True}

    def _fill_slot(self, slot: dict, idx: int, width: int) -> None:
        """池行填充数据索引 idx（复用控件，仅改文本/颜色/绑定）。"""
        app = self._app
        cluster = self._data[idx]
        p = app._palette()
        states = app._row_states()
        if idx == app._selected_row:
            bg = states["selected"]
        elif idx == self._hovered:
            bg = states["hover"]
        else:
            bg = states["bg"]
        head_color = app._row_color(cluster) or p["row_text"]
        slot["idx"] = idx
        try:
            slot["frame"].configure(bg=bg)
            slot["head"].configure(
                bg=bg, fg=head_color, text=app._row_text(cluster))
            slot["summary"].configure(
                bg=bg, fg=p["row_text"],
                text=app._clip(cluster.summary, self.SUMMARY_CLIP))
            self._canvas.itemconfigure(slot["win"], state="normal",
                                       width=width,
                                       height=self.ROW_HEIGHT)
            self._canvas.coords(slot["win"], 0, idx * self.ROW_HEIGHT)
            # 重新绑定到当前数据索引（池行复用后索引变化）
            for w in (slot["frame"], slot["head"], slot["summary"]):
                w.bind("<Button-1>",
                       lambda e, i=idx: app._select_cluster(i))
                w.bind("<Enter>",
                       lambda e, i=idx: self._hover(i, True))
                w.bind("<Leave>",
                       lambda e, i=idx: self._hover(i, False))
        except (tk.TclError, ValueError):
            pass

    def _hover(self, idx: int, hovered: bool) -> None:
        self._hovered = idx if hovered else -1
        # 复用 app 的行悬停绘制（含选中态保持逻辑）
        self._app._hover_row(idx, hovered)


class LogCompressorApp(_make_app_base()):
    """日志AI压缩器主窗口。"""

    def __init__(self):
        self._store = ConfigStore()
        self._config = self._store.load()
        # 修复缺陷R1：四态主题（兼容旧配置的 light/dark 值）
        raw_theme = str(self._config.get("appearance", "dark")).lower()
        self._theme = _THEME_ALIASES.get(raw_theme, "dark")
        ctk.set_appearance_mode("dark" if self._theme_is_dark() else "light")
        super().__init__()

        window = self._config.get("window", {})
        # 修复缺陷R9：默认窗口高度 840→1000 —— 840 逻辑高无法同时容纳
        # 22/18 号列表字体与 6 行可视错误（6 行纯文本即约 470px）；对
        # 旧配置一次性升级（标志位防止覆盖用户后续手动调整）。
        if not self._config.get("window_h_upgraded"):
            if int(window.get("height", 840)) < 900:
                window["height"] = 1000
                self._config["window"] = window
            self._config["window_h_upgraded"] = True
        self.title(f"{HUMAN_NAME}  v{__version__}")
        width = window.get("width", 1280)
        height = window.get("height", 1000)
        self.geometry(f"{width}x{height}")
        self.minsize(1000, 680)
        # 修复缺陷R9：DPI 缩放系数 —— CTkLabel 对 CTkFont 自动施加控件
        # 缩放，而行内摘要用原生 tk.Label（命名字体原样使用、不缩放），
        # 高 DPI 屏上摘要实际渲染只有标称字号的 1/scale，偏小 —— 摘要
        # 字体也需按同倍率换算为缩放元组（_scaled_font）。
        try:
            self._font_scale = ctk.ScalingTracker.get_widget_scaling(self)
        except (AttributeError, tk.TclError):
            self._font_scale = 1.0

        # 运行状态
        self._result: Optional[AnalysisResult] = None
        self._compare_results: List[CompareResult] = []
        self._displayed: List[ErrorCluster] = []
        self._cluster_rows: List[dict] = []
        self._selected_row: int = -1
        self._queue: "queue.Queue" = queue.Queue()
        # 共享字体：行级字体必须复用（每行新建 CTkFont 会被 GC 在
        # 任意线程析构，tkinter.Font.__del__ 跨线程调用 Tk 造成死锁）
        # 修复缺陷R9：列表字体（级别/次数 22 加粗，摘要 18）
        # 修复缺陷R10：字号随档位缩放（小/中/大/特大，默认中），并
        # 全屏字体再放大（头部 28 加粗 / 摘要 24 / 实例行 20）
        self._font_size = (self._config.get("font_size")
                           if self._config.get("font_size") in FONT_SIZE_SCALE
                           else "中")
        self._font_row_head = ctk.CTkFont(
            family="Consolas", size=self._font_px(22), weight="bold")
        self._font_row_summary = ctk.CTkFont(size=self._font_px(18))
        self._font_hint = ctk.CTkFont(size=12)
        self._font_fs_head = ctk.CTkFont(
            family="Consolas", size=self._font_px(28), weight="bold")
        self._font_fs_summary = ctk.CTkFont(size=self._font_px(24))
        self._font_fs_inst = ctk.CTkFont(size=self._font_px(20))
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._chart_window: Optional[ctk.CTkToplevel] = None
        # 修复缺陷R6：虚拟列表 / 全屏窗口预创建缓存
        self._virtual_list: Optional[VirtualClusterList] = None
        self._fs_list_win: Optional[ctk.CTkToplevel] = None
        self._fs_list_refresh = None          # 全屏列表内容刷新回调
        self._fs_list_sig: Optional[tuple] = None    # 已渲染数据签名
        self._fs_detail_win: Optional[ctk.CTkToplevel] = None
        self._fs_detail_box: Optional[ctk.CTkTextbox] = None

        # 修复缺陷R1：主题调色板登记表（切换时按角色批量刷新）
        self._bg_widgets: List[tuple] = []       # (控件, "window"/"card"/"header")
        self._muted_labels: List = []            # 次要文字标签
        self._accent_buttons: List[tuple] = []   # (按钮, "accent"/"danger")

        # 修复缺陷R12：分隔条位置（左右宽度比例）持久化恢复
        try:
            r = float(self._config.get("splitter_ratio",
                                       _SPLITTER_DEFAULT_RATIO))
        except (TypeError, ValueError):
            r = _SPLITTER_DEFAULT_RATIO
        self._splitter_ratio = min(max(r, 0.05), 0.95)

        self._build_ui()
        self._apply_palette()
        self._setup_drag_and_drop()
        self._restore_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_queue)

    # ------------------------------------------------------------------
    # 主题调色板
    # ------------------------------------------------------------------
    def _palette(self) -> Dict[str, str]:
        """当前主题调色板。"""
        return THEMES.get(self._theme, THEMES["dark"])

    def _theme_is_dark(self) -> bool:
        """当前主题是否为暗色系（blue/green 视为亮色系）。"""
        return THEMES.get(self._theme, THEMES["dark"])["is_dark"] == "1"

    def _scaled_font(self, font_obj):
        """CTkFont -> 原生 tk.Label 字体（施加与 CTkLabel 一致的缩放）。

        修复缺陷R9：经典/虚拟/全屏行的摘要为原生 tk.Label，直接传
        CTkFont 命名字体不参与 DPI 缩放（高 DPI 屏渲染偏小），换算为
        缩放元组后与 CTkLabel 渲染尺寸一致（经典/虚拟两种模式相同）。
        """
        try:
            return font_obj.create_scaled_tuple(self._font_scale)
        except (AttributeError, ValueError):
            return font_obj

    def _font_px(self, base: int) -> int:
        """基准字号 -> 当前档位实际字号（修复缺陷R10：四档缩放）。"""
        return max(10, round(base * FONT_SIZE_SCALE.get(self._font_size, 1.0)))

    def _apply_font_size(self, choice: str) -> None:
        """字体大小档位切换（修复缺陷R10：小/中/大/特大，立即生效）。

        - 共享 CTkFont 就地重配（CTk 控件自动跟随更新）；
        - 原生 tk.Label 的缩放元组是创建时快照，需整表重渲染；
        - 虚拟列表行高按字体度量计算，重建后自动适配新字号；
        - 全屏窗口的内联字体（搜索框/详情框）随构建固化 —— 销毁
          缓存窗口，下次打开按新字号重建（列表行随共享字体刷新）。
        """
        if choice not in FONT_SIZE_SCALE or choice == self._font_size:
            return
        self._font_size = choice
        self._font_row_head.configure(size=self._font_px(22))
        self._font_row_summary.configure(size=self._font_px(18))
        self._font_fs_head.configure(size=self._font_px(28))
        self._font_fs_summary.configure(size=self._font_px(24))
        self._font_fs_inst.configure(size=self._font_px(20))
        # 重渲染已有结果（经典 / 虚拟 / 对比模式的行级原生标签）
        if self._compare_results:
            self._render_compare_list()
        elif self._result is not None:
            self._render_cluster_list()
        # 全屏窗口缓存销毁（内联字体下次打开重建）
        for win_attr in ("_fs_list_win", "_fs_detail_win"):
            win = getattr(self, win_attr, None)
            if win is not None and win.winfo_exists():
                win.destroy()
            setattr(self, win_attr, None)
        self._fs_list_refresh = None
        self._fs_list_sig = None
        self._fs_detail_box = None
        self._save_config()

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
        # 修复缺陷R1：标题栏升级 —— 项目图标 + 大号加粗标题 + 主题角色登记
        bar = ctk.CTkFrame(self, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        self._bg_widgets.append((bar, "header"))
        icon = ctk.CTkLabel(bar, text="🗂️", font=ctk.CTkFont(size=26))
        icon.grid(row=0, column=0, padx=(16, 4), pady=6, sticky="w")
        title = ctk.CTkLabel(
            bar, text=f"{HUMAN_NAME}  v{__version__}",
            font=ctk.CTkFont(size=20, weight="bold"))
        title.grid(row=0, column=1, padx=(2, 6), pady=6, sticky="w")
        subtitle = ctk.CTkLabel(
            bar, text="海量日志压缩投喂大模型 · 快速故障排查",
            font=ctk.CTkFont(size=12))
        subtitle.grid(row=0, column=2, padx=6, sticky="w")
        self._muted_labels.append(subtitle)
        # 修复缺陷R13（第三轮#7）：主题切换改为下拉选择 —— 可直接选择
        # 任意主题（无需循环点击）；下拉列表只列其他三态（当前项不重复
        # 出现），选中即触发原四态切换逻辑（调色板批量刷新）。
        # 修复缺陷R14：选择框为复合控件（图标列固定宽 + 主题名 + ▼），
        # 自定义弹窗两列布局 —— emoji 宽度不一不再影响文字对齐。
        # 修复缺陷R15：主题名居中 —— 中间列弹性（weight=1）+ 文字
        # anchor="center"，字样始终位于左侧图标与右侧▼箭头的正中间。
        self._theme_box = ctk.CTkFrame(bar, corner_radius=6, width=120,
                                        cursor="hand2")
        self._theme_box.grid(row=0, column=3, padx=(6, 16))
        self._theme_box.grid_columnconfigure(1, weight=1)  # 中间弹性列
        p = self._palette()
        self._theme_box_icon = ctk.CTkLabel(
            self._theme_box, text=p["icon"],
            width=self._measure_theme_icon_col(), anchor="center")
        # 修复缺陷R16（续）：图标列左距 10 逻辑 px 与弹窗行对齐 ——
        # 弹窗行 padx=2 + 图标 padx=8 = 10，此前按钮为 8，导致按钮
        # 图标列比弹窗图标列左偏 2 逻辑 px（200% DPI 下 4 物理 px，
        # 太阳/月亮可见中心随之偏左），两者左缘同起点后完全重合
        self._theme_box_icon.grid(row=0, column=0, padx=(10, 0), pady=(3, 3))
        self._theme_box_name = ctk.CTkLabel(
            self._theme_box, text=p["label"], anchor="center")
        self._theme_box_name.grid(row=0, column=1, sticky="ew",
                                  padx=(4, 4))
        self._theme_box_arrow = ctk.CTkLabel(
            self._theme_box, text="▼", anchor="center")
        self._theme_box_arrow.grid(row=0, column=2, padx=(0, 8))
        # 点击选择框任意区域（含图标/文字/箭头）弹出下拉
        for w in (self._theme_box, self._theme_box_icon,
                  self._theme_box_name, self._theme_box_arrow):
            w.bind("<Button-1>", self._on_theme_box_click)
        self._build_theme_popup()

    # ------------------------------------------------------------------
    def _build_tabs(self) -> None:
        self._tabview = ctk.CTkTabview(self)
        # 修复缺陷R9：顶部区域压缩（结果区获得更大高度）
        self._tabview.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 2))
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
        hint = ctk.CTkLabel(tab, text=hint_text)
        hint.grid(row=0, column=0, sticky="w", pady=(2, 4))
        self._muted_labels.append(hint)
        self._file_entry = ctk.CTkEntry(tab, placeholder_text="日志文件路径…")
        self._file_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        browse = ctk.CTkButton(tab, text="选择文件", width=90,
                               command=self._browse_file)
        browse.grid(row=1, column=1)
        self._accent_buttons.append((browse, "accent"))

    def _build_text_tab(self) -> None:
        tab = self._tabview.tab("文本粘贴")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        hint = ctk.CTkLabel(
            tab, text="直接粘贴日志片段（适合几万行以内的快速排查，无需存为文件）")
        hint.grid(row=0, column=0, sticky="w", pady=(2, 4))
        self._muted_labels.append(hint)
        # 修复缺陷#11：undo=False —— 大段粘贴时 undo 栈与 autoseparator
        # 会随粘贴体量膨胀（几万行日志可达数百 MB），关闭撤销换取内存稳定
        self._paste_box = ctk.CTkTextbox(tab, height=100, undo=False,
                                         font=ctk.CTkFont(family="Consolas",
                                                          size=12))
        self._paste_box.grid(row=1, column=0, sticky="ew")
        self._bg_widgets.append((self._paste_box, "card"))

    def _build_compare_tab(self) -> None:
        tab = self._tabview.tab("多文件对比")
        tab.grid_columnconfigure(1, weight=1)
        cmp_hint = ctk.CTkLabel(
            tab, text="2~3 个日志文件对比（第一个为基准；适配版本对比 / 修复前后对比）")
        cmp_hint.grid(row=0, column=0, columnspan=3, sticky="w", pady=(2, 6))
        self._muted_labels.append(cmp_hint)
        self._compare_entries = []
        for i, label in enumerate(("基准文件 A", "对比文件 B", "对比文件 C（可选）")):
            ctk.CTkLabel(tab, text=label, width=110, anchor="w").grid(
                row=i + 1, column=0, padx=(0, 6), pady=3, sticky="w")
            entry = ctk.CTkEntry(tab, placeholder_text="日志文件路径…")
            entry.grid(row=i + 1, column=1, sticky="ew", padx=(0, 6))
            btn = ctk.CTkButton(tab, text="选择", width=64,
                                command=lambda e=entry: self._browse_file(e))
            btn.grid(row=i + 1, column=2)
            self._accent_buttons.append((btn, "accent"))
            self._compare_entries.append(entry)

    # ------------------------------------------------------------------
    def _build_config_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        # 修复缺陷R9：顶部区域压缩（pady 4→3）
        panel.grid(row=2, column=0, sticky="ew", padx=10, pady=3)
        self._bg_widgets.append((panel, "card"))
        for col in range(6):
            panel.grid_columnconfigure(col, weight=1)

        ctk.CTkLabel(panel, text="级别过滤", font=ctk.CTkFont(weight="bold")
                     ).grid(row=0, column=0, padx=(12, 4), sticky="w")
        # 修复缺陷R10：复选框集中容器（FATAL 置首）
        # 优化：六个级别复选框左侧各挂一个 ⓘ 悬停说明
        # （原仅 FATAL 右侧一个；样式统一 #3B82F6 + 手型光标 +
        # 悬停加深，垂直与复选框居中对齐）
        self._level_vars: Dict[str, tk.BooleanVar] = {}
        self._level_tooltips: Dict[str, Tooltip] = {}
        level_box = ctk.CTkFrame(panel, fg_color="transparent")
        level_box.grid(row=0, column=1, columnspan=6, sticky="w")
        col = 0
        for level in LEVEL_CHECKS:
            # 修复缺陷R10：默认勾选 FATAL/ERROR/FAIL（FATAL 受控显示）
            var = tk.BooleanVar(value=level in DEFAULT_SELECTED_LEVELS)
            self._level_vars[level] = var
            # 优化：每个级别复选框左侧 ⓘ（与详情面板 ⓘ 同款蓝色）
            info = ctk.CTkLabel(
                level_box, text="ⓘ", text_color="#3B82F6",
                font=ctk.CTkFont(size=13, weight="bold"),
                cursor="hand2")
            info.grid(row=0, column=col, padx=(5, 2), sticky="e")
            info.bind("<Enter>", lambda e, w=info: w.configure(
                text_color="#2563EB"))
            info.bind("<Leave>", lambda e, w=info: w.configure(
                text_color="#3B82F6"))
            col += 1
            ctk.CTkCheckBox(level_box, text=level, variable=var,
                            checkbox_width=18, checkbox_height=18).grid(
                row=0, column=col, padx=(2, 6), sticky="w")
            col += 1
            self._level_tooltips[level] = Tooltip(
                info, lambda lv=level: _LEVEL_HELP[lv])

        self._include_entry = ctk.CTkEntry(panel, width=200,
                                           placeholder_text="包含关键字（逗号分隔）")
        self._include_entry.grid(row=1, column=0, columnspan=2, padx=(12, 6),
                                 pady=(4, 6), sticky="ew")
        self._exclude_entry = ctk.CTkEntry(panel, width=200,
                                           placeholder_text="排除关键字（逗号分隔）")
        self._exclude_entry.grid(row=1, column=2, columnspan=2, padx=6,
                                 pady=(4, 6), sticky="ew")
        ctk.CTkLabel(panel, text="Top N").grid(row=1, column=4, padx=(6, 2),
                                               sticky="e")
        self._topn_entry = ctk.CTkEntry(panel, width=60)
        self._topn_entry.insert(0, str(DEFAULT_TOP_N))
        self._topn_entry.grid(row=1, column=5, padx=(2, 12), pady=(4, 6),
                              sticky="w")

        # 修复缺陷#5：上下文行数可调节输入框（5~200，默认 50）
        ctk.CTkLabel(panel, text="上下文行数").grid(
            row=2, column=0, padx=(12, 2), pady=(0, 6), sticky="e")
        self._ctx_entry = ctk.CTkEntry(panel, width=60)
        self._ctx_entry.insert(0, str(DEFAULT_CONTEXT_LINES))
        self._ctx_entry.grid(row=2, column=1, padx=(2, 6), pady=(0, 6),
                             sticky="w")
        # 修复缺陷R11：字体大小选择器移至错误列表标题栏后，提示文字
        # 扩展跨列填充原空白（col 2~5，不留大块空隙）
        ctx_hint = ctk.CTkLabel(
            panel, text="典型样例前后各保留的上下文行数（5~200）")
        ctx_hint.grid(row=2, column=2, columnspan=4, padx=(2, 12),
                      pady=(0, 6), sticky="w")
        self._muted_labels.append(ctx_hint)

        # 修复缺陷R10：级别复选框容器跨列 1~6，解析规则右移至列 7~9
        ctk.CTkLabel(panel, text="解析规则").grid(row=0, column=7, padx=(6, 2),
                                                  sticky="e")
        self._rule_menu = ctk.CTkOptionMenu(panel, values=list(RULE_NAMES),
                                            width=130,
                                            command=self._on_rule_changed)
        self._rule_menu.grid(row=0, column=8, padx=(2, 0), sticky="w")
        # 修复缺陷#8：解析规则悬停说明（跟随当前选中规则动态变化）
        rule_help = ctk.CTkLabel(
            panel, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        rule_help.grid(row=0, column=9, padx=(4, 12), sticky="w")
        self._rule_help_tooltip = Tooltip(
            rule_help,
            lambda: RULE_DESCRIPTIONS.get(self._rule_menu.get(), ""))

    def _build_action_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        # 修复缺陷R9：顶部区域压缩（pady 4→3，按钮高度 34→30）
        panel.grid(row=3, column=0, sticky="ew", padx=10, pady=3)
        self._bg_widgets.append((panel, "card"))
        for col in range(7):
            panel.grid_columnconfigure(col, weight=1)

        self._start_btn = ctk.CTkButton(
            panel, text="开始分析", font=ctk.CTkFont(size=14, weight="bold"),
            height=30, command=self._on_start)
        self._start_btn.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
        self._cancel_btn = ctk.CTkButton(
            panel, text="取消", state="disabled", fg_color="#7b3535",
            hover_color="#94424a", command=self._on_cancel, height=30)
        self._cancel_btn.grid(row=0, column=1, padx=6, sticky="ew")
        self._export_btn = ctk.CTkButton(panel, text="导出报告",
                                         state="disabled", height=30,
                                         command=self._on_export)
        self._export_btn.grid(row=0, column=2, padx=6, sticky="ew")
        self._copy_btn = ctk.CTkButton(panel, text="复制摘要", state="disabled",
                                       height=30, command=self._on_copy_summary)
        self._copy_btn.grid(row=0, column=3, padx=6, sticky="ew")
        self._chart_btn = ctk.CTkButton(panel, text="统计图表", state="disabled",
                                        height=30, command=self._show_charts)
        self._chart_btn.grid(row=0, column=4, padx=6, sticky="ew")
        self._accent_buttons.append((self._start_btn, "accent"))
        self._accent_buttons.append((self._export_btn, "accent"))
        self._accent_buttons.append((self._copy_btn, "accent"))
        self._accent_buttons.append((self._chart_btn, "accent"))
        self._accent_buttons.append((self._cancel_btn, "danger"))

        progress_frame = ctk.CTkFrame(panel, fg_color="transparent")
        progress_frame.grid(row=0, column=5, columnspan=2, padx=10,
                            sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        self._progress_label = ctk.CTkLabel(progress_frame, text="就绪",
                                            anchor="w")
        self._progress_label.grid(row=0, column=0, sticky="ew")
        self._muted_labels.append(self._progress_label)
        self._progress_bar = ctk.CTkProgressBar(progress_frame,
                                                progress_color="#3B82F6")
        self._progress_bar.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self._progress_bar.set(0)

    def _build_result_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        # 修复缺陷R9：结果区上下留白压缩（列表/详情获得更大高度）
        panel.grid(row=4, column=0, sticky="nsew", padx=10, pady=(2, 2))
        self._bg_widgets.append((panel, "card"))
        # 修复缺陷R12：左右分栏改为 place 比例布局 —— panel 内三个并列
        # 子部件（列表列 / 分隔条 / 详情列），宽度由 _splitter_ratio
        # 精确控制（grid weight 无法保证比例精确且受内容请求宽度干扰）。
        self._result_panel = panel
        self._list_col = ctk.CTkFrame(panel, fg_color="transparent")
        self._detail_col = ctk.CTkFrame(panel, fg_color="transparent")
        for col in (self._list_col, self._detail_col):
            col.grid_columnconfigure(0, weight=1)
            col.grid_rowconfigure(1, weight=1)
        self._build_splitter(panel)
        # 窗口缩放（最大化/还原/手动调整）时按比例重排（最小宽度钳制）
        panel.bind("<Configure>", lambda e: self._layout_splitter())
        self._layout_splitter()

        # 修复缺陷#7：列表 / 详情标题行增加「全屏」按钮（独立最大化窗口）
        # 修复缺陷R11：字体大小选择器移入标题栏（标题 → 字体大小 → 全屏）
        list_head = ctk.CTkFrame(self._list_col, fg_color="transparent")
        # 修复缺陷R12：留存标题栏引用（动态最小宽度实测依据）
        self._list_head = list_head
        list_head.grid(row=0, column=0, padx=10, pady=(4, 2), sticky="ew")
        list_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(list_head, text="错误分类列表（按优先级降序）",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w")
        # 修复缺陷R11：「字体大小」选择器（小/中/大/特大档，控制列表字号）
        self._font_label = ctk.CTkLabel(list_head, text="字体大小",
                                        font=ctk.CTkFont(size=12))
        self._font_label.grid(row=0, column=1, padx=(10, 2), sticky="e")
        self._muted_labels.append(self._font_label)
        self._font_menu = ctk.CTkOptionMenu(
            list_head, values=list(FONT_SIZE_OPTIONS), width=80, height=26,
            command=self._apply_font_size)
        self._font_menu.set(self._font_size)
        self._font_menu.grid(row=0, column=2, padx=(0, 6), sticky="e")
        self._list_fs_btn = ctk.CTkButton(list_head, text="⛶ 全屏", width=84,
                                          height=26,
                                          command=self._open_list_fullscreen)
        self._list_fs_btn.grid(row=0, column=3, padx=(0, 0), sticky="e")
        self._accent_buttons.append((self._list_fs_btn, "accent"))

        # 修复缺陷#6：「典型样例」术语加悬停说明（ⓘ 图标触发）
        detail_head = ctk.CTkFrame(self._detail_col, fg_color="transparent")
        # 修复缺陷R12：留存标题栏引用（动态最小宽度实测依据）
        self._detail_head = detail_head
        detail_head.grid(row=0, column=0, padx=10, pady=(4, 2), sticky="ew")
        detail_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(detail_head, text="详情（典型样例 · 上下文 · 降噪堆栈）",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w")
        sample_help = ctk.CTkLabel(
            detail_head, text="ⓘ", text_color="#3B82F6",
            font=ctk.CTkFont(size=14, weight="bold"), cursor="question_arrow")
        sample_help.grid(row=0, column=1, padx=(4, 0), sticky="w")
        self._sample_help_tooltip = Tooltip(
            sample_help,
            "该错误类型的代表性日志样例，包含完整的错误信息、堆栈跟踪"
            "和前后上下文，用于快速定位问题")
        self._detail_fs_btn = ctk.CTkButton(detail_head, text="⛶ 全屏", width=84,
                                            height=26,
                                            command=self._open_detail_fullscreen)
        self._detail_fs_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")
        self._accent_buttons.append((self._detail_fs_btn, "accent"))

        # 修复缺陷R9：列表宿主容器（经典滚动 / 虚拟滚动两模式切换）
        self._list_host = ctk.CTkFrame(self._list_col, fg_color="transparent")
        # 修复缺陷R7：宿主四向拉伸（sticky 补 e）+ 右边距 10 与
        # 全屏按钮右缘严格对齐，列表占满左列全部可用宽度（>90%）
        # 修复缺陷R9：pady 压缩（2,8→2,4），列表可视高度更大
        self._list_host.grid(row=1, column=0, sticky="nsew", padx=(10, 10),
                             pady=(2, 4))
        self._list_host.grid_columnconfigure(0, weight=1)
        self._list_host.grid_rowconfigure(0, weight=1)
        self._bg_widgets.append((self._list_host, "window"))
        # 修复缺陷R7：去除固定 width=470（固定宽度导致右侧大量留白）
        self._cluster_list = ctk.CTkScrollableFrame(self._list_host)
        self._cluster_list.grid(row=0, column=0, sticky="nsew")
        # 修复缺陷R9：水平滚动条（摘要单行不换行，左右滑动查看长摘要）
        self._list_hbar = self._make_hscroll(self._cluster_list)
        self._list_hbar.grid(row=1, column=0, sticky="ew")
        # 修复缺陷R5：详情字体放大到 13（摘要/堆栈/上下文更易读）
        self._detail_box = ctk.CTkTextbox(
            self._detail_col, font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none")
        self._detail_box.grid(row=1, column=0, sticky="nsew", padx=(4, 10),
                              pady=(2, 8))
        self._bg_widgets.append((self._detail_box, "card"))
        self._setup_detail_tags()

    # ------------------------------------------------------------------
    # 修复缺陷R12：错误列表 | 详情面板 可拖动分隔条
    # ------------------------------------------------------------------
    def _build_splitter(self, panel) -> None:
        """构建分隔条（宽 6px 圆角条 + 三个握点 + ↔ 光标）。"""
        p = self._palette()
        self._splitter_dragging = False
        # 优化（实时拖动）：按下时缓存的拖动几何（motion 内零
        # winfo 查询，纯算术 + place 比例参数直接几何更新）
        self._splitter_drag_ctx = None
        # 优化（位图快照代理）：拖动期截图代理覆盖层（见
        # _splitter_proxy_begin；None = 未激活/截图失败回退直拖）
        self._splitter_proxy = None
        self._splitter = ctk.CTkFrame(
            panel, width=_SPLITTER_WIDTH, corner_radius=3,
            fg_color=p["splitter"], cursor="sb_h_double_arrow")
        # 三个小握点（视觉提示可拖动；绑同一组事件不阻断拖动）
        self._splitter_dots = []
        for dy in (-8, 0, 8):
            dot = tk.Frame(self._splitter, width=2, height=2, bd=0,
                           highlightthickness=0, bg=p["splitter_grip"],
                           cursor="sb_h_double_arrow")
            dot.place(relx=0.5, rely=0.5, y=dy, anchor="center")
            self._splitter_dots.append(dot)
        for target in [self._splitter] + self._splitter_dots:
            target.bind("<ButtonPress-1>", self._on_splitter_press)
            target.bind("<B1-Motion>", self._on_splitter_drag)
            target.bind("<ButtonRelease-1>", self._on_splitter_release)
            target.bind("<Double-Button-1>", self._on_splitter_dblclick)
            target.bind("<Enter>", lambda e: self._splitter_hover(True))
            target.bind("<Leave>", lambda e: self._splitter_hover(False))
        # CTkFrame.bind 实际注册在其内部 canvas（真实点击的命中目标）；
        # 外层 tk.Frame 再绑一份 —— event_generate 直发外层时不经过
        # canvas，双注册保证两种派发路径都能触发（真实点击只命中
        # canvas 一路，不会重复触发）
        for seq, handler in (
                ("<ButtonPress-1>", self._on_splitter_press),
                ("<B1-Motion>", self._on_splitter_drag),
                ("<ButtonRelease-1>", self._on_splitter_release),
                ("<Double-Button-1>", self._on_splitter_dblclick)):
            tk.Frame.bind(self._splitter, seq, handler)

    def _splitter_hover(self, hovered: bool) -> None:
        """悬停/拖动高亮（选中蓝，提示可拖动）。"""
        if self._splitter_dragging and not hovered:
            return          # 拖动中离开仍保持高亮
        try:
            p = self._palette()
            self._splitter.configure(
                fg_color=p["row_selected"] if hovered else p["splitter"])
        except (tk.TclError, ValueError):
            pass

    def _splitter_min_widths(self):
        """动态测量左右列最小宽度（标题栏内容实测宽 + 边距，物理像素）。

        修复缺陷R12（极限遮挡）：固定 200/300 逻辑像素小于标题栏
        内容宽度 —— 拖到最左时左列标题「错误分类列表（按优先级
        降序）」整体被裁，拖到最右时右列「详情」起首两字被分隔条
        挡住。以标题栏请求宽度实测为准（winfo_reqwidth 物理值，
        DPI 缩放已含其中），文案/字号/按钮尺寸变化自动适配；标题
        栏尚未布局完成（请求宽 ≤1）时回退保守常量。
        """
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        # 标题栏 grid padx 单侧 10 逻辑 px + 两侧；再加 20 逻辑 px 余量
        base = 10 * scale * 2 + 20 * scale
        try:
            lreq = self._list_head.winfo_reqwidth()
            rreq = self._detail_head.winfo_reqwidth()
        except (tk.TclError, AttributeError):
            lreq = rreq = 0
        if lreq <= 1 or rreq <= 1:        # 标题栏尚未布局完成
            return _SPLITTER_MIN_LIST * scale, _SPLITTER_MIN_DETAIL * scale
        return lreq + base, rreq + base

    def _layout_splitter(self) -> None:
        """按比例布局左右列与分隔条（全部 relx/relwidth 比例参数）。

        修复缺陷R12（高DPI错位）：CTk place() 会对显式 x/y 乘控件缩放
        系数，而 relwidth/relheight 不缩放 —— x（像素）与 relwidth
        （比例）混用在高 DPI（widget_scaling>1）下两套坐标系分裂：
        左列按比例、分隔条/右列按被二次缩放的像素放置，整体被推出
        面板外（详情面板消失、中间大片空白、拖动反馈错乱）。全部改用
        比例参数后缩放天然无关；分隔条自身宽度由构造器 width=6
        （逻辑 px，CTk 自动换算物理）承担。三列 relx 首尾相接占满面板。
        最小宽度以标题栏实测动态值为准（防极限遮挡）。
        优化（实时拖动）：拖动中复用按下时缓存的最小宽度（每帧
        免两次 winfo_reqwidth Tcl 同步调用），松开时清除缓存。
        """
        panel = self._result_panel
        pw = panel.winfo_width()          # 物理像素
        r = self._splitter_ratio
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        sp_w = _SPLITTER_WIDTH * scale    # 分隔条物理宽
        cached = getattr(self, "_splitter_drag_mins", None)
        if cached is not None:
            left_min, right_min = cached
        else:
            left_min, right_min = self._splitter_min_widths()
        # 面板太窄放不下「两列最小宽 + 分隔条」：按比例铺满但不改写
        # 比例（<Configure> 中间态宽度逐级增长，此处钳制会把比例
        # 污染成窄面板下的极值），等宽度足够时再按保存比例重排
        if pw < left_min + right_min + sp_w:
            sp_r = sp_w / max(1, pw)
            self._list_col.place(relx=0, rely=0, relwidth=r, relheight=1)
            self._splitter.place(relx=r, rely=0, relheight=1)
            self._detail_col.place(relx=min(1.0, r + sp_r), rely=0,
                                   relwidth=max(0.0, 1 - r - sp_r),
                                   relheight=1)
            return
        # 最小宽度换算为比例钳制（左≥标题栏实测宽 / 右≥标题栏实测宽）
        lo_r = left_min / pw
        hi_r = 1 - (right_min + sp_w) / pw
        r = min(max(r, lo_r), hi_r)
        self._splitter_ratio = r
        sp_r = sp_w / pw
        self._list_col.place(relx=0, rely=0, relwidth=r, relheight=1)
        self._splitter.place(relx=r, rely=0, relheight=1)
        self._detail_col.place(relx=r + sp_r, rely=0,
                               relwidth=max(0.0, 1 - r - sp_r),
                               relheight=1)

    def _on_splitter_press(self, event) -> None:
        """按下：缓存拖动几何 + 构建位图快照代理（视觉实时 + 零撕裂）。

        优化（位图快照代理）：撕裂根源是拖动中逐 motion 真实重排
        左右两列的全部 CTk/Text 子窗口（大字体+200%DPI 单帧全量
        重绘 100ms+，多窗口异步重绘交错上屏 → 撕裂/残影/文字重叠）。
        现按下时对结果区截屏一次，用「右固定画布（视口滚动）+
        左裁剪框」代理展示拖动全程（资源管理器 / VS Code 分隔条
        拖拽的标准做法）：左右列视觉宽度逐 motion 实时跟手、内容
        按容器正常裁剪/填充，且每帧只有 GDI BitBlt 级画布视口操作
        —— 零真实控件重排 → 物理上不可能撕裂。松开时真实容器
        一次性 place 到最终位置（在代理之下完成重排重绘后再销毁
        代理，无闪变）。PIL/截图不可用（无头/远程/窗口出屏）时
        静默回退为逐 motion 实时 place 真实布局（仍实时，大内容
        下可能有轻微撕裂）。详见 _splitter_proxy_begin。
        """
        self._splitter_dragging = True
        self._splitter_hover(True)
        panel = self._result_panel
        sp_w = _SPLITTER_WIDTH * max(1.0, getattr(self, "_font_scale", 1.0))
        left_min, right_min = self._splitter_min_widths()
        try:
            pw = max(1, panel.winfo_width())
            rootx = panel.winfo_rootx()
        except (tk.TclError, AttributeError):
            return
        lo, hi = left_min, pw - right_min - sp_w
        self._splitter_drag_ctx = {"pw": pw, "rootx": rootx,
                                   "sp_w": sp_w, "lo": lo, "hi": hi}
        self._splitter_drag_mins = (left_min, right_min)
        if hi >= lo:
            self._splitter_proxy_begin()
        # 拖动中窗口失焦（alt-tab 等）→ 结束拖动并应用当前位置
        self.bind("<FocusOut>", self._on_splitter_focusout)
        if self._virtual_list is not None:
            self._virtual_list.set_splitter_drag(True)

    def _splitter_proxy_begin(self) -> bool:
        """构建位图快照代理（详见 _on_splitter_press 说明）。

        结构（实测择优：画布 xview 视口滚动 ~2-4ms，画布/窗口
        resize/move 全量失效 ~5-18ms，小图元 coords ~2ms）：
        - 右画布：固定全幅、逐帧只滚动视口（GDI BitBlt 级）；高亮
          竖线与握点用「固定内容坐标」图元 —— 内容坐标随视口平移
          自动贴住分隔条，零逐帧图元更新；视口右端越过截图右缘时
          露出画布底色（card）= 右列自然填充。
        - 左裁剪框：可变宽 Frame（纯色填充 + 子画布只重绘新暴露
          窄条）；框内左画布固定宽（永不缩放 → 永不全量失效），
          视口锁 0 显示截图左列静态内容；其右侧遮罩图元在拖宽时
          露出左列背景填充（card + 列表区 window 色带）。
        - 两窗口叠放次序：右画布（底）< 左裁剪框（顶），覆盖全部
          真实控件；真实分隔条被完整遮住，无重影。
        """
        try:
            from PIL import ImageGrab, ImageTk
        except Exception:
            return False
        panel = self._result_panel
        ctx = getattr(self, "_splitter_drag_ctx", None) or {}
        pw = ctx.get("pw") or max(1, panel.winfo_width())
        sp_w = ctx.get("sp_w") or _SPLITTER_WIDTH
        lo, hi = ctx.get("lo", 0), ctx.get("hi", pw)
        try:
            panel.update_idletasks()     # 截图前确保当前布局已完全绘制
            px, py = panel.winfo_rootx(), panel.winfo_rooty()
            ph = panel.winfo_height()
            lw = self._list_col.winfo_width()
            if min(pw, ph, lw) <= 2 or pw - lw - sp_w <= 2:
                return False
            shot = ImageGrab.grab(bbox=(px, py, px + pw, py + ph))
            # 进程 DPI 感知下 Tk 坐标即物理像素，截图 1:1；尺寸不符
            # （窗口部分出屏 / 远程会话缩放异常）时放弃代理走回退
            if shot.size != (pw, ph):
                return False
            photo = ImageTk.PhotoImage(shot)
        except Exception:
            return False
        p = self._palette()
        hi = max(hi, lw)             # 布局未完成等极端时的保守修正
        # --- 右画布：固定全幅 + 视口滚动（内容映射：屏幕 s ↔ 截图
        # 像素 s + lw − left，右列内容左缘精确贴住分隔条） ---
        right_c = tk.Canvas(panel, bd=0, highlightthickness=0,
                            bg=p["card"], cursor="sb_h_double_arrow")
        right_c.place(x=0, y=0, relwidth=1, relheight=1)
        # 滚动区域向两侧扩展：视口目标 lw−left ∈ [lw−hi, lw−lo]
        m2 = max(0, hi - lw)         # 左侧余量（拖右时视口为负）
        m1 = max(0, lw - lo)         # 右侧余量（拖左时视口越过右缘）
        right_c.configure(scrollregion=(-m2, 0, pw + m1, ph),
                         xscrollincrement=1)
        right_c.create_image(0, 0, anchor="nw", image=photo)
        # 高亮分隔条竖线 + 握点：固定内容坐标 [lw, lw+sp]，随视口
        # 平移自动出现在屏幕 [left, left+sp]（并盖住截图里的旧
        # 分隔条像素，杜绝重影）；初始视口 0 即正确（left=lw）
        right_c.create_rectangle(lw, 0, lw + sp_w, ph,
                                 fill=p["row_selected"], width=0)
        for dy in (-8, 0, 8):
            right_c.create_rectangle(
                lw + sp_w / 2 - 1, ph / 2 + dy - 1,
                lw + sp_w / 2 + 1, ph / 2 + dy + 1,
                fill=p["splitter_grip"], width=0)
        # --- 左裁剪框 + 固定宽左画布（视口锁 0，静态左列） ---
        left_clip = tk.Frame(panel, bg=p["window"], bd=0,
                             highlightthickness=0,
                             cursor="sb_h_double_arrow")
        left_clip.place(x=0, y=0, width=lw, relheight=1)
        left_c = tk.Canvas(left_clip, bd=0, highlightthickness=0,
                           bg=p["window"], width=hi)
        left_c.place(x=0, y=0, relheight=1)
        left_c.create_image(0, 0, anchor="nw", image=photo)
        # 左列右侧遮罩：拖宽时 [lw, left] 露出左列背景（card + 列表
        # 区 window 色带）而非右列内容 —— 单行列表真实拖宽正是行
        # 背景延伸，该近似与真实重排视觉一致；拖窄时被裁剪框
        # 遮住，自动不可见
        try:
            y0 = self._list_col.winfo_y() + self._list_host.winfo_y()
            y1 = y0 + self._list_host.winfo_height()
        except (tk.TclError, AttributeError):
            y0, y1 = 0, ph
        left_c.create_rectangle(lw, 0, hi, ph, fill=p["card"], width=0)
        left_c.create_rectangle(lw, y0, hi, max(y1, y0 + 1),
                                fill=p["window"], width=0)
        self._splitter_proxy = {
            "clip": left_clip, "left": left_c, "right": right_c,
            "photo": photo, "lw": lw, "sp_w": sp_w, "ph": ph}
        return True

    def _splitter_proxy_end(self) -> None:
        """销毁位图快照代理（幂等；截图失败回退时为空操作）。"""
        proxy = getattr(self, "_splitter_proxy", None)
        self._splitter_proxy = None
        if proxy is None:
            return
        proxy["photo"] = None      # 显式释放 PhotoImage 引用（防泄漏）
        for key in ("right", "clip"):
            try:
                proxy[key].destroy()  # 左画布随裁剪框一并销毁
            except (tk.TclError, KeyError):
                pass

    def _on_splitter_focusout(self, _event) -> None:
        """拖动中窗口失焦：结束拖动并应用当前位置（兼容性边界）。"""
        if self._splitter_dragging:
            self._on_splitter_release(None)

    def _on_splitter_drag(self, event) -> None:
        """拖动：只滚动右画布视口 + 调整左裁剪框（视觉实时、零重排）。

        修复缺陷R12（拖动错位/拖不回）：x_root 是事件自带的屏幕
        绝对坐标，与事件接收窗口（分隔条内部 canvas / 2px 握点）
        无关。
        优化（位图快照代理）：motion 内钳制算术后只做两步 ——
        左裁剪框 place_configure(width)（子画布固定不缩放，只重绘
        新暴露窄条）+ 右画布 xview_scroll 到 lw−left（视口平移
        使右列内容/高亮竖线/握点整体精确贴住分隔条；实测画布视口
        滚动 ~2-4ms，画布 resize/move 全量失效 ~5-18ms，故视口是
        唯一逐帧操作的原语）。不触碰任何真实控件、不触发 CTk/Text
        重绘，也不调用 update()/update_idletasks()（Tk 主循环空闲
        帧自然刷新，事件流永不被阻塞）—— 鼠标每移动 1px 左右列宽
        立即跟着变 1px，完全跟手。无代理（截图失败）时回退为逐
        motion 实时 place 真实布局。
        """
        if not self._splitter_dragging:
            return
        ctx = getattr(self, "_splitter_drag_ctx", None)
        if ctx is None:
            return
        if ctx["hi"] < ctx["lo"]:          # 窗口太窄：锁定分隔条
            return
        try:
            x = event.x_root - ctx["rootx"] - ctx["sp_w"] // 2
        except AttributeError:
            return
        left = min(max(x, ctx["lo"]), ctx["hi"])
        self._splitter_ratio = left / ctx["pw"]
        proxy = getattr(self, "_splitter_proxy", None)
        if proxy is not None:
            # 每帧仅两步（全部 GDI BitBlt 级）：左裁剪框变宽（子画布
            # 只重绘新暴露窄条）+ 右画布视口滚到 lw−left（内容随视口
            # 平移：右列内容/高亮竖线/握点整体精确贴住分隔条）
            try:
                proxy["clip"].place_configure(width=left)
                target = proxy["lw"] - left
                cur = proxy["right"].canvasx(0)
                delta = int(round(target - cur))
                if delta:
                    proxy["right"].xview_scroll(delta, "units")
            except (tk.TclError, KeyError):
                pass
            return
        self._layout_splitter()      # 无代理回退：实时 place 真实布局

    def _on_splitter_release(self, event) -> None:
        """松开：真实容器一次性 place 到最终位置并销毁代理。

        优化（位图快照代理）：先 _layout_splitter() + 一次
        update_idletasks() 让真实列在覆盖层之下完成几何应用与
        重绘，再销毁覆盖层 —— 顺序颠倒会闪现一帧拖动前旧布局。
        虚拟列表补一次全量 _sync 并持久化比例。
        """
        if not self._splitter_dragging:
            return
        self._splitter_dragging = False
        self._splitter_hover(False)
        self._splitter_drag_ctx = None
        self._splitter_drag_mins = None
        self.unbind("<FocusOut>")
        self._layout_splitter()
        try:
            self._result_panel.update_idletasks()
        except (tk.TclError, AttributeError):
            pass
        self._splitter_proxy_end()
        if self._virtual_list is not None:
            self._virtual_list.set_splitter_drag(False)
        self._save_config()

    def _on_splitter_dblclick(self, event) -> None:
        """双击恢复默认比例（2:3）并闪烁反馈。"""
        self._splitter_dragging = False
        self._splitter_drag_ctx = None
        self._splitter_drag_mins = None
        self.unbind("<FocusOut>")
        self._splitter_proxy_end()
        if self._virtual_list is not None:
            self._virtual_list.set_splitter_drag(False)
        self._splitter_ratio = _SPLITTER_DEFAULT_RATIO
        self._layout_splitter()
        self._flash_splitter()
        self._save_config()

    def _flash_splitter(self) -> None:
        """分隔条闪烁（高亮 150ms 后回落主题色）。"""
        try:
            p = self._palette()
            self._splitter.configure(fg_color=p["accent"])
            self.after(150, lambda: self._splitter.configure(
                fg_color=self._palette()["splitter"]))
        except (tk.TclError, ValueError):
            pass

    def _make_hscroll(self, scrollable: "ctk.CTkScrollableFrame"
                      ) -> "ctk.CTkScrollbar":
        """为垂直方向 CTkScrollableFrame 补水平滚动（修复缺陷R9）。

        CTkScrollableFrame 内部把内容窗口宽度锁死为画布宽（超宽内容
        直接被裁剪、无横向滚动）。此处接入一条水平 CTkScrollbar：
        - 画布 xscrollcommand <-> 滚动条（command=xview）双向联动；
        - 画布/内容尺寸变化后把内容窗口宽度放宽到内容自然宽
          （覆盖 CTk 内部的锁宽行为，绑定注册在其后、add=True）；
        - Shift+滚轮横向滚动复用 CTk 内建处理（xview 有范围时生效）。
        返回滚动条（调用方自行 grid/pack 到宿主底部）。
        """
        canvas = scrollable._parent_canvas
        # 真正的宿主：CTkScrollableFrame 自身 master 是内部画布，
        # 其外层 _parent_frame 的 master 才是调用方传入的宿主容器
        host = scrollable._parent_frame.master
        hbar = ctk.CTkScrollbar(host, orientation="horizontal",
                                height=14, command=canvas.xview)
        canvas.configure(xscrollcommand=hbar.set)

        def fit(*_args) -> None:
            try:
                # 内容自然宽 = 内部行控件请求宽的最大值（不换行时为
                # 完整文本宽）；与画布宽取大（窄内容时行仍占满视口）
                need = canvas.winfo_width()
                for child in scrollable.winfo_children():
                    w = child.winfo_reqwidth()
                    if w > need:
                        need = w
                canvas.itemconfigure(scrollable._create_window_id,
                                     width=need)
            except (tk.TclError, ValueError, AttributeError):
                pass

        canvas.bind("<Configure>", fit, add=True)
        scrollable.bind("<Configure>", fit, add=True)
        return hbar

    def _setup_detail_tags(self) -> None:
        """主面板详情标签配置（转发共享方法）。"""
        self._apply_detail_tags(self._detail_box)

    def _apply_detail_tags(self, box: ctk.CTkTextbox,
                          big: bool = False) -> None:
        """详情文本高亮标签：关键字 / 业务栈帧 / 元信息 / 折叠提示 / 段落标题。

        修复缺陷R5：业务栈帧（bstack）琥珀色加粗，与普通日志行区分
        更明显；系统库折叠提示（fold）独立紫色，视觉清晰。
        修复缺陷R10：big=True 时启用全屏大字号标签（摘要 20 加粗 /
        元信息 16 / 栈帧 18 加粗——正文为 box 基础字体 18）。

        说明：CTkTextbox.tag_config 禁用 font 选项（与 DPI 缩放不
        兼容），加粗经内部 tk.Text 配置并手动缩放；失败时降级为
        无高亮（不阻断渲染）。
        """
        try:
            for tag, color in _DETAIL_TAG_COLORS.items():
                box.tag_config(tag, foreground=color)
            # 业务栈帧加粗（内部 tk.Text 支持 tag font）
            inner = getattr(box, "_textbox", None)
            if inner is not None:
                size_b = self._font_px(13 if not big else 18)
                font = ("Consolas", size_b, "bold")
                scaler = getattr(box, "_apply_font_scaling", None)
                if callable(scaler):
                    font = scaler(font)
                inner.tag_config("bstack", font=font)
                if big:
                    # 全屏详情大字号：段标题 20 加粗 / 元信息 16
                    for tag, spec in (
                            ("header", ("Consolas", self._font_px(20),
                                        "bold")),
                            ("meta", ("Consolas", self._font_px(16)))):
                        f = scaler(spec) if callable(scaler) else spec
                        inner.tag_config(tag, font=f)
        except (tk.TclError, AttributeError):
            pass

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0)
        bar.grid(row=5, column=0, sticky="ew")
        self._bg_widgets.append((bar, "header"))
        bar.grid_columnconfigure(0, weight=1)
        self._status_label = ctk.CTkLabel(bar, text="就绪 · 支持文件导入 / 文本粘贴 / 多文件对比",
                                          anchor="w")
        self._status_label.grid(row=0, column=0, padx=12, pady=4, sticky="w")
        self._muted_labels.append(self._status_label)

    # ==================================================================
    # 配置持久化
    # ==================================================================
    def _restore_config(self) -> None:
        cfg = self._config
        # 修复缺陷R10：旧配置一次性升级 —— 新增 FATAL 复选框前 FATAL
        # 是「始终放行」语义（用户无感知），升级默认勾选保持 FATAL 可见
        levels = list(cfg.get("levels") or DEFAULT_SELECTED_LEVELS)
        if not cfg.get("fatal_level_upgraded"):
            if "FATAL" not in levels:
                levels.insert(0, "FATAL")
            cfg["fatal_level_upgraded"] = True
            cfg["levels"] = levels
        for level, var in self._level_vars.items():
            var.set(level in levels)
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
        # 修复缺陷R10：字体大小档位恢复（__init__ 已按档位建字体，
        # 此处仅同步选择器显示；字号一致时回调为空操作）
        if cfg.get("font_size") in FONT_SIZE_SCALE:
            self._font_menu.set(cfg["font_size"])
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
            # 修复缺陷R1：保存四态主题名（light/dark/blue/green）
            "appearance": self._theme,
            # 修复缺陷R10：字体大小档位持久化（下次启动自动恢复）
            "font_size": self._font_size,
            # 修复缺陷R12：分隔条位置持久化（左右宽度比例）
            "splitter_ratio": self._splitter_ratio,
            "fatal_level_upgraded": self._config.get(
                "fatal_level_upgraded", False),
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

    def _measure_theme_icon_col(self) -> int:
        """实测主题 emoji 最宽渲染宽 → 图标列宽（逻辑 px，≥ 基准 28）。

        修复缺陷R14：CTkLabel 的 width 只是**最小值** —— 内容更宽时
        标签自动扩展（如 200% DPI 下 🔵 渲染 62 物理 px > 28 逻辑
        ×2=56），固定列宽失效、文字错位。启动时按真实渲染宽度实测
        取 max+余量，保证四行图标列宽严格一致且不裁剪图标。
        """
        if getattr(self, "_theme_icon_col", 0):
            return self._theme_icon_col
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        probe = ctk.CTkFrame(self, fg_color="transparent")
        labels = [ctk.CTkLabel(probe, text=THEMES[k]["icon"])
                  for k in THEME_ORDER]
        for i, lbl in enumerate(labels):
            lbl.grid(row=0, column=i)
        probe.update_idletasks()
        try:
            max_phys = max(lbl.winfo_reqwidth() for lbl in labels)
        finally:
            probe.destroy()
        self._theme_icon_col = max(_THEME_ICON_COL,
                                   int(-(-max_phys // scale)) + 2)
        return self._theme_icon_col

    def _build_theme_popup(self) -> None:
        """构建主题下拉弹窗（两列对齐：固定宽图标列 + 左对齐文字列）。

        修复缺陷R14：CTkOptionMenu 内部是 tkinter.Menu（纯文本项），
        四个 emoji（☀️🌙🔵🟢）字形宽度不一导致文字起始位置错乱。
        自定义 CTkToplevel 弹窗：每行 grid 两列 —— 图标列固定宽
        _THEME_ICON_COL 且居中（图标宽度差异被列宽吸收），文字列
        左对齐 —— 四行文字起始 x 完全一致。当前主题行打开时隐藏
        （R13 排除当前项语义保留）。
        """
        win = ctk.CTkToplevel(self)
        win.overrideredirect(True)          # 无边框原生弹窗
        win.withdraw()
        win.attributes("-topmost", True)
        self._theme_popup = win
        self._theme_popup_rows: Dict[str, dict] = {}
        self._theme_popup_opened_at = 0.0
        win.grid_columnconfigure(0, weight=1)
        # 修复缺陷R15：全局点击收起（常驻 all 绑定，弹窗未开时
        # 直接返回；与焦点解耦，不受 CTkToplevel 全局 set_focus
        # 干扰 —— FocusOut 方案会被它立即误触发关闭）
        self.bind_all("<Button-1>", self._on_theme_global_click, add=True)
        # 主窗口最小化时同步收起（弹窗 topmost 不随主窗隐藏）
        self.bind("<Unmap>", lambda e: self._close_theme_popup())
        icon_col = self._measure_theme_icon_col()
        for i, key in enumerate(THEME_ORDER):
            row = ctk.CTkFrame(win, corner_radius=4,
                              fg_color="transparent", cursor="hand2")
            row.grid(row=i, column=0, sticky="ew", padx=2, pady=1)
            t = THEMES[key]
            icon = ctk.CTkLabel(row, text=t["icon"], width=icon_col,
                               anchor="center")
            icon.grid(row=0, column=0, padx=(8, 0), pady=(4, 4))
            name = ctk.CTkLabel(row, text=t["label"], anchor="w")
            name.grid(row=0, column=1, padx=(4, 8))
            # 行 + 子控件均可点击选择；悬停整行高亮。
            # CTk 控件 bind 实际注册在内部 canvas（真实点击命中处），
            # 外层 tk 部件再绑一份 —— event_generate 直发外层时不经过
            # canvas，双注册保证两种派发路径都能触发（真实点击只命中
            # canvas 一路，不会重复触发；与分隔条同方案）
            for w in (row, icon, name):
                w.bind("<Button-1>",
                       lambda e, k=key: self._on_theme_selected(k))
                w.bind("<Enter>",
                       lambda e, r=row: self._theme_row_hover(r, True))
                w.bind("<Leave>",
                       lambda e, r=row: self._theme_row_hover(r, False))
                tk.Frame.bind(w, "<Button-1>",
                             lambda e, k=key: self._on_theme_selected(k))
            self._theme_popup_rows[key] = {
                "row": row, "icon": icon, "name": name}

    def _theme_row_hover(self, row, hovered: bool) -> None:
        """弹窗行悬停高亮（当前调色板 row_hover 色）。"""
        try:
            row.configure(fg_color=self._palette()["row_hover"]
                          if hovered else "transparent")
        except (tk.TclError, ValueError):
            pass

    def _on_theme_box_click(self, _event=None) -> None:
        """点击选择框：切换弹窗开合。"""
        if self._theme_popup.state() == "normal":
            self._close_theme_popup()
        else:
            self._open_theme_popup()

    def _open_theme_popup(self) -> None:
        """弹出下拉列表：贴选择框正下方（物理像素定位，DPI 精确）。

        修复缺陷R15（点击无反应）：不用 focus/FocusOut 收起 ——
        CTkToplevel.__init__ 注册了全局 bind_all("<Button-1>",
        set_focus)，每次点击把焦点强制设回被点击控件；弹窗
        deiconify 的瞬间焦点先到弹窗、随即被其抢回 → FocusOut
        立即收起（表现为点击无反应、列表闪没）。改为全局点击
        收起机制（_on_theme_global_click），与焦点完全解耦。
        """
        self._update_theme_menu()           # 刷新可见行（排除当前项）
        box = self._theme_box
        win = self._theme_popup
        try:
            x = box.winfo_rootx()
            y = box.winfo_rooty() + box.winfo_height() + 2
        except tk.TclError:
            return
        win.update_idletasks()
        w = max(box.winfo_width(), win.winfo_reqwidth() + 8)
        # wm_geometry 用物理像素（CTk 的 geometry 会二次缩放）
        win.wm_geometry(f"{w}x{win.winfo_reqheight()}+{x}+{y}")
        self._theme_popup_opened_at = time.perf_counter()
        win.deiconify()

    def _on_theme_global_click(self, event) -> None:
        """全局点击收起下拉（弹窗外任意点击；不依赖焦点）。

        豁免两类点击：弹窗打开后 150ms 内（打开动作本身的同一
        事件链，all bindtag 在 widget 绑定之后触发，若不豁免会
        开了立刻关）与落点在弹窗内的点击（行间隙等空白区）。
        """
        try:
            if self._theme_popup.state() != "normal":
                return
            if time.perf_counter() - self._theme_popup_opened_at < 0.15:
                return
            hit = self.winfo_containing(event.x_root, event.y_root)
            if hit is not None and hit.winfo_toplevel() is self._theme_popup:
                return          # 点在弹窗内（行间隙）
        except tk.TclError:
            return
        self._close_theme_popup()

    def _close_theme_popup(self) -> None:
        """收起下拉列表。"""
        if self._theme_popup.state() != "withdrawn":
            self._theme_popup.withdraw()

    def _theme_popup_items(self) -> List[str]:
        """下拉列表当前可见项（除当前主题外的其他三态，保持顺序）。

        修复缺陷R13：当前主题不重复出现在列表中（避免重复选择）；
        列表顺序沿用 THEME_ORDER（亮色、暗色、蓝调、绿调）去掉当前项。
        """
        return [k for k in THEME_ORDER if k != self._theme]

    def _on_theme_selected(self, key: str) -> None:
        """下拉选择主题：直接切换到所选主题（淡出 -> 切换 -> 淡入）。

        修复缺陷R13（第三轮#7）：由循环点击改为下拉直达 —— 可跳过
        中间主题直接选中任意目标；切换动画与底层刷新逻辑不变。
        """
        if key not in THEMES or key == self._theme:
            return
        self._close_theme_popup()
        self._fade_out(key, 0)

    # 主题过渡帧序列（窗口透明度）
    _FADE_OUT_STEPS = (1.0, 0.82, 0.66, 0.55)
    _FADE_IN_STEPS = (0.55, 0.66, 0.82, 1.0)

    def _set_window_alpha(self, alpha: float) -> bool:
        """设置窗口透明度（平台不支持时返回 False 降级直切）。"""
        try:
            self.attributes("-alpha", alpha)
            return True
        except tk.TclError:
            return False

    def _fade_out(self, target: str, step: int) -> None:
        """过渡第一阶段：窗口渐隐（4 帧后于谷底切换主题）。"""
        if step >= len(self._FADE_OUT_STEPS):
            self._apply_theme_switch(target)
            self._fade_in(0)
            return
        if not self._set_window_alpha(self._FADE_OUT_STEPS[step]):
            # 平台不支持透明度：直接切换（无动画但功能完整）
            self._apply_theme_switch(target)
            return
        self.after(28, lambda: self._fade_out(target, step + 1))

    def _fade_in(self, step: int) -> None:
        """过渡第二阶段：窗口渐显恢复。"""
        if step >= len(self._FADE_IN_STEPS):
            self._set_window_alpha(1.0)
            return
        if not self._set_window_alpha(self._FADE_IN_STEPS[step]):
            return
        self.after(28, lambda: self._fade_in(step + 1))

    def _apply_theme_switch(self, target: str) -> None:
        """实际执行主题切换（调色板 / 按钮标识 / 行颜色 / 持久化）。"""
        self._theme = target
        self._apply_palette()
        # 修复缺陷#12/R1：切换立即持久化（不等关闭/下次分析）
        self._store.save(self._current_config_dict())

    def _apply_palette(self) -> None:
        """把当前主题调色板应用到全部登记控件（修复缺陷R1）。

        CTk 的 appearance mode 只有两态，蓝调/绿调在 light 基础上
        按登记角色批量覆盖颜色实现。
        """
        p = self._palette()
        ctk.set_appearance_mode("dark" if p["is_dark"] == "1" else "light")
        try:
            self.configure(fg_color=p["window"])
        except (tk.TclError, ValueError):
            pass
        # 背景类容器（部分控件已销毁时跳过）
        for widget, role in self._bg_widgets:
            try:
                widget.configure(fg_color=p[role])
            except (tk.TclError, ValueError, AttributeError):
                continue
        # 次要文字
        for label in self._muted_labels:
            try:
                label.configure(text_color=p["muted"])
            except (tk.TclError, ValueError, AttributeError):
                continue
        # 主按钮（accent 蓝系 / 蓝调绿调下白底深字）与危险按钮（红系）
        for btn, kind in self._accent_buttons:
            try:
                if kind == "danger":
                    btn.configure(text_color="#ffffff")
                else:
                    btn.configure(fg_color=p["accent"],
                                  hover_color=p["accent_hover"],
                                  text_color=p["accent_text"])
            except (tk.TclError, ValueError, AttributeError):
                continue
        # 修复缺陷R12：分隔条主题色刷新（条身 + 握点）
        try:
            self._splitter.configure(fg_color=p["splitter"])
        except (tk.TclError, ValueError, AttributeError):
            pass
        for dot in getattr(self, "_splitter_dots", []):
            try:
                dot.configure(bg=p["splitter_grip"])
            except (tk.TclError, ValueError):
                pass
        # 修复缺陷R13/R14：主题选择框（复合控件）+ 下拉弹窗配色 ——
        # 框身用 accent 底色 + accent_text 文字（蓝调/绿调下为白底
        # 深字）；弹窗用卡片底色 + 正文色，行悬停 row_hover，四态协调
        box = getattr(self, "_theme_box", None)
        if box is not None:
            try:
                box.configure(fg_color=p["accent"])
                for lbl in (self._theme_box_icon, self._theme_box_name):
                    lbl.configure(text_color=p["accent_text"])
                self._theme_box_arrow.configure(text_color=p["accent_text"])
                self._theme_popup.configure(fg_color=p["card"])
                for entry in self._theme_popup_rows.values():
                    entry["icon"].configure(text_color=p["text"])
                    entry["name"].configure(text_color=p["text"])
            except (tk.TclError, ValueError, AttributeError):
                pass
        self._refresh_row_colors()
        self._update_theme_menu()

    def _update_theme_menu(self) -> None:
        """同步选择框显示与弹窗可见行（当前主题不重复出现）。

        修复缺陷R14：图标/文字分列显示（两列布局对齐）；弹窗当前
        主题行 grid_remove 隐藏（其余行按 THEME_ORDER 原顺序重排，
        图标列固定宽不受行增删影响）。
        """
        box = getattr(self, "_theme_box", None)
        if box is None:
            return
        p = self._palette()
        if self._theme_box_icon.cget("text") != p["icon"]:
            self._theme_box_icon.configure(text=p["icon"])
        if self._theme_box_name.cget("text") != p["label"]:
            self._theme_box_name.configure(text=p["label"])
        vis = 0
        for key in THEME_ORDER:
            row = self._theme_popup_rows[key]["row"]
            if key == self._theme:
                row.grid_remove()          # 排除当前项（R13 语义）
            else:
                row.grid()
                row.grid_configure(row=vis)   # 压实行序不留空行
                vis += 1

    def _refresh_row_colors(self) -> None:
        """主题切换后刷新列表行配色（原生 tk.Label 不随 CTk 主题）。"""
        # 修复缺陷R6：虚拟模式由虚拟列表自刷（池行原生控件配色）
        if self._virtual_list is not None:
            self._virtual_list.apply_palette()
            return
        rows = getattr(self, "_cluster_rows", ())
        if not rows:
            return
        p = self._palette()
        fg = p["row_text"]
        selected = getattr(self, "_selected_row", -1)
        for i, row in enumerate(rows):
            # 选中行保持选中色（主列表模式）
            bg = p["row_selected"] if i == selected and "idx" in row \
                else p["row_bg"]
            try:
                row["summary"].configure(fg=fg, bg=bg)
                row["frame"].configure(fg_color=bg)
            except (tk.TclError, ValueError, KeyError):
                continue

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
            # 修复缺陷#11：粘贴文本读取健壮性处理
            # - Tk Text 的 get("1.0","end") 恒返回尾部换行（strip 去除）；
            # - BOM（\ufeff）非空白字符 strip 不去除，会把首行变成
            #   无结构行导致解析失败 —— 显式剥离；
            # - \r\n / \r 由 run_text 的 splitlines 正确分行，此处不动。
            text = self._paste_box.get("1.0", "end").strip()
            text = text.lstrip("\ufeff").strip()
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
        self._clear_list()
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

    def _clear_list(self) -> None:
        """清空左侧列表（销毁虚拟模式 / 经典行，恢复经典滚动容器）。"""
        # 修复缺陷R6：虚拟列表销毁 + 经典容器恢复
        if self._virtual_list is not None:
            self._virtual_list.destroy()
            self._virtual_list = None
        # 修复缺陷R9：恢复经典水平滚动条（虚拟模式自带独立的 hbar）
        if _widget_alive(self._cluster_list):
            self._cluster_list.grid()          # grid_remove 后恢复
            if _widget_alive(self._list_hbar):
                self._list_hbar.grid()
            for child in self._cluster_list.winfo_children():
                child.destroy()
        self._cluster_rows = []

    def _render_cluster_list(self) -> None:
        """左侧错误列表：Top N 行（图标/优先级/次数行 + 单行摘要）。

        修复缺陷：原单行 CTkButton 长文本溢出右侧且无横向滚动能力，
        R9 起摘要单行不换行 + 底部水平滚动条左右滑动查看完整内容。
        修复缺陷R6：行数超过 VIRTUAL_LIST_THRESHOLD 切换虚拟滚动
        （池化复用可见区行控件，列表长度不再影响渲染耗时）。
        """
        assert self._result is not None
        n = self._current_top_n()
        self._displayed = self._result.clusters[:n]
        self._clear_list()
        # 清理随列表销毁的动态 muted 标签（防登记表无限累积）
        self._muted_labels = [
            w for w in self._muted_labels
            if not hasattr(w, "winfo_exists") or _widget_alive(w)]
        self._selected_row = -1
        if not self._displayed:
            empty = ctk.CTkLabel(self._cluster_list, text="未发现符合条件的错误")
            empty.pack(pady=20)
            self._muted_labels.append(empty)
            return
        # 修复缺陷R6：大列表走虚拟滚动（控件池只建可见区行数）
        if len(self._displayed) > VIRTUAL_LIST_THRESHOLD:
            self._cluster_list.grid_remove()
            # 修复缺陷R9：虚拟模式隐藏经典 hbar（虚拟列表自带 hbar）
            if _widget_alive(self._list_hbar):
                self._list_hbar.grid_remove()
            self._virtual_list = VirtualClusterList(self._list_host, self)
            self._cluster_rows = self._virtual_list.slots
            self._virtual_list.set_data(self._displayed)
            return
        for idx, cluster in enumerate(self._displayed):
            self._make_cluster_row(self._cluster_list, idx, cluster)
        total = len(self._result.clusters)
        if total > n:
            more = ctk.CTkLabel(
                self._cluster_list,
                text=f"…… 其余 {total - n} 种错误可通过调大 Top N 查看",
                font=self._font_hint)
            more.pack(pady=6)
            self._muted_labels.append(more)

    def _make_cluster_row(self, parent, idx: int, cluster: ErrorCluster,
                          register: bool = True,
                          on_select=None, on_hover=None,
                          font_head=None, font_summary=None,
                          on_toggle=None,
                          native: bool = False) -> dict:
        """构建单条错误行（主列表与全屏列表复用，修复缺陷#7）。

        修复缺陷R2：字体放大、行距加大、选中态蓝色高亮（palette
        row_selected）。修复缺陷R9：主列表头部 22 加粗 / 摘要 18、
        摘要单行不换行（水平滚动查看完整内容）。
        修复缺陷R4：font_head/font_summary 覆盖字体（全屏 24/20）；
        on_toggle 提供时行首渲染「▶ ×N」可点击展开按钮（次数从
        行首元信息移入按钮）。
        修复缺陷R6：native=True 全原生 tk 控件（全屏列表用）——
        CTk 复合控件每行 4 个内部 Canvas 约 20ms/行，57 行全量
        渲染卡 4.5s；原生行约 8ms/行且无内部 Canvas。

        参数：
            register: 登记进 self._cluster_rows（主列表选中态管理）
            on_select / on_hover: 自定义回调（全屏窗口联动高亮用）
        """
        p = self._palette()
        f_head = font_head or self._font_row_head
        # 修复缺陷R9：摘要字体施加 DPI 缩放（与 CTkLabel 渲染一致）
        f_sum = self._scaled_font(font_summary or self._font_row_summary)
        # 修复缺陷R9：摘要取消自动换行（wraplength=0 单行完整显示），
        # 长摘要靠列表底部水平滚动条左右滑动查看（大字体下换行会使
        # 单条错误占多行、可视错误数骤减）。
        toggle = None
        if native:
            # 全原生行（全屏列表）：bg/fg 直取调色板
            # 修复缺陷R9：原生行头部同样施加 DPI 缩放（与经典 CTkLabel
            # 渲染一致——命名字体直传 tk.Label 不缩放）
            f_head = self._scaled_font(f_head)
            frame = tk.Frame(parent, bg=p["row_bg"], bd=0,
                             highlightthickness=0)
            frame.pack(fill="x", padx=5, pady=3)
            if on_toggle is not None:
                link = ("#60a5fa" if p["is_dark"] == "1" else "#2563EB")
                line = tk.Frame(frame, bg=p["row_bg"], bd=0,
                                highlightthickness=0)
                line.pack(fill="x", padx=(10, 10), pady=(7, 2))
                toggle = tk.Label(
                    line, text=f"\u25b6 \u00d7{cluster.count}",
                    font=f_head, bg=p["row_bg"], fg=link,
                    cursor="hand2")
                toggle.pack(side="left", padx=(0, 10))
                head = tk.Label(
                    line, text=self._row_text(cluster, with_count=False),
                    anchor="w", font=f_head, bg=p["row_bg"],
                    fg=self._row_color(cluster) or p["row_text"])
                head.pack(side="left", fill="x", expand=True)
                self._bind_row_events((toggle,), on_toggle,
                                      lambda hovered: None)
            else:
                head = tk.Label(
                    frame, text=self._row_text(cluster), anchor="w",
                    font=f_head, bg=p["row_bg"],
                    fg=self._row_color(cluster) or p["row_text"])
                head.pack(fill="x", padx=(10, 10), pady=(7, 2))
            summary = tk.Label(
                frame, text=cluster.summary, anchor="w", justify="left",
                wraplength=0, font=f_sum,
                bg=p["row_bg"], fg=p["row_text"])
            summary.pack(fill="x", padx=(10, 4), pady=(2, 6))
            select_cb = on_select or (lambda: self._select_cluster(idx))
            hover_cb = on_hover or (
                lambda hovered: self._hover_row(idx, hovered))
            self._bind_row_events((frame, head, summary), select_cb,
                                  hover_cb)
            row = {"frame": frame, "summary": summary, "idx": idx,
                   "native": True}
            if toggle is not None:
                row["toggle"] = toggle
            if register:
                self._cluster_rows.append(row)
            return row
        # 修复缺陷R2：行距/内边距加大（大字体下行高充足不拥挤）
        frame = ctk.CTkFrame(parent, corner_radius=6,
                             fg_color=p["row_bg"])
        frame.pack(fill="x", padx=5, pady=3)
        if on_toggle is not None:
            # 修复缺陷R4：「×N」展开按钮（▶ 收起 / ▼ 展开，可点击）
            link = ("#60a5fa" if p["is_dark"] == "1" else "#2563EB")
            line = ctk.CTkFrame(frame, fg_color="transparent")
            line.pack(fill="x", padx=(10, 10), pady=(7, 2))
            toggle = ctk.CTkLabel(
                line, text=f"\u25b6 \u00d7{cluster.count}",
                font=f_head, text_color=link, cursor="hand2")
            toggle.pack(side="left", padx=(0, 10))
            head = ctk.CTkLabel(
                line, text=self._row_text(cluster, with_count=False),
                anchor="w",
                text_color=self._row_color(cluster) or None,
                font=f_head)
            head.pack(side="left", fill="x", expand=True)
            # 展开按钮独立绑定（不触发行选中）
            self._bind_row_events((toggle,), on_toggle,
                                  lambda hovered: None)
        else:
            head = ctk.CTkLabel(
                frame, text=self._row_text(cluster), anchor="w",
                text_color=self._row_color(cluster) or None,
                font=f_head)
            head.pack(fill="x", padx=(10, 10), pady=(7, 2))
        # 修复缺陷R9：摘要单行不换行（wraplength=0）
        summary = tk.Label(
            frame, text=cluster.summary, anchor="w", justify="left",
            wraplength=0,
            font=f_sum,
            bg=p["row_bg"], fg=p["row_text"])
        summary.pack(fill="x", padx=(10, 4), pady=(2, 6))
        select_cb = on_select or (lambda: self._select_cluster(idx))
        hover_cb = on_hover or (lambda hovered: self._hover_row(idx, hovered))
        # 修复缺陷R2：点击/悬停绑定到全部子控件（含 CTkLabel 内部）
        self._bind_row_events((frame, head, summary), select_cb, hover_cb)
        row = {"frame": frame, "summary": summary, "idx": idx}
        if toggle is not None:
            row["toggle"] = toggle
        if register:
            self._cluster_rows.append(row)
        return row

    @staticmethod
    def _is_dark_mode() -> bool:
        """兼容旧接口：CTk appearance 模式是否为 dark。

        修复缺陷R1：四态下 blue/green 映射到 light 基础，
        此方法仅反映 CTk 底层模式（供 Tooltip 等外部类使用）。
        """
        return ctk.get_appearance_mode().lower() == "dark"

    @staticmethod
    def _resolve_row_color(color) -> str:
        """主题色元组 -> 当前模式下的实际颜色值。"""
        if isinstance(color, (tuple, list)):
            return color[1] if LogCompressorApp._is_dark_mode() else color[0]
        return color

    def _row_states(self) -> Dict[str, str]:
        """行三态配色（背景/悬停/选中），随主题切换。"""
        p = self._palette()
        return {"bg": p["row_bg"], "hover": p["row_hover"],
                "selected": p["row_selected"]}

    @staticmethod
    def _bind_row_events(widgets, select_cb, hover_cb) -> None:
        """行级点击 / 悬停事件绑定（修复缺陷R2）。

        Tk 事件不冒泡：真实鼠标点击命中的是 CTk 复合控件内部的
        子控件（CTkLabel 内部的 Canvas / tk.Label），仅绑定容器
        会导致「点击头部行不生效、只能保持默认选中第一行」的缺陷。
        此处把绑定同时挂到容器与其全部子控件上。

        悬停态去重（state 字典）：指针在容器与子控件间移动会触发
        成对的 Leave/Enter，直接透传会闪烁，先比对当前态再回调。
        """
        targets: list = []
        for widget in widgets:
            targets.append(widget)
            targets.extend(widget.winfo_children())
        state = {"hover": None}

        def set_hover(hovered: bool) -> None:
            if state["hover"] == hovered:
                return
            state["hover"] = hovered
            hover_cb(hovered)

        for target in targets:
            target.bind("<Button-1>", lambda e: select_cb())
            target.bind("<Enter>", lambda e: set_hover(True))
            target.bind("<Leave>", lambda e: set_hover(False))

    def _apply_row_bg(self, idx: int, color) -> None:
        """统一更新行背景（经典 CTk 行 / 虚拟池化行都支持）。

        修复缺陷R6：虚拟模式下行池控件为原生 tk 控件（bg 而非
        fg_color），且池位置与数据索引不再一一对应——按 idx 字段
        查找目标行。
        """
        resolved = self._resolve_row_color(color)
        for row in self._cluster_rows:
            if row.get("idx") != idx:
                continue
            try:
                if row.get("virtual"):
                    row["frame"].configure(bg=resolved)
                    row["head"].configure(bg=resolved)
                    row["summary"].configure(bg=resolved)
                else:
                    row["frame"].configure(fg_color=color)
                    row["summary"].configure(bg=resolved)
            except (tk.TclError, ValueError):
                continue
            return

    def _hover_row(self, idx: int, hovered: bool) -> None:
        """行悬停高亮（选中行保持选中色）。"""
        if not (0 <= idx < len(self._cluster_rows)):
            return
        if idx == self._selected_row:
            return
        states = self._row_states()
        self._apply_row_bg(
            idx, states["hover"] if hovered else states["bg"])

    def _mark_selected_row(self, idx: int) -> None:
        """更新选中行高亮（清除旧选中，标记新选中；蓝色选中态）。"""
        previous = getattr(self, "_selected_row", -1)
        states = self._row_states()
        if 0 <= previous < len(self._cluster_rows):
            self._apply_row_bg(previous, states["bg"])
        if 0 <= idx < len(self._cluster_rows):
            self._apply_row_bg(idx, states["selected"])
        self._selected_row = idx

    @staticmethod
    def _row_text(cluster: ErrorCluster, with_count: bool = True) -> str:
        """行首元信息：图标 + 优先级 + 级别 + （次数） + 模块（不含摘要）。

        修复缺陷R4：with_count=False 时次数移至独立的「▶ ×N」展开
        按钮（全屏窗口用），行首不再重复显示。
        """
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
        if with_count:
            return (f"{icon} {cluster.priority_label} {cluster.level:<5} "
                    f"\u00d7{cluster.count:<4}{module}")
        return f"{icon} {cluster.priority_label} {cluster.level:<5}{module}"

    @staticmethod
    def _clip(text: str, width: int) -> str:
        return text if len(text) <= width else text[:width - 1] + "…"

    @staticmethod
    def _row_color(cluster: ErrorCluster) -> Optional[str]:
        # 修复缺陷R10：FATAL 红色（#ff5252）比 ERROR（默认行色）更醒目，
        # 用户一眼区分致命错误
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
        """主界面详情面板渲染（转发到通用填充函数）。"""
        self._fill_cluster_detail(self._detail_box, cluster)

    @staticmethod
    def _detail_writer(box: ctk.CTkTextbox):
        """详情文本写入三件套（段标题 / 元信息 / 普通行）。"""
        def header(text: str) -> None:
            box.insert("end", text + "\n", "header")

        def meta(text: str) -> None:
            box.insert("end", text + "\n", "meta")

        def plain(text: str = "") -> None:
            box.insert("end", text + "\n")
        return header, meta, plain

    def _fill_cluster_detail(self, box: ctk.CTkTextbox,
                             cluster: ErrorCluster) -> None:
        """簇详情渲染（主面板与全屏详情面板共用，修复缺陷R4）。"""
        header, meta, plain = self._detail_writer(box)
        box.configure(state="normal")
        box.delete("1.0", "end")

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
                    # 修复缺陷R5：折叠提示独立配色（清晰可辨）
                    box.insert("end", line + "\n", "fold")
                else:
                    box.insert("end", line + "\n", "bstack")

        if sample.after:
            plain()
            header("──── 后上下文 ────")
            for line in sample.after:
                plain(line)
        box.configure(state="disabled")
        self._highlight_keywords(box)

    def _fill_instance_detail(self, box: ctk.CTkTextbox,
                              cluster: ErrorCluster,
                              instance: "ClusterInstance") -> None:
        """单个错误实例详情（修复缺陷R4：全屏展开实例点击查看）。

        展示该实例自身的前上下文、原始日志与堆栈（区别于簇的
        典型样例）；超岀详情保留上限的实例仅有元数据（提示降级）。
        """
        header, meta, plain = self._detail_writer(box)
        box.configure(state="normal")
        box.delete("1.0", "end")
        header(f"【实例详情】{instance.summary}")
        meta(f"时间 {format_timestamp(instance.timestamp)} | "
             f"行 {instance.line_no}~{instance.last_line_no} | "
             f"所属簇 ×{cluster.count}（{cluster.level}）")
        if instance.entry is None:
            plain()
            meta("该实例超出详情保留上限（仅记录时间与摘要）。"
                 "完整堆栈与上下文请查看该簇的「典型样例」。")
            box.configure(state="disabled")
            return
        entry = instance.entry
        if instance.before:
            plain()
            header("──── 前上下文 ────")
            for line in instance.before:
                plain(line)
        plain()
        header("──── 原始日志 ────")
        plain(entry.raw)
        for extra in entry.message_extra:
            plain(extra)
        if entry.stack:
            simplified = simplify_stack(entry.stack)
            plain()
            header(f"──── 堆栈（业务帧高亮，折叠噪声帧 {simplified.noise_count} 行）────")
            for line in simplified.lines:
                if "已折叠" in line:
                    # 修复缺陷R5：折叠提示独立配色（清晰可辨）
                    box.insert("end", line + "\n", "fold")
                else:
                    box.insert("end", line + "\n", "bstack")
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
        """构建单条对比差异行：符号 + 级别 + 次数变化 + 摘要（自动换行）。

        修复缺陷R2：字体/行距随主列表放大（头部加粗 / 摘要）。
        """
        p = self._palette()
        # 修复缺陷R2：行距/内边距与主列表一致（大字体下行高充足）
        frame = ctk.CTkFrame(parent, corner_radius=6,
                             fg_color=p["row_bg"])
        frame.pack(fill="x", padx=5, pady=3)
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
        # 修复缺陷R9：行距与主列表一致 + 摘要单行不换行（水平滚动查看）
        head.pack(fill="x", padx=(10, 10), pady=(7, 2))
        # 修复缺陷R10：对比行摘要补 DPI 缩放（与主列表渲染一致）
        summary = tk.Label(
            frame, text=item.summary, anchor="w", justify="left",
            wraplength=0,
            font=self._scaled_font(self._font_row_summary),
            bg=p["row_bg"], fg=p["row_text"])
        summary.pack(fill="x", padx=(10, 4), pady=(2, 6))
        return {"frame": frame, "summary": summary, "kind": kind,
                "item": item, "text": f"{item.summary} {item.level} "
                                      f"{self._CMP_SYMBOL[kind]}"}

    def _render_compare_list(self) -> None:
        """左侧对比差异列表（新增/消失/共同 全量渲染，经典模式）。"""
        self._clear_list()
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
            # 登记进 _cluster_rows：主题切换时统一刷新明暗配色（修复缺陷#12）
            self._cluster_rows.append(
                self._make_compare_row(self._cluster_list, kind, cmp, item))

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
        """创建全屏窗口（屏幕正中央、尺寸 ≥80%、ESC 返回主界面）。

        修复缺陷R2：原实现仅 state("zoomed")，在 zoomed 未生效的
        环境下窗口落在系统默认偏移位置（左上角附近）。现先显式
        计算居中几何（屏幕 85% × 88%，正中央），再尝试系统最大
        化——最大化失败时仍保持居中大窗。
        """
        win = ctk.CTkToplevel(self)
        win.title(f"{title}（全屏 · ESC 返回）")
        try:
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w, h = int(sw * 0.85), int(sh * 0.88)
            win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        except tk.TclError:
            pass
        try:
            win.state("zoomed")            # Windows / macOS（100% 全屏）
        except tk.TclError:
            try:
                win.attributes("-fullscreen", True)   # Linux 回退
            except tk.TclError:
                pass
        win.bind("<Escape>", lambda e: win.destroy())
        win.after(60, win.focus_set)       # 抢焦点以接收 ESC
        return win

    def _open_list_fullscreen(self) -> None:
        """错误分类列表全屏：左右分栏 + 簇展开 + 实例详情联动。

        修复缺陷#10：对比模式下展示差异列表（+ 新增 / - 消失 / = 共同）。
        修复缺陷R4（核心功能）：
        - 左侧错误簇列表，每簇「▶ ×N」可点击展开全部 N 个实例
          （时间戳 + 摘要；展开 ▼ / 收起 ▶，批量渐进动画）；
        - 展开的实例可点击，右侧详情面板显示该实例的前上下文、
          原始日志与堆栈；
        - 点击簇行，右侧显示簇详情（典型样例/上下文/降噪堆栈），
          同时联动主界面选中；
        - 实例行用原生 tk.Label 复用（不为每实例建独立 CTkFrame），
          展开按 25 条/帧批量创建（大簇不卡顿）。
        """
        compare_mode = bool(self._compare_results) and not self._displayed
        if not self._displayed and not compare_mode:
            messagebox.showinfo("提示", "请先完成一次分析再使用全屏")
            return
        if compare_mode:
            # 对比模式结构不同（无簇/实例概念）：独立窗口，每次新建
            self._open_compare_fullscreen()
            return
        # 修复缺陷R6：全屏窗口预创建复用 —— 首次创建后 withdraw 隐藏，
        # 二次打开仅 deiconify（数据签名变化时才重渲染列表内容）
        win = self._fs_list_win
        if win is None or not win.winfo_exists():
            win = self._build_fs_list_window()
        else:
            win.deiconify()
            try:
                win.state("zoomed")        # 恢复最大化（个别环境丢失）
            except tk.TclError:
                pass
            # 数据未变则跳过重渲染（复用已有行控件）
            sig = (id(self._result), len(self._displayed),
                   self._current_top_n())
            if sig != self._fs_list_sig and callable(self._fs_list_refresh):
                self._fs_list_refresh()
                self._fs_list_sig = sig
        win.after(60, win.focus_set)

    def _build_fs_list_window(self) -> ctk.CTkToplevel:
        """构建（并缓存）错误分类列表全屏窗口（修复缺陷R6）。"""
        win = self._make_fullscreen_window("错误分类列表")
        self._fs_list_win = win
        # 隐藏而非销毁：ESC / 关闭按钮返回主界面但保留窗口复用
        def hide() -> None:
            win.withdraw()
        win.bind("<Escape>", lambda e: hide())

        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        # 修复缺陷R10：全屏搜索框字体放大到 18（标签同步，视觉平衡）
        ctk.CTkLabel(bar, text="搜索：",
                     font=ctk.CTkFont(size=18)).pack(
            side="left", padx=(12, 4))
        search_var = tk.StringVar()
        search = ctk.CTkEntry(bar, textvariable=search_var,
                             font=ctk.CTkFont(size=18),
                             placeholder_text="按摘要 / 模块 / 级别过滤…")
        search.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=8)
        count_label = ctk.CTkLabel(bar, text="", text_color="#8fa4b8")
        count_label.pack(side="right", padx=12)
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=hide).pack(side="right", padx=(0, 12))

        # ---------------- 常规模式：左右分栏 ----------------
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        body.grid_columnconfigure(0, weight=2)
        body.grid_columnconfigure(1, weight=3)
        body.grid_rowconfigure(0, weight=1)

        list_area = ctk.CTkScrollableFrame(body, width=520)
        # 修复缺陷R9：sticky 补 e —— 原缺失导致全屏左列仅 520px 固定宽
        list_area.grid(row=0, column=0, sticky="nsew", padx=(6, 3))
        # 修复缺陷R9：全屏列表水平滚动条（摘要单行完整显示）
        fs_hbar = self._make_hscroll(list_area)
        fs_hbar.grid(row=1, column=0, sticky="ew", padx=(6, 3))

        # 右侧详情面板（修复缺陷R4：点击簇/实例即时联动）
        detail_head = ctk.CTkFrame(body, fg_color="transparent")
        detail_head.grid(row=0, column=1, sticky="new", padx=(3, 6))
        # 修复缺陷R10：全屏详情面板字体放大（正文 18，标题随行放大）
        ctk.CTkLabel(detail_head, text="详情（簇典型样例 / 实例原始日志）",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            side="left", pady=(4, 2))
        fs_detail = ctk.CTkTextbox(
            body, font=ctk.CTkFont(family="Consolas",
                                   size=self._font_px(18)),
            wrap="none")
        fs_detail.grid(row=0, column=1, sticky="nsew", padx=(3, 6),
                       pady=(26, 0))
        # 详情高亮标签（与主面板同一套配色，修复缺陷R5）
        # 修复缺陷R10：全屏详情大字号标签（摘要 20 / 元信息 16 / 栈帧 18）
        self._apply_detail_tags(fs_detail, big=True)

        fs_rows: List[dict] = []
        render_jobs: List[str] = []        # 分批渲染的挂起任务（重过滤时取消）
        expanded: Dict[int, dict] = {}     # idx -> 展开状态（实例标签等）
        p = self._palette()
        inst_bg = p["row_hover"]           # 实例行底色（与簇行区分）
        selected_inst: Dict[str, object] = {"label": None}

        def paint_row(idx: int, color) -> None:
            resolved = self._resolve_row_color(color)

            def paint_native(widget) -> None:
                # 原生行递归刷背景（frame/line/head/toggle/summary）
                try:
                    widget.configure(bg=resolved)
                except (tk.TclError, ValueError):
                    pass
                for child in widget.winfo_children():
                    paint_native(child)

            for row in fs_rows:
                if row["idx"] == idx:
                    if row.get("native"):
                        paint_native(row["frame"])
                    else:
                        row["frame"].configure(fg_color=color)
                        row["summary"].configure(bg=resolved)

        def fs_select(idx: int) -> None:
            # 联动主界面详情 + 全屏窗口内高亮（主题调色板配色）
            self._select_cluster(idx)
            states = self._row_states()
            for row in fs_rows:
                color = (states["selected"] if row["idx"] == idx
                         else states["bg"])
                paint_row(row["idx"], color)
            _fill_fs_cluster(idx)

        def _fill_fs_cluster(idx: int) -> None:
            if 0 <= idx < len(self._displayed):
                self._fill_cluster_detail(fs_detail, self._displayed[idx])

        def _clear_inst_selection() -> None:
            if selected_inst["label"] is not None:
                try:
                    selected_inst["label"].configure(bg=inst_bg)
                except (tk.TclError, ValueError):
                    pass
                selected_inst["label"] = None

        def select_instance(idx: int, inst: ClusterInstance,
                            label) -> None:
            """实例点击：高亮该行 + 右侧显示实例详情。"""
            _clear_inst_selection()
            selected_inst["label"] = label
            try:
                label.configure(bg=p["row_selected"])
            except (tk.TclError, ValueError):
                pass
            self._fill_instance_detail(fs_detail, self._displayed[idx],
                                       inst)

        def _make_instance_label(parent, idx: int, inst: ClusterInstance):
            """实例行：原生 tk.Label（控件复用，轻量批量创建）。"""
            ts = format_timestamp(inst.timestamp)
            text = f"{ts}  L{inst.line_no}  {inst.summary}"
            # 修复缺陷R10：实例行放大到 20 号 + DPI 缩放 + 单行不换行
            # （长实例文本靠列表底部水平滚动条左右滑动查看完整内容）
            lbl = tk.Label(parent, text=text, anchor="w", justify="left",
                           font=self._scaled_font(self._font_fs_inst),
                           bg=inst_bg, fg=p["row_text"],
                           cursor="hand2", wraplength=0)
            lbl.pack(fill="x", padx=(34, 8), pady=3)
            lbl.bind("<Button-1>",
                     lambda e, i=inst, l=lbl: select_instance(idx, i, l))
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(
                bg=p["row_selected"] if selected_inst["label"] is l
                else p["row_hover"]))
            lbl.bind("<Leave>", lambda e, l=lbl: l.configure(
                bg=p["row_selected"] if selected_inst["label"] is l
                else inst_bg))
            return lbl

        def toggle_expand(idx: int) -> None:
            """展开 / 收起簇实例列表（▶ / ▼ 切换 + 批量渐进动画）。"""
            cluster = self._displayed[idx]
            row = next(r for r in fs_rows if r["idx"] == idx)
            state = expanded.get(idx)
            if state is not None:
                # 收起：取消挂起的展开批次，逐批移除实例行
                state["cancelled"] = True
                if "job" in state:
                    try:
                        win.after_cancel(state["job"])
                    except (tk.TclError, ValueError, KeyError):
                        pass
                expanded.pop(idx, None)
                row["toggle"].configure(text=f"\u25b6 \u00d7{cluster.count}")
                labels = state["labels"]

                def remove_batch() -> None:
                    if not labels:
                        return
                    for lbl in labels[-25:]:
                        try:
                            lbl.destroy()
                        except tk.TclError:
                            pass
                    del labels[-25:]
                    if labels:
                        state["job"] = win.after(12, remove_batch)
                    else:
                        try:
                            state["area"].destroy()
                        except tk.TclError:
                            pass
                state["job"] = win.after(12, remove_batch)
                return
            # 展开：批量创建实例行（25 条/帧，大簇不冻结 UI）
            area = tk.Frame(list_area, bg=inst_bg, bd=0,
                            highlightthickness=0)
            state = {"area": area, "labels": [], "cancelled": False,
                     "pos": 0}
            expanded[idx] = state
            row["toggle"].configure(text=f"\u25bc \u00d7{cluster.count}")
            # 实例区插入到本簇行之后、下一簇行之前（pack before 定位）
            row_pos = next(i for i, r in enumerate(fs_rows)
                           if r["idx"] == idx)
            if row_pos + 1 < len(fs_rows):
                area.pack(fill="x", padx=(12, 2),
                          before=fs_rows[row_pos + 1]["frame"])
            else:
                area.pack(fill="x", padx=(12, 2))
            insts = cluster.instances

            def add_batch() -> None:
                if state["cancelled"] or idx not in expanded:
                    return
                batch = insts[state["pos"]:state["pos"] + 25]
                for inst in batch:
                    state["labels"].append(
                        _make_instance_label(area, idx, inst))
                state["pos"] += len(batch)
                if state["pos"] < len(insts):
                    state["job"] = win.after(12, add_batch)
                else:
                    # 截断提示（实例超出保留上限时）
                    if len(insts) < cluster.count:
                        tk.Label(
                            area,
                            text=f"…… 共 {cluster.count} 次，"
                                 f"仅展示前 {len(insts)} 条实例",
                            font=self._scaled_font(self._font_fs_inst),
                            bg=inst_bg,
                            fg=p["muted"], anchor="w"
                        ).pack(fill="x", padx=(34, 8), pady=(2, 4))
            add_batch()

        def render(keyword: str = "") -> None:
            """渲染匹配行（修复缺陷R6：分批渐进创建，打开即响应）。

            CTk 复合控件创建开销大（每行含 2 个 Canvas），57 行全量
            一次性创建实测 4.5s 卡死窗口。改为每批 20 行、12ms 间隔
            渐进创建：窗口即刻可用，剩余行后台补齐。
            """
            kw = keyword.strip().lower()
            # 取消上一轮挂起的批次（重新过滤 / 窗口复用刷新）
            for job in render_jobs:
                try:
                    win.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
            render_jobs.clear()
            for child in list_area.winfo_children():
                child.destroy()
            fs_rows.clear()
            expanded.clear()
            _clear_inst_selection()
            matched = []
            for idx, cluster in enumerate(self._displayed):
                hay = (f"{cluster.summary} {cluster.module} "
                       f"{cluster.level} {cluster.priority_label}").lower()
                if kw and kw not in hay:
                    continue
                matched.append((idx, cluster))
            total = len(matched)
            states = self._row_states()
            prog = {"pos": 0}
            # 匹配计数同步更新（过滤即时反馈，不等异步批次）
            count_label.configure(
                text=f"显示 {total} / {len(self._displayed)} 条")

            def add_batch() -> None:
                batch = matched[prog["pos"]:prog["pos"] + 20]
                for idx, cluster in batch:
                    # 修复缺陷R6：全屏行用原生控件（8ms/行 vs CTk 20ms/行）
                    fs_rows.append(
                        self._make_cluster_row(
                            list_area, idx, cluster, register=False,
                            native=True,
                            on_select=lambda i=idx: fs_select(i),
                            on_hover=lambda hovered, i=idx: paint_row(
                                i, states["hover"] if hovered
                                else states["bg"]),
                            font_head=self._font_fs_head,
                            font_summary=self._font_fs_summary,
                            on_toggle=lambda i=idx: toggle_expand(i)))
                prog["pos"] += len(batch)
                if prog["pos"] < total:
                    render_jobs.append(win.after(12, add_batch))

            # 首批也异步：窗口 deiconify 后即刻可见（点击响应 <300ms）
            render_jobs.append(win.after(1, add_batch))

        # 文本变化即过滤（trace 不依赖键盘事件，无焦点也可靠触发）
        search_var.trace_add("write", lambda *a: render(search_var.get()))
        # 修复缺陷R6：登记刷新回调 + 数据签名（数据未变时跳过重渲染）
        self._fs_list_refresh = render
        self._fs_list_sig = (id(self._result), len(self._displayed),
                             self._current_top_n())
        render()
        return win

    def _open_compare_fullscreen(self) -> None:
        """对比差异列表全屏（独立窗口，每次新建后销毁）。"""
        win = self._make_fullscreen_window("对比差异列表")
        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        # 修复缺陷R10：对比全屏搜索框字体 18（与全屏列表窗口一致）
        ctk.CTkLabel(bar, text="搜索：",
                     font=ctk.CTkFont(size=18)).pack(side="left",
                                                      padx=(12, 4))
        search_var = tk.StringVar()
        search = ctk.CTkEntry(bar, textvariable=search_var,
                             font=ctk.CTkFont(size=18),
                             placeholder_text="按摘要 / 模块 / 级别过滤…")
        search.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=8)
        count_label = ctk.CTkLabel(bar, text="", text_color="#8fa4b8")
        count_label.pack(side="right", padx=12)
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=win.destroy).pack(side="right", padx=(0, 12))
        # 图例常驻顶栏（对比模式）
        ctk.CTkLabel(bar, text="+ 新增  - 消失  = 共同",
                     text_color="#8fa4b8").pack(side="right", padx=12)
        list_area = ctk.CTkScrollableFrame(win)
        list_area.pack(fill="both", expand=True)
        # 修复缺陷R9：对比全屏列表水平滚动条（摘要单行完整显示）
        cmp_hbar = self._make_hscroll(list_area)
        cmp_hbar.pack(fill="x")
        fs_rows: List[dict] = []

        def render_compare(keyword: str = "") -> None:
            kw = keyword.strip().lower()
            for child in list_area.winfo_children():
                child.destroy()
            fs_rows.clear()
            shown = 0
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
            count_label.configure(text=f"显示 {shown} / {total} 条")

        search_var.trace_add(
            "write", lambda *a: render_compare(search_var.get()))
        render_compare()

    def _open_detail_fullscreen(self) -> None:
        """详情面板全屏：完整详情 + 上下左右滚动（高亮一并复制）。

        修复缺陷R6：窗口预创建复用（隐藏而非销毁，二次打开仅刷新）。
        """
        content = self._detail_box.get("1.0", "end").rstrip("\n")
        if not content:
            messagebox.showinfo("提示", "请先选择一个错误查看详情")
            return
        win = self._fs_detail_win
        if win is None or not win.winfo_exists():
            win = self._fs_detail_win = self._build_fs_detail_window()
        else:
            win.deiconify()
            try:
                win.state("zoomed")
            except tk.TclError:
                pass
        box = self._fs_detail_box
        box.configure(state="normal")
        box.delete("1.0", "end")
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
        win.after(60, win.focus_set)

    def _build_fs_detail_window(self) -> ctk.CTkToplevel:
        """构建（并缓存）详情全屏窗口（修复缺陷R6）。"""
        win = self._make_fullscreen_window("错误详情")
        # 隐藏而非销毁：ESC / 关闭返回主界面但保留窗口复用
        def hide() -> None:
            win.withdraw()
        win.bind("<Escape>", lambda e: hide())
        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text="错误详情（典型样例 · 上下文 · 降噪堆栈 · "
                               "支持上下左右滚动）").pack(side="left", padx=12)
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=hide).pack(side="right", padx=12, pady=8)
        # 修复缺陷R10：详情全屏基础字号 13 -> 18（全屏窗口大字体）
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas",
                                                  size=self._font_px(18)),
                             wrap="none")
        # 水平滚动条（垂直滚动条 CTkTextbox 自带）
        xbar = tk.Scrollbar(win, orient="horizontal", command=box.xview)
        box.configure(xscrollcommand=xbar.set)
        xbar.pack(side="bottom", fill="x")
        box.pack(fill="both", expand=True, pady=(0, 4))
        self._fs_detail_box = box
        return win

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
        # 修复缺陷R6：清理虚拟列表与全屏缓存窗口（释放控件/句柄）
        self._fs_list_refresh = None
        for win in (self._fs_list_win, self._fs_detail_win,
                    self._chart_window):
            if win is not None:
                try:
                    win.destroy()
                except (tk.TclError, ValueError):
                    pass
        self._fs_list_win = None
        self._fs_detail_win = None
        if self._virtual_list is not None:
            self._virtual_list.destroy()
            self._virtual_list = None
        self.destroy()


def main() -> None:
    """GUI 启动入口。"""
    app = LogCompressorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
