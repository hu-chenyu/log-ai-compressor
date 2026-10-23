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

from log_ai_compressor.core.parser import TimestampParser

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

    **在带 TZ 的子进程里跑**（2026-10-22 第十一轮修正）。

    原来这条是**进程内**调 `_to_epoch` —— 期望值按 UTC 算，而被测代码
    改回 localtime 解释后，在 **UTC 机器上两者相同** ⇒ 抓不住
    （实测：本地 UTC+8 杀掉、CI 的 ubuntu 上**存活**）。
    ⇒ 判据必须在**能观察到差异的时区**里跑：子进程带 `TZ=UTC-8`。
    """
    check = (
        "from datetime import datetime, timezone;"
        "from log_ai_compressor.core.parser import _to_epoch;"
        "dt=datetime(2024,3,15,8,0,0);"
        "print(_to_epoch(dt));"
        "print(dt.replace(tzinfo=timezone.utc).timestamp())"
    )
    env = dict(os.environ, TZ=_TZ_SHIFTED, PYTHONPATH=str(_ROOT),
               PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", check], cwd=str(_ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    assert p.returncode == 0, "跑 _to_epoch 校验失败：%s" % p.stderr[-400:]
    lines = p.stdout.strip().splitlines()
    assert len(lines) >= 2, "子进程没有输出两个数"
    got, expected = float(lines[0]), float(lines[1])
    assert got == expected, (
        "naive 时间戳被按本地时区解释了：%r != %r" % (got, expected))


def test_timestamp_parser_uses_utc_for_naive_input():
    """解析器入口同样按 UTC 解释裸时间戳。

    **在带 TZ 的子进程里跑**（理由同上一条：进程内在 UTC 机器上抓不住）。
    """
    check = (
        "from log_ai_compressor.core.parser import TimestampParser;"
        "print(TimestampParser().parse('2024-03-15 08:00:00'))"
    )
    env = dict(os.environ, TZ=_TZ_SHIFTED, PYTHONPATH=str(_ROOT),
               PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", check], cwd=str(_ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    assert p.returncode == 0, "跑解析器校验失败：%s" % p.stderr[-400:]
    got = float(p.stdout.strip())
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

    **在带 TZ 的子进程里跑**（2026-10-22 第十一轮修正）。

    原来这条是**进程内**调 `analyze_text` —— 那它看到的时区是**本机**的：
    在 UTC 机器上 `time.localtime` 与 `time.gmtime` **完全一样**，
    于是把 `gmtime` 改回 `localtime` 在 CI（ubuntu = UTC）上**抓不住**
    （实测：本地 UTC+8 杀掉、CI 的 ubuntu 上**存活** ⇒ 记分卡报「存活 2」）。
    ⇒ 判据必须在**能观察到差异的时区**里跑。

    跑在 `TZ=UTC-8`（UTC+8）的子进程里：日志写的是 08:00/14:00/20:00，
    窗口取 12:00~18:00。
    * 按 UTC 取 ⇒ 命中 14:00 那条 ⇒ 留 1 个簇；
    * 按本地时区取 ⇒ UTC+8 上那三条落在 16:00/22:00/04:00 ⇒ 留 0 个簇。

    **为什么走库 API 而不是 CLI**：CLI **没有** `--tod-start/--tod-end`
    这两个参数（实测 `run` 子命令不认），只有 `analyze_text` 的关键字。
    """
    check = (
        "from log_ai_compressor.core.pipeline import analyze_text;"
        "log=('2024-01-01 08:00:00 ERROR [db] early morning error\\n'"
        "'2024-01-01 14:00:00 ERROR [db] midday error\\n'"
        "'2024-01-01 20:00:00 ERROR [db] evening error\\n');"
        "r=analyze_text(log, tod_start=12*3600, tod_end=18*3600);"
        "print(len(r.clusters));"
        "print(';'.join(c.summary for c in r.clusters))"
    )
    env = dict(os.environ, TZ=_TZ_SHIFTED, PYTHONPATH=str(_ROOT),
               PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", check], cwd=str(_ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    assert p.returncode == 0, "跑 tod 校验失败：%s" % p.stderr[-400:]
    lines = p.stdout.strip().splitlines()
    assert lines, "子进程没有输出"
    assert lines[0].strip() == "1", (
        "12:00~18:00 的窗口留下了 %s 个簇（应为 1）—— 时段口径可能用了本地时区。"
        "实际留下的：%s" % (lines[0].strip(), lines[1] if len(lines) > 1 else ""))
    if len(lines) > 1:
        assert "midday" in lines[1], (
            "留下的不是 14:00 那条，而是 %r" % lines[1])


# ---------------------------------------------------------------------------
# 5  展示口径统一 UTC
# ---------------------------------------------------------------------------
def test_displayed_time_range_is_utc_everywhere():
    """md/txt 的时间范围与 JSON 的 ISO 串必须指向同一个钟点。

    **在指定 TZ 的子进程里跑**（2026-10-22 第十一轮修正）——
    理由同 `test_tod_window_uses_utc`：进程内跑时看到的是**本机**时区，
    在 UTC 机器上 localtime 与 UTC 渲染**输出相同** ⇒ 变异抓不住。

    **两条路径都要验**（`r11/display-localtime` 改的是 `models.format_timestamp`
    —— 人读的那条；JSON 那条走 `export/reporters.py::_iso_ts`，本来就是 UTC，
    只验它**抓不住**那个变异）。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "one.log"
        src.write_text(
            "2024-03-15 08:00:00 ERROR [db] connection refused\n",
            encoding="utf-8")
        out = Path(td) / "r.json"
        env = dict(os.environ, TZ=_TZ_SHIFTED, PYTHONPATH=str(_ROOT),
                   PYTHONIOENCODING="utf-8")
        p = subprocess.run(
            [sys.executable, "-m", "log_ai_compressor", "run", str(src),
             "-f", "json", "-o", str(out)],
            cwd=str(_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        assert p.returncode == 0, "跑单行样例失败：%s" % p.stderr[-400:]
        data = json.loads(out.read_text(encoding="utf-8"))
        # 人读的那条路径（txt）：`-f txt` 且无 `-o` 时 stdout 就是那个格式
        q = subprocess.run(
            [sys.executable, "-m", "log_ai_compressor", "run", str(src),
             "-f", "txt"],
            cwd=str(_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        assert q.returncode == 0, "跑 txt 输出失败：%s" % q.stderr[-400:]
        txt = q.stdout
    # JSON 侧：ISO 串必须带 +00:00（UTC 口径）
    ts = (data.get("meta") or {}).get("time_start")
    assert ts == "2024-03-15T08:00:00+00:00", (
        "JSON 的 time_start 是 %r —— 应当是日志里写的那个钟点（UTC 口径）" % ts)
    # 人读的那侧：时间范围必须是日志里写的那个钟点
    want = "时间范围: 2024-03-15 08:00:00 ~ 2024-03-15 08:00:00"
    assert want in txt, (
        "txt 报告里的时间范围不是日志里写的那个钟点 —— 展示口径可能用了本地时区。"
        "期望含 %r，实际输出：\n%s" % (want, txt[:600]))


def test_service_time_text_is_utc():
    """Web 接口的时间文本也必须是 UTC（与 JSON 导出同一套规则）。

    **在指定 TZ 的子进程里跑**（2026-10-22 第十一轮修正）——
    进程内跑时看到的是**本机**时区，在 UTC 机器上 localtime 与 UTC
    渲染**输出相同** ⇒ 变异抓不住（实测：CI 的 ubuntu 上该变异**存活**）。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "one.log"
        src.write_text(
            "2024-03-15 08:00:00 ERROR [db] connection refused\n",
            encoding="utf-8")
        out = Path(td) / "r.json"
        env = dict(os.environ, TZ=_TZ_SHIFTED, PYTHONPATH=str(_ROOT),
                   PYTHONIOENCODING="utf-8")
        p = subprocess.run(
            [sys.executable, "-m", "log_ai_compressor", "run", str(src),
             "-f", "json", "-o", str(out)],
            cwd=str(_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        assert p.returncode == 0, "跑单行样例失败：%s" % p.stderr[-400:]
        # Web 接口的形状由 service.result_to_dict 决定；这里直接验那一条规则
        check = (
            "import json,sys;"
            "sys.path.insert(0, %r);"
            "from log_ai_compressor import service as S;"
            "from log_ai_compressor.core.pipeline import analyze_text;"
            "log=open(%r,encoding='utf-8').read();"
            "print(S.result_to_dict(analyze_text(log))['stats']['time_range_text'])"
            % (str(_ROOT), str(src))
        )
        q = subprocess.run([sys.executable, "-c", check], cwd=str(_ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        assert q.returncode == 0, "跑 service 校验失败：%s" % q.stderr[-400:]
        shown = q.stdout.strip()
    assert shown == "2024-03-15 08:00:00 ~ 2024-03-15 08:00:00", (
        "Web 的时间范围是 %r —— 应当与 md/JSON 同一口径（UTC）" % shown)


def test_malformed_timestamps_still_do_not_crash():
    """改口径不能把「解析失败退化成 None」这条路堵上。"""
    tp = TimestampParser()
    assert tp.parse("9" * 400) is None
    assert tp.parse("not a timestamp") is None
    # 相对计时（小于 1e9）仍按原样返回
    assert tp.parse("1700000000") == 1700000000.0
