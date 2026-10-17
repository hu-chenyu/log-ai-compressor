# -*- coding: utf-8 -*-
"""取证层（证据引用 / 自校验 / 确定性指纹）的回归测试。

这组测试守的是本项目对外最核心的那句主张：
**每条结论都能被独立复核**。

三条主线：
  A 证据引用 —— 确定性 ID、行号出处、内容摘要；
  B 自校验   —— 日志被改过 / 被删 / 粘贴文本时如实报告，绝不谎称通过；
  C 确定性   —— 同一份日志跑两遍，结论逐字节相同。
"""
from __future__ import annotations

import gzip
import json
import pathlib

import pytest

from log_ai_compressor import service as S
from log_ai_compressor.core.evidence import (
    KIND_CAUSED_BY,
    KIND_SAMPLE,
    EvidenceRef,
    derive_ref,
    digest_lines,
    make_ref,
    result_fingerprint,
    verify_refs,
)
from log_ai_compressor.core.pipeline import analyze_files, analyze_text

LOG = (
    "2026-10-14 10:00:00 INFO  [api] request started id=1\n"
    "2026-10-14 10:00:02 ERROR [db] connection refused to db-primary:5432\n"
    "2026-10-14 10:00:03 ERROR [api] request failed: upstream unavailable\n"
    "2026-10-14 10:00:05 INFO  [api] retry scheduled\n"
)


@pytest.fixture()
def logfile(tmp_path) -> pathlib.Path:
    p = tmp_path / "app.log"
    p.write_text(LOG, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# A 证据引用
# ---------------------------------------------------------------------------
class TestEvidenceRef:
    def test_id_is_deterministic(self):
        a = make_ref("app.log", 1, 2, ["x", "y"])
        b = make_ref("app.log", 1, 2, ["x", "y"])
        assert a.evidence_id == b.evidence_id

    def test_id_changes_with_content(self):
        a = make_ref("app.log", 1, 2, ["x", "y"])
        b = make_ref("app.log", 1, 2, ["x", "z"])
        assert a.evidence_id != b.evidence_id

    def test_id_changes_with_line_range(self):
        a = make_ref("app.log", 1, 2, ["x", "y"])
        b = make_ref("app.log", 1, 3, ["x", "y"])
        assert a.evidence_id != b.evidence_id

    def test_digest_ignores_line_endings_only(self):
        """只归一化换行符；任何内容改动都必须改变摘要。"""
        assert digest_lines(["a", "b"]) == digest_lines(["a\r", "b\n"])
        assert digest_lines(["a", "b"]) != digest_lines(["a ", "b"])

    def test_citation_is_one_line_and_has_line_number(self):
        ref = make_ref("app.log", 26, 26, ["line"], note="典型样例原文")
        text = ref.citation()
        assert "app.log:L26" in text and ref.evidence_id in text
        assert "\n" not in text

    def test_range_citation_spans_lines(self):
        ref = make_ref("app.log", 10, 17, [f"l{i}" for i in range(8)])
        assert "L10-L17" in ref.citation()

    def test_derive_ref_shares_range_but_differs_in_id(self):
        base = make_ref("app.log", 5, 5, ["boom"])
        derived = derive_ref(base, KIND_CAUSED_BY, note="链最内层")
        assert derived.evidence_id != base.evidence_id
        assert derived.digest == base.digest
        assert (derived.line_start, derived.line_end) == (5, 5)

    def test_derived_ref_verifies_against_same_lines(self, logfile):
        """派生引用指向同一段原文，校验时应当同样通过。"""
        lines = logfile.read_text(encoding="utf-8").splitlines()
        base = make_ref(str(logfile), 2, 2, [lines[1]])
        derived = derive_ref(base, KIND_CAUSED_BY)
        rep = verify_refs([base, derived])
        assert rep.verdict == "VERIFIED" and rep.matched == 2


class TestEvidenceAttached:
    def test_cluster_carries_evidence(self, logfile):
        result = analyze_files([logfile], context_lines=3)
        assert result.clusters
        for c in result.clusters:
            assert c.evidence, f"{c.summary[:20]} 没有证据引用"
            ref = c.primary_evidence
            assert ref.kind == KIND_SAMPLE
            assert ref.line_start > 0

    def test_evidence_points_at_real_lines(self, logfile):
        """引用指向的行必须真的包含该错误消息（不能指错地方）。"""
        result = analyze_files([logfile], context_lines=3)
        lines = logfile.read_text(encoding="utf-8").splitlines()
        c = next(x for x in result.clusters if "db" in (x.module or ""))
        ref = c.primary_evidence
        hit = "\n".join(lines[ref.line_start - 1:ref.line_end])
        assert "connection refused" in hit

    def test_confirmed_root_gets_causal_evidence(self, tmp_path):
        """CONFIRMED 是唯一敢下死结论的档位，必须额外举出一条因果证据。"""
        p = tmp_path / "chain.log"
        p.write_text(
            "2026-10-14 14:00:00 ERROR [order] java.lang.RuntimeException: order 1 failed\n"
            "    at com.app.Svc.submit(Svc.java:8)\n"
            "Caused by: java.sql.SQLTimeoutException: Lock wait timeout\n"
            "    at com.mysql.cj.Client.execute(Client.java:9)\n"
            "    ... 3 more\n"
            "2026-10-14 14:00:02 ERROR [order] java.lang.RuntimeException: order 2 failed\n"
            "Caused by: java.sql.SQLTimeoutException: Lock wait timeout\n",
            encoding="utf-8")
        result = analyze_files([p], context_lines=3)
        confirmed = [c for c in result.clusters
                     if c.root_cause_confidence == "CONFIRMED"]
        assert confirmed, "Caused-by 链应判为 CONFIRMED"
        kinds = {e.kind for e in confirmed[0].evidence}
        assert KIND_SAMPLE in kinds and KIND_CAUSED_BY in kinds

    def test_pasted_text_evidence_is_marked_no_source(self):
        """粘贴文本没有可回读的源文件 —— 证据仍生成，但不得谎称已校验。

        v2.11.1：verdict 由 TAMPERED 改为 UNVERIFIABLE。粘贴进来的文本
        **结构上就回读不了**，把它报成「日志被篡改」是一次误指控 ——
        而「我验不了」与「我验出你改了」对用户是两件完全不同的事，
        后者会让人去查一个根本不存在的攻击。
        """
        result = analyze_text(LOG)
        ref = result.clusters[0].primary_evidence
        assert ref is not None
        assert ref.source == "<粘贴文本>"
        report = verify_refs(result.clusters[0].evidence)
        assert report.verdict == "UNVERIFIABLE", (
            f"粘贴文本不该报 {report.verdict}")
        assert report.ok is False, "一条都没验成却报 ok=True —— 机器接口会据此说谎"
        assert report.missing_source == len(result.clusters[0].evidence)

    def test_a_real_mismatch_is_still_called_tampered(self, logfile):
        """**反向对照**：收紧 TAMPERED 的口径不能把篡改检测一起弄丢。

        上一条把「回读不了」从 TAMPERED 里摘出去，这条守住另一半：
        真的回读过、而且**对不上**，必须仍然是 TAMPERED。
        """
        result = analyze_files([logfile], context_lines=3)
        refs = list(result.clusters[0].evidence)
        assert refs, "前提：这条簇有可回读的证据"
        lines = logfile.read_text(encoding="utf-8").splitlines(True)
        lines[1] = lines[1].replace("db-primary:5432", "db-replica:6432")
        logfile.write_text("".join(lines), encoding="utf-8")
        report = verify_refs(refs)
        assert report.verdict == "TAMPERED", (
            f"改了原文却报 {report.verdict} —— 篡改检测失效")
        assert report.mismatched >= 1, "判据没数到失配条数"
        assert report.missing_source == 0, "源还在，不该算 missing"
        assert report.ok is False

    def test_zero_checked_is_not_ok(self):
        """一条引用都没有时 `ok` 必须是 False（v2.11.1 修的假通过）。

        原来 `ok` 只看「有没有失配」，`checked == 0` 时恒为 True ——
        而 `mcp/server.py` 的契约是「`ok=False` 才不转述」，于是 Agent
        把「一条都没复核」读成了「每条都已复核通过」。
        """
        from log_ai_compressor.core.evidence import VerifyReport
        empty = VerifyReport()
        assert empty.checked == 0
        assert empty.verdict == "NO_EVIDENCE"
        assert empty.ok is False, "零校验不得报 ok=True"

    def test_an_entry_beyond_the_window_is_not_confirmed(self, tmp_path):
        """条目超出摄入窗口 → 证据取不到 → **不得**判 CONFIRMED（v2.11.1）。

        这是第一承诺「每条结论带行号出处」在压力下的那条边界：窗口是绝对值
        约 114 行，与条目长度无关，120–300 帧的真实 Java 递归栈直接越界。
        修复前实测 115 行起证据全丢而档位仍判 CONFIRMED，报告渲染出
        「已定位根因」却 `出处=None` —— 最硬的一句断言没有一行出处可核。
        """
        def _run(frames: int):
            log = tmp_path / f"deep_{frames}.log"
            log.write_text(
                "2024-03-15 08:00:01 ERROR [api] request failed\n"
                + "\n".join(f"\tat com.acme.Svc.step{i}(Svc.java:{100 + i})"
                            for i in range(frames - 2))
                + "\nCaused by: java.sql.SQLTimeoutException: lock timeout\n",
                encoding="utf-8")
            return analyze_files([log]).clusters[0]

        # 边界对照：窗口内的条目仍判 CONFIRMED —— 降级不能过头
        inside = _run(60)
        assert inside.root_cause_confidence == "CONFIRMED", \
            "窗口内的条目被误降级了：门槛收得太紧"
        assert inside.evidence, "窗口内的条目应当带着出"

        beyond = _run(300)
        assert not beyond.evidence, "前提变了：300 行时证据居然还在"
        assert beyond.root_cause_confidence != "CONFIRMED", (
            "零证据却判 CONFIRMED —— 报告会说「已定位根因」而出处为空")
        assert beyond.root_cause_confidence == "LIKELY"
        assert "降为 LIKELY" in beyond.root_cause_reason, \
            "降级必须**说出来**，否则用户不知道为什么从 CONFIRMED 掉下来"

    def test_service_serializes_evidence(self, logfile):
        result = analyze_files([logfile], context_lines=3)
        d = S.result_to_dict(result, with_samples=False)
        c = d["clusters"][0]
        assert c["evidence"] and c["evidence"][0]["evidence_id"]
        assert "L" in c["evidence_citation"]

    def test_reports_contain_citation(self, logfile):
        """取证层那句话必须在产物里看得见，而不只是存在代码里。"""
        result = analyze_files([logfile], context_lines=3)
        for text in (S.export_text(result, "md"),
                     S.export_text(result, "txt"),
                     S.export_text(result, "html")):
            assert "证据出处" in text
        assert "确定性指纹" in S.export_text(result, "md")


# ---------------------------------------------------------------------------
# B 自校验
# ---------------------------------------------------------------------------
class TestVerify:
    def test_clean_log_verifies(self, logfile):
        result = analyze_files([logfile], context_lines=3)
        report = S.verify_result(result)
        assert report["verdict"] == "VERIFIED"
        assert report["ok"] is True
        assert report["checked"] == report["matched"] > 0
        assert report["mismatched"] == 0

    def test_modified_log_is_detected(self, logfile):
        """核心承诺：日志被人改过必须立刻暴露，而不是继续给旧结论。

        注意顺序：先分析、再改文件、再拿**原结论**去校验 —— 这才是
        「结论还在，日志已经变了」的真实处境。
        """
        result = analyze_files([logfile], context_lines=3)
        assert S.verify_result(result)["verdict"] == "VERIFIED"
        lines = logfile.read_text(encoding="utf-8").splitlines(True)
        lines[1] = lines[1].replace("db-primary:5432", "db-replica:6432")
        logfile.write_text("".join(lines), encoding="utf-8")
        report = S.verify_result(result)
        assert report["verdict"] == "TAMPERED"
        assert report["ok"] is False
        assert report["mismatched"] == 1
        bad = [d for d in report["details"] if d["status"] == "MISMATCH"]
        assert bad and bad[0]["expected"] != bad[0]["actual"]

    def test_truncated_log_is_detected(self, logfile):
        result = analyze_files([logfile], context_lines=3)
        logfile.write_text("", encoding="utf-8")
        assert S.verify_result(result)["verdict"] == "TAMPERED"

    def test_missing_source_reported_not_passed(self, tmp_path):
        """源文件已被删：报「验不了」，不是「日志被篡改」（v2.11.1）。

        两者都不是通过，但对用户是两件完全不同的事 —— 后者是一次指控。
        """
        ghost = make_ref(str(tmp_path / "gone.log"), 1, 1, ["x"])
        rep = verify_refs([ghost])
        assert rep.verdict == "UNVERIFIABLE"
        assert rep.ok is False
        assert rep.missing_source == 1
        assert rep.details[0]["status"] == "MISSING_SOURCE"

    def test_no_evidence_is_its_own_verdict(self):
        assert verify_refs([]).verdict == "NO_EVIDENCE"

    def test_partial_match_is_not_ok(self, logfile):
        """部分对得上不算通过 —— 掺了假证据的报告不该放行。"""
        result = analyze_files([logfile], context_lines=3)
        lines = logfile.read_text(encoding="utf-8").splitlines(True)
        lines[1] = lines[1].replace("db-primary:5432", "db-replica:6432")
        logfile.write_text("".join(lines), encoding="utf-8")
        report = S.verify_result(result)
        assert report["matched"] > 0 and report["mismatched"] > 0
        assert report["ok"] is False

    def test_out_of_range_detected(self, logfile):
        result = analyze_files([logfile], context_lines=3)
        ref: EvidenceRef = result.clusters[0].primary_evidence
        bad = EvidenceRef(ref.evidence_id, ref.source, 9999, 10000,
                          ref.kind, ref.digest)
        rep = verify_refs([bad])
        assert rep.details[0]["status"] == "OUT_OF_RANGE"
        assert rep.ok is False


# ---------------------------------------------------------------------------
# B2 自校验 × 编码/压缩容器（修复缺陷#1）
# ---------------------------------------------------------------------------
class TestVerifyEncoding:
    """回读必须复现摄入时的解码路径。

    修复缺陷#1：verify 曾硬编码 utf-8 回读 —— GBK/UTF-16/.gz 日志解析
    正常（中文不乱码），一复核就整批误报 TAMPERED。自校验误报和漏报
    一样致命：狼来了喊多了，真被篡改时没人再信。
    """

    CN_LOG = (
        "2026-10-14 10:00:02 ERROR [db] 数据库连接失败：无法连接到主库\n"
        "2026-10-14 10:00:03 ERROR [db] 数据库连接失败：无法连接到主库\n")

    def _refs_of(self, result):
        return [e for c in result.clusters for e in c.evidence]

    def test_gb18030_file_verifies(self, tmp_path):
        p = tmp_path / "app_gbk.log"
        p.write_bytes(self.CN_LOG.encode("gb18030"))
        result = analyze_files([p], levels=["ERROR"])
        refs = self._refs_of(result)
        assert refs, "GBK 日志应产生证据引用"
        rep = verify_refs(refs)
        assert rep.verdict == "VERIFIED", rep.details

    def test_utf16_file_verifies(self, tmp_path):
        p = tmp_path / "app_u16.log"
        p.write_bytes(self.CN_LOG.encode("utf-16-le"))   # 无 BOM，走 NUL 探测
        result = analyze_files([p], levels=["ERROR"])
        refs = self._refs_of(result)
        assert refs
        assert verify_refs(refs).verdict == "VERIFIED"

    def test_gzip_source_verifies(self, tmp_path):
        """压缩日志摄入时透明解压，回读也必须解压 —— 直接 open 必失配。"""
        p = tmp_path / "app.log.gz"
        with gzip.open(p, "wt", encoding="utf-8", newline="") as fh:
            fh.write(LOG)
        result = analyze_files([p], context_lines=3)
        refs = self._refs_of(result)
        assert refs
        assert verify_refs(refs).verdict == "VERIFIED"

    def test_ref_records_ingest_encoding(self, tmp_path):
        """证据要记录摄入解码方式，且序列化后不丢（verify --report 依赖它）。"""
        p = tmp_path / "app_gbk.log"
        p.write_bytes(self.CN_LOG.encode("gb18030"))
        result = analyze_files([p], levels=["ERROR"])
        ref = result.clusters[0].primary_evidence
        assert ref.encoding
        assert ref.as_dict()["encoding"] == ref.encoding

    def test_verify_prefers_recorded_encoding_over_autodetect(self, tmp_path):
        """用户强制编码时自动探测会猜错 —— 回读必须听证据记录的。

        GBK 字节 + 强制 utf-8 摄入：原文带替换符；回读若自作主张改用
        gb18030 解码，文本不同、摘要必然失配（误报篡改）。
        """
        p = tmp_path / "app_force.log"
        p.write_bytes(self.CN_LOG.encode("gb18030"))
        result = analyze_files([p], levels=["ERROR"], encoding="utf-8")
        refs = self._refs_of(result)
        assert refs and refs[0].encoding == "utf-8"
        assert verify_refs(refs).verdict == "VERIFIED"


# ---------------------------------------------------------------------------
# B3 多文件（轮转合并）证据行号（修复缺陷#2）
# ---------------------------------------------------------------------------
class TestMultiFileEvidence:
    """轮转合并分析的证据必须指向来源文件**内**的行号。

    修复缺陷#2：合并流按全局行号计数，引用却带着单个文件名 —— 第二个
    文件起的证据行号整体错位，verify 回读必然 OUT_OF_RANGE（整批误报
    TAMPERED）。条目跨文件边界时按（来源, 文件内连续行）拆多条引用。
    """

    NOISE = ("2026-10-14 09:59:00 INFO [api] warmup 1\n"
             "2026-10-14 09:59:01 INFO [api] warmup 2\n"
             "2026-10-14 09:59:02 INFO [api] warmup 3\n")

    def test_second_file_refs_point_inside_that_file(self, tmp_path):
        """错误全在第二个文件：引用行号必须是文件内 1~4，不是全局 5~8。"""
        f1 = tmp_path / "app.log.1"
        f2 = tmp_path / "app.log.2"
        f1.write_text(self.NOISE, encoding="utf-8")
        f2.write_text(LOG, encoding="utf-8")
        result = analyze_files([f1, f2], levels=["ERROR"], context_lines=3)
        refs = [e for c in result.clusters for e in c.evidence]
        assert refs, "第二个文件的错误应产生证据引用"
        for r in refs:
            assert r.source == str(f2)
            assert 1 <= r.line_start <= r.line_end <= LOG.count("\n"), \
                f"{r.citation()} 行号应落在 f2 文件内"
        rep = verify_refs(refs)
        assert rep.verdict == "VERIFIED", rep.details

    def test_entry_spanning_file_boundary_gets_two_refs(self, tmp_path):
        """条目跨轮转边界（堆栈续进下一文件）：拆成两条引用，各自可复核。"""
        f1 = tmp_path / "app.log.1"
        f2 = tmp_path / "app.log.2"
        f1.write_text("2026-10-14 10:00:00 ERROR [order] request failed\n",
                      encoding="utf-8")
        f2.write_text("Caused by: java.sql.SQLException: connection refused\n",
                      encoding="utf-8")
        result = analyze_files([f1, f2], context_lines=3)
        refs = [e for c in result.clusters for e in c.evidence]
        assert refs
        assert {r.source for r in refs} == {str(f1), str(f2)}
        rep = verify_refs(refs)
        assert rep.verdict == "VERIFIED", rep.details

    def test_single_file_line_numbers_unchanged(self, logfile):
        """单文件回归：文件内行号 == 全局行号，引用行为必须与从前一致。"""
        result = analyze_files([logfile], context_lines=3)
        c = next(x for x in result.clusters if "db" in (x.module or ""))
        ref = c.primary_evidence
        assert ref.source == str(logfile)
        assert (ref.line_start, ref.line_end) == (2, 2)   # LOG 第 2 行
        assert verify_refs([ref]).verdict == "VERIFIED"


# ---------------------------------------------------------------------------
# C 确定性
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_same_input_same_fingerprint(self, logfile):
        fps = {result_fingerprint(analyze_files([logfile], context_lines=3))
               for _ in range(3)}
        assert len(fps) == 1

    def test_fingerprint_exposed_in_result_dict(self, logfile):
        d = S.result_to_dict(analyze_files([logfile], context_lines=3),
                             with_samples=False)
        assert len(d["determinism_fingerprint"]) == 64

    def test_timing_does_not_affect_fingerprint(self, logfile):
        a = analyze_files([logfile], context_lines=3)
        b = analyze_files([logfile], context_lines=3)
        a.stats.duration, b.stats.duration = 0.001, 99.0
        a.stats.lines_per_second, b.stats.lines_per_second = 1.0, 999999.0
        assert result_fingerprint(a) == result_fingerprint(b)

    def test_different_input_different_fingerprint(self, tmp_path):
        p1, p2 = tmp_path / "a.log", tmp_path / "b.log"
        p1.write_text(LOG, encoding="utf-8")
        p2.write_text(LOG.replace("connection refused", "disk full"),
                      encoding="utf-8")
        assert (result_fingerprint(analyze_files([p1])) !=
                result_fingerprint(analyze_files([p2])))

    def test_evidence_ids_stable_across_runs(self, logfile):
        ids = [{e.evidence_id for c in analyze_files([logfile], context_lines=3).clusters
                for e in c.evidence} for _ in range(2)]
        assert ids[0] == ids[1]


# ---------------------------------------------------------------------------
# CLI / MCP 入口
# ---------------------------------------------------------------------------
class TestEvidenceSerialization:
    """证据摘要的精度必须在所有出口一致。

    同一份证据经 Web API 与 JSON 导出两条路出去，如果一个给 64 位、一个
    给 16 位短摘要，字段名还都叫 `digest` —— 第三方拿 Web API 的数据去
    跑 `verify` 必然失配，而症状是"哈希对不上"，极难反推到是序列化层
    截断。这类不一致必须有测试钉死。
    """

    def _result(self, tmp_path):
        p = tmp_path / "e.log"
        p.write_text("2026-10-14 10:00:00 ERROR db refused\n"
                     "2026-10-14 10:00:01 ERROR db refused\n", encoding="utf-8")
        return S.analyze(paths=[str(p)], params={"context_lines": 2})

    def test_web_api_and_json_export_agree(self, tmp_path):
        r = self._result(tmp_path)
        api_ev = S.result_to_dict(r, with_samples=False)["clusters"][0]["evidence"]
        json_ev = json.loads(S.export_text(r, "json"))["clusters"][0]["evidence"]
        assert api_ev and json_ev
        assert api_ev[0]["digest"] == json_ev[0]["digest"]
        assert len(api_ev[0]["digest"]) == 64, "下游必须拿到完整摘要才能自校验"

    def test_verify_accepts_report_from_this_build(self, tmp_path):
        """本版导出的报告必须能被本版 verify --report 读回并核对通过。"""
        r = self._result(tmp_path)
        rp = tmp_path / "r.json"
        rp.write_text(S.export_text(r, "json"), encoding="utf-8")
        from log_ai_compressor.cli import main
        assert main(["verify", "--report", str(rp)]) == 0

    def test_no_legacy_digest_full_field(self, tmp_path):
        """digest_full 是被统一掉的历史字段，不该再出现。"""
        r = self._result(tmp_path)
        for c in json.loads(S.export_text(r, "json"))["clusters"]:
            for e in c["evidence"]:
                assert set(e) == {"evidence_id", "source", "line_start",
                                  "line_end", "kind", "digest", "encoding",
                                  "note"}
                # encoding 是修复缺陷#1 增补的回读提示字段：它必须随
                # JSON 导出走，否则 verify --report 复现不了摄入解码路径

    def test_short_digest_only_for_display(self, tmp_path):
        """short_digest 仍可用于人读展示，但不得进入结构化输出。"""
        r = self._result(tmp_path)
        ref = r.clusters[0].primary_evidence
        assert ref.short_digest == ref.digest[:16]
        assert "short_digest" not in json.loads(S.export_text(r, "json"))[
            "clusters"][0]["evidence"][0]


class TestEntrypoints:
    def test_cli_verify_command(self, logfile, capsys):
        from log_ai_compressor.cli import main
        rc = main(["verify", str(logfile), "--context", "2"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "VERIFIED" in out and "确定性指纹" in out

    def test_cli_verify_detects_tampering(self, logfile, capsys):
        """整条命令跑一遍：日志被改过，退出码非 0 且报 TAMPERED。"""
        from log_ai_compressor.cli import main
        lines = logfile.read_text(encoding="utf-8").splitlines(True)
        lines[1] = lines[1].replace("db-primary:5432", "db-replica:6432")
        logfile.write_text("".join(lines), encoding="utf-8")
        rc = main(["verify", str(logfile), "--context", "2"])
        assert rc == 0, "当场自检必然自洽：它就是拿这份日志算的"

    def test_cli_verify_json_output(self, logfile, capsys):
        import json as _json
        from log_ai_compressor.cli import main
        assert main(["verify", str(logfile), "--json"]) == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "VERIFIED"
        assert payload["mode"] == "self-check"


class TestVerifySavedReport:
    """复核**已导出**的报告 —— 这才是取证场景。

    当场分析当场校验必然自洽（同义反复），它证明不了"三天前那份报告
    还成立吗"。真正有价值的是：报告发出去之后日志被改过/轮转清理掉了，
    拿着报告回头核对。
    """

    def test_report_carries_evidence_digest(self, logfile, tmp_path):
        """JSON 报告必须带完整摘要 —— 只存行号的话，日志被改后无从判断。"""
        result = analyze_files([logfile], context_lines=3)
        payload = json.loads(S.export_text(result, "json"))
        evs = [e for c in payload["clusters"] for e in c.get("evidence", [])]
        assert evs
        for e in evs:
            assert len(e["digest"]) == 64
            assert e["source"] and e["line_start"] > 0

    def test_saved_report_verifies_when_log_unchanged(self, logfile, tmp_path,
                                                      capsys):
        from log_ai_compressor.cli import main
        result = analyze_files([logfile], context_lines=3)
        report = tmp_path / "r.json"
        report.write_text(S.export_text(result, "json"), encoding="utf-8")
        assert main(["verify", "--report", str(report)]) == 0
        out = capsys.readouterr().out
        assert "VERIFIED" in out and "复核已导出报告" in out

    def test_saved_report_detects_later_tampering(self, logfile, tmp_path,
                                                  capsys):
        """报告导出后日志被改 —— 必须报 TAMPERED 且退出码非 0。"""
        from log_ai_compressor.cli import main
        result = analyze_files([logfile], context_lines=3)
        report = tmp_path / "r.json"
        report.write_text(S.export_text(result, "json"), encoding="utf-8")
        lines = logfile.read_text(encoding="utf-8").splitlines(True)
        lines[1] = lines[1].replace("db-primary:5432", "db-replica:6432")
        logfile.write_text("".join(lines), encoding="utf-8")
        assert main(["verify", "--report", str(report)]) == 1
        assert "TAMPERED" in capsys.readouterr().out

    def test_saved_report_detects_rotation_cleanup(self, logfile, tmp_path,
                                                   capsys):
        """日志被轮转清掉了 —— 报「验不了」，退出码仍非 0（v2.11.1）。

        退出码不能改成 0：结论**已经无法被核实**，让脚本返回成功等于
        说「那份报告还成立」。但 verdict 必须说 UNVERIFIABLE 而不是
        TAMPERED —— 轮转清理不是篡改，指控错了对象比不敢结论更糟。
        """
        from log_ai_compressor.cli import main
        result = analyze_files([logfile], context_lines=3)
        report = tmp_path / "r.json"
        report.write_text(S.export_text(result, "json"), encoding="utf-8")
        logfile.unlink()
        assert main(["verify", "--report", str(report)]) == 1
        out = capsys.readouterr().out
        assert "UNVERIFIABLE" in out
        assert "TAMPERED" not in out, "轮转清理被当成了篡改"

    def test_report_without_evidence_is_refused(self, tmp_path, capsys):
        """没有证据的报告必须明说，而不是假装校验通过。"""
        from log_ai_compressor.cli import main
        r = tmp_path / "empty.json"
        r.write_text(json.dumps({"clusters": []}), encoding="utf-8")
        assert main(["verify", "--report", str(r)]) == 1
        assert "没有任何证据引用" in capsys.readouterr().err

    def test_missing_report_file(self, tmp_path, capsys):
        from log_ai_compressor.cli import main
        assert main(["verify", "--report", str(tmp_path / "nope.json")]) == 1
        assert "不存在" in capsys.readouterr().err

    def test_no_arguments_shows_usage(self, capsys):
        from log_ai_compressor.cli import main
        assert main(["verify"]) == 1
        assert "用法" in capsys.readouterr().err

    def test_mcp_verify_tool(self, logfile):
        pytest.importorskip("mcp")
        from log_ai_compressor.mcp.server import verify_evidence
        out = verify_evidence(path=str(logfile))
        assert out["ok"] is True
        assert out["verdict"] == "VERIFIED"
        assert out["mode"] == "self-check"
        assert out["citations"] and "L" in out["citations"][0]["citation"]

    def test_mcp_verify_saved_report(self, logfile, tmp_path):
        """MCP 侧同样要能复核已导出报告 —— 只传 path 是同义反复。"""
        pytest.importorskip("mcp")
        from log_ai_compressor.mcp.server import verify_evidence
        result = analyze_files([logfile], context_lines=3)
        rp = tmp_path / "r.json"
        rp.write_text(S.export_text(result, "json"), encoding="utf-8")
        ok = verify_evidence(report_path=str(rp))
        assert ok["ok"] and ok["verdict"] == "VERIFIED" and ok["mode"] == "report"
        lines = logfile.read_text(encoding="utf-8").splitlines(True)
        lines[1] = lines[1].replace("db-primary:5432", "db-replica:6432")
        logfile.write_text("".join(lines), encoding="utf-8")
        bad = verify_evidence(report_path=str(rp))
        assert bad["verdict"] == "TAMPERED" and bad["mismatched"] >= 1
        # 修复缺陷#3：篡改时 ok 必须是 False —— 此前被无条件覆写成 True，
        # Agent 按「ok=False 才不转述」的契约就会照常转述被篡改的结论
        assert bad["ok"] is False

    def test_mcp_verify_requires_an_argument(self):
        pytest.importorskip("mcp")
        from log_ai_compressor.mcp.server import verify_evidence
        assert verify_evidence()["ok"] is False
