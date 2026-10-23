# -*- coding: utf-8 -*-
"""两处「把『验不了 / 做不了』说成『出事了 / 没事』」的缺陷。

两处的形状是同一个：**一个函数用同一个返回值代表了几种不同的现实**，
于是上层按最坏的那种理解，输出了一个它没有证据支撑的结论。

1. `_read_range` 用 `None` 同时表示「行数不够」与「**压根读不到**」，
   于是**日志正被另一个进程独占**（正在写入 / 正在轮转 / 编辑器开着 ——
   日志场景里最常见的状态）被报成「日志确实被改过，请勿直接采信」，
   退出码 1。实测解锁后内容**逐字节相同**，再验立刻翻回 `VERIFIED`。

2. `redact_text` 在 `re.sub` 上 `except re.error: pass`，一条写错的自定义
   打码规则**静默地什么也不做** —— 用户以为脱敏了，本该遮住的敏感串
   原样出现在报告里，屏幕上没有任何提示。

两边都要钉住**不能放松的那一侧**：文件真的被截短仍必须报 TAMPERED；
合法自定义规则仍必须生效。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from log_ai_compressor import service as S
from log_ai_compressor.core import evidence as E
from log_ai_compressor.core import redact as R

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SAMPLE = _ROOT / "examples" / "sample_system.log"


# ---------------------------------------------------------------------------
# 1. 读不到 ≠ 被改过
# ---------------------------------------------------------------------------
class TestUnreadableIsNotTampered:
    def test_read_range_has_three_states_not_one(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("l1\nl2\nl3\n", encoding="utf-8")
        assert E._read_range(p, 1, 3)[0] == E._READ_OK
        # 读到了，但那一段不存在 —— 文件被截短了，**这才是真的变了**
        assert E._read_range(p, 1, 9)[0] == E._READ_OUT_OF_RANGE

    def test_an_unreadable_file_is_not_a_mismatch(self, tmp_path, monkeypatch):
        """读不到 → missing_source → verdict UNVERIFIABLE，而不是 TAMPERED。"""
        p = tmp_path / "a.log"
        p.write_text("l1\nl2\n", encoding="utf-8")

        def _boom(*a, **kw):
            raise PermissionError("file is locked by another process")

        # 接缝是**解码器**（core.encoding.LogDecoder），不是 open_text_stream：
        # 回读路径已经改成与摄入共用同一个解码器（修复缺陷#2）。
        # 若这里还去 patch open_text_stream，异常永远不触发，测试会**假绿** ——
        # 它断言的是「读不到不被当成被改过」，而接缝错位后这条断言
        # 根本走不到要验的那条分支。
        monkeypatch.setattr(E, "LogDecoder", _boom)
        ref = E.EvidenceRef(evidence_id="x", source=str(p),
                            line_start=1, line_end=2, digest="deadbeef")
        report = E.verify_refs([ref])
        assert report.mismatched == 0, "读不到被算成了「对不上」"
        assert report.missing_source == 1
        assert report.verdict == "UNVERIFIABLE", report.verdict
        assert report.details[0]["status"] == "UNREADABLE"

    @pytest.mark.skipif(sys.platform != "win32"
                        if (sys := __import__("sys")) else True,
                        reason="独占锁是 Windows 语义")
    def test_a_file_held_by_another_process_is_not_tampered(self, tmp_path):
        """真机复现：msvcrt 独占锁住文件，内容一个字没改。

        这是日志场景里最常见的状态 —— 另一个进程正在往这个日志里写。
        修复前它会被报成 TAMPERED 并让退出码变成 1。
        """
        import msvcrt

        log = tmp_path / "app.log"
        log.write_bytes(_SAMPLE.read_bytes())
        result = S.analyze(paths=[str(log)])
        refs = [r for c in result.clusters for r in c.evidence]
        assert refs, "没有可比对的证据"

        fh = open(log, "r+b")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 64)
        try:
            report = E.verify_refs(refs)
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 64)
            fh.close()

        assert report.verdict == "UNVERIFIABLE", (
            f"文件只是被独占锁住，却被报成 {report.verdict}")
        # 解锁后必须真的能验过 —— 否则「UNVERIFIABLE」只是把问题藏起来
        assert E.verify_refs(refs).verdict == "VERIFIED"


class TestCliSurfacesUnreadable:
    """CLI 层：`UNREADABLE` 必须出现在 stderr 明细里。

    只改 verdict 不够 —— 逐条明细有个独立的过滤元组，加了新状态忘了加进去，
    屏幕上就只剩一个 `UNVERIFIABLE`，用户既看不到**哪几条**验不了、也看不到
    是哪个文件。那些正是排查时唯一有用的信息。

    走 `--report` 而不是自检模式：自检模式会先 `analyze` 同一个文件，
    而本测试要模拟的正是「分析时读得好好的、回读时被独占锁挡住」，
    report 模式从 JSON 取引用、不碰日志文件，才造得出这个状态。
    """

    def test_it_lists_unreadable_refs_on_stderr(self, tmp_path,
                                                monkeypatch, capsys):
        from log_ai_compressor.cli import main

        log = tmp_path / "app.log"
        log.write_bytes(_SAMPLE.read_bytes())
        rep = tmp_path / "report.json"
        rep.write_text(S.export_text(S.analyze(paths=[str(log)]), "json"),
                       encoding="utf-8")

        def _boom(*a, **kw):
            raise PermissionError("file is locked by another process")

        monkeypatch.setattr(E, "LogDecoder", _boom)
        rc = main(["verify", "--report", str(rep)])
        cap = capsys.readouterr()          # capsys 只认一次，两路一起取
        said = cap.out + cap.err

        # 退出码仍是 1（v2.11.1 的明确决定：返回 0 等于说「那份报告还成立」）
        assert rc == 1, rc
        assert "UNREADABLE" in cap.err, cap.err
        assert "验不了" in said, said
        assert "被改" not in said and "TAMPERED" not in said, (
            "读不到被说成了「日志被改过」：\n" + said)


class TestGenuineTruncationStillFails:
    """反例对照：不能因为修了读不到，就把真被改过的那一侧一起放松。"""

    def test_a_truncated_log_is_still_tampered(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_bytes(_SAMPLE.read_bytes())
        result = S.analyze(paths=[str(log)])
        refs = [r for c in result.clusters for r in c.evidence]
        lines = log.read_text(encoding="utf-8").splitlines(True)
        log.write_text("".join(lines[:5]), encoding="utf-8")   # 砍到只剩 5 行
        report = E.verify_refs(refs)
        assert report.verdict == "TAMPERED", report.verdict
        assert report.mismatched > 0

    def test_an_edited_line_is_still_tampered(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_bytes(_SAMPLE.read_bytes())
        result = S.analyze(paths=[str(log)])
        refs = [r for c in result.clusters for r in c.evidence]
        lines = log.read_text(encoding="utf-8").splitlines(True)
        # 改**第一条证据覆盖到的那一行**的最后一个字符。
        # 第一版写死改第 2 行的某个字符串 —— 样例那一行里根本没有它，
        # replace 空转，测试自己测了个「没被改过的文件」。
        idx = refs[0].line_start - 1
        assert lines[idx].strip(), "那一行是空的，换一条"
        lines[idx] = lines[idx].replace(lines[idx].strip()[-1], "X", 1)
        log.write_text("".join(lines), encoding="utf-8")
        assert E.verify_refs(refs).verdict == "TAMPERED"


# ---------------------------------------------------------------------------
# 2. 非法打码规则不许静默跳过
# ---------------------------------------------------------------------------
_SENSITIVE = "account CUST-998877 locked"     # 内置规则碰不到，才测得出来


class TestInvalidCustomRedactionIsRejected:
    def test_a_valid_custom_rule_still_applies(self):
        assert R.redact_text(_SENSITIVE, [r"CUST-\d+"]) == \
            "account [自定义] locked"

    @pytest.mark.parametrize("bad", ["CUST-[", "(unclosed", "*x", "(?P<", "[z-a]"])
    def test_an_invalid_rule_raises(self, bad):
        with pytest.raises(ValueError) as e:
            R.redact_text(_SENSITIVE, [bad])
        msg = str(e.value)
        assert "不是合法正则" in msg, msg
        assert bad in msg, "报错里必须带上原始规则，否则用户不知道怎么改"

    def test_it_names_which_rule_is_wrong(self):
        with pytest.raises(ValueError) as e:
            R.redact_text(_SENSITIVE, [r"CUST-\d+", "second-is-bad("])
        assert "第 2 条" in str(e.value), str(e.value)

    def test_the_sensitive_string_is_not_shipped_on_error(self):
        """回归本缺陷的原形状：非法规则下敏感串**原样出去**且无提示。"""
        with pytest.raises(ValueError):
            R.redact_text(_SENSITIVE, ["CUST-["])
        # 抛错即意味着**没有产出报告**；下面这条断言是在提醒实现者别改成
        # 「打个警告继续导出」—— 那正是本缺陷本身。
        assert R.redact_text(_SENSITIVE, []) == _SENSITIVE

    def test_empty_rules_are_fine(self):
        assert R.redact_text(_SENSITIVE, [None, "", None]) == _SENSITIVE
        assert R.redact_text(_SENSITIVE, None) == _SENSITIVE


class TestServiceSurfacesTheError:
    def test_export_text_raises_service_error(self, tmp_path):
        result = S.analyze(paths=[str(_SAMPLE)])
        with pytest.raises(S.ServiceError) as e:
            S.export_text(result, "md", redact=True, custom_rules=["bad-("])
        assert "不是合法正则" in str(e.value)

    def test_it_is_not_a_service_error_when_redact_is_off(self, tmp_path):
        """不脱敏就不该校验 —— 规则只在 redact=True 时才生效。"""
        result = S.analyze(paths=[str(_SAMPLE)])
        assert S.export_text(result, "md", custom_rules=["bad-("])


def test_the_false_claim_about_the_ui_layer_is_gone():
    """原注释写着「非法正则已在界面层拦（截/挡）」—— 那是假的。

    Web 前端恒传 `custom_rules: []`（app.js），界面层压根没有自定义规则的
    入口。把这种「以为别处已经校验过」写进注释，下一个人就会再删掉校验。

    判据要避开**引用它来纠正它**的那些段落 —— 第一版把检查写成子串
    「界面层拦截」不在文件里，结果被我自己那句「原注释写着……」命中，
    于是这条测试逼着我把正确的解释也删掉。**判据脆而有据，别写成宽而漏。**
    """
    src = (_ROOT / "log_ai_compressor" / "core" / "redact.py").read_text(
        encoding="utf-8")
    claims = [ln for ln in src.splitlines()
              if re.search(r"非法正则.{0,10}(界面层|前端|UI)", ln)
              and not re.search(r"那句话是假的|原注释", ln)]
    assert not claims, (
        "core/redact.py 还在声称别处会拦截非法正则 —— 没有任何地方会：\n"
        + "\n".join(claims))
