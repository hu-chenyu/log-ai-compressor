# -*- coding: utf-8 -*-
"""同预算公平对照的口径锁定（scripts/eval_budget.py）。

锁三个「改坏了不会报错」的地方：
1. 留存判定必须从宽（类名或消息前缀命中即算），不冤枉对照组；
2. _clip 预算封顶必须真实生效（否则各方法不在同一把尺子上）；
3. 语料构造必须真的把故障埋进噪音（链数与场景数一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_budget as eb  # noqa: E402
from eval_rootcause import REPRO_CORPUS, scan_chains  # noqa: E402


class TestRetained:
    def test_class_name_hit(self):
        assert eb.retained(
            "Caused by: java.net.ConnectException: Connection refused",
            "java.net.connectexception:connection refused")

    def test_message_prefix_hit(self):
        assert eb.retained(
            "……根因：Connection refused (Connection refused)",
            "java.net.connectexception:connection refused (connection refused)")

    def test_miss(self):
        assert not eb.retained("一切正常", "java.net.connectexception:xxx")


class TestClip:
    def test_budget_enforced(self):
        text = "\n".join(f"line {i} " + "x" * 200 for i in range(200))
        for b in (50, 200, 800):
            out = eb._clip(text, b)
            assert eb.estimate_tokens(out) <= b + 20  # 行粒度截断的粒度误差

    def test_short_text_untouched(self):
        assert eb._clip("hello", 100) == "hello"


class TestCorpus:
    def test_chains_buried_in_noise(self, tmp_path):
        noise = [f"2024-01-01 00:00:{i % 60:02d} INFO noise line {i}"
                 for i in range(300)]
        corpus = tmp_path / "c.log"
        eb.build_corpus(noise, corpus)
        chains = scan_chains(corpus)
        # 场景数与链数一致：order_service 2 + batch_job 1 + mixed 2（对照组 0）
        assert len(chains) == 5
        assert len(REPRO_CORPUS) == 4

    def test_methods_respect_budget(self, tmp_path):
        noise = [f"2024-01-01 00:00:{i % 60:02d} WARN noise {i}"
                 for i in range(300)]
        corpus = tmp_path / "c.log"
        eb.build_corpus(noise, corpus)
        lines = corpus.read_text(encoding="utf-8").splitlines()
        for fn in (eb.method_tail, eb.method_grep, eb.method_drain3):
            out = fn(lines, 200)
            assert eb.estimate_tokens(out) <= 220
