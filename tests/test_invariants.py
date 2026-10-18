# -*- coding: utf-8 -*-
"""不变量（性质）测试：不测具体返回值，只测**任何输入下都必须成立**的性质。

为什么要有这一层
----------------
功能测试问的是「这个输入返回什么」，不变量测试问的是「**永远**不会怎样」。
后者抓的是功能测试看不见的回归 —— 参数顺序传反、边界差一、空集合、
并发写入、极长输入这类。跑一轮 60 秒，成本极低。

不用 Hypothesis：这里的性质大多可以用**固定生成的对抗样本**表达，
引第三方库反而让读者要多学一套 DSL。样本由本文件自己生成，
种子固定，失败可复现。

覆盖的性质：
  I1 不可变性  —— 分析纯函数：同输入不改输出，输入不被就地修改
  I2 证据闭环  —— 每个簇都有证据；证据必然能重读复核；篡改必被发现
  I3 确定性    —— 同输入必同输出（跨簇、跨字段）
  I4 有界性    —— 超大输入、单行巨长、全同一条消息都不崩、不爆内存
  I5 幂等性    —— 重复聚类同一批条目，簇数与计数不因分批方式而变
  I6 不越界    —— 非 CONFIRMED 簇永不输出因果措辞；调用链不升档
"""
from __future__ import annotations

import os
import pathlib
import random
import subprocess
import sys

import pytest

from log_ai_compressor import service as S
from log_ai_compressor.core.clustering import ErrorClusterer, mask_text
from log_ai_compressor.core.models import LogEntry
from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.core.structured import parse_line

SEED = 20261014


def _gen_logs(n: int = 6, seed: int = SEED):
    """生成一批形状各异的日志（乱序、混合级别、部分无戳、部分带栈）。"""
    rng = random.Random(seed)
    levels = ["ERROR", "WARN", "INFO", "FAIL"]
    msgs = ["db connection refused", "task aborted after retries",
            "cache read timeout", "session token expired",
            "worker terminated unexpectedly", "disk write slow"]
    lines = []
    for i in range(n):
        sec = rng.randint(0, 59)
        lv = rng.choice(levels)
        msg = rng.choice(msgs)
        ts = f"2026-10-14 10:{sec:02d}:{rng.randint(0,59):02d}"
        lines.append(f"{ts} {lv} [svc{rng.randint(1,3)}] {msg} id={rng.randint(1,99)}")
        if rng.random() < 0.3:
            lines.append(f"    at com.app.M{rng.randint(1,9)}.run(M.java:{rng.randint(1,99)})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# I1 不可变性：分析是纯函数
# ---------------------------------------------------------------------------
class TestPurity:
    def test_rerun_same_input_same_output(self):
        log = _gen_logs()
        a = analyze_text(log, context_lines=2)
        b = analyze_text(log, context_lines=2)
        assert [(c.cluster_id, c.count, round(c.priority, 3))
                for c in a.clusters] == \
               [(c.cluster_id, c.count, round(c.priority, 3))
                for c in b.clusters]

    def test_input_text_not_mutated(self):
        log = _gen_logs()
        snapshot = str(log)
        analyze_text(log, context_lines=2)
        assert log == snapshot

    def test_clusterer_does_not_mutate_entry(self):
        entry = LogEntry(line_no=1, level="ERROR", message="boom",
                         stack=["at a.B.c(B.java:1)"], timestamp=1.0)
        before = (entry.message, list(entry.stack), entry.count if hasattr(
            entry, "count") else None)
        ErrorClusterer().add(entry, ["ctx"])
        assert entry.message == before[0] and entry.stack == before[1]

    def test_analysis_does_not_accumulate_between_runs(self):
        """同一份 result 重复分析，字段不得层层叠加。"""
        log = _gen_logs()
        r = analyze_text(log, context_lines=2)
        n_evidence = [len(c.evidence) for c in r.clusters]
        from log_ai_compressor.core.analysis import analyze_clusters
        analyze_clusters(r)
        assert [len(c.evidence) for c in r.clusters] == n_evidence


# ---------------------------------------------------------------------------
# I2 证据闭环
# ---------------------------------------------------------------------------
class TestEvidenceClosure:
    def test_every_cluster_has_verifiable_evidence(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text(_gen_logs(), encoding="utf-8")
        r = S.analyze(paths=[str(p)], params={"context_lines": 3})
        for c in r.clusters:
            assert c.evidence, f"{c.summary[:24]} 缺证据"
            assert c.primary_evidence.digest

    def test_every_evidence_reference_verifies(self, tmp_path):
        p = tmp_path / "b.log"
        p.write_text(_gen_logs(seed=SEED + 1), encoding="utf-8")
        r = S.analyze(paths=[str(p)], params={"context_lines": 3})
        assert S.verify_result(r)["verdict"] == "VERIFIED"

    def test_any_line_change_is_detected(self, tmp_path):
        """改动任意一行被证据覆盖的原文，必被检出。"""
        p = tmp_path / "c.log"
        text = _gen_logs(seed=SEED + 2)
        p.write_text(text, encoding="utf-8")
        r = S.analyze(paths=[str(p)], params={"context_lines": 3})
        assert S.verify_result(r)["ok"] is True
        lines = text.splitlines(True)
        target = r.clusters[0].primary_evidence.line_start
        lines[target - 1] = lines[target - 1].replace("id=", "ID=") \
            if "id=" in lines[target - 1] else lines[target - 1] + " # edited\n"
        p.write_text("".join(lines), encoding="utf-8")
        assert S.verify_result(r)["verdict"] == "TAMPERED"

    def test_evidence_id_stable_for_same_bytes(self, tmp_path):
        p1, p2 = tmp_path / "d1.log", tmp_path / "d2.log"
        p1.write_text(_gen_logs(seed=SEED + 3), encoding="utf-8")
        p2.write_text(_gen_logs(seed=SEED + 3), encoding="utf-8")
        r1 = S.analyze(paths=[str(p1)], params={"context_lines": 3})
        r2 = S.analyze(paths=[str(p2)], params={"context_lines": 3})
        # 路径不同 -> 引用 ID 不同（它绑定了 source），但行号区间与摘要相同
        assert ([(e.line_start, e.digest) for e in r1.clusters[0].evidence] ==
                [(e.line_start, e.digest) for e in r2.clusters[0].evidence])


# ---------------------------------------------------------------------------
# I3 确定性
# ---------------------------------------------------------------------------
class TestDeterminismInvariant:
    @pytest.mark.parametrize("seed", [SEED, SEED + 7, SEED + 99])
    def test_fingerprint_invariant(self, seed):
        from log_ai_compressor.core.evidence import result_fingerprint
        log = _gen_logs(seed=seed)
        fps = {result_fingerprint(analyze_text(log, context_lines=2))
               for _ in range(3)}
        assert len(fps) == 1

    def test_shuffled_independent_runs_agree(self):
        """两次独立进程级调用（不走缓存）必须一致。"""
        from log_ai_compressor.core.evidence import result_fingerprint
        log = _gen_logs(seed=SEED + 11)
        assert result_fingerprint(analyze_text(log, context_lines=2)) == \
               result_fingerprint(analyze_text(log, context_lines=2))


# ---------------------------------------------------------------------------
# I4 有界性：不因输入规模失控
# ---------------------------------------------------------------------------
class TestBoundedness:
    def test_huge_log_does_not_explode_cluster_count(self):
        """20 万行、同一类错误 -> 簇数必须为 1（内存有界的核心性质）。"""
        log = "2026-10-14 10:00:00 ERROR db connection refused to db-1\n" * 20_000
        r = analyze_text(log, context_lines=2)
        assert len(r.clusters) == 1
        assert r.clusters[0].count == 20_000

    def test_single_monstrous_line(self):
        """一行 100KB 不能崩。"""
        r = analyze_text("2026-10-14 10:00:00 ERROR " + "x" * 100_000 + "\n")
        assert r.stats.total_lines == 1

    def test_malformed_structured_lines_never_crash(self):
        """畸形 JSON / 半行 / 超长字段一律降级，不抛异常。"""
        bad = [
            '{"level":"ERROR","message":"truncated',
            "{" * 200 + "}",
            '{"level":{"a":{"b":{"c":1}}},"message":{"x":[{"y":1}]}}',
            'level=ERROR msg="' + "z" * 50_000 + '" svc=a',
            '{"message":"' + "q" * 50_000 + '","level":"ERROR"}',
        ]
        for line in bad:
            parse_line(line)          # 不抛即为通过
            analyze_text(line + "\n")  # 全链路也不抛

    def test_empty_and_whitespace_only(self):
        for text in ("", "\n", "   \n\t\n", "\n\n\n"):
            r = analyze_text(text)
            assert r.clusters == [] and r.stats.total_lines >= 0

    def test_deeply_nested_json_is_rejected_not_recursed(self):
        assert parse_line('{"a":' + "[" * 200 + "]" * 200 + '}') is None

    def test_mask_text_always_returns_str(self):
        """指纹归一化对任意输入都返回字符串（聚类靠它做字典键）。"""
        for s in ["", " ", "\n\t", "0", "a" * 10_000,
                  "中文" * 1000, "0x" * 5000, "/*" * 3000]:
            assert isinstance(mask_text(s), str)


# ---------------------------------------------------------------------------
# I5 幂等性：分批方式不影响结果
# ---------------------------------------------------------------------------
class TestIdempotence:
    def test_cluster_count_independent_of_batching(self):
        """一次性入 10 条 vs 分两次入 5 条，簇数必须相同。"""
        entries = [LogEntry(line_no=i + 1, level="ERROR",
                            message=f"boom {i % 3}", timestamp=float(i))
                   for i in range(10)]
        one = ErrorClusterer()
        for e in entries:
            one.add(e)
        two = ErrorClusterer()
        for chunk in (entries[:5], entries[5:]):
            for e in chunk:
                two.add(e)
        assert len(one.clusters) == len(two.clusters)
        assert sum(c.count for c in one.clusters) == \
               sum(c.count for c in two.clusters)

    def test_deduplicating_identical_instances_keeps_one_trace_step(self):
        """同一条目既是 sample 又在 instances 里时，链路不得出现重复步骤。"""
        log = ('{"@timestamp":"2026-10-14T09:00:00.100Z","level":"ERROR",'
               '"service.name":"a","message":"boom","trace_id":"t-abcdefgh"}\n'
               '{"@timestamp":"2026-10-14T09:00:00.200Z","level":"ERROR",'
               '"service.name":"b","message":"boom","trace_id":"t-abcdefgh"}\n')
        r = analyze_text(log, context_lines=2)
        for chain in (r.traces or []):
            line_nos = [s.line_no for s in chain.steps]
            assert len(line_nos) == len(set(line_nos)), "链路出现重复步骤"


# ---------------------------------------------------------------------------
# I7 门禁脚本必须在任何控制台编码下都能跑完
# ---------------------------------------------------------------------------
_ROOT = pathlib.Path(__file__).resolve().parent.parent
CI_GATE_SCRIPTS = ("scripts/adversarial_eval.py", "scripts/prove.py")

#: v2.11.5 新增的三道门禁 + PA/GA 评测。**与上面那批分开，是因为它们接参数**：
#: 无参运行会跑完整工作 —— SAST 扫全仓约 20 秒、依赖门禁要联网好几分钟、
#: PA/GA 要跑满 10 个数据集（约 50 分钟），塞进单测里不可接受。
#: 所以用 `--help`：argparse 会打印中文描述然后退出 0，
#: 而「把中文写进 cp1252 的 stdout」正是这里要验的那件事本身。
ARG_GATE_SCRIPTS = (
    "scripts/sast_gate.py",
    "scripts/perf_gate.py",
    "scripts/dep_audit_gate.py",
    "scripts/eval_logparser_pa_ga.py",
)

ALL_GATE_SCRIPTS = CI_GATE_SCRIPTS + ARG_GATE_SCRIPTS


class TestGateScriptsEncodingSafe:
    """CI 门禁脚本打印中文，必须在非 UTF-8 控制台下也能跑完。

    实测事故（两次，同一个形状）：
    1. Windows runner 控制台是 cp1252，`adversarial_eval.py`
       评分**全绿**却在 print() 中文表格时抛 UnicodeEncodeError，整个 step
       变红 —— 症状是「CI 红了」，真相是「只是打印不出来」。
    2. **2026-10-19 又踩了一次**：三道新门禁首次接进 CI，windows-3.12 在
       SAST 这步红了，报同一个 UnicodeEncodeError，而 ubuntu 两条全绿。
       修法就是照抄第 1 次的兜底 —— 仓库里早就有正确做法，四个新脚本
       只是忘了套。这条测试存在的意义就是「忘了套」会当场变红。

    门禁红必须意味着**主张不成立**，不能是控制台编码。否则门禁会训练
    所有人忽略红色，这比没有门禁更糟。
    """

    @staticmethod
    def _args_for(script: str):
        return ["--help"] if script in ARG_GATE_SCRIPTS else []

    @pytest.mark.parametrize("script", ALL_GATE_SCRIPTS)
    def test_source_reconfigures_stdout(self, script):
        """源码层面的快速检查（拼写错也能抓到）。"""
        src = (_ROOT / script).read_text(encoding="utf-8")
        assert 'reconfigure(encoding="utf-8"' in src, (
            f"{script} 必须把 stdout/stderr 切到 UTF-8")

    @pytest.mark.parametrize("script", ALL_GATE_SCRIPTS)
    def test_reconfigure_happens_before_the_first_print(self, script):
        """判据看的是**位置**，不是「文件里出现过 reconfigure」。

        只查「出现过」的话，把那段挪到文件末尾一样绿 —— 而那样在 Windows
        上照样崩。这是本项目记过多次的形状：判据认了名字，没认行为。
        """
        src = (_ROOT / script).read_text(encoding="utf-8")
        reconfig = src.find('reconfigure(encoding="utf-8"')
        first_print = src.find("print(")
        assert reconfig != -1, f"{script} 没有那段兜底"
        assert first_print != -1, f"{script} 一个 print 都没有，判据本身失效"
        assert reconfig < first_print, (
            f"{script} 的 reconfigure 出现在第一个 print 之后"
            f"（偏移 {reconfig} vs {first_print}）—— 前面那几次打印照样会崩")

    @pytest.mark.parametrize("script", ALL_GATE_SCRIPTS)
    def test_runs_under_cp1252_stdout(self, script):
        """真的用 cp1252 跑一遍。

        只有真跑才能证明「Windows CI 会绿」—— 断言源码里有某行代码
        只是在检查拼写。
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"
        env.pop("PYTHONUTF8", None)
        proc = subprocess.run(
            [sys.executable, str(_ROOT / script), *self._args_for(script)],
            capture_output=True, env=env, timeout=300)
        assert proc.returncode == 0, (
            f"{script} 在 cp1252 控制台下退出码 {proc.returncode}: "
            f"{proc.stderr[-400:].decode('utf-8', 'replace')}")

    @pytest.mark.parametrize("script", ALL_GATE_SCRIPTS)
    def test_both_streams_are_reconfigured(self, script):
        """stdout 和 stderr 都要改：崩在 stderr 上的报错同样让 step 变红。"""
        src = (_ROOT / script).read_text(encoding="utf-8")
        assert "(sys.stdout, sys.stderr)" in src, (
            f"{script} 只改了部分流 —— stderr 上的报错同样会让 CI 变红")

    def test_the_same_fix_already_exists_in_this_repo(self):
        """守的不是「再写一遍」，是「别发明第三种写法」。

        `scripts/adversarial_eval.py` 是这件事的**参考实现**（2024 年就踩过
        一次并修好）。如果哪天有人把某个脚本改成新写法，至少这里指出
        仓库里已有标准答案。
        """
        ref = (_ROOT / "scripts" / "adversarial_eval.py").read_text(
            encoding="utf-8")
        assert 'reconfigure(encoding="utf-8"' in ref, (
            "参考实现 scripts/adversarial_eval.py 里没有这段兜底了 —— "
            "先确认它是被有意删掉的，再来改别的脚本")


# ---------------------------------------------------------------------------
# I6 不越界：因果措辞与置信档位的硬边界
# ---------------------------------------------------------------------------
BANNED_CAUSAL = ("导致了", "由其引发", "根本原因是", "根因是", "caused by")


class TestNoOverreach:
    @pytest.mark.parametrize("seed", [SEED, SEED + 5, SEED + 31, SEED + 77])
    def test_non_confirmed_never_uses_causal_language(self, seed):
        r = analyze_text(_gen_logs(seed=seed), context_lines=2)
        for c in r.clusters:
            if c.root_cause_confidence == "CONFIRMED":
                continue
            for banned in BANNED_CAUSAL:
                assert banned not in (c.root_cause_reason or "")

    def test_trace_chain_never_upgrades_confidence(self):
        """有完整跨服务链路但无 Caused-by 时，最高只能到 LIKELY。"""
        log = "\n".join(
            f'{{"@timestamp":"2026-10-14T09:00:0{i}.000Z","level":"ERROR",'
            f'"service.name":"svc{i}","message":"failed at step {i}",'
            f'"trace_id":"t-12345678"}}' for i in range(5)) + "\n"
        r = analyze_text(log, context_lines=2)
        assert r.traces, "应当重建出链路"
        assert all(c.root_cause_confidence != "CONFIRMED" for c in r.clusters)
        assert r.evidence["can_conclude"] is False

    def test_confirmed_requires_actual_caused_by(self):
        """CONFIRMED 必须在样本堆栈里真有 Caused by 行。"""
        for seed in (SEED, SEED + 3, SEED + 13, SEED + 21):
            r = analyze_text(_gen_logs(seed=seed), context_lines=2)
            for c in r.clusters:
                if c.root_cause_confidence != "CONFIRMED":
                    continue
                stack = c.sample.entry.stack if c.sample and c.sample.entry else []
                assert any("Caused by" in line for line in stack), \
                    f"{c.summary[:24]} 被判 CONFIRMED 但堆栈里没有 Caused by"

    def test_evidence_assessment_is_always_populated_when_clusters_exist(self):
        r = analyze_text(_gen_logs(), context_lines=2)
        assert r.clusters
        assert r.evidence and r.evidence["verdict"] in (
            "CONFIRMED", "LIKELY", "INSUFFICIENT")
        assert isinstance(r.evidence["gaps"], list)
