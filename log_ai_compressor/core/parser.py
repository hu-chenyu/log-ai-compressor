# -*- coding: utf-8 -*-
"""流式日志行解析器：规则引擎匹配 + 多行聚合（消息折行 / 堆栈跟踪）。

设计思路
--------
1. 增量 feed(line)：仅当新条目开始时才产出上一个完整条目，天然支持
   消息折行与堆栈多行聚合，无需整文件缓冲（纯流式、O(1) 内存）；
2. 热路径性能：普通行（非缩进、非堆栈特征）只需 1 次规则正则匹配；
   缩进行与已知堆栈前缀先行分流，完整堆栈特征扫描仅作兜底，
   避免每行 ~10 次特征正则的开销；
3. 时间戳解析带「原始串缓存 + 上次成功格式优先」两级优化：
   日志中同一秒的时间戳大量重复，缓存命中后接近 O(1)；
4. 级别缺失时（Jenkins 等无级别格式）由规则集的关键词提示推断。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Optional

from log_ai_compressor.constants import (
    LEVEL_ALIASES,
    LEVEL_ORDER,
    TIMESTAMP_CACHE_SIZE,
    normalize_level,
)
from log_ai_compressor.core.models import LogEntry
from log_ai_compressor.rules.engine import RuleSet

# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------
# 常见时间格式（strptime 兜底；ISO 变体优先走 fromisoformat 快路径）
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%d/%b/%Y:%H:%M:%S",           # Apache 访问日志
    "%d-%b-%Y %H:%M:%S",
    "%b %d %H:%M:%S",              # syslog（无年份，相对排序足够）
    "%H:%M:%S.%f",
    "%H:%M:%S",
)
# 尾部时区（fromisoformat 不支持的紧凑形式）
_TS_TZ_SUFFIX = re.compile(r"\s*(?:Z|[+-]\d{2}:?\d{2})\s*$")
# 纯数字（epoch 秒 / 嵌入式相对秒）
_TS_NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")

_CACHE_MISS = object()


def _to_epoch(dt: datetime) -> float:
    """datetime -> 浮点秒（兼容 Windows：1970 前的 naive 时间会抛 OSError）。

    无年份格式（HH:MM:SS / syslog）经 strptime 得到 1900 年，此处统一
    替换为安全基准年 2000，保证同文件内时间排序正确。
    """
    if dt.year < 1970:
        dt = dt.replace(year=2000)
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        return (dt - datetime(1970, 1, 1)).total_seconds()


class TimestampParser:
    """时间戳解析器：原始串缓存 + 上次成功格式优先。"""

    def __init__(self, cache_size: int = TIMESTAMP_CACHE_SIZE):
        self._cache: Dict[str, Optional[float]] = {}
        self._cache_size = cache_size
        self._fmt_index = 0   # 上次成功的 strptime 格式索引

    def parse(self, raw: Optional[str]) -> Optional[float]:
        """解析时间戳为浮点秒（epoch 或相对秒）；失败返回 None。"""
        if not raw:
            return None
        key = raw.strip()
        if not key:
            return None
        hit = self._cache.get(key, _CACHE_MISS)
        if hit is not _CACHE_MISS:
            return hit  # type: ignore[return-value]
        value = self._parse_uncached(key)
        if len(self._cache) >= self._cache_size:
            self._cache.clear()   # 防内存膨胀：简单清空重建
        self._cache[key] = value
        return value

    # ------------------------------------------------------------------
    def _parse_uncached(self, key: str) -> Optional[float]:
        # 1) ISO 快路径（fromisoformat 覆盖大多数现代日志）
        iso = key.replace(",", ".")
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        try:
            return _to_epoch(datetime.fromisoformat(iso))
        except ValueError:
            pass

        # 2) 纯数字：epoch 秒或嵌入式相对计时秒
        if _TS_NUMERIC.match(key):
            try:
                return float(key)
            except ValueError:
                return None

        # 3) 紧凑时区后缀剥离后重试 ISO
        stripped = _TS_TZ_SUFFIX.sub("", key).strip()
        if stripped != key:
            iso = stripped.replace(",", ".")
            try:
                return _to_epoch(datetime.fromisoformat(iso))
            except ValueError:
                key = stripped

        # 4) strptime 格式列表（上次成功格式优先）
        order = [self._fmt_index] + [
            i for i in range(len(_TS_FORMATS)) if i != self._fmt_index
        ]
        for i in order:
            try:
                dt = datetime.strptime(key, _TS_FORMATS[i])
                self._fmt_index = i
                return _to_epoch(dt)
            except ValueError:
                continue
        return None


# ---------------------------------------------------------------------------
# 模块/级别推断（消息头部）
# ---------------------------------------------------------------------------
# "auth - login failed" / "auth: login failed" / "auth | login failed"
_HEAD_TOKEN_SEP = re.compile(r"^([\w][\w.\-/]{0,63})\s*(?::\s|\s[-|>]\s|\s\|\s)")


def _is_level_token(token: str) -> bool:
    up = token.upper()
    return up in LEVEL_ALIASES or up in LEVEL_ORDER


# ---------------------------------------------------------------------------
# 日志解析器
# ---------------------------------------------------------------------------
# 已知堆栈前缀（非缩进的堆栈行特征，用于廉价的预分流）
_FLUSH_STACK_PREFIXES = ("Caused by", "Traceback", "Backtrace", "at ",
                         "File ", "raise ", "terminate")
# 异常摘要行（flush-left，如 java.net.ConnectException: xxx）
_EXCEPTION_LINE_RE = re.compile(
    r"^[A-Za-z_][\w.$]*(?:Exception|Error|Fault|Interrupt)\s*[:({]")


class LogParser:
    """增量式日志解析器（通过 RuleSet 匹配行，与具体格式解耦）。

    判定顺序（热路径优化）：
    A. 空行跳过；
    B. 缩进行：堆栈模式直接并入堆栈，否则查堆栈特征，再否则消息折行；
    C. 非缩进行：已知堆栈前缀 / 异常摘要行先于规则匹配判定
       （防止宽松 pattern 吞掉堆栈帧）；
    D. 规则匹配 -> 产出上一个条目，开启新条目；
    E. 规则未命中 -> 完整堆栈特征扫描（兜底）；
    F. 无结构行：关键词推级别，独立成条目。
    """

    def __init__(self, ruleset: RuleSet):
        self._rules = ruleset
        self._ts = TimestampParser()
        self._current: Optional[LogEntry] = None
        self._in_stack = False   # 当前条目处于堆栈聚合模式

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def feed(self, line: str, line_no: int) -> Optional[LogEntry]:
        """喂入一行（不含换行符）。

        若该行开启了新条目，则返回上一个完整条目；否则返回 None
        （空行跳过 / 续行并入当前条目）。
        """
        if not line or line.isspace():
            return None  # 空行不产出、不断裂续行

        # A/B) 缩进行：堆栈优先（Python 回溯的源码行等），否则消息折行
        if line[0] in " \t":
            if self._in_stack or self._rules.match_stack_indicator(line.strip()):
                self._append_stack_line(line, line_no)
            elif self._current is not None:
                self._current.message_extra.append(line.strip())
                self._current.last_line_no = line_no
            else:
                # 无当前条目的缩进行：按堆栈帧独立处理
                self._append_stack_line(line, line_no)
            return None

        # C) 已知堆栈前缀 / 异常摘要行：先于规则匹配判定
        if (line.startswith(_FLUSH_STACK_PREFIXES)
                or _EXCEPTION_LINE_RE.match(line)):
            if self._rules.match_stack_indicator(line.strip()):
                self._append_stack_line(line, line_no)
                return None

        # D) 规则匹配 -> 产出上一个条目，开启新条目
        match = self._rules.match_line(line)
        if match is not None:
            prev = self._current
            self._current = self._build_entry(match, line, line_no)
            self._in_stack = False
            return prev

        # E) 完整堆栈特征扫描（兜底：未命中任何规则的堆栈形态）
        if self._rules.match_stack_indicator(line.strip()):
            self._append_stack_line(line, line_no)
            return None

        # F) 无结构行：关键词推级别，独立成条目
        prev = self._current
        self._current = LogEntry(
            line_no=line_no, raw=line, last_line_no=line_no,
            level=self._rules.infer_level_by_keyword(line) or "INFO",
            message=line.strip(),
        )
        self._in_stack = False
        return prev

    def flush(self) -> Optional[LogEntry]:
        """流结束时返回最后一个未产出条目。"""
        entry = self._current
        self._current = None
        self._in_stack = False
        return entry

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _append_stack_line(self, line: str, line_no: int) -> None:
        """将堆栈行并入当前条目（保留缩进便于阅读）。"""
        text = line.rstrip()
        if self._current is None:
            # 堆栈出现在条目之外：独立成条目（常见于截断日志）
            self._current = LogEntry(
                line_no=line_no, raw=line, last_line_no=line_no,
                level=self._rules.infer_level_by_keyword(line) or "ERROR",
                message="", stack=[text],
            )
        else:
            self._current.stack.append(text)
            self._current.last_line_no = line_no
        self._in_stack = True

    def _build_entry(self, match: re.Match, line: str, line_no: int) -> LogEntry:
        """从规则匹配结果构建 LogEntry（含级别/模块/时间戳后处理）。"""
        groups = match.groupdict()
        ts_raw = groups.get("timestamp")
        level_raw = (groups.get("level") or "").strip()
        module = (groups.get("module") or "").strip()
        message = (groups.get("message") or "").strip()

        # 级别：显式字段 > 关键词推断 > INFO
        if level_raw:
            level = normalize_level(level_raw)
        else:
            level = self._rules.infer_level_by_keyword(message) or "INFO"

        # 消息头部令牌：可能是模块名，也可能是补写的级别（"ERROR - xxx"）
        # （已有模块与级别时跳过，节省热路径开销）
        if not module or not level_raw:
            head = _HEAD_TOKEN_SEP.match(message)
            if head:
                token = head.group(1)
                if _is_level_token(token):
                    if not level_raw:
                        level = normalize_level(token)
                    message = message[head.end():].strip()
                elif not module:
                    module = token
                    message = message[head.end():].strip()

        return LogEntry(
            line_no=line_no, raw=line, last_line_no=line_no,
            timestamp=self._ts.parse(ts_raw),
            level=level, module=module, message=message,
        )
