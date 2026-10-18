# -*- coding: utf-8 -*-
"""威胁模型的措辞守卫：防御必须说清「防的是谁、防不住谁」。

来源：一次外部评审。它指出 `web/server.py` 的模块 docstring 以
「**安全边界**」为标题，宣称「回环绑定 + Host 校验」两道门合起来
「只有本机能访问」—— 可同一台机器上任何**同权限进程**都能直接
`curl 127.0.0.1:8765` 并带上合法的 `Host`，两道门都放行。

那个进程本来就能读当前用户能读的一切，所以它不是漏洞；但把它算成
「被防住」意味着**基于这条边界做的安全推演全是错的**。一个把「边界
为什么成立」当卖点的项目，却没画过威胁模型 —— 这是对称的漏洞。

本文件守的是**措辞**，不是行为。行为（Host 校验真的生效、rebinding
真的被挡）由 `tests/test_web_host_guard.py` 那组测；这里守的是
「文档不许做出它没有支撑的安全断言」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
THREAT_MODEL = ROOT / "docs" / "THREAT_MODEL.md"
SERVER_PY = ROOT / "log_ai_compressor" / "web" / "server.py"

# 「承认不防」的那一句必须在**同一行**里同时出现「不防」与「同一台机器 / 同一权限」。
# 限定同行是为了不跨行匹配 —— 否则威胁模型表格里那一行（`本机同权限进程 … ❌ 不防`）
# 会和后面某一行拼成一次假的命中，判据就又落回「附近有这几个字」。
_ADMIT_RE = re.compile(r"(不防|防不住)[^。\n]{0,40}同一(台机器|权限)")


def _server_docstring() -> str:
    """取 server.py 的模块 docstring（中文 docstring，不是 __doc__）。"""
    import ast

    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    return ast.get_docstring(tree) or ""


@pytest.fixture(scope="module")
def doc() -> str:
    assert THREAT_MODEL.is_file(), (
        "docs/THREAT_MODEL.md 不存在 —— 防御一旦没有对手清单，"
        "它就不是防御，是巧合")
    return THREAT_MODEL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 一、文档本身
# ---------------------------------------------------------------------------
class TestThreatModelDoc:
    def test_every_opening_is_named(self, doc: str) -> None:
        """威胁模型必须先列对手。缺了对手的防御清单没有边界。"""
        assert "## 1. 对手清单" in doc, (
            "威胁模型没有对手清单 —— 没有对手就没有边界，"
            "「防住了什么」也就无从判定")

    def test_it_says_what_is_not_defended(self, doc: str) -> None:
        """最要紧的一条：**不防**什么必须写出来。

        外部评审真正指出的不是「少了一道门」，而是「文档假装
        门是全的」。所以判据是「必须承认不防本机同权限进程」。
        """
        assert "不防" in doc, "威胁模型里没有「防不住」的条目"
        assert "本机同权限进程" in doc, (
            "威胁模型漏掉了最该被承认的那一类对手："
            "同一台机器上、同权限级别的任何进程")

    def test_it_records_the_known_gaps(self, doc: str) -> None:
        """已知缺口必须留在文档里，不能因为「先记下」就当修好了。

        本项目以「门禁可失效性」为卖点，自身却缺了行业标配门禁的
        一半（SAST / 依赖扫描 / 性能回归）。把它写下来是为了不让它隐形，
        不是为了让它显得已经处理过。
        """
        for gap in ("SAST", "依赖"):
            assert gap in doc, f"威胁模型没有记录已知缺口：{gap}"


# ---------------------------------------------------------------------------
# 二、代码里的措辞不许比文档更强
# ---------------------------------------------------------------------------
class TestServerDocstringDoesNotOverclaim:
    def test_section_is_named_threat_model_not_security_boundary(self) -> None:
        """判据是**标题**，不是有没有提「安全」两个字。

        原 docstring 的小节标题就是「安全边界」四个字，而它防的只有
        DNS rebinding 一种。把标题改回「安全边界」必须变红。
        """
        doc = _server_docstring()
        assert "威胁模型" in doc, (
            "server.py 的模块 docstring 没有以威胁模型的形式说明边界 —— "
            "它回到只讲「挡住了什么」的老写法，就等于重新做出那个过度声明")
        lines = [ln.strip() for ln in doc.splitlines()]
        bare_claims = [ln for ln in lines
                       if ln in ("安全边界", "Security Boundary")]
        assert not bare_claims, (
            f"docstring 里出现了无边界的整体断言：{bare_claims}。"
            "要么改成「威胁模型」，要么在同一处写清它防的是谁、不防谁")

    def test_it_admits_the_local_process_case(self) -> None:
        """代码层的 docstring 必须自己承认不防本机进程。

        文档层有一页表格还不够 —— 读代码的人多半不会去翻 docs/。
        """
        doc = _server_docstring()
        assert "本机同权限进程" in doc, (
            "server.py 的 docstring 没有承认「本机同权限进程不在防御范围内」。"
            "这条是外部评审指出的核心问题，只写在 docs/ 里等于没写")

    def test_the_admission_is_prose_not_just_a_table_row(self) -> None:
        """表格里标一个 ❌ 不够 —— 还要有一句**散文**把它说出来。

        这条是被变异记分卡逼出来的：把表格里那句
        「**明确不防的：同一台机器上、同一权限级别的任何进程。**」换成
        「**本服务仅监听回环，仅本机可访问。**」时，上面那条
        `test_it_admits_the_local_process_case` **照样绿** ——
        因为威胁模型表格那一行里也有「本机同权限进程」四个字。

        判据落在**同一句话**上，才不会退化成「附近有这几个字就算」。
        """
        doc = _server_docstring()
        found = [ln.strip() for ln in doc.splitlines()
                 if _ADMIT_RE.search(ln)]
        assert found, (
            "docstring 没有用一句话承认「同一台机器上、同一权限级别的进程"
            "不在防御范围内」—— 表格里标一个 ❌ 不算数，"
            "读者不会把表格那一格当成整体结论")

    def test_it_points_at_the_full_model(self) -> None:
        docstring = _server_docstring()
        assert "docs/THREAT_MODEL.md" in docstring, (
            "server.py 的 docstring 必须指向完整威胁模型，"
            "否则两份文档迟早各自漂移")
