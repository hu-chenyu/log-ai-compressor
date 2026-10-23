# -*- coding: utf-8 -*-
"""README 首屏那段示例输出，必须与仓库里的**真实输出**对得上。

为什么要有这条（2026-10-22 外部复核抓到）：

README 第一屏写「24 行日志（8 条错误、4 种类型）」并贴了一段带具体行号、
指纹、簇编号的输出 —— 但 `examples/` 里**没有 24 行的那个文件**
（只有 13 / 13 / 21 / 8 / 52 行）。于是那段输出**读者复现不出来**。

而本项目的规矩是「对外数字必须能被复现」—— 首屏那段是最该能被复现的：
它是**第一个**被读到的东西，也是决定人信不信这个工具的东西。

修法：把那份日志放进 `examples/app.log`，并让 README 贴**它的真实输出**；
本文件负责**盯住**这件事不再漂。

判据（逐条）：
  1. `examples/app.log` 存在，且行数与 README 首屏写的一致；
  2. README 首屏里的**簇行号区间**必须与真实跑出来的**逐条相等**
     （只查「有一个区间出现了」太松 —— 区间整体错位也能混过去）；
  3. README 首屏里的**确定性指纹**必须与真实跑出来的相等；
  4. 「24 行 / 8 条错误 / 4 种类型」这三个数必须与真实输出一致。

**改坏会红吗**：把 README 里的行号区间改一个数字、把 `examples/app.log`
删掉、或把首屏那三个数改错，本文件立刻红。
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_README = _ROOT / "README.md"
_APP = _ROOT / "examples" / "app.log"
_PY = sys.executable


def _run_app() -> dict:
    """真跑一遍 examples/app.log，返回它的 JSON 报告。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "r.json"
        env = {"PYTHONPATH": str(_ROOT), "PYTHONIOENCODING": "utf-8"}
        import os
        e = dict(os.environ, **env)
        p = subprocess.run(
            [_PY, "-m", "log_ai_compressor", "run", str(_APP),
             "-f", "json", "-o", str(out)],
            cwd=str(_ROOT), env=e, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        assert p.returncode == 0, "跑 examples/app.log 失败：%s" % p.stderr[-400:]
        return json.loads(out.read_text(encoding="utf-8"))


def _run_app_human() -> str:
    """照 README 里写的**那条命令**跑一遍，返回人读的那份摘要。

    为什么必须用**相对路径**、且 cwd 在仓库根：README 首屏贴的就是
    `log-ai-compressor run examples/app.log` 的输出，而「压缩后 tokens」
    **随路径长度变**（摘要里有那一行路径）——
    实测相对路径 175、仓库里的绝对路径 186。
    用绝对路径去比，判据就成了「另一个运行的数」。

    路径分隔符是平台差异（Windows `examples\\app.log` / POSIX `examples/app.log`），
    但**长度相同** ⇒ token 估算相同 ⇒ 这一行仍可跨平台比对。
    """
    import os
    env = dict(os.environ, PYTHONPATH=str(_ROOT), PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [_PY, "-m", "log_ai_compressor", "run", "examples/app.log"],
        cwd=str(_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    assert p.returncode == 0, "跑 examples/app.log 失败：%s" % p.stderr[-400:]
    return p.stdout


def _readme_head() -> str:
    return _README.read_text(encoding="utf-8")[:4000]


def test_the_sample_file_exists():
    """首屏那段输出所依赖的输入**必须在仓库里**。

    没有它 ⇒ 读者复现不出来 ⇒ 那段输出就是一句无法验证的话。
    """
    assert _APP.is_file(), (
        "examples/app.log 不存在 —— README 首屏那段输出读者复现不出来")


def test_the_first_screen_numbers_match_a_real_run():
    """首屏写的「24 行 / 8 条错误 / 4 种类型」必须与真跑出来的一致。"""
    data = _run_app()
    meta = data.get("meta") or {}
    head = _readme_head()
    m = re.search(r"examples/app\.log`（(\d+) 行、(\d+) 条错误、(\d+) 种类型）", head)
    assert m, (
        "README 首屏没写清样例的三个数（行 / 错误 / 类型）—— "
        "读者不知道该期待什么")
    claimed = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    real = (meta.get("total_lines"), meta.get("error_lines"),
            data.get("error_kinds"))
    assert claimed == real, (
        "README 首屏写 %s，真实跑出来是 %s —— "
        "示例与仓库脱节了" % (claimed, real))


def test_the_first_screen_cluster_ranges_match_a_real_run():
    """首屏里那几条**簇行号区间**必须与真跑出来的一致。

    只查「某个区间出现了」太松 —— 区间整体错位一位也能混过去。
    所以这里做**集合相等**。
    """
    data = _run_app()
    head = _readme_head()
    real = set()
    for c in data.get("clusters") or []:
        real.add((c.get("first_line"), c.get("last_line")))
    claimed = set()
    for m in re.finditer(r"行 (\d+)~(\d+)", head):
        claimed.add((int(m.group(1)), int(m.group(2))))
    assert claimed, "README 首屏里一个簇行号区间都没找到"
    assert claimed == real, (
        "README 首屏的簇行号区间 %s 与真实跑出来的 %s 对不上 —— "
        "示例已经过期了" % (sorted(claimed), sorted(real)))


def test_the_first_screen_fingerprint_matches_a_real_run():
    """首屏里的**确定性指纹**必须与真跑出来的一致。

    指纹是最容易被手写示例漏掉的一处：它每次跑都一样，所以读者最容易
    发现「这段是编的」。
    """
    data = _run_app()
    head = _readme_head()
    claimed = re.search(r"确定性指纹 `([0-9a-f]{16})", head)
    assert claimed, "README 首屏没写确定性指纹"
    real = data.get("fingerprint") or ""
    assert real, "跑出来的报告里没有指纹"
    assert real.startswith(claimed.group(1)), (
        "README 首屏写 %s…，真实跑出来是 %s… —— 示例已经过期了"
        % (claimed.group(1), real[:16]))


def test_the_first_screen_token_line_matches_a_real_run():
    """首屏里那行「原始日志 ≈N tokens → 压缩后 ≈M tokens」必须与真跑出来的一致。

    为什么补这条（2026-10-23 第十一轮外部复核后）：
    首屏那块此前是**三次不同的运行拼起来的** ——
    标题与路径来自「相对路径」那次，token 数来自「绝对路径」那次
    （同一份日志，相对路径 175、绝对路径 186），
    而「时间范围」的写法来自改显示格式之前那版。
    换句话说：**没有任何一次运行能产出 README 里那块**。
    而这一条门禁此前只校三样（三个数 / 行号区间 / 指纹），
    恰好漏掉了会漂的那两行。

    **改坏会红吗**：把 README 里的 175 改成 186，本条立刻红。
    """
    head = _readme_head()
    claimed = re.search(r"原始日志 ≈\d+ tokens → 压缩后 ≈(\d+) tokens（压缩 (\d+)%）", head)
    assert claimed, "README 首屏没写那行 token 数"
    real_line = None
    for ln in _run_app_human().splitlines():
        if "tokens" in ln and "压缩后" in ln:
            real_line = ln
            break
    assert real_line, "真跑一遍没有拿到那行 token 数"
    m = re.search(r"压缩后 ≈(\d+) tokens（压缩 (\d+)%）", real_line)
    assert m, "真跑出来的那行长得不一样：%r" % real_line
    assert (int(claimed.group(1)), int(claimed.group(2))) == (int(m.group(1)), int(m.group(2))), (
        "README 首屏写 压缩后 ≈%s tokens（压缩 %s%%），真跑出来是 ≈%s tokens（压缩 %s%%）"
        " —— 那不是同一次运行的输出" % (claimed.group(1), claimed.group(2),
                                        m.group(1), m.group(2)))


def test_the_first_screen_time_range_matches_a_real_run():
    """首屏里那行「时间范围 A ~ B」必须与真跑出来的一致。

    这行恰恰是「改显示格式」时最容易留下的旧痕迹：2026-10-23 起时间统一
    按 UTC 展示，两端都带完整日期（此前第二端只显示钟点）。

    **改坏会红吗**：把 README 里的「~ 2024-03-15 08:00:25」改回「~ 08:00:25」，
    本条立刻红。
    """
    head = _readme_head()
    claimed = re.search(r"时间范围 ([0-9T:\-~ ]+?)。", head)
    assert claimed, "README 首屏没写时间范围"
    real_line = None
    for ln in _run_app_human().splitlines():
        if "时间范围" in ln:
            real_line = ln
            break
    assert real_line, "真跑一遍没有拿到那行时间范围"
    m = re.search(r"时间范围 ([0-9T:\-~ ]+?)。", real_line)
    assert m, "真跑出来的那行长得不一样：%r" % real_line
    assert claimed.group(1).strip() == m.group(1).strip(), (
        "README 首屏写 时间范围 %s，真跑出来是 %s —— 示例已经过期了"
        % (claimed.group(1).strip(), m.group(1).strip()))
