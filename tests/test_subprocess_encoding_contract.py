# -*- coding: utf-8 -*-
"""跨平台子进程契约：谁起子进程，谁就得管编码。

背景（v2.8.0 的真实 CI 事故）
----------------------------
`tests/test_exit_codes.py` 里有一处 `subprocess.run(..., text=True,
encoding="utf-8")`。本地跑全绿，**只有 Windows CI 红**：runner 的控制台是
cp1252，子进程按 cp1252 写中文，而这里按 UTF-8 解码 → 提示变成乱码 →
「错误提示里应含『不存在』」这条断言红。

最难受的部分是：**退出码本来就是对的（1）**，红的是「提示还能不能读」。
也就是说产品行为完全正确，测法错了。

为什么本地发现不了
------------------
本地控制台恰好是 UTF-8，于是「不设子进程编码」这个 bug 在开发机上永远
看不出来。**这不是运气好，是本地环境恰好等于理想环境**。

所以这里把「CI 的敌对环境」搬进本地：构造 cp1252 / UTF-8 关闭 /
Windows 控制台这几个真实存在过的组合，在本地跑一遍。跑不出来的组合，
在 CI 上就是定时炸弹。

两条检查
--------
1. **行为检查**：CLI 与门禁入口在 cp1252 下必须能跑完且退出码正确。
2. **静态检查**：任何 `subprocess.run(..., encoding="utf-8")` 的测试，
   若没有同时给子进程设 `PYTHONIOENCODING`，本文件直接红。

第 2 条是关键 —— 它让「下一个人写出同样的 bug」在**本地**就被拦住，
而不是等到 push 之后由 CI 告诉他。事故发生一次就该变成一条永久门禁。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"


# ---------------------------------------------------------------------------
# 一、行为检查：在敌对编码下真的跑一遍
# ---------------------------------------------------------------------------

# 真实存在过的控制台配置。每一行都是一次线上事故的等价物。
HOSTILE_ENVS = [
    pytest.param({"PYTHONIOENCODING": "cp1252"}, id="cp1252-控制台"),
    pytest.param({"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}, id="cp1252+utf8关"),
    pytest.param({"PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}, id="utf8mode关"),
]


def _env(overrides: dict) -> dict:
    env = dict(os.environ)
    env.update(overrides)
    # 子进程不该继承本机的 UTF-8 模式开关，否则等于没测
    env.pop("PYTHONWARNDEFAULTENCODING", None)
    return env


@pytest.mark.parametrize("overrides", HOSTILE_ENVS)
@pytest.mark.parametrize("argv", [
    pytest.param(["--help"], id="help"),
    pytest.param(["rules", "list"], id="rules-list"),
    pytest.param(["run", "__不存在的文件__.log"], id="run-失败路径"),
])
def test_cli_entry_survives_hostile_console(overrides, argv) -> None:
    """CLI 在敌对控制台下必须**跑完**，且成功/失败的退出码仍然正确。

    这里刻意断言失败路径退出 1：控制台编码问题最容易把失败路径搞成
    `UnicodeEncodeError` → 退出码变成 1 但提示是栈回溯 —— 数字对了、
    内容坏了。所以两边都要断言。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "log_ai_compressor", *argv],
        capture_output=True, env=_env(overrides), timeout=120,
    )
    blob = proc.stdout + proc.stderr
    text = blob.decode("utf-8", "replace")
    assert "Traceback" not in text, f"CLI 内部栈回溯泄漏给终端用户：{text[-300:]}"
    if argv[0] == "run":
        assert proc.returncode == 1, f"失败路径退出码变成 {proc.returncode}"


@pytest.mark.parametrize("overrides", HOSTILE_ENVS)
def test_module_entry_matches_console_entry(overrides) -> None:
    """`python -m` 与控制台入口必须给出**同一个退出码**。

    两者共用 main()，但只有一条路径接了返回值（v2.8.0 修的那个缺陷）。
    这条把「必须一致」钉死，免得以后又长回去。
    """
    env = _env(overrides)
    a = subprocess.run(["log-ai-compressor", "run", "no_such.log"],
                       capture_output=True, env=env, timeout=120)
    b = subprocess.run([sys.executable, "-m", "log_ai_compressor",
                        "run", "no_such.log"],
                       capture_output=True, env=env, timeout=120)
    assert a.returncode == b.returncode, (
        f"两个入口退出码不一致：控制台 {a.returncode} vs python -m {b.returncode}")


# ---------------------------------------------------------------------------
# 二、静态检查：让下一个人写不出同样的 bug
# ---------------------------------------------------------------------------

_SUBPROCESS_CALL = re.compile(
    r"subprocess\.(?:run|Popen|check_output|check_call)\s*\(", re.M)


def _test_files_with_subprocess() -> list[pathlib.Path]:
    out = []
    for p in sorted(_TESTS.rglob("*.py")):
        if p.name == pathlib.Path(__file__).name:
            continue
        if _SUBPROCESS_CALL.search(p.read_text(encoding="utf-8")):
            out.append(p)
    return out


def test_tests_do_call_subprocess() -> None:
    """前置条件：这套静态检查得真的有事可做，否则它在空转。"""
    files = _test_files_with_subprocess()
    assert files, "没有测试在起子进程 —— 静态检查形同虚设"


@pytest.mark.parametrize("path", _test_files_with_subprocess(),
                         ids=lambda p: p.name)
def test_subprocess_decoding_pins_child_encoding(path: pathlib.Path) -> None:
    """按 UTF-8 解码子进程输出，就必须同时给子进程设 PYTHONIOENCODING。

    否则这份测试**只在开发机是绿的**：本地控制台是 UTF-8，子进程恰好也按
    UTF-8 输出；换到 cp1252 的机器上（本项目的 Windows CI 就是），
    解码出来是乱码，断言随之失败 —— 而失败原因与被测代码无关。

    这是「测试自己有 bug 却表现为产品有 bug」的典型形态，比产品 bug 更难查。
    """
    src = path.read_text(encoding="utf-8")
    if 'encoding="utf-8"' not in src and "encoding='utf-8'" not in src:
        pytest.skip("这份测试没有按 UTF-8 解码子进程，不适用本约束")
    # env 里给了 PYTHONIOENCODING，或显式 decode(errors=...)，都算已处理
    if "PYTHONIOENCODING" in src:
        return
    pytest.fail(
        f"{path.name} 按 encoding='utf-8' 解码子进程，却没有给子进程设 "
        f"PYTHONIOENCODING。本地控制台是 UTF-8 时看不出来，Windows CI 的 "
        f"cp1252 控制台上会解码成乱码 —— v2.8.0 就是这样挂的。"
        f"加 env=env 时把 'PYTHONIOENCODING': 'utf-8' 写进去。"
    )


def test_test_helpers_always_pass_explicit_env() -> None:
    """凡是带 encoding= 的 subprocess 调用，都必须显式给 env。

    不给 env 就会继承开发机的全部环境变量 —— 这正是「本地绿、CI 红」的
    根源：本地恰好是对的继承结果。
    """
    bad = []
    for p in _test_files_with_subprocess():
        src = p.read_text(encoding="utf-8")
        if "PYTHONIOENCODING" in src:
            continue
        for m in re.finditer(
                r"subprocess\.(?:run|Popen|check_output)\s*\((?:[^()]|\([^()]*\))*\)",
                src, re.S):
            if "encoding=" in m.group(0) and "env=" not in m.group(0):
                bad.append(f"{p.name}: {m.group(0)[:70]}…")
                break
    assert not bad, ("这些调用按固定编码解码子进程却不传 env，"
                     "会静默继承开发机环境：\n  " + "\n  ".join(bad))
