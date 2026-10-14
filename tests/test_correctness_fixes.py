# -*- coding: utf-8 -*-
"""全量通读后修掉的一批正确性缺陷的回归测试。

每条测试对应一个**已复现**的缺陷（先构造出错误行为、确认、再改代码），
断言写成「修好后必须成立的样子」，防止将来被改回去。

分组：
  A 聚类 / 时间：簇 last_seen 被无戳条目抹掉、跨零点时段过滤全灭、
    行数上限 0 的语义
  B 统计：故障窗口 z-score 的分子未按裁剪窗口统计
  C 序列化 / 报告：变量分布模板用错、优先级档位没下发、
    行数上限采样在报告里不可见
  D 接入层：CLI 级别别名、规则正则报错类型、参数校验穿帮、
    过滤器配置往返丢 use_regex、轮转文件顺序
  E 文本处理：级别串带分隔符归一、脱敏误伤版本号
"""
from __future__ import annotations

import json
import os
import time

import pytest

from log_ai_compressor import service as S
from log_ai_compressor.cli import _parse_levels
from log_ai_compressor.constants import normalize_level
from log_ai_compressor.core.analysis import significant_in_window
from log_ai_compressor.core.clustering import (
    ErrorClusterer,
    extract_variable_distribution,
)
from log_ai_compressor.core.filters import EntryFilter, FilterConfig
from log_ai_compressor.core.models import ClusterInstance, ErrorCluster, LogEntry
from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.core.redact import redact_text
from log_ai_compressor.export.reporters import to_html, to_json, to_markdown, to_text
from log_ai_compressor.rules.engine import RuleSet, RuleSetError

LOG_MIXED = (
    "2026-10-14 23:30:00 ERROR night connection refused\n"
    "2026-10-14 12:00:00 ERROR noon connection refused\n"
    "2026-10-14 03:00:00 ERROR small hours connection refused\n"
)


# ---------------------------------------------------------------------------
# A 聚类 / 时间
# ---------------------------------------------------------------------------
class TestClusterTime:
    def test_last_seen_not_wiped_by_untimed_entry(self):
        """无时间戳的同类条目不得抹掉簇已有的 last_seen。

        复现：ERROR boom(t=1000) → ERROR boom(t=1010) → ERROR boom(t=None)
        修复前 last_seen 变 None，报告「首末时间」变空、优先级里的
        「持续发生」判定随之失效。
        """
        cl = ErrorClusterer()
        for line_no, ts in ((1, 1000.0), (2, 1010.0), (3, None)):
            cl.add(LogEntry(line_no=line_no, level="ERROR",
                            message="boom", timestamp=ts))
        c = cl.clusters[0]
        assert c.count == 3
        assert c.last_seen == 1010.0
        assert c.hist.total == 2          # 无戳条目不进直方图

    def test_last_seen_updates_with_later_timed_entry(self):
        """带戳条目仍要正常推进 last_seen（别把修复做成「永不更新」）。"""
        cl = ErrorClusterer()
        for line_no, ts in ((1, 1000.0), (2, 1099.0)):
            cl.add(LogEntry(line_no=line_no, level="ERROR",
                            message="boom", timestamp=ts))
        assert cl.clusters[0].last_seen == 1099.0

    def test_untimed_only_cluster_keeps_none(self):
        """全程无戳时 last_seen 仍是 None（没有可编造的兜底值）。"""
        cl = ErrorClusterer()
        cl.add(LogEntry(line_no=1, level="ERROR", message="boom"))
        assert cl.clusters[0].last_seen is None

    def test_find_similar_disabled_when_max_compare_zero(self):
        """max_compare=0 表示关闭相似度回退，而不是退化成全表比较。

        复现：bucket[-0:] 等于 bucket[0:]，5 个模板全部参与比较 ——
        保护参数反而打开了最贵的那条路径。
        """
        cl = ErrorClusterer(max_compare=0)
        cl.add(LogEntry(line_no=1, level="ERROR", message="alpha failure"))
        cl.add(LogEntry(line_no=2, level="ERROR", message="alpha failurex"))
        assert len(cl.clusters) == 2       # 未被并成一簇


class TestTimeOfDayFilter:
    @staticmethod
    def _kept(result):
        """存活条目数。

        不看簇数：三条消息词面高度相似，相似度回退会把它们并成一簇，
        簇数无法反映「几条通过了时段过滤」。这里要验的是过滤口径。
        """
        return sum(c.count for c in result.clusters)

    def test_overnight_window_keeps_both_sides(self):
        """跨零点窗口 22:00~06:00 必须同时命中 23:30 与 03:00。

        复现：修复前是两个独立早退（tod < start / tod > end），start > end
        时一天 86400 秒全部落在区间外 → 用户设了夜间窗口拿到空报告。
        """
        r = analyze_text(LOG_MIXED, tod_start=22 * 3600, tod_end=6 * 3600)
        assert self._kept(r) == 2

    def test_normal_window_unchanged(self):
        """非跨零点的窗口行为不变（23:00~23:30 只留 23:30 那条）。"""
        r = analyze_text(LOG_MIXED, tod_start=23 * 3600,
                         tod_end=23 * 3600 + 1800)
        assert self._kept(r) == 1

    def test_daytime_window(self):
        r = analyze_text(LOG_MIXED, tod_start=11 * 3600, tod_end=13 * 3600)
        assert self._kept(r) == 1        # 只有 12:00 那条在区间内

    def test_open_ended_window(self):
        """只给起点：08:00 之后全部保留（03:00 那条被排除）。"""
        r = analyze_text(LOG_MIXED, tod_start=8 * 3600)
        assert self._kept(r) == 2

    def test_only_end_given(self):
        """只给终点：06:00 之前全部保留。"""
        r = analyze_text(LOG_MIXED, tod_end=6 * 3600)
        assert self._kept(r) == 1


class TestMaxLines:
    def test_zero_means_unlimited(self):
        """max_lines=0 表示不限，与 service 层「0 = 全部」口径一致。

        复现：修复前 `line_no >= 0` 在第一行就成立，只读 1 行。
        """
        log = "".join(f"2026-10-14 10:00:0{i} ERROR e{i}\n" for i in range(5))
        r = analyze_text(log, max_lines=0)
        assert r.stats.total_lines == 5
        assert r.stats.limit_hit is False

    def test_limit_hit_flagged(self):
        log = "".join(f"2026-10-14 10:00:0{i} ERROR e{i}\n" for i in range(5))
        r = analyze_text(log, max_lines=2)
        assert r.stats.total_lines == 2
        assert r.stats.limit_hit is True
        assert r.stats.truncated is False   # 上限 != 取消


# ---------------------------------------------------------------------------
# B 统计：故障窗口显著性
# ---------------------------------------------------------------------------
class TestSignificantInWindow:
    @staticmethod
    def _cluster(cid, times):
        c = ErrorCluster(cluster_id=cid, template="t", level="ERROR",
                         count=len(times))
        c.instances = [ClusterInstance(timestamp=t, line_no=i + 1)
                       for i, t in enumerate(times)]
        return c

    def test_molecular_window_is_clipped_to_span(self):
        """分子必须与 expected 用同一个（裁剪后）窗口。

        复现：实例在 500/520/1000/1020，请求窗口 [0,1100]、全量跨度
        [1000,1100]。分母按交集算（4×1.0=4），分子却按未裁剪窗口数成 4，
        z=0；把其中两个实例挪出跨度后 z 被算成「突增」，而它们根本不在
        用户选中的时段里。
        """
        c = self._cluster(0, [500.0, 520.0, 1000.0, 1020.0])
        res = significant_in_window([c], 0, 1100, 1000, 1100,
                                    min_count=1, min_z=-99.0)
        assert res and res[0][1] == 2       # 只有落在 [1000,1100] 的两个

    def test_in_window_burst_still_detected(self):
        """修复不能把真正的窗口内突增一起干掉。"""
        c = self._cluster(0, [10.0, 20.0, 30.0, 40.0, 45.0, 50.0])
        hits = significant_in_window([c], 40.0, 55.0, 0.0, 100.0)
        assert hits and hits[0][1] == 3 and hits[0][2] >= 2.0


# ---------------------------------------------------------------------------
# C 序列化 / 报告
# ---------------------------------------------------------------------------
def _stacked_cluster_with_numbers():
    cl = ErrorClusterer()
    for i in range(3):
        cl.add(LogEntry(
            line_no=i * 3 + 1, level="ERROR",
            message=f"connect failed to 10.0.0.{i + 1}:8080 after 3 tries",
            stack=["at com.foo.Bar.run(Bar.java:42)"]))
    return cl.clusters[0]


class TestVariableDistribution:
    def test_service_uses_message_template(self):
        """带堆栈的簇也必须能出变量分布。

        复现：cluster_to_dict 传的是 cluster.template（级别|消息|堆栈三段
        拼接），拿它去匹配只有消息文本的实例摘要，尾部堆栈字面量永远
        匹配不上 → matched=0，variables 恒为空。MCP 的
        get_cluster_detail 把「变量取值分布」写进了工具描述。
        """
        c = _stacked_cluster_with_numbers()
        assert c.template != c.message_template      # 前提：两者确实不同
        direct, _ = extract_variable_distribution(
            c.message_template, [i.summary for i in c.instances])
        assert direct == 3
        data = S.cluster_to_dict(c, full=True)
        assert data["variables"], "带堆栈的簇变量分布不应为空"

    def test_plain_cluster_still_reports_variables(self):
        """无堆栈簇（template == message_template）同样不受影响。"""
        cl = ErrorClusterer()
        for i in range(2):
            cl.add(LogEntry(line_no=i + 1, level="ERROR",
                            message=f"connect to 10.0.0.{i + 1} timed out"))
        c = cl.clusters[0]
        assert c.template == "ERROR | " + c.message_template
        assert S.cluster_to_dict(c, full=True)["variables"]

    def test_no_placeholder_template_yields_nothing(self):
        """模板里没有占位符时返回空是设计行为，不是回归。"""
        cl = ErrorClusterer()
        cl.add(LogEntry(line_no=1, level="ERROR", message="disk is full"))
        c = cl.clusters[0]
        assert extract_variable_distribution(c.message_template, ["disk is full"]) \
            == (0, [])


class TestClusterDictFields:
    def test_priority_label_is_serialized(self):
        """Web API 必须下发 priority_label。

        复现：前端只能拼 `P${priority}`，界面上显示成「P80.0」—— 一个
        像 80 分的数字，而不是 P0~P4 分档。模型与各类报告里都有这个标签。
        """
        c = _stacked_cluster_with_numbers()
        c.priority = 80.0
        d = S.cluster_to_dict(c)
        assert d["priority_label"] == "P0"
        assert d["priority"] == 80.0


class TestLineLimitIsVisible:
    @staticmethod
    def _limited_result():
        r = analyze_text(LOG_MIXED, max_lines=1)
        r.stats.limit_hit = True
        return r

    def test_markdown_states_line_limit(self):
        md = to_markdown(self._limited_result())
        assert "行数上限" in md and "不是全量" in md

    def test_text_states_line_limit(self):
        assert "行数上限" in to_text(self._limited_result())

    def test_html_states_line_limit(self):
        assert "行数上限" in to_html(self._limited_result())

    def test_json_meta_carries_flag(self):
        """JSON 是给脚本二次消费的，采样标记必须落进 meta。"""
        meta = json.loads(to_json(self._limited_result()))["meta"]
        assert meta["limit_hit"] is True
        assert meta["truncated"] is False
        assert "ts_entries" in meta

    def test_full_run_claims_nothing(self):
        """完整分析不该出现采样提示（别把话说满，也别乱说）。"""
        r = analyze_text(LOG_MIXED)
        assert r.stats.limit_hit is False
        assert "行数上限" not in to_markdown(r)

    def test_cancel_and_limit_are_distinguishable(self):
        """取消与上限采样是两种状态，措辞必须能区分。"""
        r = analyze_text(LOG_MIXED, max_lines=1)
        r.stats.truncated = True            # 同时命中两种
        md = to_markdown(r)
        assert "取消" in md and "行数上限" in md


# ---------------------------------------------------------------------------
# D 接入层
# ---------------------------------------------------------------------------
class TestCliLevels:
    def test_warning_is_not_silently_replaced_by_default(self):
        """`--level WARNING` 必须真的只看警告。

        复现：WARNING 不在 LEVEL_ORDER 里，旧实现把它整条丢掉后回退默认
        ERROR,FAIL —— 用户要警告，报告里全是错误。
        """
        assert _parse_levels("WARNING") == ["WARN"]

    def test_aliases_are_normalized_and_others_kept(self):
        """`--level FATAL,INFO`：FATAL 归一为 ERROR，INFO 保留。"""
        assert _parse_levels("FATAL,INFO") == ["ERROR", "INFO"]

    def test_order_preserved_and_deduped(self):
        assert _parse_levels("warn, WARN , error") == ["WARN", "ERROR"]

    def test_all_invalid_falls_back_to_default(self):
        assert _parse_levels("EROR,NOPE") == ["ERROR", "FAIL"]
        assert _parse_levels("") == ["ERROR", "FAIL"]

    def test_case_insensitive(self):
        assert _parse_levels("error,debug") == ["ERROR", "DEBUG"]


class TestRuleSetErrors:
    def test_bad_stack_indicator_raises_ruleset_error(self):
        """非法 stack_indicator 必须报 RuleSetError，不能抛裸 re.error。

        复现：`run --rule bad.yaml` 撞上写错的堆栈特征正则，用户看到的是
        一整页 Python traceback。patterns 早就这么处理了，这两处漏了。
        """
        with pytest.raises(RuleSetError) as ei:
            RuleSet.from_dict({
                "name": "x",
                "patterns": [{"regex": r"^(?P<message>.*)$"}],
                "stack_indicators": ["(unclosed"],
            })
        assert "堆栈特征" in str(ei.value)

    def test_bad_level_hint_raises_ruleset_error(self):
        with pytest.raises(RuleSetError):
            RuleSet.from_dict({
                "name": "x",
                "patterns": [{"regex": r"^(?P<message>.*)$"}],
                "level_hints": {"ERROR": ["(bad"]},
            })

    def test_valid_ruleset_unaffected(self):
        rs = RuleSet.from_dict({
            "name": "x",
            "patterns": [{"regex": r"^(?P<message>.*)$"}],
            "stack_indicators": [r"^\s*at\s+\w+\("],
        })
        assert len(rs.stack_indicators) == 1


class TestParamValidation:
    @pytest.mark.parametrize("bad", [123, 4.5, object()])
    def test_non_string_keyword_is_4xx_not_500(self, bad):
        """include 传数字必须报 ServiceError（接入层转 4xx），不是 TypeError。

        复现：直接迭代 int 抛 TypeError，Web 层变成 500「服务器内部错误」
        —— 参数写错却报服务崩了。
        """
        with pytest.raises(S.ServiceError):
            S.normalize_params({"include": bad})

    def test_list_and_string_still_accepted(self):
        assert S.normalize_params({"include": "a, b"})["include"] == ["a", "b"]
        assert S.normalize_params({"include": ["a", "b"]})["include"] == ["a", "b"]


class TestFilterConfigRoundTrip:
    def test_use_regex_survives_serialization(self):
        """use_regex 必须能往返。

        复现：as_dict 不含该字段，配置存盘再恢复后「正则模式」静默变回
        子串匹配，用户配的正则不生效且无提示。
        """
        cfg = FilterConfig(include=[r"ERR-\d{4}"], use_regex=True)
        again = FilterConfig.from_dict(cfg.as_dict())
        assert again.use_regex is True
        assert again.as_dict() == cfg.as_dict()

    def test_regex_mode_actually_applies(self):
        """往返之后正则模式仍然真的按正则过滤。"""
        cfg = FilterConfig.from_dict(
            FilterConfig(include=[r"ERR-\d{4}"], use_regex=True).as_dict())
        f = EntryFilter(cfg)
        assert f.match(LogEntry(line_no=1, level="ERROR", message="boom ERR-1234"))
        assert not f.match(LogEntry(line_no=2, level="ERROR", message="boom nothing"))


class TestRotationOrder:
    def test_mcp_sorts_paths_by_mtime(self, tmp_path):
        """轮转文件必须按 mtime 从旧到新合并。

        复现：工具描述承诺了按 mtime 排序，但排序责任写在 core 注释里
        「调用方负责排序」，没有任何接入层真的排。乱序轮转会让时间范围、
        时序连锁与时间线全错，而报告看不出异常。
        """
        pytest.importorskip("mcp")
        from log_ai_compressor.mcp.server import _sorted_oldest_first
        now = time.time()
        files = []
        # 轮转命名：app.log.1 最旧、app.log.3 最新；传入顺序故意打乱
        for name, age in (("app.log.1", 300), ("app.log.3", 100),
                          ("app.log.2", 200)):
            p = tmp_path / name
            p.write_text("x", encoding="utf-8")
            os.utime(p, (now - age, now - age))
            files.append(str(p))
        assert _sorted_oldest_first(files) == [
            str(tmp_path / "app.log.1"),
            str(tmp_path / "app.log.2"),
            str(tmp_path / "app.log.3"),
        ]

    def test_missing_files_are_kept_last_not_dropped(self, tmp_path):
        pytest.importorskip("mcp")
        from log_ai_compressor.mcp.server import _sorted_oldest_first
        p = tmp_path / "a.log"
        p.write_text("x", encoding="utf-8")
        out = _sorted_oldest_first([str(p), str(tmp_path / "gone.log")])
        assert len(out) == 2 and out[0] == str(p)


# ---------------------------------------------------------------------------
# E 文本处理
# ---------------------------------------------------------------------------
class TestNormalizeLevel:
    @pytest.mark.parametrize("raw", ["ERROR:", "ERROR -", "ERROR |",
                                     "[ERROR]:", " error: ", "WARN:"])
    def test_separators_are_stripped(self, raw):
        """级别串带分隔符不得掉进 INFO 兜底。

        复现：'ERROR:' 归一为 INFO，而 ERROR 行一旦变 INFO 就被默认的
        「只看 ERROR/FAIL」过滤整条丢掉 —— 报告说没有错误，日志里全是。
        """
        assert normalize_level(raw) in ("ERROR", "WARN")

    def test_aliases_still_work(self):
        assert normalize_level("FATAL") == "ERROR"
        assert normalize_level("SEVERE") == "ERROR"
        assert normalize_level("FAILED") == "FAIL"

    def test_unknown_still_degrades_to_info(self):
        assert normalize_level("ERR0R") == "INFO"
        assert normalize_level("") == "INFO"

    def test_end_to_end_error_with_colon_survives_filter(self):
        """端到端：带冒号的级别行不会被默认过滤吞掉。"""
        log = "2026-10-14 10:00:00 ERROR: something broke\n"
        assert len(analyze_text(log).clusters) == 1


class TestRedactVersionNumbers:
    @pytest.mark.parametrize("text", [
        "version 1.2.3.4 released",
        "Version: 2.0.14.3",
        "ver 3.1.4.15",
        "版本号 3.4.5.6",
    ])
    def test_version_kept(self, text):
        """版本号不是 IP，不能被抹掉。

        复现：'version 1.2.3.4 released' 被打成 'version [IP] released'，
        把最关键的排查线索从报告里删了 —— 与本模块「宁漏勿滥」的立场
        相悖。
        """
        out = redact_text(text)
        assert "[IP]" not in out, out

    @pytest.mark.parametrize("text", [
        "connected to 192.168.1.10",
        "rtt=10.0.0.1:8080",
        "peer 172.16.0.1 refused",
        "built on host 10.0.0.5",          # build 不算版本号语境
    ])
    def test_real_ip_still_masked(self, text):
        assert "[IP]" in redact_text(text)

    def test_other_masks_unaffected(self):
        out = redact_text("mail a@b.com tel 13812345678 token=abcdef123456")
        assert "[邮箱]" in out and "[电话]" in out and "[密钥]" in out
