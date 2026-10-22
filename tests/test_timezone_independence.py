# -*- coding: utf-8 -*-
"""第十轮：同一份日志的结论指纹**不得随运行机器的时区变**（2026-10-22）。

这个洞是怎么发现的
------------------
CI run 49 红了，但红的不是覆盖率（实测值 211/4277 与 README 完全一致），
而是本轮新加的那条门禁：

    test_the_first_screen_fingerprint_matches_a_real_run
    README 首屏写 32f71d7a3c0acf3f…，真实跑出来是 56df0a5f2bdff598… —— 示例已经过期了

**本地绿、CI 红** —— 这正是本项目反复记过的那类信号：某个数只在**一台机器**上
是对的。查下来变量是 `TZ`：

  * 本机 = UTC+8  ⇒ 指纹 `32f71d7a…`
  * CI ubuntu runner = UTC ⇒ 指纹 `56df0a5f…`

差的正好 8 小时。

根因
----
`core/parser.py::_to_epoch` 对 naive datetime 调 `.timestamp()` ——
Python 按**本机本地时区**解释 naive datetime。于是同一份日志在 UTC 机器
与 UTC+8 机器上得到**不同的 epoch**；而 epoch 会进 `stats.time_start` /
`cluster.first_seen` / `last_seen`，这些字段**在结论指纹里**。

同一族缺陷的另一半：展示路径也按本地时区渲染（`fromtimestamp` 不带 tz），
于是「时间范围」在不同机器上显示不同的钟点。

修法
----
naive 时间戳按 **UTC** 解释；带显式时区（`Z` / `+08:00`）的保持原义
（那是日志作者的真实时刻）；展示口径统一成 UTC。

这与本仓库**早就有的**约定同族：`tests/test_perf_gate.py` 早已钉住
「性能负载用 gmtime 而不是 localtime —— 否则同一个 seed 在 UTC 和
UTC+8 机器上生成的是不同字节」。那条约定管住了性能门禁的负载，
漏在了主解析路径上。

判据（逐条，且每条都要能回答「把被守的那行改坏，会不会红」）
------------------------------------------------------------
  1  同一份日志在两个不同 TZ 下跑，指纹必须**相同**（子进程隔离 TZ）；
  2  naive 时间戳解析出的 epoch 必须等于「按 UTC 解释」的那个值；
  3  带显式时区的时间戳**不得**被替换（`+08:00` 仍是那个绝对时刻）；
  4  时段窗口（tod）必须按 UTC 取「一天里的第几秒」；
  5  展示口径统一 UTC：md 的时间范围与 JSON 的 ISO 串必须一致。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from log_ai_compressor.core.models import format_timestamp
from log_ai_compressor.core.parser import TimestampParser, _to_epoch
from log_ai_compressor.core.pipeline import analyze_text

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "examples" / "app.log"

#: 两个**相差 8 小时**的 TZ —— POSIX 风格串在 Windows 与 Linux 上都能改偏移
#: （实测：Windows 上 `TZ=UTC-8` 让 naive.timestamp() 差 28800 秒；
#:   Linux 上 `TZ=UTC-8` 同理）。
_TZ_UTC = "UTC"
_TZ_SHIFTED = "UTC-8"


def _run_with_tz(tz: str) -> dict:
    """在**指定 TZ 的子进程**里跑 examples/app.log，返回 JSON 报告。

    必须在子进程里跑：`TZ` 只在进程启动时被读一次，改本进程的 os.environ
    不影响已经初始化过的时区状态。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "r.json"
        env = dict(os.environ, TZ=tz, PYTHONPATH=str(_ROOT),
                   PYTHONIOENCODING="utf-8")
        p = subprocess.run(
            [sys.executable, "-m", "log_ai_compressor", "run", str(_APP),
             "-f", "json", "-o", str(out)],
            cwd=str(_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        assert p.returncode == 0, (
            "跑 examples/app.log 失败（TZ=%s）：%s" % (tz, p.stderr[-400:]))
        return json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1  指纹不得随时区变
# ---------------------------------------------------------------------------
def test_fingerprint_does_not_depend_on_machine_timezone():
    """同一份日志，两个 TZ 下指纹必须相同。

    **改坏会红吗**：把 `_to_epoch` 里的
    `dt = dt.replace(tzinfo=timezone.utc)` 删掉（改回 localtime 解释），
    两个子进程的 epoch 差 28800 秒 ⇒ 指纹不同 ⇒ 本条立刻红。
    """
    a = _run_with_tz(_TZ_UTC)
    b = _run_with_tz(_TZ_SHIFTED)
    fa, fb = a.get("fingerprint") or "", b.get("fingerprint") or ""
    assert fa and fb, "报告里没有指纹"
    assert fa == fb, (
        "同一份日志在 TZ=%s 与 TZ=%s 下指纹不同（%s vs %s）—— "
        "结论指纹不得随运行机器的时区变"
        % (_TZ_UTC, _TZ_SHIFTED, fa[:16], fb[:16]))


def test_time_range_does_not_depend_on_machine_timezone():
    """时间范围也不得随时区变（同一件事，两处口径不能打架）。"""
    a = _run_with_tz(_TZ_UTC)
    b = _run_with_tz(_TZ_SHIFTED)
    ma, mb = a.get("meta") or {}, b.get("meta") or {}
    assert ma.get("time_start") == mb.get("time_start"), (
        "time_start 随时区变了：%r vs %r"
        % (ma.get("time_start"), mb.get("time_start")))
    assert ma.get("time_end") == mb.get("time_end"), (
        "time_end 随时区变了：%r vs %r"
        % (ma.get("time_end"), mb.get("time_end")))


# ---------------------------------------------------------------------------
# 2  naive 时间戳按 UTC 解释
# ---------------------------------------------------------------------------
def test_naive_timestamp_is_interpreted_as_utc():
    """naive datetime 的 epoch 必须等于「按 UTC 解释」的那个值。

    **改坏会红吗**：改回 `dt.timestamp()`（localtime 解释）⇒ 本机在
    UTC+8 上得到 1710460800 而非 1710489600 ⇒ 差 28800 ⇒ 红。
    （在 UTC 机器上这一条不会红 —— 所以它**不能单独**当门禁用，
    必须与上面那条「两个 TZ 对比」合起来才完整。）
    """
    dt = datetime(2024, 3, 15, 8, 0, 0)
    expected = dt.replace(tzinfo=timezone.utc).timestamp()
    assert _to_epoch(dt) == expected, (
        "naive 时间戳被按本地时区解释了：%r != %r"
        % (_to_epoch(dt), expected))


def test_timestamp_parser_uses_utc_for_naive_input():
    """解析器入口同样按 UTC 解释裸时间戳。"""
    tp = TimestampParser()
    got = tp.parse("2024-03-15 08:00:00")
    expected = datetime(2024, 3, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    assert got == expected, (
        "裸时间戳解析结果 %r != UTC 口径的 %r" % (got, expected))


# ---------------------------------------------------------------------------
# 3  带显式时区的时间戳不得被替换
# ---------------------------------------------------------------------------
def test_explicit_timezone_timestamps_keep_their_meaning():
    """`Z` / `+08:00` 是日志作者的真实时刻，**不得**被当成 UTC 替换掉。

    这是本修复最容易引入的回归：把 naive 一律当 UTC 时，很容易顺手把
    带时区的也一起改了 —— 那会把 `16:00+08:00` 说成 `16:00Z`，
    整整差 8 小时，且**方向相反**（改前是「当成本地时间」，改后是
    「当成 UTC」）。这两种错都静默。
    """
    tp = TimestampParser()
    # 16:00+08:00 == 08:00Z
    got = tp.parse("2024-03-15T16:00:00+08:00")
    expected = datetime(2024, 3, 15, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    assert got == expected, (
        "带 +08:00 的时间戳被替换了：%r != %r" % (got, expected))
    # Z 结尾同理
    got_z = tp.parse("2024-03-15T08:00:00Z")
    assert got_z == expected, (
        "Z 结尾的时间戳被替换了：%r != %r" % (got_z, expected))


# ---------------------------------------------------------------------------
# 4  时段窗口按 UTC 取「一天里的第几秒」
# ---------------------------------------------------------------------------
def test_tod_window_uses_utc():
    """`--tod-start/--tod-end` 必须按 UTC 取小时内秒数。

    **改坏会红吗**：把 `time.gmtime(ts)` 改回 `time.localtime(ts)`，
    在 UTC+8 机器上「12:00~18:00」的窗口会选出 08:00~14:00 的条目 ⇒
    留下的簇数从 1 变成别的值 ⇒ 红。
    """
    log = "\n".join([
        "2024-01-01 08:00:00 ERROR [db] early morning error",
        "2024-01-01 14:00:00 ERROR [db] midday error",
        "2024-01-01 20:00:00 ERROR [db] evening error",
    ])
    r = analyze_text(log, tod_start=12 * 3600, tod_end=18 * 3600)
    assert len(r.clusters) == 1, (
        "12:00~18:00 的窗口留下了 %d 个簇（应为 1）—— 时段口径可能用了本地时区"
        % len(r.clusters))
    assert "midday" in r.clusters[0].summary


# ---------------------------------------------------------------------------
# 5  展示口径统一 UTC
# ---------------------------------------------------------------------------
def test_displayed_time_range_is_utc_everywhere():
    """md/txt 的时间范围与 JSON 的 ISO 串必须指向同一个钟点。

    **改坏会红吗**：把 `format_timestamp` 里的
    `tz=timezone.utc` 去掉（改回 localtime 渲染）⇒ 在 UTC+8 机器上
    md 显示 00:00:00 而 JSON 显示 08:00:00+00:00 ⇒ 两边对不上 ⇒ 红。
    """
    log = "2024-03-15 08:00:00 ERROR [db] connection refused\n"
    r = analyze_text(log)
    shown = format_timestamp(r.stats.time_start)
    assert shown == "2024-03-15 08:00:00", (
        "时间范围显示为 %r —— 应当是日志里写的那个钟点（UTC 口径）" % shown)


def test_service_time_text_is_utc():
    """Web 接口的时间文本也必须是 UTC（与 JSON 导出同一套规则）。"""
    from log_ai_compressor import service as S
    log = "2024-03-15 08:00:00 ERROR [db] connection refused\n"
    r = analyze_text(log)
    payload = S.result_to_dict(r)
    assert payload["stats"]["time_range_text"] == "2024-03-15 08:00:00 ~ 2024-03-15 08:00:00", (
        "Web 的时间范围是 %r —— 应当与 md/JSON 同一口径（UTC）"
        % payload["stats"]["time_range_text"])


def test_malformed_timestamps_still_do_not_crash():
    """改口径不能把「解析失败退化成 None」这条路堵上。"""
    tp = TimestampParser()
    assert tp.parse("9" * 400) is None
    assert tp.parse("not a timestamp") is None
    # 相对计时（小于 1e9）仍按原样返回
    assert tp.parse("1700000000") == 1700000000.0
