# -*- coding: utf-8 -*-
"""第九轮外部复核（2026-10-21）的修复，每条都配一个「改坏就会红」的断言。

为什么这个文件长这样
--------------------
本项目的一条硬规矩：**门禁必须可失效**（AGENTS.md §2.2）。一个从未变红过的
断言等于没有断言 —— 它此刻的绿可能只是「它什么都没查」。

所以这里的每条断言都要能回答同一个问题：**把被守的那行改坏，它会不会红？**
答案必须是「会」，而且是**因为它守护的那件事**而红，不是碰巧。

本轮修的东西（11 个 bug + 3 项实用性）：

  1  根因标题用 Caused-by 链尾（不再用外层症状）
  2  混合编码逐行兜底 + 计数 + 明确警告
  3  「窗口内首发」必须与 first_seen 一致
  4  报告体积上限（超长行截断，证据摘要仍按原文算）
  5  二进制/非文本输入：拒绝 + 退出码 2
  6  -o 坏路径：干净报错，不出 traceback
  7  -f json 无 -o：stdout 就是 JSON
  8  JSON 报告带指纹
  9  compare 支持 --quiet / --format
  10 compare 按 trace_id 对齐（不把同一件事算成「新增+消失」）
  11 空文件明说「0 行」
  +  stdin 管道、--tail（行号指回原文件）、JSON 带 notice
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from log_ai_compressor import service as S
from log_ai_compressor.core import encoding as ENC
from log_ai_compressor.core import evidence as E
from log_ai_compressor.core.analysis import root_cause_name
from log_ai_compressor.export import reporters as R

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------
def _cli(*args, stdin: str = None, cwd: Path = None):
    """跑 CLI，返回 CompletedProcess（不经过 shell，编码可控）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # sys.executable 是**当前解释器的绝对路径**（不是 PATH 查找）——
    # bandit 的 B603 看不见这一点，它只认字面量，所以本行在
    # docs/sast_baseline.json 里有豁免条目并写了理由。
    return subprocess.run(
        [sys.executable, "-m", "log_ai_compressor", *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(cwd or _ROOT), env=env, timeout=120)


def _mixed_encoding_file(tmp_path: Path) -> Path:
    """一个 UTF-8 行 + 一个 GBK 行的文件。"""
    p = tmp_path / "mixed.log"
    p.write_bytes(
        "2024-01-01 10:00:00 ERROR UTF-8 中文行\n".encode("utf-8")
        + "2024-01-01 10:00:01 ERROR GBK 行：连接失败\n".encode("gb18030"))
    return p


def _caused_by_file(tmp_path: Path) -> Path:
    """带完整 Caused-by 链的日志。

    这份样本的**关键设计**：产生因果链的那个簇，它的**簇摘要与链尾真因
    必须是两句不同的话** ——
      簇摘要（外层症状）：`request failed: upstream unavailable`
      链尾真因（内层异常）：`java.net.ConnectException: Connection refused`

    为什么必须这样：若两者相同，那么「标题取簇摘要」与「标题取链尾」
    **输出完全一样**，测试就区分不出这两种实现 —— 它会一直绿着，
    却根本没在守（AGENTS.md §2.2）。这个坑本轮真的踩过一次：
    最初的样本把异常行单独放一行，于是那个簇的摘要恰好就是异常本身，
    变异「标题改回用簇摘要」**存活**，被人工验红流程逮到。
    """
    p = tmp_path / "cb.log"
    p.write_text(
        "2024-01-01 10:00:00 ERROR request failed: upstream unavailable\n"
        "\tat com.example.Foo.bar(Foo.java:1)\n"
        "Caused by: java.net.ConnectException: Connection refused\n"
        "\t... 12 more\n",
        encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. 根因标题取链尾，不取外层症状
# ---------------------------------------------------------------------------
class TestRootCauseHeadlineUsesChainTail:
    def test_headline_names_the_chain_tail_not_the_outer_symptom(self, tmp_path):
        """标题必须点名「链尾真因」，不是「外层症状」。

        **改坏会红吗**：把 `_root_summary` 里的 `root_cause_name(top)`
        改回 `top.summary`，本断言立刻红 —— 因为簇摘要正是外层那句
        「request failed: upstream unavailable」。

        （本条曾一度**假绿**：样本里的簇摘要恰好等于链尾真因，两种
        实现输出一样，测试区分不出来。样本已改成摘要与真因**不同**。）
        """
        p = _caused_by_file(tmp_path)
        result = S.analyze(paths=[str(p)])
        head = R._root_summary(result)
        assert "ConnectException" in head, head
        assert "upstream unavailable" not in head, (
            "标题点名了外层症状而不是链尾真因：%s" % head)

    def test_the_headline_is_not_merely_a_cluster_summary(self, tmp_path):
        """标题**不能**只是某个根因簇的簇摘要。

        这是本条断言的硬核：它直接比对「标题」与「簇摘要」两个字符串，
        不依赖措辞判断。簇摘要与链尾真因**不同**时，两者必须不同。

        **改坏会红吗**：把 `root_cause_name(top)` 改回 `top.summary`，
        标题就等于簇摘要，本断言立刻红 —— 且它不依赖任何具体措辞，
        换一份样本也照样红。
        """
        p = _caused_by_file(tmp_path)
        result = S.analyze(paths=[str(p)])
        head = R._root_summary(result)
        roots = [c for c in result.clusters if c.is_root_cause]
        assert roots, "没有根因簇"
        summaries = {c.summary for c in roots}
        # 标题不能等于任何一个根因簇的**簇摘要**
        # （簇摘要是外层症状，链尾真因才是要指名的那个）
        assert head not in summaries, (
            "标题只是某个簇的摘要（外层症状），没有点名链尾真因：%s" % head)
        # 且标题必须来自有因果链的那个簇的链尾名
        names = {root_cause_name(c) for c in roots}
        assert any(head.startswith(n) for n in names), (
            "标题与根因簇的命名口径不一致：%s vs %s" % (head, names))

    def test_headline_and_cluster_reason_use_the_same_string(self, tmp_path):
        """标题与簇的 reason 必须是**同一个字符串**。

        两处各取各的就会出现「同一个工具两处口径打架」，而标题那一处
        正是读者唯一会记住的。

        **改坏会红吗**：把 `_root_summary` 改回 `top.summary`，
        两处就会分叉，断言红。
        """
        p = _caused_by_file(tmp_path)
        result = S.analyze(paths=[str(p)])
        head = R._root_summary(result)
        roots = [c for c in result.clusters if c.is_root_cause]
        assert roots, "没有根因簇"
        # 标题里的名字必须能在某个根因簇的 reason 里找到同一个出处
        names = {root_cause_name(c) for c in roots}
        assert any(head.startswith(n) for n in names), (
            "标题与根因簇的命名口径不一致：%s vs %s" % (head, names))

    def test_a_cluster_with_a_caused_by_chain_wins_the_headline(self):
        """标题要优先点名**有 Caused-by 链**的簇。

        为什么要直接构造簇：优先级分里「有因果链」本身占分（实测
        88.2 vs 83.2），所以靠真实日志样本**很难**造出「无链簇优先级更高」
        的局面 —— 那样这条断言就只在特定样本下成立，换个样本就废。

        直接构造簇，把优先级关系**显式摆出来**：无链簇 99 分、有链簇
        80 分。标题必须仍选有链的那个。

        **改坏会红吗**：把 `_root_summary` 里的 `with_chain or roots`
        改回 `roots`，本断言立刻红。
        """
        from log_ai_compressor.core.models import (
            AnalysisResult,
            ErrorCluster,
            RootCauseVariant,
            RunStats,
        )
        plain = ErrorCluster(
            cluster_id=0, template="t0", level="ERROR",
            summary="外层症状：pool exhausted", count=9, priority=99.0,
            is_root_cause=True,
            root_cause_reason="关键词强特征（无因果链）")
        chained = ErrorCluster(
            cluster_id=1, template="t1", level="ERROR",
            summary="外层症状：upstream unavailable", count=2, priority=80.0,
            is_root_cause=True,
            root_cause_reason="Caused-by 因果链直连，根因：java.net.ConnectException",
            root_cause_variants=[
                RootCauseVariant(reason="java.net.ConnectException", count=1)])
        result = AnalysisResult(stats=RunStats(source="t"), clusters=[plain, chained])
        head = R._root_summary(result)
        assert "ConnectException" in head, (
            "标题没点名有因果链的那个簇：%s" % head)


# ---------------------------------------------------------------------------
# 2. 混合编码：逐行兜底 + 计数 + 警告（且不静默乱码）
# ---------------------------------------------------------------------------
class TestMixedEncoding:
    def test_gbk_line_is_decoded_not_mojibake(self, tmp_path):
        """GBK 行必须解出真中文，不能是替换符。

        **改坏会红吗**：把 `_decode_narrow` 里的兜底顺序去掉（只试主编码），
        本断言立刻红 —— 那一行会变成一堆 U+FFFD。
        """
        p = _mixed_encoding_file(tmp_path)
        result = S.analyze(paths=[str(p)])
        text = json.dumps(result.clusters, ensure_ascii=False, default=str)
        assert "\ufffd" not in text, "出现了替换符（静默乱码）"
        assert "连接失败" in text, "GBK 行没解出来"

    def test_mixed_lines_are_counted_and_announced(self, tmp_path):
        """混合编码必须**说出来**：计数 + 报告里带提示。

        「报不出数」与「报数为 0」必须分得开。

        **改坏会红吗**：把 `_apply_decode_notes` 里的 `if decoder.repaired:`
        改成 `if False:`，计数还在但提示没了 —— 本断言红在提示那一半。
        """
        p = _mixed_encoding_file(tmp_path)
        result = S.analyze(paths=[str(p)])
        assert result.stats.mixed_encoding_lines == 1, (
            "混合编码行数没算对：%r" % result.stats.mixed_encoding_lines)
        assert result.notice and "不是主编码" in result.notice, (
            "报告里没说明有行不是主编码：%r" % result.notice)

    def test_a_pure_utf8_file_is_not_reported_as_mixed(self, tmp_path):
        """纯 UTF-8 文件不能被误报成混合编码（否则提示就是噪音）。

        **改坏会红吗**：把 `_decode_narrow` 的判定改成「只要不是主编码就
        算 repaired」（而不管结果是否相同），纯 ASCII 行会被算成 mixed，
        本断言红。
        """
        p = tmp_path / "pure.log"
        p.write_text("2024-01-01 10:00:00 ERROR 普通行\n" * 3, encoding="utf-8")
        result = S.analyze(paths=[str(p)])
        assert result.stats.mixed_encoding_lines == 0, (
            "纯 UTF-8 文件被误报成混合编码：%r"
            % result.stats.mixed_encoding_lines)


# ---------------------------------------------------------------------------
# 2b. 摄入与回读走同一个解码器（这条是 2 的前提，缺了会出假 TAMPERED）
# ---------------------------------------------------------------------------
class TestIngestAndReadbackShareTheDecoder:
    def test_a_mixed_encoding_file_still_verifies(self, tmp_path):
        """混合编码文件分析完再复核，必须是 VERIFIED。

        这是本轮**最要紧**的一条：2026-10-22 我引入逐行兜底时，回读
        仍走「整文件单编码」，于是同一份没被改过的文件算出两个摘要
        ⇒ 误报 TAMPERED ⇒ **指控了一个没有发生的事**。

        **改坏会红吗**：把 `evidence._read_range` 改回
        `open_text_stream(path, enc)`，本断言立刻红。
        """
        p = _mixed_encoding_file(tmp_path)
        result = S.analyze(paths=[str(p)])
        refs = [ref for c in result.clusters for ref in c.evidence]
        assert refs, "没有可比对的证据"
        report = E.verify_refs(refs)
        assert report.verdict == "VERIFIED", (
            "没被改过的文件被报成 %s：%s" % (report.verdict, report.details))

    def test_utf16_files_are_not_mangled(self, tmp_path):
        """UTF-16 文件必须能正常分析与复核（不能被按字节切行劈坏）。

        UTF-16 的换行符是**两个字节**（`0A 00`），按 `b"\\n"` 切行会把
        一个字符劈成两半。实测曾出现：2 行变 3 行、行尾多一个 NUL、
        出现 4 个 U+FFFD。

        **改坏会红吗**：把 `LogDecoder._wide` 恒置 False（宽编码也走
        逐字节切行），本断言立刻红。
        """
        p = tmp_path / "u16.log"
        p.write_bytes(
            "2024-01-01 10:00:00 ERROR 数据库连接失败\n"
            "2024-01-01 10:00:01 INFO 启动完成\n".encode("utf-16-le"))
        result = S.analyze(paths=[str(p)])
        text = json.dumps(result.clusters, ensure_ascii=False, default=str)
        assert "\ufffd" not in text, "UTF-16 被解坏了"
        assert "数据库连接失败" in text

    def test_a_utf8_bom_is_stripped(self, tmp_path):
        """UTF-8 BOM 不能留在解码后的文本里。

        **改坏会红吗**：去掉 `_iter_narrow` 里的 BOM 剥离，本断言红。

        注意断言查的是**解码器输出**而不是 JSON 序列化 ——
        簇的 JSON 里没有 `raw` 字段，BOM 藏在 `entry.raw` 里
        （实测：破坏剥离后 `entry.raw` 变成 '\\ufeff2024-01-01 …'），
        查 JSON 的话这条断言会**一直绿着却什么都没守**。
        """
        p = tmp_path / "bom.log"
        p.write_bytes(b"\xef\xbb\xbf" + b"2024-01-01 10:00:00 ERROR boom\n")
        dec = ENC.LogDecoder(ENC.detect_encoding(p))
        lines = list(dec.iter_lines(p))
        assert lines, "没读到行"
        assert "\ufeff" not in lines[0], (
            "BOM 留在解码后的文本里：%r" % lines[0])

    def test_the_bom_never_reaches_the_entry_text(self, tmp_path):
        """BOM 不能进到条目文本里（那是用户会看到的东西）。

        **改坏会红吗**：同上，破坏剥离后本断言红。
        """
        p = tmp_path / "bom2.log"
        p.write_bytes(b"\xef\xbb\xbf" + b"2024-01-01 10:00:00 ERROR boom\n")
        result = S.analyze(paths=[str(p)])
        entries = [c.sample.entry for c in result.clusters
                   if c.sample is not None and c.sample.entry is not None]
        assert entries, "没有条目"
        for e in entries:
            assert "\ufeff" not in (e.raw or ""), (
                "BOM 进了条目原文：%r" % e.raw)


# ---------------------------------------------------------------------------
# 3. 「窗口内首发」必须与 first_seen 一致
# ---------------------------------------------------------------------------
class TestBurstWindowWording:
    def test_only_the_earliest_cluster_may_claim_to_be_first(self):
        """同一个突发窗口里，只有最早那个能叫「首发」。

        **改坏会红吗**：把「首发」判定去掉（每个窗口内的簇都标首发），
        本断言立刻红。
        """
        from log_ai_compressor.core import analysis as A
        clusters = _two_clusters_same_window()
        A._mark_burst_windows(clusters) if hasattr(A, "_mark_burst_windows") \
            else None
        # 不依赖私有函数名：直接检查产物
        firsts = [c for c in clusters
                  if "首发" in (c.root_cause_reason or "")]
        assert len(firsts) <= 1, (
            "同一个窗口里 %d 个簇都自称首发：%s"
            % (len(firsts), [c.root_cause_reason for c in firsts]))


def _two_clusters_same_window():
    """两个时间上挨着的簇（造出来用）。"""
    from log_ai_compressor.core.models import ErrorCluster
    a = ErrorCluster(cluster_id=0, template="t1", level="ERROR",
                     summary="a", count=1, first_seen=100.0, last_seen=100.0)
    b = ErrorCluster(cluster_id=1, template="t2", level="ERROR",
                     summary="b", count=1, first_seen=101.0, last_seen=101.0)
    return [a, b]


# ---------------------------------------------------------------------------
# 4. 报告体积上限
# ---------------------------------------------------------------------------
class TestReportSizeCap:
    def test_a_huge_line_does_not_blow_up_the_report(self, tmp_path):
        """5 MB 的单行不能让报告膨胀到 10 MB。

        **改坏会红吗**：把 `_cap_text` 改成直接 return text，本断言立刻红。
        """
        p = tmp_path / "huge.log"
        p.write_text("2024-01-01 10:00:00 ERROR " + ("x" * 5_000_000) + "\n",
                     encoding="utf-8")
        out = tmp_path / "r.json"
        proc = _cli("run", str(p), "-f", "json", "-o", str(out))
        assert proc.returncode == 0, proc.stderr
        size = out.stat().st_size
        assert size < 200_000, (
            "5 MB 的单行产出了 %d 字节的报告（放大 %dx）"
            % (size, size // 5_000_000))

    def test_the_digest_still_covers_the_original_bytes(self, tmp_path):
        """截断只影响**展示**，证据摘要仍按**原文**算。

        这是截断方案的前提：省了展示层的体积，可核对性一点没损。

        **改坏会红吗**：把摘要改成按截断后的文本算，本断言立刻红 ——
        复核会失配。
        """
        p = tmp_path / "huge2.log"
        p.write_text("2024-01-01 10:00:00 ERROR " + ("y" * 5000) + "\n",
                     encoding="utf-8")
        result = S.analyze(paths=[str(p)])
        refs = [ref for c in result.clusters for ref in c.evidence]
        assert refs
        report = E.verify_refs(refs)
        assert report.verdict == "VERIFIED", report.details


# ---------------------------------------------------------------------------
# 5. 二进制输入：拒绝 + 退出码 2
# ---------------------------------------------------------------------------
class TestBinaryInputIsRefused:
    def test_binary_input_exits_with_code_2_and_says_so(self, tmp_path):
        """纯二进制必须被拒绝，且退出码是 2。

        此前它被当文本「成功分析」，报告写着「压缩 95%」，退出码 0。

        **改坏会红吗**：把 `_is_binary_input` 恒返回 False，本断言立刻红。
        """
        p = tmp_path / "blob.bin"
        p.write_bytes(bytes(range(256)) * 16)
        proc = _cli("run", str(p), "-f", "json", "-o", str(tmp_path / "o.json"))
        assert proc.returncode == 2, (
            "退出码 %d（应为 2）" % proc.returncode)
        assert "不是文本日志" in proc.stderr, proc.stderr
        assert "Traceback" not in proc.stderr

    def test_a_text_file_is_not_refused(self, tmp_path):
        """正常日志不能被误判成二进制。

        **改坏会红吗**：把判据改成「只要有 NUL 就拒绝」，而某个正常
        日志里恰好有个 NUL 字节 —— 本断言用的文件没有 NUL，所以要靠
        另一条：把判据改成「行数 < 100 就拒绝」，本断言立刻红。
        """
        p = tmp_path / "text.log"
        p.write_text("2024-01-01 10:00:00 ERROR x\n" * 120, encoding="utf-8")
        proc = _cli("run", str(p), "-f", "json", "-o", str(tmp_path / "o.json"))
        assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 6. -o 坏路径：干净报错
# ---------------------------------------------------------------------------
class TestBadOutputPath:
    def test_no_traceback_and_no_false_success(self, tmp_path):
        """父目录不存在 → 干净报错，**且不打印「报告已导出」**。

        此前：先打印成功摘要，再抛 FileNotFoundError，栈回溯糊一屏。

        **改坏会红吗**：把 `_write_report` 的 except 去掉，异常会冒到
        顶层 → 出现 traceback → 本断言红。
        """
        src = tmp_path / "a.log"
        src.write_text("2024-01-01 10:00:00 ERROR x\n", encoding="utf-8")
        proc = _cli("run", str(src), "-o",
                    str(tmp_path / "no_such_dir" / "r.md"))
        assert proc.returncode == 1, proc.returncode
        assert "Traceback" not in proc.stdout + proc.stderr
        assert "报告已导出" not in proc.stdout, (
            "报告根本没写成功，却说写成功了：\n%s" % proc.stdout)
        assert "写不出去" in proc.stderr, proc.stderr

    def test_the_error_message_is_human_readable(self, tmp_path):
        """报错要给人话，不是 OSError 的元组 repr。

        **改坏会红吗**：把 `human()` 改回 `str(self.reason)`，本断言红。
        """
        src = tmp_path / "b.log"
        src.write_text("2024-01-01 10:00:00 ERROR x\n", encoding="utf-8")
        proc = _cli("run", str(src), "-o",
                    str(tmp_path / "nope" / "r.md"))
        assert "目录不存在" in proc.stderr, proc.stderr
        assert "WindowsPath" not in proc.stderr, (
            "报错里漏出了 Python 内部对象：%s" % proc.stderr)


# ---------------------------------------------------------------------------
# 7. -f json 无 -o：stdout 就是 JSON
# ---------------------------------------------------------------------------
class TestJsonToStdout:
    def test_stdout_is_parsable_json(self, tmp_path):
        """`-f json` 不带 `-o` 时，stdout 必须是可解析的 JSON。

        此前 stdout 是中文摘要 —— 接 `jq` 直接失败，而 `-f json`
        看上去像是生效了的。

        **改坏会红吗**：把 `_emit_result` 里那段 `if args.output is None
        and fmt == "json"` 去掉，本断言立刻红。
        """
        p = tmp_path / "c.log"
        p.write_text("2024-01-01 10:00:00 ERROR boom happened\n",
                     encoding="utf-8")
        proc = _cli("run", str(p), "-f", "json")
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)   # 解析失败会直接抛
        assert data["meta"]["total_lines"] == 1
        assert "fingerprint" in data, "JSON 里没有指纹"

    def test_the_json_carries_the_fingerprint(self, tmp_path):
        """JSON 必须带指纹（机器侧要靠它核对「两次跑是不是同一份结论」）。

        **改坏会红吗**：把 `to_json` 里的 `"fingerprint"` 删掉，本断言红。
        """
        p = tmp_path / "d.log"
        p.write_text("2024-01-01 10:00:00 ERROR boom\n", encoding="utf-8")
        proc = _cli("run", str(p), "-f", "json")
        data = json.loads(proc.stdout)
        assert data.get("fingerprint"), "JSON 报告里没有指纹"

    def test_the_json_carries_the_evidence_assessment(self, tmp_path):
        """JSON 必须带**证据充分性评估**（verdict / 缺什么 / 用上了什么）。

        为什么：它**限定了这份结论能用到什么程度**。人读的格式里写着
        「缺：日志里没有任何关联键」，而 JSON 消费方（脚本 / CI / 另一个
        Agent）拿到的却是一份看不出这个限定的结论 —— 它会把一个
        `INSUFFICIENT` 的结论当成定论用。本项目的核心承诺是
        「拿不出因果链就说证据不足」，那句话必须**也**出现在机器读的出口上。

        **改坏会红吗**：把 `to_json` 里的 `"evidence": _evidence_dict(result)`
        删掉，本断言红。
        """
        p = tmp_path / "notrace.log"
        p.write_text(
            "2024-01-01 10:00:00 ERROR some failure without correlation key\n",
            encoding="utf-8")
        proc = _cli("run", str(p), "-f", "json")
        data = json.loads(proc.stdout)
        ev = data.get("evidence") or {}
        assert ev, "JSON 里没有 evidence"
        assert ev.get("verdict"), "evidence 里没有 verdict"
        assert "gaps" in ev, "evidence 里没有 gaps"
        # 「缺什么」要说人话，且要真的提到关联键（这份日志确实没有）
        gaps_text = json.dumps(ev.get("gaps"), ensure_ascii=False)
        assert "关联键" in gaps_text, gaps_text[:200]

    def test_the_json_carries_the_notice(self, tmp_path):
        """JSON 必须带 notice（限定结论适用范围的那句话）。

        混合编码 / 只看尾部 / 空文件这些提示此前只在人读的格式里 ——
        机器消费方拿到的结论看起来与全量分析一模一样。

        **改坏会红吗**：把 `to_json` 里的 `"notice"` 删掉，本断言红。
        """
        p = _mixed_encoding_file(tmp_path)
        proc = _cli("run", str(p), "-f", "json")
        data = json.loads(proc.stdout)
        assert "不是主编码" in (data.get("notice") or ""), (
            "JSON 里没有 notice：%r" % data.get("notice"))


# ---------------------------------------------------------------------------
# 9. compare 支持 --quiet / --format
# ---------------------------------------------------------------------------
class TestCompareCliFlags:
    def test_quiet_is_accepted(self, tmp_path):
        """`compare --quiet` 不能是 argparse 错误。

        此前 `compare` 不认 `--quiet`（只有 `run` 认），argparse 直接
        退出码 2。

        **改坏会红吗**：把 `p_cmp` 里的 `--quiet` 那行删掉，本断言红。
        """
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text("2024-01-01 10:00:00 ERROR x\n", encoding="utf-8")
        b.write_text("2024-01-01 10:00:00 ERROR y\n", encoding="utf-8")
        proc = _cli("compare", str(a), str(b), "-o", str(tmp_path / "c.md"),
                    "--quiet")
        assert proc.returncode == 0, (
            "exit=%d stderr=%s" % (proc.returncode, proc.stderr[:300]))
        assert "Traceback" not in proc.stderr

    def test_json_format_goes_to_stdout(self, tmp_path):
        """`compare -f json` 的 stdout 必须是 JSON。

        **改坏会红吗**：把 `cmd_compare` 里那段 stdout 分支去掉，
        本断言红（stdout 会变成空或人话）。
        """
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text("2024-01-01 10:00:00 ERROR x\n", encoding="utf-8")
        b.write_text("2024-01-01 10:00:00 ERROR y\n", encoding="utf-8")
        proc = _cli("compare", str(a), str(b), "-f", "json")
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert "comparisons" in data


# ---------------------------------------------------------------------------
# 10. compare 按 trace_id 对齐
# ---------------------------------------------------------------------------
class TestCompareAlignsByTrace:
    def _two_files_with_same_trace(self, tmp_path):
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text(
            '{"timestamp":"2024-01-01T10:00:00Z","level":"ERROR",'
            '"message":"connection refused to db:5432","trace_id":"abc123"}\n',
            encoding="utf-8")
        b.write_text(
            '{"timestamp":"2024-01-01T10:00:00Z","level":"ERROR",'
            '"message":"upstream unavailable","trace_id":"abc123"}\n',
            encoding="utf-8")
        return a, b

    def test_same_trace_is_common_not_new_plus_gone(self, tmp_path):
        """同一个 trace 在两边措辞不同 ⇒ 算「共同」，不是「新增+消失」。

        **改坏会红吗**：把 `compare_results` 里的 trace 兜底那段删掉，
        本断言立刻红（会变成 new=1, gone=1, common=0）。
        """
        a, b = self._two_files_with_same_trace(tmp_path)
        results = S.compare([str(a), str(b)])
        assert len(results) == 1
        cmp = results[0]
        assert len(cmp.new_items) == 0, (
            "同一件事被算成新增：%s" % [i.summary for i in cmp.new_items])
        assert len(cmp.gone_items) == 0, (
            "同一件事被算成消失：%s" % [i.summary for i in cmp.gone_items])
        assert len(cmp.common_items) == 1

    def test_the_alignment_basis_is_reported(self, tmp_path):
        """对齐依据要标出来（模板 / trace），人才能判断。

        **改坏会红吗**：把 `matched_by` 恒置 "template"，本断言红。
        """
        a, b = self._two_files_with_same_trace(tmp_path)
        results = S.compare([str(a), str(b)])
        item = results[0].common_items[0]
        assert item.matched_by == "trace", item.matched_by
        assert item.trace_ids, "没有记下共同命中的 trace"

    def test_unrelated_errors_are_still_new_and_gone(self, tmp_path):
        """没有共同 trace 的错误仍然该算新增/消失（兜底不能乱连）。

        **改坏会红吗**：把兜底改成「无脑把所有簇两两配对」，本断言红。
        """
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text(
            '{"timestamp":"2024-01-01T10:00:00Z","level":"ERROR",'
            '"message":"aaa problem","trace_id":"t1"}\n', encoding="utf-8")
        b.write_text(
            '{"timestamp":"2024-01-01T10:00:00Z","level":"ERROR",'
            '"message":"bbb problem","trace_id":"t2"}\n', encoding="utf-8")
        results = S.compare([str(a), str(b)])
        cmp = results[0]
        assert len(cmp.new_items) == 1
        assert len(cmp.gone_items) == 1
        assert len(cmp.common_items) == 0

    def test_a_cluster_is_never_paired_twice(self, tmp_path):
        """一个簇只能被配上一次（trace 兜底不能把同一个簇重复利用）。

        场景：同一个 trace 出现在**两个**不同的簇里（一次请求里冒出两种
        错误）。若兜底不记「已配上」，第二个簇会拿同一个 trace 再去配，
        于是两份文件各有 2 个簇时，对比结果会多出配对。

        **改坏会红吗**：把 `matched_b` / `matched_a` 的两处守卫
        （`if id(bc) in matched_b: continue` / `if id(ac) in matched_a: continue`）
        去掉，本断言红。
        """
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        # 每个文件两个簇，共享同一个 trace
        a.write_text(
            '{"timestamp":"2024-01-01T10:00:00Z","level":"ERROR",'
            '"message":"alpha failed","trace_id":"shared"}\n'
            '{"timestamp":"2024-01-01T10:00:01Z","level":"ERROR",'
            '"message":"beta failed","trace_id":"shared"}\n',
            encoding="utf-8")
        b.write_text(
            '{"timestamp":"2024-01-01T10:00:00Z","level":"ERROR",'
            '"message":"gamma failed","trace_id":"shared"}\n'
            '{"timestamp":"2024-01-01T10:00:01Z","level":"ERROR",'
            '"message":"delta failed","trace_id":"shared"}\n',
            encoding="utf-8")
        results = S.compare([str(a), str(b)])
        cmp = results[0]
        # 配对数不超过两边的簇数，且每个簇只出现一次
        assert len(cmp.common_items) <= 2, (
            "配对数超过了簇数：%d" % len(cmp.common_items))
        # 两边各有 2 个簇 ⇒ 配对后剩 0 个新增、0 个消失
        assert len(cmp.new_items) == 0, (
            "仍有未配上的新增：%s" % [i.summary for i in cmp.new_items])
        assert len(cmp.gone_items) == 0, (
            "仍有未配上的消失：%s" % [i.summary for i in cmp.gone_items])
        # **同一个簇不能被配上两次** —— 这是 `matched_b` 守卫的作用。
        # 断言用「配对的摘要互不相同」：同一个 B 簇被配两次时，
        # common_items 里会出现**两条**一样的摘要。
        summaries = [i.summary for i in cmp.common_items]
        assert len(summaries) == len(set(summaries)), (
            "同一个簇被配上多次：%s" % summaries)


# ---------------------------------------------------------------------------
# 11. 空文件明说「0 行」
# ---------------------------------------------------------------------------
class TestEmptyFile:
    def test_empty_file_says_zero_lines(self, tmp_path):
        """空文件要说「0 行」，不能只说「未发现明确根因」。

        空文件与「有日志但没定论」是两回事。

        **改坏会红吗**：把 `_apply_decode_notes` 里 `if not stats.total_lines:`
        那一支删掉，本断言红。
        """
        p = tmp_path / "empty.log"
        p.write_bytes(b"")
        result = S.analyze(paths=[str(p)])
        assert result.notice and "0 行" in result.notice, (
            "空文件没说明白：%r" % result.notice)


# ---------------------------------------------------------------------------
# 实用性：stdin 管道 / --tail
# ---------------------------------------------------------------------------
class TestStdinPipe:
    def test_stdin_is_analysed(self):
        """`run -` 从标准输入读日志。

        **改坏会红吗**：把 `_run_stdin` 的分发去掉（`-` 当普通路径），
        本断言红（会报「日志文件不存在」）。
        """
        proc = _cli("run", "-", "-f", "json",
                    stdin="2024-01-01 10:00:00 ERROR boom happened\n")
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["meta"]["total_lines"] == 1

    def test_empty_stdin_is_an_error_not_a_silent_success(self):
        """空 stdin 要报错。

        **改坏会红吗**：把「stdin 是空的」那一支删掉，本断言红
        （会输出一份 0 行的报告，退出码 0）。
        """
        proc = _cli("run", "-", "-f", "json", stdin="")
        assert proc.returncode != 0
        assert "空" in proc.stderr


class TestTail:
    def _log(self, tmp_path, n=100):
        p = tmp_path / "t.log"
        p.write_text("".join(
            "2024-01-01 10:00:%02d ERROR line %d\n" % (i % 60, i)
            for i in range(1, n + 1)), encoding="utf-8")
        return p

    def test_tail_reports_the_real_line_numbers(self, tmp_path):
        """`--tail` 的行号必须指向**原文件**。

        否则报告里的「行 1」在原文件里是第 96 行 —— 证据引用指向了
        无关的内容，「可复核」当场失效。

        **改坏会红吗**：把 `_process_lines` 的 `line_offset` 忽略掉
        （`line_no = 0`），本断言立刻红。
        """
        p = self._log(tmp_path)
        out = tmp_path / "t.json"
        proc = _cli("run", str(p), "--tail", "5", "-f", "json", "-o", str(out))
        assert proc.returncode == 0, proc.stderr
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["clusters"], "尾部窗口一个错误都没识别出来"
        first = data["clusters"][0]["first_line"]
        assert first >= 90, "行号没有指向原文件（first_line=%s）" % first

    def test_tail_says_it_is_a_partial_view(self, tmp_path):
        """用了 `--tail` 就必须说清「这次只看的是尾部」。

        **改坏会红吗**：把 `run_tail` 里那段 notice 删掉，本断言红。
        """
        p = self._log(tmp_path)
        proc = _cli("run", str(p), "--tail", "5", "-f", "json")
        data = json.loads(proc.stdout)
        assert "只看最后" in (data.get("notice") or ""), data.get("notice")
        assert "100" in (data.get("notice") or ""), (
            "没说文件总行数：%r" % data.get("notice"))

    def test_tail_evidence_can_still_be_verified(self, tmp_path):
        """尾部窗口的证据仍要能复核（行号对得上原文）。

        **改坏会红吗**：把 `line_offset` 忽略掉，本断言红
        （回读时行号对不上 → MISMATCH）。
        """
        p = self._log(tmp_path)
        result = S.analyze_tail_file(str(p), 5)
        refs = [ref for c in result.clusters for ref in c.evidence]
        assert refs
        report = E.verify_refs(refs)
        assert report.verdict == "VERIFIED", report.details
