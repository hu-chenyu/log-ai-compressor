# -*- coding: utf-8 -*-
"""进程退出码是 CLI 的**对外契约**，两条入口路径必须一致。

背景（v2.8.0 修复的真实缺陷）：`__main__.py` 写的是 `main()`，返回值被
丢弃，于是同一个失败在两个入口上表现不同：

    log-ai-compressor run 不存在的.log    -> exit=1   打印「错误：日志文件不存在」
    python -m log_ai_compressor run ...   -> exit=0   打印同一句话

为什么这条比「看着别扭」严重得多：本工具的 `verify` 子命令，退出码**就是
它的产品契约**（0 = 校验通过，1 = TAMPERED 或调用失败）。任何用
`python -m` 写的 CI 脚本、shell 管道、pre-commit 钩子，都会把失败当成功。

只测 `python -m`：它不依赖控制台脚本是否被安装，任何环境都能跑；
控制台入口由 `cli.main()` 的返回值测试覆盖（见 test_forensics）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HEAD = "2024-01-15 10:00:00"


def _run_module(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "log_ai_compressor", *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


@pytest.fixture()
def good_log(tmp_path: Path) -> Path:
    p = tmp_path / "good.log"
    p.write_text(f"{HEAD} ERROR boom happened\n", encoding="utf-8")
    return p


def test_missing_input_exits_nonzero() -> None:
    """找不到文件必须非零退出 —— 退出码是 verify 的契约本身。"""
    r = _run_module("run", "definitely_not_here.log")
    assert r.returncode != 0, (
        f"python -m 入口对「文件不存在」返回了 0，退出码契约破了："
        f"{r.stdout!r}")


def test_missing_verify_report_exits_nonzero(tmp_path: Path) -> None:
    """verify 读不到报告同样必须非零（否则 TAMPERED 与「没跑」不可分）。"""
    r = _run_module("verify", "--report", str(tmp_path / "no_such.json"))
    assert r.returncode != 0


def test_successful_run_exits_zero(good_log: Path) -> None:
    """成功路径必须是 0 —— 否则修好上面那条会变成「永远失败」。"""
    r = _run_module("run", str(good_log))
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


def test_help_exits_zero() -> None:
    r = _run_module("--help")
    assert r.returncode == 0, f"{r.stderr}"


def test_error_message_is_still_printed_too() -> None:
    """退出码变了，但**人看的错误信息不能丢**。

    修「退出码恒 0」最省事的做法是改成裸抛异常，那会让用户拿到一整页
    栈回溯 —— 而栈里的文件路径对排查日志的人毫无价值。这里钉住：
    退出码正确 **且** 仍有中文提示、**且** 没有 traceback。
    """
    r = _run_module("run", "definitely_not_here.log")
    assert "不存在" in (r.stdout + r.stderr), "错误提示不见了"
    assert "Traceback" not in (r.stdout + r.stderr), \
        "CLI 把内部栈回溯暴露给了终端用户"
