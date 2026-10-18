# -*- coding: utf-8 -*-
"""`docs/CLI.md` 必须与解析器**实际注册**的一致。

背景：这份文档此前的形状是 —— 一个开了没关的 `bash` 围栏、把
「自定义解析规则」的标题吞进代码块、**CLI 章节一个字都没有**，
而 README 第一屏称它「完整命令行用法」。一份自称完整却连结构都不闭合的
文档，比没有更糟：读者会以为照着抄不会错。

所以这里不校文字，校**结构与事实**：

1. 围栏成对 —— 这条能抓住当时那个问题；
2. 每个子命令都在文档里 —— 「完整」的最小含义；
3. 文档里写的每个长选项，解析器里**真的注册了** ——
   照着抄不会抄出一个不存在的参数；
4. `--format` 的取值列表与解析器的 `choices` 一致 ——
   `CONTRIBUTING.md` 曾经写着 `-f summary`，跑不起来；这类错误正是本条
   要挡住的。
"""
from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs" / "CLI.md"
_FENCE = "`" * 3


@pytest.fixture(scope="module")
def text() -> str:
    return _DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def parser():
    from log_ai_compressor.cli import build_parser
    return build_parser()


def _subcommands(parser) -> dict:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return action.choices
    raise AssertionError("解析器里没有子命令了 —— 先更新本测试")


# ---------------------------------------------------------------------------
# 1. 文档本身得是合法的 markdown
# ---------------------------------------------------------------------------
class TestTheFileIsWellFormed:
    def test_code_fences_are_balanced(self, text):
        """闭合的围栏必须成对。

        这正是原文件的病：一个 ```` ```bash ```` 开了没关，后面的 yaml 围栏
        被当成了它的闭合，**整篇渲染塌掉**。
        """
        n = text.count(_FENCE)
        assert n % 2 == 0, (
            f"docs/CLI.md 里有 {n} 个代码围栏（奇数 = 有没关上的）")

    def test_it_is_not_a_stub(self, text):
        """「完整命令行用法」不许是一句空标题。"""
        body = text.split("## 自定义解析规则")[0]
        assert len(body) > 1500, (
            f"CLI 那一节只有 {len(body)} 字节 —— README 称它「完整命令行用法」")


# ---------------------------------------------------------------------------
# 2. 子命令
# ---------------------------------------------------------------------------
def _documented_in_table(text: str) -> set:
    """子命令表里列出来的那些。

    只判「文件里出现过没有」是不够的：`run` 在代码示例里到处都是，
    把表格里那一行删掉也照样「出现过」。而**表格才是「完整」二字的
    落点** —— 读者先看表，再看细节。
    """
    return set(re.findall(r"^\| `([a-z][a-z-]*)` \|", text, re.M))


class TestEverySubcommandIsDocumented:
    def test_all_subcommands_appear_in_the_table(self, parser, text):
        missing = sorted(set(_subcommands(parser)) - _documented_in_table(text))
        assert not missing, (
            f"这些子命令没在 docs/CLI.md 的表格里出现：{missing}\n"
            f"README 称该文档是「完整命令行用法」—— 新增子命令请同步补。")

    def test_no_documented_subcommand_was_removed(self, parser, text):
        """反向也封住：表格里不能留着一个已经不存在的子命令。"""
        ghost = sorted(_documented_in_table(text) - set(_subcommands(parser)))
        assert not ghost, (
            f"docs/CLI.md 还在写这些子命令，但解析器里没有：{ghost}")


# ---------------------------------------------------------------------------
# 3. 选项 —— 照着抄不能抄错
# ---------------------------------------------------------------------------
def _real_long_flags(parser) -> set:
    flags = set()
    for sp in _subcommands(parser).values():
        for act in sp._actions:
            flags.update(act.option_strings)
    return flags


def test_every_documented_long_flag_exists(parser, text):
    """文档里每个 `--xxx` 都必须在解析器里真的注册了。

    注意会漏掉几处**有意**的散文用法（`--no-Top`、`--xxx`）——
    这正是判据要脆而准的地方，所以用等宽反引号把「当作参数写的」标出来。
    """
    documented = set(re.findall(r"`(--[a-z][a-z-]*)`", text))
    unknown = sorted(documented - _real_long_flags(parser))
    assert not unknown, (
        f"docs/CLI.md 写了这些选项，但解析器里没有：{unknown}\n"
        f"照着抄会直接报 unrecognized arguments。")


def test_format_choices_match_the_parser(parser, text):
    """`-f` 的取值必须与解析器的 choices 一致。

    `CONTRIBUTING.md` 曾经写着 `-f summary`，而 `-f` 只接受
    json/markdown/md/text/txt —— 那条命令跑起来直接退出码非 0。
    """
    real = None
    for act in _subcommands(parser)["run"]._actions:
        if "--format" in act.option_strings:
            real = act.choices
            break
    assert real, "run 子命令的 --format 没有 choices —— 先更新本测试"
    in_doc = re.search(r"`--format`, `-f`\s*\|([^|]*)\|", text)
    assert in_doc, "docs/CLI.md 里找不到 --format 那一行"
    for choice in real:
        assert f"`{choice}`" in in_doc.group(1), (
            f"--format 的取值 {choice!r} 没在文档里写出来")
    # 文档不许写出解析器不接受的值
    for claimed in re.findall(r"`([a-z][a-z0-9]*)`", in_doc.group(1)):
        if claimed not in ("json", "markdown", "md", "text", "txt"):
            assert claimed not in real, (
                f"docs/CLI.md 说 -f 可以取 {claimed!r}，解析器不接受")
