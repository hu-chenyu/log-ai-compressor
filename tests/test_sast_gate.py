# -*- coding: utf-8 -*-
"""scripts/sast_gate.py 的测试 —— 这道门禁必须有牙齿，否则它只是装饰。

为什么这道门禁特别需要负向测试
----------------------------
SAST 门禁的失败模式全是「**绿**」的：

1. 基线里多了一条没人看过的豁免 —— 门禁绿，但那个洞已经没人管了；
2. 代码里新冒出一个漏洞、基线没跟上 —— 门禁绿；
3. bandit 版本变了、报的东西全变了 —— 基线对不上，门禁红（这一条是好的），
   但如果反过来：bandit 静默不报了，门禁也会绿。

第 3 条尤其要命：**一个报「检查通过」但其实什么都没扫的门禁，
比没有门禁更糟** —— 没有门禁你知道那里没检查。所以下面每条用例都是
「故意把门禁搞坏 → 必须红」。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "sast_gate", ROOT / "scripts" / "sast_gate.py")
assert _SPEC and _SPEC.loader
sast = importlib.util.module_from_spec(_SPEC)
sys.modules["sast_gate"] = sast
_SPEC.loader.exec_module(sast)


def _write_baseline(tmp_path: Path, entries, monkeypatch) -> Path:
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                 encoding="utf-8")
    monkeypatch.setattr(sast, "BASELINE", p)
    return p


# ---------------------------------------------------------------------------
# 正向：正常情况下必须绿
# ---------------------------------------------------------------------------
def test_matching_baseline_passes(tmp_path, monkeypatch, capsys):
    _write_baseline(tmp_path, [
        {"test_id": "B110", "path": "a.py", "count": 2, "reason": "有意为之"},
    ], monkeypatch)
    from collections import Counter
    assert sast.check(Counter({"B110|a.py": 2})) == 0
    assert "通过" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 负向 1：新发现（代码里冒出一个洞，基线没跟上）
# ---------------------------------------------------------------------------
def test_unknown_finding_fails(tmp_path, monkeypatch, capsys):
    _write_baseline(tmp_path, [
        {"test_id": "B110", "path": "a.py", "count": 2, "reason": "有意为之"},
    ], monkeypatch)
    from collections import Counter
    assert sast.check(Counter({"B110|a.py": 2, "B607|b.py": 1})) == 1
    out = capsys.readouterr().out
    assert "B607|b.py" in out and "不在基线" in out


# ---------------------------------------------------------------------------
# 负向 2：过期豁免（代码里已经不报了，基线里还留着）
#
# 这一条最容易被漏：基线只增不减，最后变成一张谁也不敢删的旧账。
# 而「没人再看的那条豁免」既不是检查也不是豁免。
# ---------------------------------------------------------------------------
def test_stale_baseline_entry_fails(tmp_path, monkeypatch, capsys):
    _write_baseline(tmp_path, [
        {"test_id": "B110", "path": "a.py", "count": 2, "reason": "有意为之"},
        {"test_id": "B607", "path": "gone.py", "count": 1, "reason": "以前是这样"},
    ], monkeypatch)
    from collections import Counter
    assert sast.check(Counter({"B110|a.py": 2})) == 1
    assert "过期豁免" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 负向 3：豁免没写理由（「我看过、决定不修」没有落成文字）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", None])
def test_missing_reason_fails(tmp_path, monkeypatch, capsys, bad):
    _write_baseline(tmp_path, [
        {"test_id": "B110", "path": "a.py", "count": 1, "reason": bad},
    ], monkeypatch)
    from collections import Counter
    assert sast.check(Counter({"B110|a.py": 1})) == 1
    assert "没有写理由" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 负向 4：同一 (规则, 文件) 下多出一条 —— 不能悄悄并进旧理由
# ---------------------------------------------------------------------------
def test_count_mismatch_fails(tmp_path, monkeypatch, capsys):
    _write_baseline(tmp_path, [
        {"test_id": "B603", "path": "a.py", "count": 2, "reason": "复核过"},
    ], monkeypatch)
    from collections import Counter
    assert sast.check(Counter({"B603|a.py": 3})) == 1
    assert "数量对不上" in capsys.readouterr().out


def test_duplicate_baseline_key_fails(tmp_path, monkeypatch, capsys):
    _write_baseline(tmp_path, [
        {"test_id": "B603", "path": "a.py", "count": 1, "reason": "x"},
        {"test_id": "B603", "path": "a.py", "count": 1, "reason": "y"},
    ], monkeypatch)
    from collections import Counter
    assert sast.check(Counter({"B603|a.py": 1})) == 1
    assert "出现两次" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 排除规则必须**只**排除它声称排除的那一类
#
# 这一条守的是一个具体事故：B101 的排除写宽了（比如写成"按前缀匹配"
# 或干脆把整个 B101 排除掉），于是 log_ai_compressor/ 与 scripts/ 里的
# 裸 assert 也一起消失了 —— 而发布代码里的裸 assert 是真问题。
# ---------------------------------------------------------------------------
def test_b101_excluded_in_tests_but_not_in_package():
    assert sast._excluded("B101", "tests/test_foo.py") is not None
    assert sast._excluded("B101", "log_ai_compressor/core/x.py") is None
    assert sast._excluded("B101", "scripts/x.py") is None
    # 别的规则不在射程里
    assert sast._excluded("B603", "tests/test_foo.py") is None


# ---------------------------------------------------------------------------
# 仓库里真实的基线文件本身要合法
# ---------------------------------------------------------------------------
def test_real_baseline_is_valid_and_reasoned():
    doc = json.loads((ROOT / "docs" / "sast_baseline.json")
                     .read_text(encoding="utf-8"))
    entries = doc["entries"]
    assert entries, "基线不能是空的 —— 空基线等于这道门禁没上线"
    keys = set()
    for e in entries:
        key = "%s|%s" % (e["test_id"], e["path"])
        assert key not in keys, "基线里有重复键 %s" % key
        keys.add(key)
        reason = str(e.get("reason") or "").strip()
        assert len(reason) >= 12, (
            "%s 的理由太短（%d 字）：一句「已确认」不构成可被三个月后的人"
            "复核的判断" % (key, len(reason)))
        assert not e["path"].startswith("/"), "路径必须是仓库相对路径"


# ---------------------------------------------------------------------------
# 文档里写的豁免条数，必须等于基线里的条目数
#
# 真发生过：`tests/test_dep_audit_gate.py` 去掉了一个用不上的
# `import subprocess` 之后，基线里那条 B404 豁免变成**过期条目**，
# 门禁正确地红了。而我顺手把 docs/DEVELOPMENT.md 的「43」改成「44」，
# 依据是 `len(json["entries"]) == 44` —— 那 44 里有一条是过期的。
# **过期条目不该算豁免**：它是门禁正在报的债，不是「我看过、决定不修」。
# 照着它数出来的 N，会让文档比门禁多报一条豁免。
# ---------------------------------------------------------------------------
_COUNT_PATTERNS = (r"剩下\s*(\d+)\s*条", r"（(\d+)\s*条豁免")


@pytest.mark.parametrize("doc", ["docs/DEVELOPMENT.md", "CHANGELOG.md"])
def test_documented_exemption_count_matches_the_baseline(doc):
    import re
    baseline = json.loads((ROOT / "docs" / "sast_baseline.json")
                          .read_text(encoding="utf-8"))
    want = len(baseline["entries"])
    text = (ROOT / doc).read_text(encoding="utf-8")
    found = []
    for pat in _COUNT_PATTERNS:
        found += [int(x) for x in re.findall(pat, text)]
    assert found, "%s 里找不到豁免条数 —— 格式变了，先更新本测试" % doc
    for claimed in found:
        assert claimed == want, (
            "%s 写 %d 条豁免，docs/sast_baseline.json 里有 %d 条 —— "
            "基线动了（新增/删除/过期清理）都要回填文档"
            % (doc, claimed, want))


# ---------------------------------------------------------------------------
# 排除条数必须**实测**，不许写死在理由里
#
# 真发生过：`EXCLUDES` 的理由里写着「全量套件 2,307 条」。写死的当天是
# 对的，之后每加一批测试它就悄悄变成一句假话 —— 而它出现在**门禁输出**里，
# 读者会当成实测值。实测值已经漂到两千四百多了。
# ---------------------------------------------------------------------------
def test_exclude_reasons_carry_no_hardcoded_count():
    """排除理由里出现「N 条」就是写死的数字 —— 必须由运行时实测代替。

    这条判据读的是**文本**，而它要守的**就是**「这段文本里不许有数字」
    这件事本身 —— 所以用文本搜索是对的形态。
    """
    import re
    for tid, prefix, why in sast.EXCLUDES:
        m = re.search(r"[\d,]+\s*条", why)
        assert m is None, (
            "排除理由 %s @ %s 里写着「%s」—— 写死的条数必然漂，"
            "改成引用运行时实测值" % (tid, prefix, m.group(0) if m else ""))


def test_reported_exclusion_count_is_the_measured_one(monkeypatch, capsys):
    """输出里的排除条数必须是被传进来的那个值。

    喂一个假的 37，输出里就得是 37 —— 如果实现里写死了某个数，这里
    会看到另一个数。这条不需要跑 bandit（纯离线）。
    """
    import collections
    monkeypatch.setattr(sast, "load_baseline", lambda: [])
    rc = sast.check(collections.Counter(), collections.Counter({"B101|tests/": 37}))
    out = capsys.readouterr().out
    assert "（本次实测 37 条）" in out, (
        "输出没有报出实测的排除条数 —— 现在它要么被写死了，要么根本没打。\n%s"
        % out)
    assert rc == 0
