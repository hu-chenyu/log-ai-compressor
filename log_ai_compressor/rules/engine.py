# -*- coding: utf-8 -*-
"""YAML 可插拔解析规则引擎。

设计思路
--------
1. 解析规则与核心逻辑彻底解耦：规则以 YAML 声明（正则 patterns + 堆栈特征 +
   级别关键词提示），引擎在加载时统一编译并缓存，运行期零编译开销；
2. 无需修改代码即可扩展：内置 generic / embedded / jenkins 三套模板，
   同时支持 `--rule <path/to/custom.yaml>` 加载外部规则；
3. 占位符 `{LEVEL}` 会被展开为标准级别令牌交替表，保证各模板级别口径一致；
4. 引擎内置与 generic.yaml 等价的兜底规则集，即使包数据缺失也能正常工作。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Pattern

import yaml

from log_ai_compressor.constants import normalize_level

PRESET_DIR = Path(__file__).resolve().parent / "presets"

# 标准级别令牌交替表（供 {LEVEL} 占位符展开；长令牌必须排在短前缀之前，
# 否则 "ERR" 会先于 "ERROR" 命中导致级别被截断）
LEVEL_TOKENS = (
    "TRACE|DEBUG|INFO|NOTICE|NOTE|WARNING|WARN|ERROR|ERR|SEVERE|FATAL|"
    "CRITICAL|CRIT|PANIC|EMERG|ALERT|FAILURE|FAILED|FAIL|EXCEPTION|ASSERTION|ASSERT"
)

# 单字母/短缩写级别（仅建议在明确的括号上下文中使用）
SHORT_LEVEL_TOKENS = "ERR|E|W|I|D|T|F|P"

# 非结构化行的级别关键词提示（引擎默认值，可被 YAML 覆盖）
# 修复缺陷R18：删除 FATAL 提示（\bFATAL\b 忽略大小写误中 gcc 选项
# -Wfatal-errors —— 构建日志编译命令行被批量误判为致命错误；真实
# 致命错误经显式级别字段 / gcc_style 的 fatal error 别名仍正确
# 识别为 FATAL，关键词推断最高级别为 ERROR）
DEFAULT_LEVEL_HINTS = {
    "ERROR": [r"\bERROR\b", r"\bERR\b", r"\berror\b", r"\bException\b",
              r"\bexception\b", r"uncaught"],
    "FAIL": [r"\bFAIL(?:ED|URE|URES)?\b", r"\bfail(?:ed|ure)?\b",
             r"\bASSERT(?:ION)?\b", r"\bassert\b"],
    "WARN": [r"\bWARN(?:ING)?\b", r"\bwarning\b"],
}

# 堆栈行识别特征（引擎默认值，可被 YAML 覆盖）
DEFAULT_STACK_INDICATORS = [
    r"^\s*at\s+[\w$.]+\(",                # Java 帧格式
    r"^\s*Caused by\s*:?",                # Java 因果链
    r"^Traceback \(most recent call last\)",  # Python 回溯头
    r'^\s*File\s+"[^"]+".*,\s*line\s+\d+',   # Python 帧
    r"^\s*raise\s+\w",                    # Python raise
    r"^Backtrace:",                       # 通用回溯头
    r"^\s*~?\$?\s*0x[0-9a-fA-F]{4,}\s*<",   # C/C++ 符号帧
    r"^[A-Za-z_][\w.$]*(?:Exception|Error|Fault|Interrupt)\s*[:({]",  # 异常摘要行
]

# 正则 flag 字符串 -> re 常量
_FLAG_MAP = {"i": re.IGNORECASE, "ignorecase": re.IGNORECASE,
             "m": re.MULTILINE, "multiline": re.MULTILINE}


class RuleSetError(ValueError):
    """规则集定义错误（正则非法 / 缺少 patterns 等）。"""


@dataclass
class PatternRule:
    """单条解析正则规则（加载时已编译）。"""
    name: str
    regex: Pattern[str]
    pattern_text: str


@dataclass
class RuleSet:
    """一套完整的日志解析规则（patterns + 堆栈特征 + 级别提示）。"""

    name: str
    description: str = ""
    patterns: List[PatternRule] = field(default_factory=list)
    stack_indicators: List[Pattern[str]] = field(default_factory=list)
    level_hints: Dict[str, List[Pattern[str]]] = field(default_factory=dict)
    source: str = "builtin"
    # 上次命中的 pattern 索引（热路径优化：同格式日志优先重试上次命中的正则）
    _last_pattern: int = -1

    # 修复缺陷#9：无结构行兜底路径的合并正则缓存（构造时构建）
    # - 堆栈特征 8 条 -> 单条交替正则（1 次 search 替代 8 次）
    # - 级别提示按级别合并（每级别 1 次 search 替代逐条多次）
    _stack_combined: Optional[Pattern[str]] = None
    _hint_combined: Dict[str, Pattern[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """构造后处理：预合并热路径正则（失败时自动退回逐条匹配）。"""
        try:
            self._stack_combined = re.compile(
                "|".join(p.pattern for p in self.stack_indicators))
        except re.error:
            self._stack_combined = None  # 含命名组冲突等场景，退回逐条
        self._hint_combined = {}
        for level, pats in self.level_hints.items():
            if not pats:
                continue
            try:
                self._hint_combined[level] = re.compile(
                    "|".join(p.pattern for p in pats), re.IGNORECASE)
            except re.error:
                continue  # 退回该级别逐条匹配

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict, source: str = "builtin") -> "RuleSet":
        """从字典（YAML 反序列化结果或内置定义）构建并编译规则集。"""
        if not isinstance(data, dict):
            raise RuleSetError("规则集必须是字典结构")
        name = str(data.get("name") or "unnamed")

        patterns: List[PatternRule] = []
        raw_patterns = data.get("patterns") or []
        if not raw_patterns:
            raise RuleSetError(f"规则集 {name!r} 至少需要一条 pattern")
        for i, item in enumerate(raw_patterns):
            if isinstance(item, str):
                item = {"regex": item}
            text = item.get("regex")
            if not text:
                raise RuleSetError(f"规则集 {name!r} 第 {i + 1} 条 pattern 缺少 regex")
            text = cls._expand(text)
            flags = 0
            for f in item.get("flags") or []:
                flags |= _FLAG_MAP.get(str(f).lower(), 0)
            try:
                compiled = re.compile(text, flags)
            except re.error as exc:
                raise RuleSetError(
                    f"规则集 {name!r} 第 {i + 1} 条 pattern 正则非法: {exc}"
                ) from exc
            patterns.append(PatternRule(str(item.get("name") or f"p{i}"), compiled, text))

        stack_indicators = [
            re.compile(cls._expand(t))
            for t in (data.get("stack_indicators") or DEFAULT_STACK_INDICATORS)
        ]

        level_hints: Dict[str, List[Pattern[str]]] = {}
        raw_hints = data.get("level_hints") or DEFAULT_LEVEL_HINTS
        for raw_level, pats in raw_hints.items():
            canon = normalize_level(str(raw_level))
            level_hints.setdefault(canon, []).extend(
                re.compile(p, re.IGNORECASE) for p in pats
            )
        return cls(name=name, description=str(data.get("description") or ""),
                   patterns=patterns, stack_indicators=stack_indicators,
                   level_hints=level_hints, source=source)

    @staticmethod
    def _expand(text: str) -> str:
        """展开正则占位符（{LEVEL} 等），统一各模板的级别口径。"""
        return text.replace("{LEVEL}", LEVEL_TOKENS)

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    def match_line(self, line: str) -> Optional[re.Match]:
        """按顺序尝试所有 pattern（优先重试上次命中的），返回首个匹配。"""
        if 0 <= self._last_pattern < len(self.patterns):
            m = self.patterns[self._last_pattern].regex.match(line)
            if m:
                return m
        for idx, rule in enumerate(self.patterns):
            m = rule.regex.match(line)
            if m:
                self._last_pattern = idx
                return m
        return None

    def match_stack_indicator(self, line: str) -> bool:
        """判断一行是否命中堆栈特征。

        修复缺陷#9：优先使用构造期合并的单条交替正则（1 次扫描
        覆盖全部特征，无结构行兜底路径提速约 8 倍）；合并失败或
        语义需要逐条时自动退回原实现。
        """
        combined = self._stack_combined
        if combined is not None:
            return combined.search(line) is not None
        return any(p.search(line) for p in self.stack_indicators)

    def infer_level_by_keyword(self, text: str) -> Optional[str]:
        """对无级别字段的行按关键词推断级别（ERROR > FAIL > WARN）。

        修复缺陷R40：FATAL 级别删除（归一 ERROR），推断序列移除；
        修复缺陷#9：每级别先查合并正则（单次扫描），未命中再逐条
        兜底；级别间仍按优先级顺序判定，语义与逐条实现完全一致。
        """
        for level in ("ERROR", "FAIL", "WARN"):
            combined = self._hint_combined.get(level)
            if combined is not None:
                if combined.search(text):
                    return level
                continue
            for pat in self.level_hints.get(level, ()):
                if pat.search(text):
                    return level
        return None


# ---------------------------------------------------------------------------
# 兜底规则集（等价 generic.yaml，包数据缺失时使用）
# ---------------------------------------------------------------------------
_TS_ISO = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?"
    r"(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?"
)

BUILTIN_RULESET = {
    "name": "generic",
    "description": "内置兜底规则（等价 generic.yaml）",
    "patterns": [
        {
            "name": "iso_level_module",
            # ISO 时间戳 + 级别(可带括号) + [模块] + 分隔符 + 内容
            "regex": (
                r"^(?P<timestamp>%s)\s*[\[(]?\s*(?P<level>{LEVEL})\s*[\])]?:?\s*"
                r"(?:[\[(](?P<module>[^()\]]{1,64})[)\]]\s*)?"
                r"(?:[-:>|\u2014]\s*)?(?P<message>.*)$" % _TS_ISO
            ),
        },
        {
            "name": "iso_level_icase",
            # ISO 时间戳 + 小写级别 + 强制分隔符（避免消息首词误判为级别）
            "regex": (
                r"^(?P<timestamp>%s)\s*(?P<level>{LEVEL})\s*[:\-\|]\s*"
                r"(?P<message>.*)$" % _TS_ISO
            ),
            "flags": ["i"],
        },
        {
            "name": "time_level_module",
            # HH:MM:SS[.fff] [模块] 级别 内容
            "regex": (
                r"^(?P<timestamp>\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)\s*"
                r"(?:\[(?P<module>[^\]]{1,64})\]\s*)?"
                r"[\[(]?\s*(?P<level>{LEVEL})\s*[\])]?:?\s*(?P<message>.*)$"
            ),
        },
        {
            "name": "level_first",
            # [级别] / 级别: 开头（pytest、maven 等风格）
            "regex": (
                r"^[\[(]?\s*(?P<level>{LEVEL})\s*[\])]?\s*[:\-\|]?\s*"
                r"(?P<message>\S.*)$"
            ),
        },
        {
            "name": "gcc_style",
            # 源文件:行号: 级别: 内容（gcc/clang/嵌入式单测输出）
            "regex": (
                r"^(?P<module>\S+\.(?:c|h|cc|cpp|cxx|hpp|hh)):(?P<lineno>\d+)"
                r"(?::\d+)?\s*:\s*(?:(?P<level>fatal error|error|warning|note)\s*:\s*)?"
                r"(?P<message>.*)$"
            ),
            "flags": ["i"],
        },
        {
            "name": "syslog",
            # syslog: 月 日 时间 主机/进程 消息
            "regex": (
                r"^(?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
                r"(?P<module>[\w.\-/]+)(?:\[\d+\])?:?\s*(?P<message>.*)$"
            ),
        },
    ],
}


# ---------------------------------------------------------------------------
# 加载接口
# ---------------------------------------------------------------------------
def _load_file(path: Path) -> RuleSet:
    """从 YAML 文件加载并构建规则集。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleSetError(f"YAML 解析失败: {path} ({exc})") from exc
    return RuleSet.from_dict(data or {}, source=str(path))


def list_presets() -> List[str]:
    """列出内置模板名（presets 目录下的 yaml 文件名）。"""
    if not PRESET_DIR.exists():
        return ["generic"]
    return sorted(p.stem for p in PRESET_DIR.glob("*.yaml"))


def load_ruleset(name_or_path: Optional[str] = None) -> RuleSet:
    """加载解析规则集。

    参数支持三种形式：
    - None / "" / "generic"     -> 通用模板（缺失时退回内置兜底规则）
    - 模板名（generic/embedded/jenkins）-> presets 目录下同名 yaml
    - YAML 文件路径             -> 外部自定义规则
    """
    if not name_or_path:
        path = PRESET_DIR / "generic.yaml"
        return _load_file(path) if path.exists() else RuleSet.from_dict(BUILTIN_RULESET)

    candidate = Path(name_or_path)
    # 明确的 yaml/yml 后缀或存在的路径 -> 按文件加载
    if candidate.suffix.lower() in (".yaml", ".yml"):
        if not candidate.exists():
            raise FileNotFoundError(f"规则文件不存在: {candidate}")
        return _load_file(candidate)
    if candidate.exists():
        return _load_file(candidate)

    # 按模板名加载
    preset = PRESET_DIR / f"{name_or_path}.yaml"
    if preset.exists():
        return _load_file(preset)
    if name_or_path == "generic":
        return RuleSet.from_dict(BUILTIN_RULESET)
    available = ", ".join(list_presets())
    raise RuleSetError(f"未找到解析规则 {name_or_path!r}（可用模板: {available}）")
