# -*- coding: utf-8 -*-
"""结构化日志原生解析：JSON Lines 与 logfmt。

为什么这是本工具最重要的功能缺口
--------------------------------
2026 年主流框架的**默认输出已经是 JSON**：Spring Boot 4.x 原生支持
ECS / Logstash / GELF 三种 JSON 格式并内置 OpenTelemetry starter；
OpenTelemetry 日志数据模型本身也以结构化字段为准。这意味着一个只会
拿正则去刮纯文本日志的分析工具，在真实生产日志面前是**残废的** ——
它把 `{"level":"ERROR","message":"db down"}` 整行当 message，
级别、模块、trace_id 全部丢失。

这也是本工具相对云厂商唯一结构性的优势：**Datadog / SLS / Grafana
需要你先装 agent、配 schema、走埋点，才能拿到结构化字段；本工具只要
一个文件路径。** 零部署成本，这是打不赢也绕不开的那条路。

两种格式的处理
--------------
- **JSON Lines**：每行一个 JSON 对象。用标准库 `json`，不引新依赖。
- **logfmt**：`key=value` 空格分隔（Heroku 约定，Vector / Fluentd
  默认输出格式）。手写解析，避免为了一个格式加依赖。

设计约束
--------
1. **零新依赖**：只用标准库。PyPI 上轻量是硬通货（见 POSITIONING §5.1）。
2. **不破坏既有路径**：解析器只在"这一行确实像结构化日志"时才走这里，
   纯文本日志的规则匹配路径一行代码都不改。
3. **字段别名映射**：不同生态叫法不同（`level`/`severity`/`lvl`、
   `message`/`msg`/`@message`、Otel 的 `log.level` 点号命名空间…），
   统一收敛到 LogEntry 的既有字段。
4. **其余字段不丢**：JSON 里所有没被认领的键保留下来（`fields`），
   trace_id / request_id 这类关联键单独提出来 —— 它们是证据链的原料。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 字段别名表（大小写不敏感；点号命名空间走扁平化后的末段匹配）
# ---------------------------------------------------------------------------
_LEVEL_KEYS = ("level", "severity", "severity_text", "severitytext", "lvl",
               "loglevel", "levelname", "level_name", "priority")
_MESSAGE_KEYS = ("message", "msg", "log", "event", "@message", "short_message",
                 "text", "description")
_TIMESTAMP_KEYS = ("timestamp", "time", "ts", "@timestamp", "eventtime",
                   "datetime", "date", "asctime", "event_time")
_SERVICE_KEYS = ("service", "service.name", "servicename", "logger",
                 "logger_name", "loggername", "app", "application", "app_name",
                 "module", "component", "source", "logger_name")
_TRACE_KEYS = ("trace_id", "traceid", "trace.id", "otel.trace_id", "trace")
_REQUEST_KEYS = ("request_id", "requestid", "request.id", "correlation_id",
                 "correlationid", "req_id", "reqid", "requestid_internal")
_SPAN_KEYS = ("span_id", "spanid", "span.id", "otel.span_id")
_ERROR_KEYS = ("error", "err", "exception", "exc", "error.message",
               "stack_trace", "stacktrace", "stack")
_EXCEPTION_KEYS = ("exception.type", "exception_type", "exceptiontype",
                   "exc_type")

# logfmt：`key=value`，value 可带引号
_LOGFMT_RE = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|'
                        r"'(?:[^'\\]|\\.)*'|[^\s]*)")


def _flatten(d: Dict[str, Any], out: Optional[Dict[str, Any]] = None,
             prefix: str = "") -> Dict[str, Any]:
    """展开嵌套命名空间，使一套别名表通吃各生态写法。

    三种写法都要覆盖（真实日志里同时存在）：
        {"log.level":"ERROR"}          点号命名空间（OTel / ECS 扁平输出）
        {"log": {"level": "ERROR"}}    真正嵌套
        {"service": {"name": "api"}}   嵌套对象整体是一个语义字段

    父键保留**原始值**（不拼成字符串）：`{"message": {"x": 2}}` 拼起来会
    得到 "2" 这种垃圾消息，宁可让 `_as_text` 判否、退回纯文本路径。
    同时递归产出点号全路径，`{"service":{"name":"api"}}` 因此能被
    `service.name` 这个别名命中。原始键完整保留在 fields 里，信息不丢。
    """
    out = {} if out is None else out
    for k, v in d.items():
        key = k.lower()
        full = f"{prefix}{key}"
        out[full] = v
        if isinstance(v, dict):
            _flatten(v, out, prefix=f"{full}.")
    return out


def _pick(flat: Dict[str, Any], keys) -> Optional[Any]:
    """按别名表取字段。

    裸别名（`level`/`message`/`trace_id`）额外做一次**点号末段**匹配，
    以覆盖 `log.level` / `event.severity` 这类前缀五花八门的写法 ——
    前缀没法穷举，末段是稳定的。

    带点号的别名（`error.message`、`exception.type`）**只做精确匹配**。
    否则 `error.message` 的末段 "message" 会去匹配普通的 `message`
    字段，把错误消息错认成异常摘要 —— 而且看起来完全正常，不会报错。
    任何兜底匹配都可能抓错对象，宁可漏判也不能抓错。
    """
    for k in keys:
        if k in flat and flat[k] not in (None, "", {}):
            v = flat[k]
            if isinstance(v, (dict, list)):
                continue
            return v
    for k in keys:
        if "." in k:
            continue                    # 带命名空间的别名不参与末段兜底
        for full, v in flat.items():
            if isinstance(v, (dict, list)) or v in (None, "", {}):
                continue
            if full.rsplit(".", 1)[-1] == k:
                return v
    return None


@dataclass
class ParsedFields:
    """一行结构化日志解析出的字段。"""

    message: str = ""
    level: str = ""
    timestamp: Optional[str] = None       # 原始串，交给 TimestampParser
    service: str = ""                     # 服务 / 模块 / logger
    trace_id: str = ""
    request_id: str = ""
    span_id: str = ""
    error: str = ""                       # 异常摘要或堆栈
    fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def correlation_id(self) -> str:
        """跨服务关联键：优先 request_id（调用链语义），其次 trace_id。"""
        return self.request_id or self.trace_id


def _as_text(v: Any) -> str:
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return ""


# ---------------------------------------------------------------------------
# 判定：这行是不是结构化日志（必须极便宜，它在热路径上）
# ---------------------------------------------------------------------------
def looks_json(line: str) -> bool:
    """是否**可能**是 JSON 行。刻意保守：只看首字符与是否闭合。"""
    s = line.lstrip()
    return len(s) > 2 and s[0] == "{" and s[-1] == "}"


def looks_logfmt(line: str) -> bool:
    """是否可能是 logfmt。

    判据是**第一个 kv 对必须从行首开始**，而不是"整行被 kv 覆盖多少"。
    这一条把两类东西分开：
      `level=ERROR svc=api msg=db down`  -> 首部就是 kv -> 收
      `connect failed after retries=3 timeout=30` -> 前面是自由文本 -> 拒

    用覆盖率判定会同时犯两个错：把普通消息误吞（误判），以及把
    `msg=` 后面那句很长的说明判成"不是 logfmt"（漏判）。
    """
    s = line.strip()
    if not s or "=" not in s:
        return False
    first = _LOGFMT_RE.search(s)
    if first is None or first.start() != 0:
        return False
    return len(_LOGFMT_RE.findall(s)) >= 2


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        body = v[1:-1]
        return (body.replace('\\"', '"').replace("\\'", "'")
                .replace("\\n", "\n").replace("\\\\", "\\"))
    return v


def parse_logfmt(line: str) -> Optional[ParsedFields]:
    """解析 logfmt 行（key=value）。

    支持 `msg=` 之后接自由文本的常见写法（`level=error msg=db down`）——
    未被 kv 对覆盖的尾巴并进消息，否则真实日志里最常见的那种写法会把
    消息截断成第一个词。
    """
    s = line.strip()
    if not looks_logfmt(s):
        return None
    raw: Dict[str, Any] = {}
    last_end = 0
    for m in _LOGFMT_RE.finditer(s):
        raw[m.group(1).lower()] = _unquote(m.group(2))
        last_end = m.end()
    parsed = _from_mapping(raw)
    if parsed is None:
        return None
    tail = s[last_end:].strip()
    if tail and parsed.message:
        parsed.message = f"{parsed.message} {tail}"
    return parsed


def parse_json(line: str) -> Optional[ParsedFields]:
    """解析单行 JSON。非法 JSON / 非对象 / 解析失败一律返回 None。

    **绝不抛异常**：日志文件里混进半行 JSON、被截断的 JSON、或者只是
    恰好以 { 开头的话，都是生产常态。解析失败必须退化成"这不是结构化
    日志"，让调用方走原有的纯文本路径 —— 而不是在一条坏日志上崩掉
    整个分析。
    """
    s = line.strip()
    if not looks_json(s):
        return None
    try:
        obj = json.loads(s)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or not obj:
        return None
    return _from_mapping(obj)


def _from_mapping(obj: Dict[str, Any]) -> Optional[ParsedFields]:
    """字段映射：认领标准字段，其余原样保留。"""
    flat = _flatten({k: v for k, v in obj.items()})
    message = _as_text(_pick(flat, _MESSAGE_KEYS))
    level = _as_text(_pick(flat, _LEVEL_KEYS))
    ts = _pick(flat, _TIMESTAMP_KEYS)
    # 没有 message 也没 level 的行不值得当成结构化日志（多半是 JSON
    # 数据行，不是日志行），交给纯文本路径更稳妥
    if not message and not level:
        return None
    err = _pick(flat, _ERROR_KEYS)
    exc_type = _as_text(_pick(flat, _EXCEPTION_KEYS))
    error_text = _as_text(err) or exc_type
    claimed = set(_LEVEL_KEYS) | set(_MESSAGE_KEYS) | set(_TIMESTAMP_KEYS) \
        | set(_SERVICE_KEYS) | set(_TRACE_KEYS) | set(_REQUEST_KEYS) \
        | set(_SPAN_KEYS) | set(_ERROR_KEYS) | set(_EXCEPTION_KEYS)
    rest = {k: v for k, v in obj.items()
            if k.lower() not in claimed and not isinstance(v, (dict, list))}
    return ParsedFields(
        message=message or error_text,
        level=level,
        timestamp=_as_text(ts) if ts is not None else None,
        service=_as_text(_pick(flat, _SERVICE_KEYS)),
        trace_id=_as_text(_pick(flat, _TRACE_KEYS)),
        request_id=_as_text(_pick(flat, _REQUEST_KEYS)),
        span_id=_as_text(_pick(flat, _SPAN_KEYS)),
        error=error_text,
        fields=rest,
    )


def parse_line(line: str) -> Optional[ParsedFields]:
    """自动识别 JSON / logfmt，都不像就返回 None（走纯文本路径）。"""
    s = line.strip()
    if not s:
        return None
    if s[0] == "{":
        return parse_json(s)
    if "=" in s:
        return parse_logfmt(s)
    return None


#: 报告/证据里用的可读名（不是所有日志都有全部字段）
SUPPORTED_FORMATS = ("json-lines", "logfmt")
