# -*- coding: utf-8 -*-
"""覆盖率的分母由 `pyproject.toml` 的 `omit` 决定，而它必须有人守。

它治的病
--------
`omit` 不像变量名，写错会报语法错 —— 它只是**安静地**把某些文件移出分母。
于是「90% 地板」和「README 写着 96%」这两件对外的事，会在有人往 `omit`
里加一行之后同时变松，而没有任何东西会叫。

v2.11.1 第四轮外部复核实测的现状：

    omit = ["log_ai_compressor/cli.py"]      # 618 个非空非注释行
    README 声明生产代码 7,762 行 —— **含** cli.py
    覆盖率分母 = 7,762 - 618 = 7,144 行

也就是说，「7,762 行」和「96.09%」这两个对外数字**用的是两套分母**，
而 README 只写了前者。差异 8.0%。排除 cli.py 本身是有道理的（它是薄适配层），
**有问题的是没有任何东西记录这个取舍** —— 换个人看到 `omit` 只会以为漏配了。

为什么不顺手把 cli.py 纳入分母
-----------------------------
它覆盖率低，纳进来 96.09% 会掉，而 README 对外写的正是 96% —— 于是
「为了让覆盖率更诚实」这个动作本身，会制造下一个错数字。
**两件事必须分开做**：这里只钉住「有没有人在改它、改的理由还在不在」。

判据
----
1. `omit` 的内容必须与本文件的 `_EXPECTED_OMIT` **逐项一致** ——
   任何增删都会红，于是改动必然是有意的；
2. 每个被 omit 的文件必须**确实存在**，否则规则是空转；
3. 每条规则的排除理由必须记在本文件里（`_EXPECTED_OMIT` 的注释），
   而不是只躺在某个人脑子里。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与 pyproject.toml 的 [tool.coverage.run] omit 逐项对应。
# 改 pyproject.toml 就必须改这里，并且先回答「为什么」。
_EXPECTED_OMIT = {
    "log_ai_compressor/cli.py": (
        "薄适配层：只做参数解析与格式分发，核心判断全在 service/core 之下。"
        "排除它是有意的取舍 —— 实测它 618 个非空非注释行，占 README 声明"
        "的 7,762 行的 8.0%。"
    ),
}


def _actual_omit() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data.get("tool", {}).get("coverage", {}).get("run", {}).get("omit", []))


def test_omit_list_matches_the_recorded_decision():
    """增删任何一项都会红 —— 所以改动必然是有意的。"""
    actual = _actual_omit()
    missing = sorted(set(_EXPECTED_OMIT) - set(actual))
    extra = sorted(set(actual) - set(_EXPECTED_OMIT))
    assert not missing, (
        f"这些文件被排除出覆盖率分母，但本文件没有记录理由：{missing}。"
        f"排除覆盖率分母是一个**对外取舍**，必须写下为什么 —— "
        f"否则下一个人只会以为漏配了，或者随手把它删掉。")
    assert not extra, (
        f"[tool.coverage.run] omit 里多了 {extra}，本文件没记录。"
        f"往 omit 加一行会让 90% 地板与 README 的 96% 对账同时变松，"
        f"而没有任何东西会叫 —— 除非这里叫。")


def test_every_omitted_path_really_exists():
    """排除一个不存在的文件 = 空转规则，看起来做了事其实没做。"""
    for rel, why in _EXPECTED_OMIT.items():
        assert (ROOT / rel).is_file(), f"omit 里的 {rel} 不存在（{why}）"


def test_the_exclusion_is_actually_visible_to_readers():
    """排除这件事必须**在文档里写着**，否则它只活在配置里。

    README 声明的 7,762 行含被 omit 的文件，而 96% 是在扣掉它之后的
    分母上算的 —— 这两个数字口径不同，读者有权知道。
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "omit" in readme or "分母" in readme, (
        "README 声明的生产行数含被 omit 的 cli.py，而覆盖率分母不含 —— "
        "这个口径差异必须在 README 里说出来，否则两个对外数字看上去"
        "用的是同一个分母。")


def test_coverage_floor_is_still_enforced_in_ci():
    """`omit` 变松了，地板也得跟着真的在卡。

    门禁 6 是 `pytest --cov-fail-under=90`。而 `omit` 与
    `--cov-fail-under` 可以被写在两个地方（配置文件 / 命令行）——
    命令行那份会**覆盖**配置文件里的，于是 omit 就白写了。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "cov-fail-under" in ci, "CI 里没有 --cov-fail-under，地板是假的"
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    configured = data.get("tool", {}).get("coverage", {}).get("report", {})
    # 配置里可以不写 fail_under（由 CI 命令行给），但写了就必须与 CI 一致
    if "fail_under" in configured:
        assert str(configured["fail_under"]) in ci, (
            f"pyproject 写着 fail_under={configured['fail_under']}，"
            f"CI 命令行却是另一个数 —— 两个地方各写一份，迟早漂。")
