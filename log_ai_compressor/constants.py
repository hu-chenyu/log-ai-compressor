# -*- coding: utf-8 -*-
"""全局常量：日志级别体系、堆栈降噪规则、智能分析词表等。

设计说明：
- 级别体系与别名映射集中维护，解析器/过滤/分析层共用，保证口径一致；
- 词表均编译为正则后使用，避免逐行重复编译带来的性能损耗。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

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


def canonical_level(raw: str) -> Optional[str]:
    """级别写法 -> 规范级别；**无法识别时返回 None**（不猜）。

    级别串两端常见分隔符（``[ERROR]`` / ``ERROR:`` / ``ERROR -``）一并
    剥掉：拼写带分隔符时此前一律落到 normalize_level 的 INFO 兜底，而
    ERROR 行的级别一旦变 INFO 就会被默认的「只看 ERROR/FAIL」过滤
    **整条丢掉** —— 用户拿到一份「没有错误」的报告，日志里却全是错误。

    与 normalize_level 的区别就在「不认得的写法」上：本函数说不知道，
    normalize_level 保守当 INFO（解析层需要不丢行），而**参数校验**必须
    用本函数 —— 否则 `--level EROR` 会被悄悄改成 INFO，报告里全是
    信息级条目，比报错更糟。
    """
    token = raw.strip(" \t[]():-|>").strip().upper()
    return LEVEL_ALIASES.get(token) or (token if token in LEVEL_ORDER else None)


def normalize_level(raw: str) -> str:
    """将任意级别写法归一化到 LEVEL_ORDER 中的规范级别。

    无法识别时返回 "INFO"（保守降级，避免未知级别丢失行）。
    """
    return canonical_level(raw) or "INFO"


# ---------------------------------------------------------------------------
# 堆栈跟踪与降噪规则
# ---------------------------------------------------------------------------
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
# 部署/变更事件关联（跨服务/时间窗/部署事件关联）
# ---------------------------------------------------------------------------
# 变更事件词表（保守）：只认明确的启动/停止/发布/重启措辞，
# 宁漏勿误 —— 把普通错误行当成"发版"会给出误导性的时间窗叙事。
CHANGE_EVENT_RE = re.compile(
    r"(?:"
    # 启动 / 停止（Spring Boot / systemd / 通用服务日志）
    r"started\s+\S.*\sin\s+\d+(?:\.\d+)?\s*(?:s|sec|seconds|ms)\b"
    r"|\bstarting\s+(?:up\s+)?(?:version|service|server|application)\b"
    r"|\bshutting\s+down\b|\bshutdown\s+(?:initiated|complete|completed|requested)\b"
    r"|\bstopped\s+(?:the\s+)?(?:service|server|daemon|instance|application)\b"
    r"|\brestarted\b"
    # 发布 / 变更
    r"|\bdeploy(?:ed|ing|ment)\b|\brollback\b|\bupgrad(?:ed|ing)\b"
    r"|\bversion\s+\S+\s+(?:started|released|deployed)\b"
    r"|\bSIGTERM\b|\bSIGKILL\b"
    # 中文
    r"|服务(?:启动|重启|停止|发布|上线|下线)|系统(?:启动|重启)"
    r"|(?:发布|发版|上线|下线|停机|重启)(?:完成|开始|成功|失败)?"
    r")",
    re.IGNORECASE,
)
CHANGE_WINDOW_SEC = 1800.0        # 变更事件 ↔ 错误首发关联时间窗（30 分钟）
MAX_CHANGE_EVENTS = 200           # 变更事件记录上限（内存有界）

# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------
# 修复缺陷#5：典型样例前后上下文默认 5 行太少，调整为 50 行；
# 修复缺陷R20：放开 200 上限（用户决策：数字可填任意大；MAX_CONTEXT_LINES
# 常量移除）——过大值的内存代价随「上下文行数×簇数」线性增长，由用户
# 按需控制；下限保留（GUI 5 行 / 配置 0 行）
DEFAULT_CONTEXT_LINES = 50     # 典型样例前后上下文行数
DEFAULT_TOP_N = 20             # 默认展示 / 导出的 Top N 错误数（无上限）
CLUSTER_SIMILARITY_THRESHOLD = 0.85   # 聚类编辑距离相似度阈值
# 优化缺陷R84：相似度阈值三档预设（严格=几乎相同才并簇 / 标准=默认
# / 宽松=相似即并、压缩更激进），GUI 选择器与管线共用
SIMILARITY_PRESETS = {"strict": 0.95,
                      "standard": CLUSTER_SIMILARITY_THRESHOLD,
                      "lenient": 0.70}
# 相似度回退的**开关**：<= 0 表示整个关掉相似度回退（只走精确指纹匹配）。
#
# 它曾经叫「比较的最大模板数」，用来把候选集截成最近 256 个 —— 那等于
# 用**结果正确性**换算力：候选集变成「最近 N 个」之后，第 1 个簇到第
# (桶长 - 256) 个簇永远比不到，聚类结果**依赖日志行序**（实测复现见
# `tests/test_cluster_order_independence.py`）。
#
# 试过「超上限时按模板字典序取前 N 个」，**更糟**：固定的前 N 个会让
# 字母靠后的模板永远找不到同类（Loghub Mac 的 INFO 桶 604 -> 1604 个簇，
# 同预算覆盖率 85.8% -> 64.4%）。任何「固定取前 N」的截断都是系统性偏袒。
#
# 所以现在**没有上限**：候选集是整个级别桶，性能靠 `_find_similar` 里
# 「ratio <= 2*min(|a|,|b|)/(|a|+|b|)」这个精确长度上界来保。
MAX_SIMILARITY_COMPARE = 256
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

# 每簇最多展开多少种**不同的** Caused-by 内层因（v2.7.0）。
# 簇是按「外层消息+堆栈指纹」聚的，内层因理论上可以无限多（把变量都归一化
# 掉即可）。不设上限就是拿内存换报告长度，且这种簇的变体清单对读者毫无价值。
# 超出部分不静默丢弃，而是计入 ErrorCluster.cause_overflow —— 报告里如实写
# 「另有 N 次的内层因未展开」。
MAX_CAUSES_PER_CLUSTER = 32
# 每簇最多记住多少个**关联键**（request_id / trace_id）。用于对比分析按
# trace 对齐（core/comparator.py）：对齐只需要「有没有共同 trace」，
# 上限只防内存无界 —— 记住更多并不让判定更准。
MAX_CLUSTER_TRACE_IDS = 64
# 报告/导出里最多列出前几种变体（cause_counts 仍全量保留）
MAX_CAUSES_SHOWN = 8

# 时间直方图（自适应分桶）参数
HIST_MAX_BUCKETS = 96                # 单簇直方图最大桶数，超出则扩宽桶宽
GLOBAL_HIST_MAX_BUCKETS = 512        # 全局错误趋势直方图最大桶数

# 配置持久化路径（用户目录，避免污染仓库）
CONFIG_DIR = Path.home() / ".log_ai_compressor"
CONFIG_FILE = CONFIG_DIR / "config.json"
