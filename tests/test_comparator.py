# -*- coding: utf-8 -*-
"""多文件对比单元测试。"""
from __future__ import annotations

import pytest

from log_ai_compressor.core.comparator import (
    compare_files,
    compare_results,
)
from log_ai_compressor.core.pipeline import analyze_text


V1 = """\
2024-01-01 10:00:00 INFO [api] start
2024-01-01 10:00:01 ERROR [db] connection refused to host 10.0.0.1
2024-01-01 10:00:02 ERROR [db] connection refused to host 10.0.0.1
2024-01-01 10:00:03 ERROR [db] connection refused to host 10.0.0.1
2024-01-01 10:00:04 ERROR [auth] token expired for session 42
2024-01-01 10:00:05 ERROR [legacy] old bug still here
"""

V2 = """\
2024-01-01 10:00:00 INFO [api] start
2024-01-01 10:00:01 ERROR [db] connection refused to host 10.0.0.1
2024-01-01 10:00:02 ERROR [auth] token expired for session 43
2024-01-01 10:00:03 ERROR [auth] token expired for session 44
2024-01-01 10:00:04 ERROR [auth] token expired for session 45
2024-01-01 10:00:05 ERROR [newmod] brand new regression
2024-01-01 10:00:06 ERROR [newmod] brand new regression
"""


@pytest.fixture()
def v1_result():
    return analyze_text(V1, source="app_v1.log", analyze=False)


@pytest.fixture()
def v2_result():
    return analyze_text(V2, source="app_v2.log", analyze=False)


class TestCompareResults:
    def test_new_items(self, v1_result, v2_result):
        cmp = compare_results(v1_result, v2_result)
        new_summaries = [i.summary for i in cmp.new_items]
        assert any("brand new regression" in s for s in new_summaries)
        assert cmp.new_items[0].count_b == 2
        assert cmp.new_items[0].change_rate is None

    def test_gone_items(self, v1_result, v2_result):
        cmp = compare_results(v1_result, v2_result)
        gone = [i.summary for i in cmp.gone_items]
        assert any("old bug" in s for s in gone)
        assert not any("connection refused" in s for s in gone)

    def test_common_items_with_change_rate(self, v1_result, v2_result):
        cmp = compare_results(v1_result, v2_result)
        by_summary = {i.summary: i for i in cmp.common_items}
        db = next(i for k, i in by_summary.items() if "refused" in k)
        assert db.count_a == 3 and db.count_b == 1
        assert db.change_rate == pytest.approx(-66.7, abs=0.1)
        token = next(i for k, i in by_summary.items() if "token" in k)
        assert token.count_a == 1 and token.count_b == 3
        assert token.change_rate == pytest.approx(200.0)

    def test_stack_difference_not_false_diff(self, v1_result, v2_result):
        # 同消息不同堆栈（有无堆栈）不应判为「新增/消失」
        v1 = analyze_text(
            "2024-01-01 10:00:00 ERROR [db] boom\n", analyze=False)
        v2 = analyze_text(
            "2024-01-01 10:00:00 ERROR [db] boom\n"
            "java.net.ConnectException: Connection refused\n"
            "\tat com.app.Main.run(Main.java:1)\n", analyze=False)
        cmp = compare_results(v1, v2)
        assert cmp.new_items == []
        assert cmp.gone_items == []
        assert len(cmp.common_items) == 1

    def test_change_text(self, v1_result, v2_result):
        cmp = compare_results(v1_result, v2_result)
        db = next(i for i in cmp.common_items if "refused" in i.summary)
        assert db.change_text.startswith("-")
        token = next(i for i in cmp.common_items if "token" in i.summary)
        assert token.change_text == "+200.0%"

    def test_as_dict(self, v1_result, v2_result):
        d = compare_results(v1_result, v2_result).as_dict()
        assert d["base"] == "app_v1.log"
        assert d["other"] == "app_v2.log"
        assert isinstance(d["new"], list)


class TestCompareFiles:
    def test_two_files(self, tmp_path):
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        a.write_text(V1, encoding="utf-8")
        b.write_text(V2, encoding="utf-8")
        results = compare_files([a, b])
        assert len(results) == 1
        assert results[0].base_name == "a.log"
        assert results[0].other_name == "b.log"

    def test_three_files_pairwise_against_base(self, tmp_path):
        a = tmp_path / "a.log"
        b = tmp_path / "b.log"
        c = tmp_path / "c.log"
        a.write_text(V1, encoding="utf-8")
        b.write_text(V2, encoding="utf-8")
        c.write_text(V1, encoding="utf-8")
        results = compare_files([a, b, c])
        assert len(results) == 2
        assert all(r.base_name == "a.log" for r in results)
        # c 与 a 相同 -> 无新增/消失
        assert results[1].new_items == []
        assert results[1].gone_items == []

    def test_requires_two_files(self, tmp_path):
        with pytest.raises(ValueError):
            compare_files([tmp_path / "only.log"])
