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
