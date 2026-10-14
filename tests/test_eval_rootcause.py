# -*- coding: utf-8 -*-
"""根因评测器地面真相扫描（scripts/eval_rootcause.scan_chains）。

锁住「改坏了不会报错」的一处关键缺陷
-----------------------------------
scan_chains 是**地面真相**：它失真，整个评测的精确率/召回率就全是错的。
真实日志（Hadoop）把异常摘要**内嵌在带时间戳的日志行里**：

    2015-10-19 17:41:30,296 INFO [...] Task: Communication exception:
        java.io.IOException: Failed on local exception: ...
        at org.apache.hadoop.net.NetUtils.wrapException(...)
    Caused by: java.io.IOException: An existing connection was ...

旧行为只认「裸异常摘要行」开头的链，把这种链整条漏扫 —— 于是工具
正确的 Caused-by 判定反被对账判成"自信错误"（真实 Hadoop 上假报
自信错误率 66%）。这些用例钉死：内嵌异常链必须被识别。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from eval_rootcause import REPRO_CORPUS, scan_chains  # noqa: E402


def _chains_of(text: str, tmp_path):
    p = tmp_path / "x.log"
    p.write_text(text, encoding="utf-8")
    return scan_chains(p)


class TestInlineExceptionChain:
    def test_hadoop_style_inline_head(self, tmp_path):
        """异常摘要内嵌在带时间戳的日志行里 → 链仍须识别。"""
        log = (
            "2015-10-19 17:41:30,296 INFO [comm] org.apache.hadoop.Task: "
            "Communication exception: java.io.IOException: Failed on local\n"
            "\tat org.apache.hadoop.net.NetUtils.wrapException(NetUtils.java:772)\n"
            "Caused by: java.io.IOException: An existing connection closed\n"
            "\tat sun.nio.ch.SocketDispatcher.read0(Native Method)\n"
            "2015-10-19 17:41:36,718 INFO [SpillThread] MapTask: Finished spill\n"
        )
        chains = _chains_of(log, tmp_path)
        assert len(chains) == 1
        assert chains[0].depth == 1
        assert "an existing connection" in chains[0].deepest_sig

    def test_repro_corpus_still_five(self, tmp_path):
        """修复不得改动复现语料口径：仍是 5 条链。"""
        total = 0
        for name, text in REPRO_CORPUS.items():
            p = tmp_path / name
            p.write_text(text, encoding="utf-8")
            total += len(scan_chains(p))
        assert total == 5


class TestNoFalseChain:
    def test_plain_messages_no_chain(self, tmp_path):
        """无 Caused by 的普通错误行不得凭空造链。"""
        log = (
            "2026-10-14 08:00:01,000 WARN  [worker] Task: connection refused\n"
            "2026-10-14 08:00:02,000 ERROR [worker] Task: timeout: downstream\n"
            "2026-10-14 08:00:03,000 ERROR [worker] Task: task failed after retries\n"
        )
        assert _chains_of(log, tmp_path) == []

    def test_two_chains_not_merged(self, tmp_path):
        """两条独立链被时间戳隔开 → 两条，不得并成一条。"""
        log = (
            "2026-10-14 09:00:00 ERROR a\n"
            "java.lang.RuntimeException: A\n"
            "\tat x.Y.z(Y.java:1)\n"
            "Caused by: java.io.IOException: root-A\n"
            "2026-10-14 09:00:01 ERROR b\n"
            "java.lang.RuntimeException: B\n"
            "\tat x.Y.z(Y.java:2)\n"
            "Caused by: java.io.IOException: root-B\n"
        )
        chains = _chains_of(log, tmp_path)
        assert len(chains) == 2
        assert "root-a" in chains[0].deepest_sig
        assert "root-b" in chains[1].deepest_sig
