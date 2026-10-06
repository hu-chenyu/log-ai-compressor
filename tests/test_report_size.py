# -*- coding: utf-8 -*-
"""报告体积上界（修复"实例行号平铺把报告撑爆"）。

问题复现
--------
OpenStack 真实日志上，3 个簇的 Markdown 报告长 519 行 / **272,744 字符**
（≈68k tokens），其中**单行 16,828 字符** —— 一个簇的 2000 个实例行号
被平铺进一行。当时对外的说法是"压缩到几百 token"，而 CLI 的**默认格式
正是 md**，也就是说压缩的卖点在那条最常用的路径上是失效的。

（这也是一条自我更正：benchmark 里的 470-746 tokens 测的是 brief_summary
格式，不是 md。两者不能混为一谈。）

这些测试把体积钉成硬约束 —— 报告渲染一旦回退到平铺，这里会立刻红。
"""
from __future__ import annotations

import json
import re

import pytest

from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.export.reporters import (
    _INSTANCE_EDGE,
    _INSTANCE_LIST_LIMIT,
    brief_summary,
    estimate_tokens,
    to_json,
    to_markdown,
    to_text,
)


def _log_with_repeats(n: int) -> str:
    """n 条同构错误 + 一条其它行：制造一个实例数 = n 的簇。"""
    lines = [f"2024-01-01 10:{i % 60:02d}:00 ERROR [db] "
             f"connection refused to replica-{i % 3}" for i in range(n)]
    lines.insert(n // 2, "2024-01-01 10:30:00 INFO [http] request handled ok")
    return "\n".join(lines) + "\n"


def _instance_line(md: str) -> str:
    return next(l for l in md.splitlines() if l.startswith("- 实例（"))


# ---------------------------------------------------------------------------
# 封顶
# ---------------------------------------------------------------------------
class TestInstanceListIsCapped:
    def test_small_cluster_lists_every_instance(self):
        """簇小的时候必须列全 —— 排查十几条重复错误时行号是有用的。"""
        md = to_markdown(analyze_text(_log_with_repeats(_INSTANCE_LIST_LIMIT)))
        nums = re.findall(r"L\d+", _instance_line(md))
        assert len(nums) == _INSTANCE_LIST_LIMIT, "小簇应列全，不能一刀切截断"

    def test_large_cluster_is_capped(self):
        """大簇必须截断，且首尾都要留 —— 只留头部看不出问题何时结束。"""
        md = to_markdown(analyze_text(_log_with_repeats(800)))
        nums = re.findall(r"L\d+", _instance_line(md))
        assert len(nums) <= _INSTANCE_EDGE * 2 + 2, f"行号未封顶：{len(nums)} 个"

    def test_capped_line_reports_total_and_range(self):
        """截断后仍必须说清「总共多少次」「跨哪一段」—— 否则信息全丢了。"""
        md = to_markdown(analyze_text(_log_with_repeats(800)))
        line = _instance_line(md)
        assert "800 处" in line
        assert re.search(r"跨 L\d+~L\d+", line), "必须给出范围"
        assert "完整清单见 JSON 导出" in line, "要告诉用户完整数据在哪拿"


# ---------------------------------------------------------------------------
# 体积硬约束
# ---------------------------------------------------------------------------
class TestReportSizeBound:
    N = 2000

    @pytest.mark.parametrize("render", [to_markdown, to_text, brief_summary],
                             ids=["md", "text", "summary"])
    def test_single_cluster_report_is_bounded(self, render):
        out = render(analyze_text(_log_with_repeats(self.N)))
        # 2000 行日志压到 8000 字符以内；曾经的实现是 27 万字符
        assert len(out) < 8_000, f"报告 {len(out)} 字符，超出上界"

    def test_no_line_is_absurdly_long(self):
        """单行长度也要有界：平铺行号时产出过 16,828 字符的单行。"""
        md = to_markdown(analyze_text(_log_with_repeats(self.N)))
        longest = max(len(l) for l in md.splitlines())
        assert longest < 1_000, f"存在 {longest} 字符的超长行"

    def test_tokens_stay_small(self):
        assert estimate_tokens(brief_summary(
            analyze_text(_log_with_repeats(self.N)))) < 2_000

    def test_density_hint_added_for_dense_cluster(self):
        """密集重复的簇给出间隔提示 —— 这比两千个行号有用：
        「均匀/不均匀、约每 N 行一次」直接区分稳态刷屏与集中爆发。"""
        md = to_markdown(analyze_text(_log_with_repeats(800)))
        assert re.search(r"(均匀|不均匀)，约每 \d+ 行一次", _instance_line(md))


# ---------------------------------------------------------------------------
# 语义保真
# ---------------------------------------------------------------------------
class TestNothingLost:
    def test_json_export_keeps_the_full_list(self):
        """摘要化只是展示层，完整清单必须仍然拿得到。"""
        data = json.loads(to_json(analyze_text(_log_with_repeats(300))))
        total = sum(len(c.get("instances") or []) for c in data.get("clusters", []))
        assert total > 0, "JSON 导出应保留完整实例清单"
