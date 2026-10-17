# -*- coding: utf-8 -*-
"""v2.10.0：README 基准表必须与实测基线逐格对账 —— 以及这条门禁本身可失效。

它治的病
--------
`claim_readme_metrics` 校的是**工程指标**（行数 / 模块数 / 主张条数）。
README 里另一张更显眼的表 —— 十行数据集 × 六个百分数 —— **从来没有被任何
东西校过**。后果已经发生，不是理论上的：

    v2.9.9 修了 mask_text 的两处误伤（read/write、input/output 被误判成路径）。
    那次改动本身是对的，但它改变了聚类结果，于是

        HPC          76 簇 / 85.1%  ->  79 簇 / 81.1%
        Zookeeper    83 簇 / 92.1%  ->  83 簇 / 91.0%
        Proxifier   117 簇 / 81.8%  ->  115 簇 / 72.7%

    README、docs/BENCHMARK.md、「唯一负例只差 2 个事件」—— 全部原地不动，
    而六道门禁全绿。这与 v2.9.5 撤回的那次是同一个病：**对外数字，
    没有任何东西在校它。**

怎么修才不会复发
----------------
全量基准要跑两小时，进不了 CI，所以这里**不重跑，只对账**：

    docs/benchmark_baseline.json   <- --write-baseline 生成的实测快照
    README 的表                     <- 人类维护的展示层

改任何影响聚类的代码 -> 两者不一致 -> 这条主张变红。

本文件证明这条门禁**真的会响**：大半用例在故意把 README 或基线改坏，
断言它变红。一条恒为真的检查等于没有检查。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_REAL_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_REAL_BENCHMARK = (_ROOT / "docs" / "BENCHMARK.md").read_text(encoding="utf-8")
_REAL_FAQ = (_ROOT / "docs" / "FAQ.md").read_text(encoding="utf-8")
_REAL_POSITIONING = (_ROOT / "docs" / "POSITIONING.md").read_text(encoding="utf-8")
_BASELINE = _ROOT / "docs" / "benchmark_baseline.json"
_TABLE_ROW = re.compile(
    r"^\|\s*(?P<name>[A-Za-z0-9_]+)\s*\|(?P<events>\s*\d+\s*)\|"
    r"(?P<delivery>[^|]*)\|(?P<budget>[^|]*)\|(?P<full>[^|]*)\|"
    r"(?P<drain>[^|]*)\|(?P<logslop>[^|]*)\|", re.M)


def _load(script: str):
    path = _ROOT / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{path.stem}_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


prove = _load("prove.py")
bench = _load("benchmark_loghub.py")


# ---------------------------------------------------------------------------
# 先证真：基线存在、完整，且当前 README 与它一致
# ---------------------------------------------------------------------------
def test_baseline_file_exists():
    assert _BASELINE.is_file(), (
        f"缺 {_BASELINE.name}。跑：python scripts/benchmark_loghub.py "
        "--budgets 32000 --write-baseline")


def test_baseline_covers_every_dataset_in_the_readme_table():
    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    in_table = {m.group(1) for m in _TABLE_ROW.finditer(_REAL_README)}
    in_base = set(base["datasets"])
    assert in_table, "README 的基准表一行都没扫到 —— 正则或表头变了，先更新本测试"
    assert in_table <= in_base, (
        f"README 表里有、基线里没有：{sorted(in_table - in_base)}")


def test_baseline_records_both_baselines():
    """基线必须含两个对照臂，否则「胜负」无从对账。"""
    base = json.loads(_BASELINE.read_text(encoding="utf-8"))
    for name, rec in base["datasets"].items():
        assert rec["drain3"] is not None, f"{name} 缺 Drain3 对照"
        assert rec["logslop"] is not None, f"{name} 缺 LogsLop 对照"


def test_current_readme_matches_baseline():
    """先证真：照现状跑是绿的。后面几条变红才有意义。"""
    assert prove.claim_benchmark_table()["holds"] is True


# ---------------------------------------------------------------------------
# 可失效性：把展示层或基线改坏，门禁必须变红
# ---------------------------------------------------------------------------
@pytest.fixture
def gate(tmp_path, monkeypatch):
    """把 prove.ROOT 指向一个只含三份文档 + 基线的临时目录。

    三份都写：v2.11.0 之前 fixture 只造 README.md，于是门禁一改成
    「读全部受校文档」，两条用例就变成在测「BENCHMARK.md 不存在」——
    测试还在响，守的东西已经不是它写的那件事了。
    """

    def _run(readme_text: str, baseline_obj, benchmark_text=None,
             faq_text=None, positioning_text=None, omit=()) -> dict:
        (tmp_path / "README.md").write_text(readme_text, encoding="utf-8")
        (tmp_path / "docs").mkdir(exist_ok=True)
        if "docs/BENCHMARK.md" not in omit:
            (tmp_path / "docs" / "BENCHMARK.md").write_text(
                _REAL_BENCHMARK if benchmark_text is None else benchmark_text,
                encoding="utf-8")
        if "docs/FAQ.md" not in omit:
            (tmp_path / "docs" / "FAQ.md").write_text(
                _REAL_FAQ if faq_text is None else faq_text, encoding="utf-8")
        # v2.11.1：POSITIONING 也进了射程。四份都写 —— 只造三份的话，
        # 「docs/POSITIONING.md 不存在」会让每条「先证真」用例假红，
        # 而红的原因与它声称要验的那件事无关。
        if "docs/POSITIONING.md" not in omit:
            (tmp_path / "docs" / "POSITIONING.md").write_text(
                _REAL_POSITIONING if positioning_text is None
                else positioning_text, encoding="utf-8")
        if baseline_obj is not None:
            (tmp_path / "docs" / "benchmark_baseline.json").write_text(
                json.dumps(baseline_obj, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(prove, "ROOT", tmp_path)
        return prove.claim_benchmark_table()

    return _run


_REAL_BASE = json.loads(_BASELINE.read_text(encoding="utf-8"))


def _bump_bench_cell(doc_text: str, dataset: str, key: str,
                     delta: float = 0.2) -> str:
    """把某文档里基准表「某数据集的某一列」调高 ``delta`` 个百分点。

    按**表头算出的列号**定位，不认「第几列」——三份文档的表列数不同
    （README 7 列 / BENCHMARK 9 列 / FAQ 6 列），按位置改只会改到别的列，
    然后测试因为「改的不是那一列」而红，红的原因和它声称的完全不是一回事。
    """
    lines = doc_text.splitlines()
    for i, line in enumerate(lines):
        if not (line.lstrip().startswith("|") and "数据集" in line):
            continue
        if not prove._MD_SEP_RE.match(lines[i + 1] if i + 1 < len(lines) else ""):
            continue
        cols = prove._bench_columns(line)
        if key not in cols:
            continue
        for j in range(i + 2, len(lines)):
            if not lines[j].lstrip().startswith("|"):
                break
            cells = lines[j].split("|")
            if len(cells) <= cols[key] + 1 or cells[1].strip() != dataset:
                continue
            idx = cols[key] + 1          # split("|") 的 0 号是行首空串
            num = re.search(r"(-?[\d.]+)\s*%", cells[idx])
            assert num, f"{dataset} 的 {key} 列不是百分数：{cells[idx]!r}"
            cells[idx] = cells[idx].replace(
                num.group(1), f"{float(num.group(1)) + delta:.1f}", 1)
            lines[j] = "|".join(cells)
            return "\n".join(lines) + ("\n" if doc_text.endswith("\n") else "")
    raise AssertionError(f"{doc_text[:20]!r}… 里找不到 {dataset} 的 {key} 列")


def test_gate_is_not_a_noop(gate):
    """把 README 里一个百分数改掉 0.1pp，门禁必须变红。"""
    row = _TABLE_ROW.search(_REAL_README)
    assert row, "README 的基准表格式变了，先更新本测试"
    ds, _events, delivery, budget, _full, _d, _l = row.groups()

    def bump(cell: str) -> str:
        m = re.search(r"(-?[\d.]+)\s*%", cell)
        assert m, f"这一格不是百分数：{cell!r}"
        return f"{float(m.group(1)) + 0.1:.1f}%"

    broken_cell = bump(budget)
    at = _REAL_README.find(budget)
    broken = (_REAL_README[:at] + broken_cell
              + _REAL_README[at + len(budget):])

    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert ds in c["detail"], "失败信息必须点名是哪个数据集"
    assert "同预算" in c["detail"]


def _bump_cell(text: str, m, group: str, delta: float = 0.2) -> str:
    """把 ``text`` 里第 ``m`` 次匹配那一行的 ``group`` 单元格调高 ``delta`` 个百分点。

    **按 span 偏移拼接，不能按单元格文本替换。** 这一条踩了两层坑：

    1. 拿单元格文本去全文 ``find``：表里「72.7%」出现两次（Proxifier 的交付列
       与 Drain3 列），改到的是**交付列**。
    2. 改成 ``row.replace(cell, bumped_cell, 1)`` 仍然错：同一行里交付列与
       Drain3 列的文本**就是同一个** ``" 72.7%"``，``replace(..., 1)`` 改的
       还是第一处。

    两层都表现为「这条测试自己绿着，而它守的那一列压根没被改」——
    与「用自己举的例子跑一遍判据」是同一类错。

    另外注意 ``Match.span(group)`` 返回的是**相对整篇文本的绝对偏移**，
    不是相对 ``m.group(0)``；再叠一次 ``m.start()`` 会翻倍，落到无关文本上
    （这里的表现是「delivery 列不是百分数写法：' 3.12 三'」——
    '三' 字来自 CI 矩阵那一行）。
    """
    cell_start, cell_end = m.span(group)
    cell = text[cell_start:cell_end]
    num = re.search(r"(-?[\d.]+)\s*%", cell)
    assert num, f"{group} 列不是百分数写法：{cell!r}"
    bumped = cell.replace(num.group(1),
                           f"{float(num.group(1)) + delta:.1f}", 1)
    assert bumped != cell, f"{group} 列没能改掉"
    return text[:cell_start] + bumped + text[cell_end:]


def test_drift_in_every_column_is_caught(gate):
    """六个百分数每一列都要校 —— 只校第一列等于其余五列可以随便漂。"""
    m = next(_TABLE_ROW.finditer(_REAL_README))
    groups = ("delivery", "budget", "full", "drain", "logslop")
    labels = ("交付", "同预算", "全量", "Drain3", "LogsLop")

    # 先证真：不改任何东西时门禁是绿的
    assert gate(_REAL_README, _REAL_BASE)["holds"] is True

    for label, group in zip(labels, groups):
        broken = _bump_cell(_REAL_README, m, group)
        assert broken != _REAL_README, f"{label} 列没能落到 README 里"

        c = gate(broken, _REAL_BASE)
        assert c["holds"] is False, f"{label} 列漂移了 0.2pp 却没被发现"
        assert label in c["detail"], (
            f"{label} 列的失败信息没点名该列（说明改的不是那一列）："
            f"{c['detail']}")


def test_dataset_missing_from_baseline_is_caught(gate):
    """表里多出一个基线中没有的数据集 —— 不能因为「扫不到就不判」。"""
    broken = _insert_row_after_first_dataset(
        _REAL_README, "| Ghost | 10 | 50.0% | **50.0%** | 50.0% | 40.0% | 10.0% |")
    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert "Ghost" in c["detail"], c["detail"]


def test_event_count_drift_is_caught(gate):
    """标注事件数也是表里的一列，同样要对账。"""
    row = _TABLE_ROW.search(_REAL_README)
    ds, events, delivery, budget, full, drain, logslop = row.groups()
    bumped = str(int(events.strip()) + 1)
    at = _REAL_README.find(events)
    broken = (_REAL_README[:at] + f" {bumped} "
              + _REAL_README[at + len(events):])
    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert "标注事件" in c["detail"]


def test_missing_baseline_fails_loudly(gate):
    """基线文件缺失必须变红，且给出**可执行**的重跑命令。

    「没有基线所以跳过」是最危险的失败模式：门禁绿着，而它根本没比过。
    """
    c = gate(_REAL_README, None)
    assert c["holds"] is False
    assert "--write-baseline" in c["detail"]


def test_empty_readme_table_fails_loudly(gate):
    """表被整块删掉也不能绿 —— 正则扫不到行不等于「没有漂移」。"""
    stripped = re.sub(r"^\|[^\n]*\n", "", _REAL_README, flags=re.M)
    assert stripped != _REAL_README
    c = gate(stripped, _REAL_BASE)
    assert c["holds"] is False


# ---------------------------------------------------------------------------
# v2.11.0：同一张表有三份副本，一份都不能放过
#
# 这条门禁自己诞生的方式就是「只堵了半边」：v2.10.0 补了 README 的对账，
# 放着 docs/BENCHMARK.md 那份**仍挂着已撤回的 85.1%** 不管；
# v2.11.0 补 BENCHMARK.md 时差点又漏掉 docs/FAQ.md —— 是扫全仓才发现的。
# 下面四条把「第三份」「第四份」「不该算的表」「缺文件」各钉一次。
# ---------------------------------------------------------------------------
def _insert_row_after_first_dataset(doc_text: str, row: str) -> str:
    """把 ``row`` 插进基准表第一条数据行之后。

    插在**表内**，不是文末。这不是改测试迁就实现：v2.10.0 那版门禁用全文
    正则，于是文末孤零零一行 ``| Ghost | … |`` 也能被抓到，看起来守住了；
    改按表头锚定扫描后它就该看不见了 —— 而文末那行本来就不是表格的一部分，
    「靠正则扫全文才顺带抓到」是**不该依赖的行为**，比漏报更坏，
    它让人以为这条守卫覆盖了它其实没覆盖的场景。真会发生的失败是
    「有人往表里加了一个基线里没有的数据集」，这才是下面那条用例测的。
    """
    lines = doc_text.splitlines()
    for i, line in enumerate(lines):
        if not (line.lstrip().startswith("|") and "数据集" in line):
            continue
        if i + 1 >= len(lines) or not prove._MD_SEP_RE.match(lines[i + 1]):
            continue
        if not prove._bench_columns(line):
            continue
        lines.insert(i + 3, row)         # i+2 是第一条数据行
        return "\n".join(lines) + ("\n" if doc_text.endswith("\n") else "")
    raise AssertionError("文档里找不到基准表，先更新本测试")


def test_benchmark_doc_copy_is_guarded_too(gate):
    """docs/BENCHMARK.md 那份副本也必须逐格对账。"""
    assert gate(_REAL_README, _REAL_BASE)["holds"] is True, "先证真"
    broken = _bump_bench_cell(_REAL_BENCHMARK, "HPC", "budget32k", 0.2)
    assert broken != _REAL_BENCHMARK, "没能改到 BENCHMARK.md 的那一格"

    c = gate(_REAL_README, _REAL_BASE, benchmark_text=broken)
    assert c["holds"] is False, "README 干净、BENCHMARK.md 漂了 0.2pp 却没被发现"
    assert "docs/BENCHMARK.md" in c["detail"], c["detail"]
    assert "HPC" in c["detail"] and "同预算" in c["detail"], c["detail"]


def test_faq_doc_copy_is_guarded_too(gate):
    """第三份副本：docs/FAQ.md 的 Q2。"""
    assert gate(_REAL_README, _REAL_BASE)["holds"] is True, "先证真"
    broken = _bump_bench_cell(_REAL_FAQ, "Mac", "delivery", 0.2)
    assert broken != _REAL_FAQ

    c = gate(_REAL_README, _REAL_BASE, faq_text=broken)
    assert c["holds"] is False, "FAQ.md 漂了 0.2pp 却没被发现"
    assert "docs/FAQ.md" in c["detail"], c["detail"]
    assert "Mac" in c["detail"] and "交付" in c["detail"], c["detail"]


def test_faq_copy_has_no_event_column_and_is_still_checked(gate):
    """FAQ 那张表**没有**标注事件列 —— 不能因为缺一列就整张放过。

    这正是把表识别判据从「必须有标注事件列」放宽的原因：
    只认事件列的话，FAQ 这张同口径的表会被结构性放过。
    """
    faq_keys = {k for cols, _b, _h in prove._bench_tables(_REAL_FAQ) for k in cols}
    assert "events" not in faq_keys, "FAQ 那张表其实有事件列，判据白写了"
    assert {"delivery", "budget32k", "full", "drain3", "logslop"} <= faq_keys
    assert "docs/FAQ.md×1" in gate(_REAL_README, _REAL_BASE)["detail"]


def test_missing_benchmark_doc_fails_loudly(gate):
    """少一份副本必须变红 ——「扫不到就不判」是这个门禁的头号死法。"""
    c = gate(_REAL_README, _REAL_BASE, omit=("docs/BENCHMARK.md",))
    assert c["holds"] is False
    assert "docs/BENCHMARK.md 不存在" in c["detail"], c["detail"]


def test_non_benchmark_tables_are_not_mistaken_for_it():
    """以「数据集」开头的表**不等于**基准表。两类假表都真踩过。

    1. README「我们**不能**证明」那张表：它的**数据行**里同时出现
       「同预算档」「交付口径」「全量口径」，只看关键词认表头就会把
       整张表当基准表校一遍，报出「基线里没有的数据集：在所有日志形态上
       都优于 Drain3」—— 真漂移被淹没在这种噪音里。
    2. docs/BENCHMARK.md 的两张 **token 压缩比**表：列名里有 Drain3，
       但那一列的语义是 token 数，拿证据保留率去校只会报假漂移。
    """
    readme_tables = prove._bench_tables(_REAL_README)
    assert len(readme_tables) == 1, (
        f"README 里认出 {len(readme_tables)} 张基准表，应该只有 1 张")

    bench_tables = prove._bench_tables(_REAL_BENCHMARK)
    assert len(bench_tables) == 2, (
        f"docs/BENCHMARK.md 里认出 {len(bench_tables)} 张基准表，"
        "应该是主表 + 达成度表")
    keys = {k for cols, _b, _h in bench_tables for k in cols}
    assert keys == {"events", "delivery", "budget32k", "full",
                    "drain3", "logslop"}, f"认出的列字段不对：{sorted(keys)}"


def _documentation_files() -> list:
    """仓库里的**文档**：根目录与 ``docs/`` 下的 .md。

    为什么不用 ``rglob("*.md")`` 全扫：``dist/`` 是构建产物目录（.gitignore
    里），上一次发版留下的 ``RELEASE_NOTES_2.9.2.md`` 也躺着一张基准表。
    把它算进来，看门狗就会天天对着一个**根本不会发布**的文件报警 ——
    而「守的东西不存在」和「不守」一样糟：真出了第四份副本时，
    那条报警已经没人看了。

    为什么不用 ``git ls-files`` 逐个过滤：那条路要起子进程，而本仓库有一条
    门禁要求「按 UTF-8 解码子进程就必须给子进程设 PYTHONIOENCODING」。
    ``git ls-files`` 不是 Python 子进程，给它设这个变量没有任何意义 ——
    照做的结果是一句骗人的仪式，而仪式比没有约束更坏。
    「文档住在根目录和 docs/ 下」是本仓库**结构性**的约定（见 CONTRIBUTING），
    按结构划范围比维护一份产物目录黑名单可靠：黑名单会悄悄过期。
    """
    out = [p for p in _ROOT.glob("*.md")]
    docs_dir = _ROOT / "docs"
    if docs_dir.is_dir():
        out.extend(docs_dir.rglob("*.md"))
    return sorted(p.relative_to(_ROOT).as_posix() for p in out)


def test_every_benchmark_copy_is_listed():
    """看门狗：把基准表抄进**第四个文件**，必须有人发现。

    白名单本身是这条门禁最容易被忘掉的一环 —— 它诞生于「只堵了半边」，
    下一个半边就是「白名单只列了三份」。所以白名单要被守：
    文档里凡是含受校基准表的 .md，要么在 `_BENCH_DOCS` 里，
    要么在 `_BENCH_DOC_EXEMPT` 里写明**为什么**豁免。
    """
    listed = set(prove._BENCH_DOCS)
    verdicts = set(prove._BENCH_VERDICT_DOCS)
    offenders: dict = {}
    scanned = _documentation_files()
    assert len(scanned) >= 8, f"只认出 {len(scanned)} 份文档，扫描范围不对：{scanned}"
    for name in scanned:
        if name in prove._BENCH_DOC_EXEMPT:
            continue
        text = (_ROOT / name).read_text(encoding="utf-8")
        if name not in listed and prove._bench_tables(text):
            offenders[name] = "含基准表，但既不在 _BENCH_DOCS 也不在 _BENCH_DOC_EXEMPT"
        elif name not in verdicts and prove._VERDICT_RE.search(text):
            offenders[name] = ("含对外战绩，但既不在 _BENCH_VERDICT_DOCS "
                               "也不在 _BENCH_DOC_EXEMPT")
    assert not offenders, offenders


def test_exempt_docs_all_carry_a_reason():
    """豁免必须带理由 —— 没有理由的豁免等于给自己开后门。"""
    for name, why in prove._BENCH_DOC_EXEMPT.items():
        assert (why and len(why) >= 8), f"{name} 的豁免没写理由"
        assert (_ROOT / name).is_file(), f"{name} 不存在，豁免该删了"


# ---------------------------------------------------------------------------
# v2.11.0：胜负场次也要对账
#
# 「同预算档 8 胜 1 平 1 负」是 README 第一屏那句话，而它从来没有被校过。
# 后果已经发生：docs/BENCHMARK.md 有**三处**把交付口径写成「1 胜 9 负」——
# 多算一场胜、把 Proxifier 那场平（72.7% vs 72.7%）整个漏掉 ——
# 而同一仓库的 README 与 docs/FAQ.md 写的是正确的「0 胜 1 平 9 负」。
# 三份文档都在门禁射程之内，却互相矛盾。
# ---------------------------------------------------------------------------
def test_verdict_counts_match_measured_tallies():
    """先证真：基线里的 verdict_* 能重算出文档写的那三个场次。"""
    counts = prove._bench_verdict_counts(_REAL_BASE["datasets"])
    assert (counts["交付"]["胜"], counts["交付"]["平"], counts["交付"]["负"]) == (0, 1, 9)
    assert (counts["同预算"]["胜"], counts["同预算"]["平"], counts["同预算"]["负"]) == (8, 1, 1)
    assert (counts["全量"]["胜"], counts["全量"]["平"], counts["全量"]["负"]) == (8, 1, 1)
    # 对 LogsLop 只有同预算一个口径有实测（10 胜）
    logslop = prove._bench_verdict_counts(_REAL_BASE["datasets"], "LogsLop")
    assert (logslop["同预算"]["胜"], logslop["同预算"]["平"], logslop["同预算"]["负"]) == (10, 0, 0)


def test_verdict_written_as_win_without_tie_is_still_caught(gate):
    """「1 胜 9 负」这种**漏写平**的写法必须被抓住。"""
    broken = _REAL_BENCHMARK.replace(
        "交付口径 top20** | 产品默认 `top_n=20`，用户开箱即用**真拿到手**的 | **0 胜 1 平 9 负**",
        "交付口径 top20** | 产品默认 `top_n=20`，用户开箱即用**真拿到手**的 | **1 胜 9 负**",
    )
    assert broken != _REAL_BENCHMARK, "没找到要改的那一处，先更新本测试"
    c = gate(_REAL_README, _REAL_BASE, benchmark_text=broken)
    assert c["holds"] is False
    assert "胜负" in c["detail"], c["detail"]
    assert "0 胜 1 平 9 负" in c["detail"], c["detail"]
    # 报错必须**如实转述文档写的那个错数**，包括把漏写的平当成 0：
    # 「文档 0 胜 0 平 0 负」同样能让人知道出事了，但那是乱码不是诊断。
    assert "文档 1 胜 0 平 9 负" in c["detail"], c["detail"]


def test_every_verdict_mention_in_every_doc_is_caught(gate):
    """四份文档里的**每一处**胜负断言都要校，不能只校第一处。

    docs/BENCHMARK.md 同一句话在三处重复；只校第一处的门禁，
    和只对账 README 的门禁是同一种病。docs/POSITIONING.md 是
    v2.11.1 才纳管的第四份 —— 它重复着 README 的三个口径。

    注意 README 的三处胜负**挤在同一行**（第 59 行那句话里），
    所以「一行一处」的假设在这里不成立 —— 必须三档口径都校到。
    """
    texts = {"README.md": _REAL_README, "docs/BENCHMARK.md": _REAL_BENCHMARK,
             "docs/FAQ.md": _REAL_FAQ, "docs/POSITIONING.md": _REAL_POSITIONING}
    assert gate(_REAL_README, _REAL_BASE)["holds"] is True, "先证真"

    for doc, text in texts.items():
        _probs, seen, n, _notes = prove._bench_verdict_problems(
            doc, text, _REAL_BASE["datasets"])
        assert n >= 3, f"{doc} 只认出 {n} 处胜负断言，口径或写法变了"
        assert set(seen) == {"交付", "同预算", "全量"}, (
            f"{doc} 只认出 {sorted(seen)} 三个口径，少了哪一档？")
        # 把这一份文档里的每一处都改坏一次：只要有一处漏校，这条就红
        lines = text.splitlines()
        targets = sorted({x for v in seen.values() for x in v})
        for lineno in targets:
            assert prove._VERDICT_RE.search(lines[lineno - 1]), doc
        broken_lines = list(lines)
        for lineno in targets:
            broken_lines[lineno - 1] = prove._VERDICT_RE.sub(
                "0 胜 0 平 9 负", broken_lines[lineno - 1])
        broken = "\n".join(broken_lines)
        assert broken != text

        kwargs = {"README.md": {}, "docs/BENCHMARK.md": {"benchmark_text": broken},
                  "docs/FAQ.md": {"faq_text": broken},
                  "docs/POSITIONING.md": {"positioning_text": broken}}
        c = (gate(broken, _REAL_BASE) if doc == "README.md"
             else gate(_REAL_README, _REAL_BASE, **kwargs[doc]))
        assert c["holds"] is False, (
            f"{doc} 的 {targets} 改成「0 胜 0 平 9 负」却没被发现")


def test_verdict_check_reports_nothing_for_clean_docs():
    """先证真：受校文档当前的胜负断言全部正确。"""
    for doc, text in (("README.md", _REAL_README),
                      ("docs/BENCHMARK.md", _REAL_BENCHMARK),
                      ("docs/FAQ.md", _REAL_FAQ),
                      ("docs/POSITIONING.md", _REAL_POSITIONING)):
        probs, _seen, n, _notes = prove._bench_verdict_problems(
            doc, text, _REAL_BASE["datasets"])
        assert probs == [], f"{doc}：{probs}"
        assert n >= 3, doc


def test_verdict_check_survives_unrelated_wording():
    """没写「胜/平/负」的行不能被误判 —— 误报的门禁比没有门禁更糟。"""
    benign = [
        "| 数据集 | 交付上限 | 达成度 |\n|---|---:|---:|\n| HPC | 27.0% | 75% |\n",
        "交付口径的那一栏很难看，算法输了 9 个，不是模型更差。\n",
        "全量口径见结果一。\n",
    ]
    for i, text in enumerate(benign):
        probs, _seen, n, _notes = prove._bench_verdict_problems(
            f"t{i}", text, _REAL_BASE["datasets"])
        assert probs == [], f"第 {i} 段被误报：{probs}"
        assert n == 0, f"第 {i} 段不该认出胜负断言，却认出 {n} 处"


def test_a_tie_left_implicit_is_reported_as_zero_ties():
    """没写「平」的那一场，报错里必须显示成 0 平，而不是别的数。"""
    text = "交付口径 2 胜 8 负 —— 这个说法漏掉了平局。\n"
    probs, _seen, n, _notes = prove._bench_verdict_problems(
        "t", text, _REAL_BASE["datasets"])
    assert n == 1
    assert len(probs) == 1 and probs[0], "漏写平的写法必须被抓住"
    assert "文档 2 胜 0 平 8 负" in probs[0], probs[0]


# ---------------------------------------------------------------------------
# v2.11.1：胜负判据的三处「测量环节静默失能」
#
# 三处都在同一个位置假设上 —— v2.11.0 判据是
# `line.find(口径标签)` 之后才开始找场次，于是：
#   (a) 对照组写在标签**前面**（「同预算档对 LogsLop：10 胜 0 平 0 负」）
#       会被拿去和 Drain3 的数比 → **假红**，而假红会把真漂移淹没；
#   (b) 标签写在场次**后面**（「……0 胜 1 平 9 负（交付口径）。」）
#       一处都校不到 → **漏校**，一句对外战绩被判成「没有断言」；
#   (c) 同一行两个场次，v2.11.0 是「有一个对得上就算过」
#       → 一行里那个写错的数被同行写对的数**顺手放过**。
# (a) 和 (b) 是同一处代码的两个方向，必须一起钉。
# ---------------------------------------------------------------------------
def test_verdict_against_logslop_is_not_judged_with_drain3_numbers():
    """(a) 对 LogsLop 的战绩不能拿 Drain3 的数字去比。"""
    probs, _seen, n, notes = prove._bench_verdict_problems(
        "t", "同预算档对 **LogsLop**：10 胜 0 平 0 负。\n", _REAL_BASE["datasets"])
    assert probs == [], f"拿错尺子量出来的假红：{probs}"
    assert n == 1, "这一处应当被校到"
    assert notes["unverifiable"] == []


def test_verdict_label_written_after_the_tally_is_still_checked():
    """(b) 标签在场次**后面**也必须校到，且错的值要判红。"""
    ok = "对 Drain3 的战绩：0 胜 1 平 9 负（交付口径）。\n"
    bad = "对 Drain3 的战绩：0 胜 0 平 9 负（交付口径）。\n"
    _p, _s, n_ok, _n = prove._bench_verdict_problems("t", ok, _REAL_BASE["datasets"])
    p_bad, _s, n_bad, _n = prove._bench_verdict_problems(
        "t", bad, _REAL_BASE["datasets"])
    assert n_ok == 1, "标签在句尾括号里时一处都没校到 —— 这正是 v2.11.0 的漏校"
    assert n_bad == 1 and p_bad, p_bad


def test_verdict_with_no_measured_combination_is_reported_not_silently_green():
    """基线里没有的「口径 × 对照组」组合必须**单列出来**。

    **「全是 0」和「没测过」是两件事。** 缺 `verdict_full_logslop` 时重算
    出来是 0/0/0，拿它去比文档里的「10 胜 0 平 0 负」会得到一条永远红的门禁；
    当成真的 0 又会永远绿。两种都是这个项目最熟的那种病。
    """
    probs, _seen, n, notes = prove._bench_verdict_problems(
        "t", "对 LogsLop：全量口径 10 胜 0 平 0 负。\n", _REAL_BASE["datasets"])
    assert probs == [], "无实测可对账时不能判红（会永远红）"
    assert n == 0
    assert notes["unverifiable"], "无实测可对账必须报出来，不能默默放过"


def test_a_wrong_tally_next_to_a_right_one_is_still_caught():
    """(c) **同一口径**写两次、一个对一个错 —— 错的那个必须判红。

    这里必须是**同一个口径**的两处场次：v2.11.0 的「只要有一个对得上
    就算过」正是被这个形状抓住的（实测：旧代码对下面这句 `problems=[]`）。

    写成两个**不同**口径的话，这条测试在旧代码上也照样通过 —— 那是装饰，
    而且装饰得很有说服力。钉它的那次变异（把逐个校改回「有对上的就放过」）
    在旧式输入下是全绿的。
    """
    text = "交付口径第一轮 0 胜 1 平 9 负，重跑后写成 0 胜 0 平 9 负。\n"
    probs, _seen, n, _notes = prove._bench_verdict_problems(
        "t", text, _REAL_BASE["datasets"])
    assert n == 2, f"只认出 {n} 处"
    assert len(probs) == 1 and "0 胜 0 平 9 负" in probs[0], probs


def test_verdict_unlabeled_claims_are_counted_not_judged():
    """历史叙述里没有口径的「7 胜 2 负」不该被猜一个尺子去量 —— 但要计数。

    猜错就是假红；而完全不数，就看不出这条门禁的覆盖面有多大。
    """
    probs, _seen, n, notes = prove._bench_verdict_problems(
        "t", "结论从「5 个全部领先」变成「9 个 7 胜 2 负」。\n",
        _REAL_BASE["datasets"])
    assert probs == [], probs
    assert n == 0
    assert notes["unlabeled"] == 1, notes


# ---------------------------------------------------------------------------
# v2.11.1 第三轮：判据自己的盲区（外部复核实测出来的）
# ---------------------------------------------------------------------------
def test_tallies_separated_by_chinese_punctuation_are_still_recognised():
    """`0 胜、1 平、9 负` 曾经**整句静默消失** —— checked=0 且 unlabeled=0。

    正则只认空格，于是改个标点整条断言就从射程里溜走，门禁一声不吭。
    这是这一整轮要治的病的最后一种形态：不是量错，是**根本没量**。
    """
    for text in ("交付口径 0 胜、1 平、9 负。",
                 "交付口径 0 胜 / 1 平 / 9 负。",
                 "交付口径 0 胜，1 平，9 负。"):
        probs, _seen, n, notes = prove._bench_verdict_problems(
            "t", text + "\n", _REAL_BASE["datasets"])
        assert n == 1, f"{text!r} 一处都没校到（unlabeled={notes['unlabeled']}）"
        assert probs == [], probs


def test_thousands_separator_in_a_tally_is_not_read_as_a_smaller_number():
    """`1,210 胜` 曾经被读成 `210 胜` —— 而同一段对 events 是会剥逗号的。

    指标列剥了逗号、场次列没剥，同一个文件里两套口径。
    """
    _p, _s, n, _notes = prove._bench_verdict_problems(
        "t", "全量口径 1,210 胜 3 平 40 负。\n", _REAL_BASE["datasets"])
    assert n == 1
    claims = prove._verdict_claims("全量口径 1,210 胜 3 平 40 负。\n")
    assert claims[0][1] == (1210, 3, 40), claims


def test_a_formula_column_header_is_not_mistaken_for_a_caliber():
    """「交付上限」是**公式列**不是实测口径 —— 表格侧早躲了，胜负侧当时没躲。

    `| HPC | 27.0% | 8 胜 1 平 1 负 | 交付上限 27.0% |` 曾把场次判给
    「交付」而报假红。假红会把真漂移淹没在噪音里，代价不低于漏红。
    """
    line = "| HPC | 27.0% | 8 胜 1 平 1 负 | 交付上限 27.0% |\n"
    probs, _seen, n, _notes = prove._bench_verdict_problems(
        "t", line, _REAL_BASE["datasets"])
    assert probs == [], probs
    assert n == 0, "「交付上限」被当成了口径标签"


def test_a_partially_filled_baseline_does_not_produce_a_false_red():
    """基线里**只有一个**数据集缺 verdict_delivery 时不许假红。

    `any()` 会把「测了 9 个」当成「测过了」，于是重算出 0 胜 1 平 8 负，
    然后反过来指责文档写错了 —— 而且怪错了对象（文档是对的）。
    """
    base = json.loads(json.dumps(_REAL_BASE))
    for rec in base["datasets"].values():
        if rec.get("verdict_delivery") == "平":      # 那个平局数据集
            rec.pop("verdict_delivery")
    probs, _seen, n, notes = prove._bench_verdict_problems(
        "t", "对 Drain3 的战绩：交付口径 0 胜 1 平 9 负。\n", base["datasets"])
    assert probs == [], f"残缺基线造出了假红：{probs}"
    assert n == 0
    assert notes["unverifiable"], "残缺必须被报成「无实测可对账」"


def test_an_out_of_domain_tally_value_is_treated_as_unmeasured():
    """基线里出现 `"tie"` 之类时属于「量不出来」，不是「量出来是 0」。"""
    base = json.loads(json.dumps(_REAL_BASE))
    for rec in base["datasets"].values():
        if rec.get("verdict_delivery") == "平":
            rec["verdict_delivery"] = "tie"
    probs, _seen, n, notes = prove._bench_verdict_problems(
        "t", "交付口径 0 胜 1 平 9 负。\n", base["datasets"])
    assert probs == [], f"把 'tie' 当成 0 会报假红：{probs}"
    assert notes["unverifiable"]


def test_a_missing_events_field_does_not_crash_prove():
    """基线里缺 `events` 曾经直接 KeyError 打死整个 prove.py。

    `gather()` 没有兜底，一条门禁因为数据残缺崩掉、带 traceback——
    「崩掉虽然也是红的，却红得没有信息」，而且它把**其他**主张一起带走了。
    """
    base = json.loads(json.dumps(_REAL_BASE))
    base["datasets"]["HPC"].pop("events", None)
    out = prove._bench_doc_problems("README.md", _REAL_README, base["datasets"])
    problems = out[3]
    assert not any("events" in p and "标注事件" in p for p in problems), problems
    notes = out[4]
    assert notes["cells_unmeasured"], "缺实测值的那几格必须被报出来"


def test_an_unmeasured_comparison_cell_is_reported_not_silently_skipped():
    """对照臂是 None 时曾经静默 `continue` —— 160 格和 157 格长得一模一样。"""
    base = json.loads(json.dumps(_REAL_BASE))
    for rec in base["datasets"].values():
        rec["logslop"] = None
    notes = prove._bench_doc_problems("README.md", _REAL_README,
                                      base["datasets"])[4]
    assert notes["cells_unmeasured"], "少校了几格却一声不吭"


# ---------------------------------------------------------------------------
# v2.11.1：列名改一个字 → 整列静默脱管（第四轮外部复核 F1）
# ---------------------------------------------------------------------------
def _rename_bench_header_cell(text: str, old: str, new: str) -> str:
    """把基准表**表头行**里的某一格改名，不碰数据行。

    两处都必须和 `_bench_columns` 保持一致，否则这个夹具在造一个
    「被测代码根本没认出来的列」：

    - 按**内容**定位而不是 `replace(old, new, 1)`：README 的表头写的是
      `交付 top20` / `**同预算 32k**` / `本项目(全量)`，而「交付」这个词
      在正文、图例、散文里都出现过，replace 第一个命中的是哪一处完全
      取决于文档改版顺序 —— 而「改坏的门禁还在绿」最省事的造法就是改错地方。
    - 匹配用**子串**而不是相等，与 `_bench_columns` 一样；相等匹配根本
      命不中表头。
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not (line.lstrip().startswith("|") and "数据集" in line):
            continue
        if i + 1 >= len(lines) or not prove._MD_SEP_RE.match(lines[i + 1]):
            continue
        if not prove._bench_columns(line):
            continue        # 以「数据集」开头但列义不同的表（token 压缩比等）
        parts = line.strip().strip("|").split("|")
        hit = False
        for j, p in enumerate(parts):
            if old in p.strip().replace("**", ""):
                parts[j] = p.strip().replace("**", "").replace(old, new)
                hit = True
        assert hit, f"基准表表头里找不到含 {old} 的格子"
        lines[i] = "|" + "|".join(parts) + "|"
        return "\n".join(lines)
    raise AssertionError("没找到基准表表头")


def test_a_renamed_column_header_fails_loudly(gate):
    """列名改一个字 → 整列不再对账 → 门禁必须变红。

    修复前的干净对照（三行都实测过）：

        原始 README                        对账 50 格 / 问题 0
        只把 10 个交付值改成 99.9%         对账 50 格 / 问题 10   ← 全响
        同样改坏 + 表头「交付」→「投递」    对账 40 格 / 问题 1    ← 整列脱管

    根因是 `if key not in cols: continue` —— 列缺失既不计数，也不进
    `cells_unmeasured`，于是 10 个**明知错误**的数字悄悄退出射程，而
    docs/BENCHMARK.md 的同一份副本仍写着 72.7%：仓库自相矛盾，
    门禁报「全部一致」。

    这是 AGENTS.md 记了四次的「扫了 N 个却报通过」，只是这次
    N 从 0（扫不到文件）变成了 50（扫到了，但少校了一整列）。
    """
    broken = _rename_bench_header_cell(_REAL_README, "交付", "投递")
    assert "投递" in broken, "夹具前提：表头确实被改了"
    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False, "列名改了却判通过 = 整列脱管且无人叫"
    assert "缺 delivery" in c["detail"], c["detail"]


def test_a_renamed_secondary_header_also_fails(gate):
    """不只交付：同预算/全量被改名同样判红（三个口径同量纲，一起被要求）。"""
    broken = _rename_bench_header_cell(_REAL_README, "全量", "完量")
    c = gate(broken, _REAL_BASE)
    assert c["holds"] is False
    assert "缺 full" in c["detail"], c["detail"]


def test_a_table_may_still_report_only_one_caliber():
    """**不得假红**：docs/BENCHMARK.md 的第二张表本来就只报交付一列。

    判据刻意是「认出 ≥2 个保留率口径才要求报全」而不是「每张表都得三列」——
    真实文档里就有只认出一个口径的合法表（实测 `own=['delivery']`）。
    """
    _rows, _checked, _v, _problems, notes = prove._bench_doc_problems(
        "docs/BENCHMARK.md", _REAL_BENCHMARK, _REAL_BASE["datasets"])
    assert not notes["cells_misnamed"], "只报一个口径的合法表被误判成拼错"


def test_every_real_bench_table_reports_all_three_calibers():
    """反向对照：新判据**不假红**的依据本身要有人守。

    一旦将来有人在真实文档里加一张「≥2 个口径却缺一个」的表，这里立刻红 ——
    而不是让新判据在真实文档上默默失效、直到某天又有一个数字脱管。
    """
    checked = 0
    for doc in ("README.md", "docs/BENCHMARK.md", "docs/FAQ.md"):
        text = (_ROOT / doc).read_text(encoding="utf-8")
        for cols, _body, _header in prove._bench_tables(text):
            own = [k for _t, k in prove._BENCH_OWN if k in cols]
            if len(own) >= 2:
                checked += 1
                assert len(own) == 3, f"{doc}: 保留率口径只认出 {own}"
    assert checked >= 3, f"只认出 {checked} 张三口径表，夹具前提变了"


# ---------------------------------------------------------------------------
# v2.11.1：「合计」行重算对账 —— 第四轮外部复核 D1
# ---------------------------------------------------------------------------
def test_the_aggregate_row_is_recomputed_not_skipped():
    """合计行的数字是**手算**的，此前列在跳过名单里没人校，已错 11.8pp。

    修复前实测：文档写 282.4%，从基线重算是 270.6%，而 `prove.py` 全绿。
    引入它的那笔提交（d299aeb）标题恰恰是「把『交付口径已达天花板』从
    断言变成脚本能复现的数字」—— 它想治的病在原地复发了。
    """
    broken = _REAL_BENCHMARK.replace("**270.6%**", "**282.4%**")
    assert broken != _REAL_BENCHMARK, "夹具前提：合计行没改到"
    _r, _c, _v, problems, _n = prove._bench_doc_problems(
        "docs/BENCHMARK.md", broken, _REAL_BASE["datasets"])
    assert any("合计 交付实测" in p and "282.4" in p for p in problems), \
        problems


def test_the_real_aggregate_row_agrees_with_the_recomputation():
    """对照：真实文档的合计行必须**本来就**对得上重算（否则夹具前提变了）。"""
    _r, _c, _v, problems, _n = prove._bench_doc_problems(
        "docs/BENCHMARK.md", _REAL_BENCHMARK, _REAL_BASE["datasets"])
    assert not any("合计" in p for p in problems), problems


def test_aggregate_row_accepts_honest_integer_rounding():
    """**不得假红**：达成度合计写整数 82%，重算 82.2% —— 诚实的取整。

    这里刻意不用固定容差：对整数声称值，0.05pp 容差天然会把正确写法判红
    （差 0.2pp），而**一条会误报的门禁比没有门禁更糟**（真漂移被淹没在噪音里）。
    """
    header = ["数据集", "标注事件", "交付实测", "交付上限", "达成度"]
    ds = _REAL_BASE["datasets"]

    def _check(claimed_att: str):
        return prove._agg_row_problems(
            "x.md", 1, "合计",
            ["合计", "", "270.6%", "343.2%", claimed_att], {}, header, ds)

    assert _check("82%") == [], "整数 82% 对重算 82.2% 是诚实取整，不该报红"
    assert _check("**82%**") == [], "加粗不该影响判定"
    bad = _check("83%")
    assert any("达成度" in p for p in bad), \
        f"83% 明明不是 82.2% 的诚实取整，却放过了（判据恒真 = 装饰）：{bad}"


def test_bench_n_del_matches_the_product_default():
    """`_BENCH_N_DEL` 必须与 `constants.DEFAULT_TOP_N` 同值。

    「交付上限 = min(报告给几个簇, 事件数) / 事件数」—— 报告给几个簇正是
    产品默认值。两者一旦漂了，重算出来的上限就是**另一个口径的上限**，
    而它看起来与文档里的数毫无区别。
    """
    spec = importlib.util.spec_from_file_location(
        "lac_constants", _ROOT / "log_ai_compressor" / "constants.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert prove._BENCH_N_DEL == mod.DEFAULT_TOP_N, (
        f"prove._BENCH_N_DEL={prove._BENCH_N_DEL} 与 "
        f"DEFAULT_TOP_N={mod.DEFAULT_TOP_N} 不同 —— 交付上限会算成另一个口径")


# ---------------------------------------------------------------------------
# v2.11.1：表头还在但数据行没了 / 少了某一行
# ---------------------------------------------------------------------------
def _faq_without_data_rows() -> str:
    lines = _REAL_FAQ.splitlines()
    head = next(i for i, l in enumerate(lines)
                if l.lstrip().startswith("|") and "数据集" in l
                and prove._MD_SEP_RE.match(lines[i + 1])
                and prove._bench_columns(l))
    j = head + 2
    while j < len(lines) and lines[j].lstrip().startswith("|"):
        j += 1
    return "\n".join(lines[:head + 2] + lines[j:])


def test_benchmark_table_with_header_but_no_rows_fails_loudly(gate):
    """表头保留、数据行删空 → 必须判红。

    v2.11.0 实测判通过：`rows=0 cells=0 problems=0`。
    `_bench_tables` 照样把它认成一张表（判据看的是表头 + 分隔行），
    而逐格循环一行都不跑 —— 「表在、行没了」比「表没了」更难发现。
    """
    stripped = _faq_without_data_rows()
    assert "\n|" in stripped, "前提不成立：没造出只有表头的表"
    c = gate(_REAL_README, _REAL_BASE, faq_text=stripped)
    assert c["holds"] is False
    assert "只剩表头" in c["detail"], c["detail"]


def test_a_single_missing_dataset_row_fails_loudly(gate):
    """只删掉**一行**（HPC）也必须判红 —— 这是比删空表更常见的漏法。

    每一行单独看都对得上，表也齐，于是门禁绿；而报告里就此少了一个负例。
    """
    lines = [l for l in _REAL_FAQ.splitlines()
             if not (l.lstrip().startswith("|") and "HPC" in l and "%" in l)]
    assert len(lines) < len(_REAL_FAQ.splitlines()), "前提不成立：没找到 HPC 行"
    c = gate(_REAL_README, _REAL_BASE, faq_text="\n".join(lines))
    assert c["holds"] is False
    assert "缺数据集 HPC 的行" in c["detail"], c["detail"]


def test_doc_without_a_table_is_not_charged_for_missing_rows():
    """先证真的方向：只有战绩、没有表的文档不得报「缺数据集的行」。

    POSITIONING.md 没有基准表，若缺行检查不随 check_tables 一起退场，
    它就会凭空报出 10 条假红 —— **一条会误报的门禁比没有门禁更糟**。
    """
    probs = prove._bench_doc_problems(
        "docs/POSITIONING.md", _REAL_POSITIONING, _REAL_BASE["datasets"],
        check_tables=False)[3]
    assert probs == [], probs


# ---------------------------------------------------------------------------
# --write-baseline 的拒绝条件：残缺基线比没有基线更糟
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv, why", [
    (["--budgets", "32000", "--write-baseline", "--no-drain3"], "缺 Drain3 对照臂"),
    (["--budgets", "32000", "--write-baseline", "--no-logslop"], "缺 LogsLop 对照臂"),
    (["--budgets", "32000", "--write-baseline", "--datasets", "HPC"], "只跑了子集"),
])
def test_write_baseline_refuses_incomplete_runs(argv, why, monkeypatch, capsys):
    """前置条件在**跑之前**就查 —— 跑完两小时才发现白跑更糟。"""
    monkeypatch.setattr(sys, "argv", ["benchmark_loghub.py"] + argv)
    assert bench.main() == 1, f"{why} 时居然写出了基线"
    assert "拒绝写基线" in capsys.readouterr().out
