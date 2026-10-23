# -*- coding: utf-8 -*-
"""「验不了」不许被说成「被抓了」——以及四档 verdict 必须在每一层都对齐。

缺陷背景
--------
`core/evidence.py` 在 v2.11.1 就把 `UNVERIFIABLE` 独立成档：**源不可读 ≠
被篡改**。但 CLI / MCP / Web / SKILL.md 四处措辞一直还按三档写。

CLI 的后果不是「少写一行说明」，而是**指控了一个没有发生的事**。实测
（导出报告后把源日志轮转走，再 `verify --report`）：

    自校验结论：UNVERIFIABLE                                    ← stdout，对的
    存在对不上的引用 —— 结论可能来自已被修改的日志，请勿直接采信。   ← stderr，错的
    exit 1

日志根本没被改过，只是没了。而 `tests/test_forensics.py` 里那条既有用例
只查了 `capsys.readouterr().out` —— **stderr 里那句指控从来没被任何测试
看见过**。

**退出码 1 是对的，本测试不碰它。** v2.11.1 明确决定过：结论已经无法被
核实，返回 0 等于说「那份报告还成立」。第一版修复时顺手把退出码也改成 0，
等于替别人重新决定了一个有理由的决定，直到那条既有用例当场拦下才意识到 ——
见 `TestUnverifiableIsNotTampered.test_it_still_fails_the_caller`。

要修的只有**措辞**：退出码 1 说的是「你没通过这一关」，不是「有人动了你的
日志」。此外这与本仓库记过多次的形状同源：**修在一层的缺陷，没有传播到其他层**，
所以这里除了钉住 CLI 的行为，还钉住「四档在每一处枚举里都要列全」。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ALL_VERDICTS = {"VERIFIED", "TAMPERED", "UNVERIFIABLE", "NO_EVIDENCE"}

#: 每一层都**必须**提到全部四档的文件。少写一层的代价是那层开始说谎。
_SURFACES = (
    "log_ai_compressor/cli.py",
    "log_ai_compressor/mcp/server.py",
    "log_ai_compressor/web/static/app.js",
    "log_ai_compressor/service.py",
    "SKILL.md",
)


def _run(*args, cwd=None):
    # env 必须显式给：tests/test_subprocess_encoding_contract.py 守的就是这条。
    # 不给就会继承开发机控制台代码页 —— 本地恰好是 UTF-8 时看不出来，
    # Windows CI 的 cp1252 上会解码成乱码（v2.8.0 就是这样挂的）。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "log_ai_compressor", *args],
                          cwd=str(cwd or _ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


@pytest.fixture()
def exported_report(tmp_path):
    """导出报告但**不动源文件** —— 用于观察「真被改过」那一侧。"""
    log = tmp_path / "app.log"
    shutil.copy(_ROOT / "examples" / "sample_system.log", log)
    report = tmp_path / "report.json"
    r = _run("run", str(log), "-f", "json", "-o", str(report), "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    return report


@pytest.fixture()
def rotated_away(exported_report, tmp_path):
    """源日志被「轮转走」—— 源不可读，不是被改过。"""
    exported_report.parent.joinpath("app.log").rename(
        tmp_path / "rotated.log")
    return exported_report


# ---------------------------------------------------------------------------
# 1. CLI 不得指控没发生的篡改
# ---------------------------------------------------------------------------
class TestUnverifiableIsNotTampered:
    def test_it_says_cannot_verify_not_log_was_modified(self, rotated_away):
        r = _run("verify", "--report", str(rotated_away))
        out = r.stdout + r.stderr
        assert "UNVERIFIABLE" in r.stdout, out
        for accusation in ("已被修改", "被篡改", "请勿直接采信"):
            assert accusation not in out, (
                f"CLI 对「源读不到」的说法里出现了 {accusation!r} —— "
                f"那是指控一个没有发生的事：\n{out}")

    def test_it_still_fails_the_caller(self, rotated_away):
        """退出码**必须**是 1 —— 这是 v2.11.1 的明确决定，不是本版新加的。

        结论已经无法被核实，返回 0 等于说「那份报告还成立」；它并未成立，
        只是验不了。两者对读报告的人意义完全不同。

        这一条同时钉住一个事实：本版**只改了措辞，没改退出码**。第一版
        修复时把退出码也改成了 0，等于替别人重新决定了一个有理由的决定 ——
        直到 tests/test_forensics.py 那条既有用例当场拦下才意识到。
        """
        r = _run("verify", "--report", str(rotated_away))
        assert r.returncode == 1, r.stdout + r.stderr

    def test_json_mode_agrees_with_the_text_mode(self, rotated_away):
        r = _run("verify", "--report", str(rotated_away), "--json")
        assert r.returncode == 1, r.stdout + r.stderr
        import json
        assert json.loads(r.stdout)["verdict"] == "UNVERIFIABLE"

    def test_genuine_tampering_still_fails(self, exported_report):
        """反例对照：真被改过的那一侧不能跟着一起放松。

        注意源文件必须**还在** —— 源不在时一律是 UNVERIFIABLE，根本走不到
        TAMPERED 那一侧，用错夹具会得到一个恒真的测试。

        **档位与退出码一个字没改**（TAMPERED / 1）；改的只是「是谁变了」
        那句话 —— 回读只分得出「两边对不上」，**分不出是哪一方**，
        所以话必须停在「对不上」这里，而不是替用户指认一个对象
        （2026-10-24 第十二轮外部复核实测：报告被改坏而日志好得很时，
        原先打印的是「日志**确实被改过**」—— 指控了一件没有发生的事）。
        """
        import json
        d = json.loads(exported_report.read_text(encoding="utf-8"))
        d["clusters"][0]["evidence"][0]["digest"] = "0" * 64     # 伪造摘要
        exported_report.write_text(json.dumps(d, ensure_ascii=False),
                                   encoding="utf-8")
        r = _run("verify", "--report", str(exported_report))
        assert r.returncode == 1, (
            f"日志确实被改过却仍然放行：\n{r.stdout}{r.stderr}")
        assert "TAMPERED" in r.stdout, r.stdout
        out = r.stdout + r.stderr
        assert "请勿直接采信" in out, out
        assert "确实被改过" not in out, (
            "它把「哪一方变了」说死了 —— 回读只分得出「两边对不上」，"
            f"分不出是日志变了还是报告变了：\n{out}")

    def test_an_out_of_range_reference_does_not_accuse_the_log(self, exported_report):
        """报告里的行区间越界 ⇒ **日志一个字没动**，不许说它「确实被改过」。

        实测（2026-10-24 第十二轮外部复核）：把报告里的 ``line_end`` 改成 9999
        （**报告**坏了，日志好得很），verify 打印的是
        「存在对不上的引用 —— 日志**确实被改过**」——
        指控了一件没有发生的事，而照着这句话去查日志的人会白查一轮。

        档位与退出码一个字没改（TAMPERED / 1）—— 要改的只是那句话。
        """
        import json
        d = json.loads(exported_report.read_text(encoding="utf-8"))
        d["clusters"][0]["evidence"][0]["line_end"] = 9999
        exported_report.write_text(json.dumps(d, ensure_ascii=False),
                                   encoding="utf-8")
        r = _run("verify", "--report", str(exported_report))
        out = r.stdout + r.stderr
        assert r.returncode == 1, out
        assert "TAMPERED" in r.stdout, out
        assert "OUT_OF_RANGE" in out, out
        assert "请勿直接采信" in out, out
        assert "确实被改过" not in out, (
            "报告越界 ≠ 日志被改过 —— 日志一个字都没动，"
            f"这句话指控了一件没有发生的事：\n{out}")


# ---------------------------------------------------------------------------
# 2. 四档在每一层都要写全
# ---------------------------------------------------------------------------
class TestEveryLayerKnowsAllFourVerdicts:
    @pytest.mark.parametrize("rel", _SURFACES)
    def test_all_four_verdicts_appear(self, rel):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        missing = sorted(v for v in _ALL_VERDICTS if v not in text)
        assert not missing, (
            f"{rel} 里没提到 {missing} —— 这一层仍按旧的三档对外说话。"
            f"少一档的后果不是「说明不全」，是**把「验不了」说成「被抓了」**。")

    @pytest.mark.parametrize("rel", _SURFACES)
    def test_no_enumeration_leaves_a_verdict_out(self, rel):
        """**每一处枚举**都必须列全，不只是「文件里出现过」。

        第一版只做后者，于是变异把 MCP 里那句
        「返回 VERIFIED / TAMPERED / UNVERIFIABLE / NO_EVIDENCE」改回三档时，
        测试照样绿 —— 因为**同一个文件别处**还提到过 UNVERIFIABLE。
        而那一句正是 Agent 真正读到的枚举。

        判据：**同一行**里同时出现 `VERIFIED` 与 `TAMPERED` 的（那就是一次
        枚举），必须同行出现另外两档。

        取窗口（±2 行）是不行的 —— 实测窗口把下一句
        「UNVERIFIABLE 不等于 TAMPERED」也吞了进来，于是把一处真的漏档
        判成通过。**判据要脆而准，不是宽而漏**。代价是枚举不许折行，
        下面两处源码已按这个约束排过版。
        """
        lines = (_ROOT / rel).read_text(encoding="utf-8").splitlines()
        partial = []
        for lineno, ln in enumerate(lines, 1):
            if "VERIFIED" in ln and "TAMPERED" in ln:
                absent = sorted(v for v in _ALL_VERDICTS if v not in ln)
                if absent:
                    partial.append(f"{rel}:{lineno} 枚举漏了 {absent} :: "
                                   f"{ln.strip()[:70]}")
        assert not partial, (
            "这些枚举少了档位（枚举必须写在同一行）：\n" + "\n".join(partial))

    def test_the_core_and_the_cli_agree_on_the_vocabulary(self):
        """核心层算出来的档位，CLI 必须全部认得。

        不认识的档位会掉进兜底分支 —— 兜底分支说的是「对不上」，于是
        一个新加的档位**默认**会被当成坏消息。
        """
        from log_ai_compressor import cli
        from log_ai_compressor.core.evidence import VerifyReport

        r = VerifyReport()
        known = set(cli._VERDICT_EXIT)
        # 造出四档里除 TAMPERED 外的三种，以及一个未知档位
        seen = set()
        for checked, mismatched, missing_source in (
                (0, 0, 0),        # NO_EVIDENCE
                (1, 0, 1),        # UNVERIFIABLE
                (1, 1, 0),        # TAMPERED
                (1, 0, 0)):       # VERIFIED
            r.checked, r.mismatched, r.missing_source = checked, mismatched, missing_source
            seen.add(r.verdict)
        assert seen == _ALL_VERDICTS, f"核心层只产出了 {seen}"
        unknown = sorted(seen - known)
        assert not unknown, (
            f"核心层会产出 {unknown}，但 CLI 的 `_VERDICT_EXIT` 里没有 —— "
            f"它会掉进「存在对不上的引用」的兜底分支")


# ---------------------------------------------------------------------------
# 3. 静态兜底：措辞里不许残留「三档」的说法
# ---------------------------------------------------------------------------
def test_no_layer_still_describes_the_verdict_as_three_tiers():
    """只查「verdict 有几档」这句话 —— 「三档置信」说的是**置信度**，是两回事。

    第一版按「三档」两个字扫全仓，把 `root_cause_confidence` 那三档也判成
    漏改。**假红比假绿好，但门禁一旦开始报不成立的东西，人就不会再读它。**
    """
    offenders = []
    for rel in _SURFACES:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for lineno, ln in enumerate(text.splitlines(), 1):
            if re.search(r"verdict[^。\n]{0,12}(只有)?\s*三(档|种)", ln):
                offenders.append(f"{rel}:{lineno} {ln.strip()[:70]}")
    assert not offenders, (
        "这些地方仍把 verdict 说成三档：\n" + "\n".join(offenders))
