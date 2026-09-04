# -*- coding: utf-8 -*-
"""全局常量：日志级别体系、堆栈降噪规则、智能分析词表等。

设计说明：
- 级别体系与别名映射集中维护，解析器/过滤/分析层共用，保证口径一致；
- 词表均编译为正则后使用，避免逐行重复编译带来的性能损耗。
"""
from __future__ import annotations

import re
from pathlib import Path

APP_NAME = "log-ai-compressor"
APP_VERSION = "1.0.0"
HUMAN_NAME = "日志AI压缩器"

# ---------------------------------------------------------------------------
# 日志级别体系
# ---------------------------------------------------------------------------
# 优先级从高到低
# 修复缺陷R40：FATAL 级别删除 —— 显式 [FATAL]/CRITICAL 等致命
# 写法经 LEVEL_ALIASES 归一为 ERROR（最高严重级），下游不再出现
# FATAL；级别权重/优先级阈值同步重划（原 ERROR 0.80 达不到 P0
# 阈值 75，P0 档位空缺）
LEVEL_ORDER = ("ERROR", "FAIL", "WARN", "INFO", "DEBUG", "TRACE")

# 级别权重（用于优先级综合计算）
# 修复缺陷R40：五级别重新划分（ERROR 0.95 / FAIL 0.75 / WARN 0.45 /
# INFO 0.20 / DEBUG 0.08）
LEVEL_WEIGHT = {
    "ERROR": 0.95,
    "FAIL": 0.75,
    "WARN": 0.45,
    "INFO": 0.20,
    "DEBUG": 0.08,
    "TRACE": 0.03,
}

# 错误类级别：参与错误统计、时间趋势与聚类
# 修复缺陷R40：FATAL 删除（归一 ERROR），错误类 = ERROR + FAIL
ERROR_LEVELS = frozenset({"ERROR", "FAIL"})

# GUI / CLI 默认勾选级别（ERROR + FAIL 两核心）
# 修复缺陷R40：FATAL 删除（归一 ERROR 后由 ERROR 复选框统一控制）
DEFAULT_SELECTED_LEVELS = ("ERROR", "FAIL")

# 变体级别 -> 规范级别（大小写不敏感匹配，见 normalize_level）
# 修复缺陷R40：致命写法（FATAL/SEVERE/CRITICAL/CRIT/PANIC/EMERG/
# ALERT/FATAL ERROR）全部归一到 ERROR
LEVEL_ALIASES = {
    "WARNING": "WARN",
    "ERR": "ERROR",
    "FATAL": "ERROR",
    "SEVERE": "ERROR",
    "CRITICAL": "ERROR",
    "CRIT": "ERROR",
    "PANIC": "ERROR",
    "EMERG": "ERROR",
    "ALERT": "ERROR",
    "FAILED": "FAIL",
    "FAILURE": "FAIL",
    "ASSERT": "FAIL",
    "ASSERTION": "FAIL",
    "EXCEPTION": "ERROR",
    "NOTICE": "INFO",
    "NOTE": "INFO",
    "PASS": "INFO",
    "FATAL ERROR": "ERROR",
    "E": "ERROR",
    "W": "WARN",
    "I": "INFO",
    "D": "DEBUG",
    "T": "TRACE",
}


def normalize_level(raw: str) -> str:
    """将任意级别写法归一化到 LEVEL_ORDER 中的规范级别。

    无法识别时返回 "INFO"（保守降级，避免未知级别丢失行）。
    """
    token = raw.strip().strip("[]()").upper()
    if token in LEVEL_ALIASES:
        return LEVEL_ALIASES[token]
    if token in LEVEL_ORDER:
        return token
    return "INFO"


# ---------------------------------------------------------------------------
# 堆栈跟踪与降噪规则
# ---------------------------------------------------------------------------
# 判定一行是否为堆栈帧的启发特征（顺序敏感，先判 Java/C 再判 Python）
STACK_FRAME_HINTS = tuple(
    re.compile(p)
    for p in (
        r"^\s*at\s+[\w$.]+\(.*\)",              # Java: at com.foo.Bar.run(Bar.java:10)
        r"^\s*Caused by\s*:?",                  # Java: Caused by: java.lang.Null...
        r"^Traceback \(most recent call last\)",  # Python 回溯头
        r'^\s*File\s+"[^"]+".*,\s*line\s+\d+',   # Python: File "x.py", line 1
        r"^\s*raise\s+\w",                      # Python: raise ValueError(...)
        r"^\s*#?\d+\s+0x[0-9a-f]+\s+in\s+\S+",  # Go/C: 8 0x4a1b2c in main.main
        r"^Backtrace:",                          # 通用 Backtrace 头
        r"^\s*~?\$?\s*0x[0-9a-fA-F]{4,}\s*<",   # 带地址的符号帧
        r"^[A-Za-z_][\w.$]*(?:Exception|Error|Fault|Interrupt)\s*[:({]",  # 异常摘要行
    )
)

# 堆栈降噪：系统库 / 第三方框架帧特征（匹配则视为噪声帧，折叠隐藏）
STACK_NOISE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Python 系统与三方库
        r"site-packages",
        r"dist-packages",
        r"/lib/python\d",
        r"<frozen importlib",
        r"\bimportlib\._bootstrap",
        r"\basyncio\base_events",
        r"\bsocketserver\.py",
        r"\bthreading\.py",
        r"\bssl\.py",
        r"\.venv/",
        r"virtualenv",
        # Java 系统库与主流框架
        r"^\s*at\s+(java|jdk|sun|javax|com\.sun)\.",
        r"java\.base@",
        r"java\.lang\.",
        r"sun\.reflect",
        r"org\.springframework\.",
        r"org\.apache\.(tomcat|catalina|kafka|http|logging)",
        r"io\.netty\.",
        r"org\.hibernate\.",
        r"com\.google\.common",
        r"com\.fasterxml\.jackson",
        r"ch\.qos\.logback",
        r"org\.slf4j",
        # Node / 前端构建
        r"node_modules",
        r"webpack:",
        r"babel[\\/.]",
        # .NET / Windows
        r"at System\.",
        r"at Microsoft\.",
        r"C:\\Windows[\\/]",
        r"Microsoft\.NET\\",
        # C/C++ 系统运行时
        r"libc\.so",
        r"libstdc\+\+",
        r"ld-linux",
        r"/usr/lib/",
        r"/usr/local/lib/",
        r"/lib/x86_64",
    )
)


def is_noise_stack_frame(line: str) -> bool:
    """判断堆栈帧是否为系统库 / 第三方框架噪声帧（用于堆栈精简降噪）。"""
    return any(p.search(line) for p in STACK_NOISE_PATTERNS)


def looks_like_stack_frame(line: str) -> bool:
    """启发式判断一行是否为堆栈帧 / 回溯行。"""
    return any(p.search(line) for p in STACK_FRAME_HINTS)


# ---------------------------------------------------------------------------
# 智能分析词表
# ---------------------------------------------------------------------------
# 根因关键词：命中提升该错误为根因的概率（基础设施类 / 资源类 / 断言类）
ROOT_CAUSE_KEYWORDS = (
    "refused", "timeout", "timed out", "unreachable", "reset by peer",
    "denied", "permission", "not found", "no such", "invalid",
    "corrupt", "overflow", "underflow", "deadlock", "mismatch",
    "out of memory", "oom", "disk full", "no space", "segfault",
    "assert", "assertion", "null pointer", "dereference",
    "connection", "handshake", "expired", "misconfig", "missing",
    # 中文日志根因特征
    "无法连接", "连接失败", "连接被拒", "超时", "内存不足", "磁盘满",
    "权限不足", "未找到", "无效", "断言", "校验失败",
)

# 连锁衍生关键词：命中降低根因概率（重试 / 兜底 / 被动失败）
CASCADE_KEYWORDS = (
    "retry", "retrying", "retried", "fallback", "give up",
    "aborting", "aborted", "after retries", "circuit breaker",
    "due to", "because of", "caused by", "while handling",
    "downstream", "backoff", "skipped", "ignored", "suppressed",
    # 中文连锁衍生特征
    "重试", "中止", "跳过", "降级", "兜底",
)

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------
# 修复缺陷#5：典型样例前后上下文默认 5 行太少，调整为 50 行；
# 修复缺陷R20：放开 200 上限（用户决策：数字可填任意大；MAX_CONTEXT_LINES
# 常量移除）——过大值的内存代价随「上下文行数×簇数」线性增长，由用户
# 按需控制；下限保留（GUI 5 行 / 配置 0 行）
DEFAULT_CONTEXT_LINES = 50     # 典型样例前后上下文行数
MIN_CONTEXT_LINES = 5          # 上下文行数下限（GUI 输入钳制）
DEFAULT_TOP_N = 20             # 默认展示 / 导出的 Top N 错误数（无上限）
CLUSTER_SIMILARITY_THRESHOLD = 0.85   # 聚类编辑距离相似度阈值
MAX_SIMILARITY_COMPARE = 256          # 相似度回退比较的最大模板数（性能保护）
TIMESTAMP_CACHE_SIZE = 65536          # 时间戳解析缓存上限（防内存膨胀）
PROGRESS_EVERY_LINES = 16384          # 进度回调触发行数间隔
CANCEL_CHECK_EVERY_LINES = 4096       # 取消检测行数间隔

# 修复缺陷R4：簇实例记录上限（三层有界，防大日志内存膨胀）
# - 每簇前 N 个实例保留完整条目+前上下文（可展开查看堆栈详情）
# - 每簇后续实例仅记时间戳/行号/摘要（元数据）
# - 全局总实例数上限（超出后不再记录，count 仍准确）
MAX_CLUSTER_INSTANCES_DETAILED = 200    # 每簇含完整详情的实例数上限
MAX_CLUSTER_INSTANCES_META = 2000       # 每簇元数据实例数上限
MAX_TOTAL_INSTANCES = 50000             # 全局实例记录总数上限

# 时间直方图（自适应分桶）参数
HIST_MAX_BUCKETS = 96                # 单簇直方图最大桶数，超出则扩宽桶宽
GLOBAL_HIST_MAX_BUCKETS = 512        # 全局错误趋势直方图最大桶数

# 配置持久化路径（用户目录，避免污染仓库）
CONFIG_DIR = Path.home() / ".log_ai_compressor"
CONFIG_FILE = CONFIG_DIR / "config.json"
