# -*- coding: utf-8 -*-
"""级别与关键字过滤：错误条目进入聚类前的准入判断。

设计说明：
- 关键字统一小写子串匹配（包含/排除），预编译缓存避免逐条重建；
- 过滤只做准入判断，不修改条目本身，保证与聚类/分析层解耦。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_SELECTED_LEVELS,
    DEFAULT_TOP_N,
)
from log_ai_compressor.core.models import LogEntry


@dataclass
class FilterConfig:
    """过滤配置（GUI / CLI 共用）。"""

    # 修复缺陷R10：默认级别含 FATAL（FATAL 改为受复选框控制）
    levels: List[str] = field(
        default_factory=lambda: list(DEFAULT_SELECTED_LEVELS))
    include: List[str] = field(default_factory=list)   # 包含关键字（任一命中即保留）
    exclude: List[str] = field(default_factory=list)   # 排除关键字（任一命中即剔除）
    top_n: int = DEFAULT_TOP_N
    context_lines: int = DEFAULT_CONTEXT_LINES

    @classmethod
    def defaults(cls) -> "FilterConfig":
        return cls()

    def normalized_include(self) -> List[str]:
        return [k.strip().lower() for k in self.include if k and k.strip()]

    def normalized_exclude(self) -> List[str]:
        return [k.strip().lower() for k in self.exclude if k and k.strip()]

    def as_dict(self) -> dict:
        return {
            "levels": list(self.levels),
            "include": list(self.include),
            "exclude": list(self.exclude),
            "top_n": self.top_n,
            "context_lines": self.context_lines,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FilterConfig":
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        if isinstance(data.get("levels"), (list, tuple)):
            cfg.levels = [str(x) for x in data["levels"]]
        for key in ("include", "exclude"):
            if isinstance(data.get(key), (list, tuple)):
                setattr(cfg, key, [str(x) for x in data[key]])
        if isinstance(data.get("top_n"), int):
            # 修复缺陷R20：Top N 无上限（数字可填任意大，≥1）
            cfg.top_n = max(1, data["top_n"])
        if isinstance(data.get("context_lines"), int):
            # 修复缺陷R20：上下文行数无上限（数字可填任意大，≥0）；
            # 过大值内存代价随上下文×簇数线性增长，由用户按需控制
            cfg.context_lines = max(0, data["context_lines"])
        return cfg


class EntryFilter:
    """错误条目准入过滤器（级别 + 包含/排除关键字）。"""

    def __init__(self, config: FilterConfig):
        self._levels = set(config.levels or DEFAULT_SELECTED_LEVELS)
        self._include = config.normalized_include()
        self._exclude = config.normalized_exclude()

    # ------------------------------------------------------------------
    def match(self, entry: LogEntry) -> bool:
        """判断条目是否通过过滤。

        修复缺陷R40：FATAL 级别删除（解析层经 LEVEL_ALIASES 归一为
        ERROR），准入判断回归纯级别集合匹配，无特殊放行分支。
        """
        if entry.level not in self._levels:
            return False
        text = self._search_text(entry)
        if self._exclude and any(k in text for k in self._exclude):
            return False
        if self._include and not any(k in text for k in self._include):
            return False
        return True

    def _search_text(self, entry: LogEntry) -> str:
        """关键字检索范围：模块 + 完整消息 + 堆栈。"""
        parts = [entry.module, entry.full_message]
        if entry.stack:
            parts.extend(entry.stack)
        return "\n".join(p for p in parts if p).lower()

    # ------------------------------------------------------------------
    @property
    def levels(self) -> set:
        return self._levels
