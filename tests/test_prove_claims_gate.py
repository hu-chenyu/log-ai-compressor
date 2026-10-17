# -*- coding: utf-8 -*-
"""自证脚本自身的一致性 —— 门禁本身也得有人看着。

背景：v2.9.0 加了第 10 条主张（接入层只经 service 触达 core/export），
README 两处却仍写「9 条」，而**全部门禁绿着** —— 因为
`claim_readme_metrics` 只校「生产代码」那一行的行数与模块数，
「自证脚本有 N 条主张」这个 README 数字**没有任何东西在校**。

一个数字有两处来源却只校一处，和 v2.8.1 那次（架构树只数总量不看名单）
是同一个形状。v2.9.1 把它并进同一条门禁。

本文件的作用是证明这条新门禁**真的会响**。一条恒为真的检查等于没有检查，
所以这里大半用例都在**故意把 README 改坏**，断言门禁变红。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_REAL_README = (_ROOT / "README.md").read_text(encoding="utf-8")
_MARKER = "条**主张"
# 「1,043 个测试」与「**1,043 用例**」两种写法都算测试数
_COUNT_RE = re.compile(r"([\d,]+)\s*(?:个测试|用例)")
_DOCS = ("README.md", "docs/FAQ.md")


def _load_prove():
    """按文件路径加载 scripts/prove.py —— 它是脚本，不在包里，import 不到。"""
    path = _ROOT / "scripts" / "prove.py"
    spec = importlib.util.spec_from_file_location("prove_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prove_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


prove = _load_prove()
_REAL_METRICS = prove._code_metrics()
_REAL_COUNT = len(prove._CLAIM_FNS)
_REAL_RATIO = prove._count_ratio()


def _rewrite(text: str, old: str, new: str) -> str:
    """把**全部**出现处换掉，并断言确实换到了 —— 防止测试自己悄悄空转。"""
    assert old in text, f"README 里找不到 {old!r}，测试前提已失效"
    return text.replace(old, new)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """把 prove.ROOT 指向一个只含 README.md 的临时目录，返回该门禁函数。

    同时把 _code_metrics 钉成**真实**值：临时目录里没有 log_ai_compressor/，
    不钉住的话行数与模块数会算成 0，门禁会因为另一个原因变红 ——
    那样测的就不是「条数对不对」，而是「目录存不存在」。
    """
    monkeypatch.setattr(prove, "_code_metrics", lambda: dict(_REAL_METRICS))

    def _run(readme_text: str) -> dict:
        (tmp_path / "README.md").write_text(readme_text, encoding="utf-8")
        monkeypatch.setattr(prove, "ROOT", tmp_path)
        return prove.claim_readme_metrics()

    return _run


class TestReadmeLineRatioIsGated:
    """`docs/FAQ.md` 的「测试/主代码行数比 ≈X」曾写 1.1，实测 1.37。

    v2.11.0 的复核顺手量到这件事，而**没有任何门禁校它** —— 与本文件
    开头写的 v2.9.0 那次（README 写「9 条主张」没人校）是同一个病：
    一个对外数字漂了，全部门禁绿着。差得不多不代表不算错：
    **「大概差不多」写成实测值，就是这个项目明令禁止的那件事。**
    """

    @staticmethod
    def _pin(monkeypatch) -> None:
        """把行数与比值都钉成**真实**值。

        不钉的话，ROOT 指到临时目录后 `_code_metrics()` 会算成 0 行 0 模块
        —— 门禁会「因为另一个原因变红」，那样测的就不是「比值对不对」，
        而是「目录存不存在」。
        """
        monkeypatch.setattr(prove, "_code_metrics", lambda: dict(_REAL_METRICS))
        monkeypatch.setattr(prove, "_count_ratio", lambda: _REAL_RATIO)

    def _run(self, tmp_path, monkeypatch, faq_text: str) -> dict:
        self._pin(monkeypatch)
        (tmp_path / "README.md").write_text(_REAL_README, encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "FAQ.md").write_text(faq_text, encoding="utf-8")
        monkeypatch.setattr(prove, "ROOT", tmp_path)
        return prove.claim_readme_metrics()

    def test_current_docs_pass(self):
        """先证真：照现状跑是绿的。"""
        assert prove.claim_readme_metrics()["holds"] is True

    def test_the_ratio_is_really_what_the_docs_say(self):
        """先证真：文档里那个数确实是当前实测值。

        用 `prove._RATIO_RE` 而不是在本文件里**再抄一遍正则** ——
        抄一份的那版，正则被改坏时它照样绿（实测：把 prove 的正则打坏，
        同类另两条测试都红，只有它 PASSED），等于装饰。
        正则真坏掉时 `claimed` 会是空列表，下面那句 assert 就是它的牙齿。
        """
        faq = (_ROOT / "docs" / "FAQ.md").read_text(encoding="utf-8")
        claimed = [float(x) for x in prove._RATIO_RE.findall(faq)]
        assert claimed, "FAQ 里找不到行数比 —— 正则坏了，或格式变了"
        for c in claimed:
            # 用门禁自己的判据，别在这儿再抄一遍
            assert prove._is_honest_rounding(str(c), _REAL_RATIO), (c, _REAL_RATIO)

    def test_the_ratio_gate_cannot_be_appeased_by_a_loose_tolerance(self):
        """「1.37 对 1.3807」不是诚实的取整，门禁必须判红。

        这条不是假设出来的：v2.11.0 用固定容差 0.01，实测 1.3807 写成
        「≈1.37」**能过**（差 0.0099 < 0.01）—— 而那正是本次修复的起因。
        而「1.4 对 1.3807」是诚实的取整，必须放过。
        """
        assert prove._is_honest_rounding("1.38", 1.3807)
        assert not prove._is_honest_rounding("1.37", 1.3807)
        assert prove._is_honest_rounding("1.4", 1.3807)

    def test_every_doc_mentioning_the_ratio_is_listed(self):
        """看门狗：把「行数比」抄进第五个文件，必须有人发现。

        `_BENCH_DOCS` 有这么一条（`test_every_benchmark_copy_is_listed`），
        本门禁起初没有 —— 而 `_METRIC_DOCS` 三份里有两份根本不含「行数比」，
        名单写错只会静默少扫一份，没有任何东西会叫。
        """
        listed = set(prove._METRIC_DOCS) | set(prove._METRIC_DOCS_EXEMPT)
        offenders: dict = {}
        for p in sorted(_ROOT.rglob("*.md")):
            rel = p.relative_to(_ROOT).as_posix()
            if rel in listed or rel.split("/")[0] in ("dist", ".git"):
                continue
            text = p.read_text(encoding="utf-8")
            if re.search(r"行数比[^\n]*\d", text):
                offenders[rel] = "写了行数比却不在 _METRIC_DOCS / _METRIC_DOCS_EXEMPT 里"
        assert not offenders, offenders

    def test_every_metric_exemption_carries_a_reason(self):
        """豁免必须写理由 —— 没有理由的豁免等于给自己开后门。"""
        for name, why in prove._METRIC_DOCS_EXEMPT.items():
            assert why and len(why) >= 8, f"{name} 的豁免没写理由"
            assert (_ROOT / name).is_file(), f"{name} 不存在，豁免该删了"

    def test_wrong_ratio_in_a_doc_fails_loudly(self, tmp_path, monkeypatch):
        c = self._run(tmp_path, monkeypatch,
                      "- 测试/主代码行数比 ≈9.9（同为非空非注释行口径）\n")
        assert c["holds"] is False, "FAQ 里的行数比写错了却没被发现"
        assert "9.9" in c["detail"], c["detail"]
        assert str(_REAL_RATIO) in c["detail"], c["detail"]

    def test_a_ratio_off_by_more_than_a_hundredth_fails(self, tmp_path,
                                                       monkeypatch):
        """容差只给显示舍入留 0.01；差 0.02 就该判红。"""
        c = self._run(tmp_path, monkeypatch,
                      f"- 测试/主代码行数比 ≈{_REAL_RATIO + 0.02:.2f}\n")
        assert c["holds"] is False, "差 0.02 却放过 —— 容差被悄悄放宽了"

    def test_a_doc_without_the_phrase_is_not_invented(self, tmp_path,
                                                      monkeypatch):
        """没写「行数比」的文档不得被凭空判红 —— 假红比漏红更糟。"""
        c = self._run(tmp_path, monkeypatch, "这里没有任何行数比。\n")
        assert c["holds"] is True, c["detail"]


class TestReadmeClaimCountIsGated:
    """README 写「自证脚本有 N 条主张」，那个 N 必须等于实际登记的条数。"""

    def test_current_readme_passes(self):
        """先证真：照现状跑是绿的。后面几条变红才有意义。"""
        assert prove.claim_readme_metrics()["holds"] is True

    def test_gate_is_not_a_noop(self, gate):
        """故意把条数写错，门禁必须变红 —— 这是本文件存在的主要理由。"""
        c = gate(_rewrite(_REAL_README, f"**{_REAL_COUNT} 条**主张",
                          f"**{_REAL_COUNT + 989} 条**主张"))
        assert c["holds"] is False
        assert str(_REAL_COUNT) in c["detail"], "失败信息必须报出实测条数"

    def test_every_occurrence_is_checked(self, gate):
        """同一个数字出现两次，就两处都要校。

        只校第一处 = 改第二处能溜过去。v2.9.0 的漂移恰恰是这种形状：
        §6.2 表格与 §8 正文各写了一个 9，只改一处不该让门禁变绿。
        """
        at = _REAL_README.rfind(f"**{_REAL_COUNT} 条**主张")
        assert at > 0, "README 里至少要有第二处待校"
        broken = (_REAL_README[:at]
                  + f"**{_REAL_COUNT - 3} 条**主张"
                  + _REAL_README[at + len(f"**{_REAL_COUNT} 条**主张"):])
        assert gate(broken)["holds"] is False

    def test_omitting_the_count_also_fails(self, gate):
        """README 不再声明条数也算漂移，不能因为「正则没匹配上」就静默通过。"""
        c = gate(_rewrite(_REAL_README, f"**{_REAL_COUNT} 条**", "**若干条**"))
        assert c["holds"] is False
        assert "未找到" in c["detail"]

    def test_metrics_still_checked_alongside_count(self, gate):
        """条数对了也不能放过行数 —— 两种漂移必须一起红。

        注意按 README 的**真实写法**（带千分位的「7,575 行」）去改：
        门禁正则会先去掉逗号再比对，所以拿 `7575 行` 去 replace 是空转的。
        本条测试第一版就是这么写的 —— 它绿着，而它本该红。
        """
        import re
        assert gate(_REAL_README)["holds"] is True

        row = re.search(r"\| 生产代码 \| ([\d,]+) 行（(\d+) 个模块）", _REAL_README)
        assert row, "README 的「生产代码」那一行格式变了，先更新本测试"
        claimed = row.group(1)
        bumped = f"{int(claimed.replace(',', '')) + 1:,}"
        broken = _REAL_README.replace(row.group(0),
                                     row.group(0).replace(claimed, bumped, 1))
        assert broken != _REAL_README, "重写没生效，测试会空转"

        c = gate(broken)
        assert c["holds"] is False
        assert str(_REAL_METRICS["lines"]) in c["detail"]


class TestSingleSourceOfTruth:
    """主张清单必须只有一个口径源，否则条数只是换了个地方漂。"""

    def test_every_claim_function_is_registered(self):
        """新写了 claim_xxx() 却忘了登记 = 它永远不跑，且没人会发现。

        这是本条门禁真正的盲点：README 的 N 和 _CLAIM_FNS 的长度会一致，
        但两者可能一起漏掉了某条主张。
        """
        declared = {n for n in dir(prove)
                    if n.startswith("claim_") and callable(getattr(prove, n))}
        registered = {fn.__name__ for fn in prove._CLAIM_FNS}
        assert declared == registered, (
            f"未登记：{sorted(declared - registered)}；"
            f"已登记但不存在：{sorted(registered - declared)}")

    def test_gather_runs_exactly_the_registered_claims(self, monkeypatch):
        """gather() 必须逐条跑 _CLAIM_FNS —— 用替身验，不真跑（真跑很贵）。"""
        seen: list[str] = []

        def _make(name):
            def _fake(*_a, **_k):
                seen.append(name)
                return {"claim": name, "holds": True, "detail": ""}
            _fake.__name__ = name
            return _fake

        fakes = tuple(_make(fn.__name__) for fn in prove._CLAIM_FNS)
        monkeypatch.setattr(prove, "_CLAIM_FNS", fakes)
        res = prove.gather(prove.SAMPLE)
        assert seen == [fn.__name__ for fn in fakes]
        assert res["all_hold"] is True

    def test_claim_names_are_unique(self):
        """重名会让「哪一条红了」变得没法读。"""
        names = [fn.__name__ for fn in prove._CLAIM_FNS]
        assert len(names) == len(set(names))


def _is_full_run(config) -> bool:
    """只在全量运行时对账。

    `-k` / `-m` / `--ignore` / `--lf` 都会让收集数天然变小，那时红是假的。
    与其猜一堆组合，不如只认「没带筛选器」这一种情况。

    位置参数也得看：**指向目录**（含 `testpaths` 展开出来的 `tests`）仍然是全量，
    **指向具体文件或节点**才是子集 —— `pytest tests/a.py::C::t` 只收集 1 个用例，
    却不会被前几条筛出来，于是拿 1 去对账 README 写的三千多，**报一个假的红**。
    这条判据是量出来的：pytest 把位置参数放在 ``config.args``，
    且不带参数时它就是 ``testpaths`` 的值（实测 `['tests']`）。
    """
    opt = config.option
    if getattr(opt, "keyword", "") or getattr(opt, "markexpr", ""):
        return False
    for flag in ("lf", "ff", "failed_first", "last_failed"):
        if getattr(opt, flag, False):
            return False
    for lst in ("ignore", "ignore_glob", "deselect"):
        if getattr(opt, lst, None):
            return False
    for a in getattr(config, "args", None) or []:
        if "::" in a or pathlib.Path(a).is_file():
            return False
    return True


class TestReadmeTestCountIsGated:
    """README / FAQ 写的测试用例数必须等于实际收集到的用例数。

    v2.9.1 抓到的第二个同形漂移：README 与 FAQ 都写「1,043 个测试」，
    而实测收集 **1,044** —— 同样是「一个对外数字，没有任何东西校它」。

    固定点说明：本用例自己也在被数的用例里，所以**先加测试、再回填数字**。
    顺序反了就会差 1 —— 那正好是它要抓的那种错。
    """

    def test_test_count_matches_every_doc(self, request):
        if not _is_full_run(request.config):
            pytest.skip("子集运行：收集数天然更小，对账无意义")
        # 用 request.session 而不是 `session` 参数：实测 pytest 7.4.4 里
        # `session` **不是**可直接注入的 fixture（最小复现：单独写
        # `def test(session)` 报 fixture not found），session 挂在 request 上。
        actual = len(request.session.items)
        for name in _DOCS:
            path = _ROOT / name
            text = path.read_text(encoding="utf-8")
            found = [int(x.replace(",", "")) for x in _COUNT_RE.findall(text)]
            assert found, f"{name} 里找不到测试数 —— 格式变了，先更新本测试"
            for claimed in found:
                assert claimed == actual, (
                    f"{name} 写 {claimed} 个测试，实际收集 {actual} 个"
                    f"（新增/删除测试后必须同步回填）")


# ---------------------------------------------------------------------------
# 「子集运行不能触发对账」，以及 —— 更要紧的 —— **对账不能在 CI 上静默失效**
# ---------------------------------------------------------------------------

_THIS_FILE = str(_ROOT / "tests" / "test_prove_claims_gate.py")


class _FakeOption:
    def __init__(self, **kw):
        self.keyword = ""
        self.markexpr = ""
        self.lf = False
        self.ff = False
        self.failed_first = False
        self.last_failed = False
        self.ignore = None
        self.ignore_glob = None
        self.deselect = None
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeConfig:
    """只带 `_is_full_run` 真正读的那几个字段。"""

    def __init__(self, args=(), **kw):
        self.args = list(args)
        self.option = _FakeOption(**kw)


class TestFullRunDetection:
    """`_is_full_run` 判错的后果是对称的，两个方向都得堵。

    判得太松 → 子集运行时拿 1 个用例去对账 README 写的三千多，**假红**，
    于是人开始习惯忽略这条门禁。
    判得太紧 → CI 上它一直 skip，界面上和"真跑了一遍全绿"长得一模一样，
    那比没有门禁更糟：没有门禁你知道那里没检查。
    """

    def test_directory_arg_is_still_a_full_run(self):
        """CI 的形态：不带路径参数时 `config.args` 就是 testpaths 展开的 `['tests']`。

        这条守的是**别把门禁写死** —— 判据一旦改成"参数必须为空"，
        CI 上它立刻变 skip，而 CI 界面不会告诉你任何事。
        """
        assert _is_full_run(_FakeConfig(["tests"])) is True
        assert _is_full_run(_FakeConfig(["."])) is True
        assert _is_full_run(_FakeConfig()) is True

    def test_file_arg_is_not_a_full_run(self):
        """`pytest tests/a.py` 只收集那一个文件 —— 拿它对账必然是假红。"""
        assert _is_full_run(_FakeConfig([_THIS_FILE])) is False

    def test_node_id_arg_is_not_a_full_run(self):
        """`pytest <文件>::<类>::<用例>` 只收集 1 个，是最容易被忽略的子集形态。"""
        assert _is_full_run(_FakeConfig([f"{_THIS_FILE}::TestFullRunDetection"])) is False

    def test_existing_filters_still_work(self):
        """改这条判据时不能顺手把原来那几条筛掉了。"""
        assert _is_full_run(_FakeConfig(keyword="foo")) is False
        assert _is_full_run(_FakeConfig(markexpr="slow")) is False
        assert _is_full_run(_FakeConfig(ignore=["tests/a.py"])) is False
        assert _is_full_run(_FakeConfig(lf=True)) is False

    def test_ci_workflow_passes_no_path_arg(self):
        """**防的是"门禁在 CI 上静默失效"。**

        CI 的 pytest 命令一旦加上位置参数（哪怕只是个目录），
        这条对账就会开始 skip —— 而 skip 出来的"绿"和真跑一遍的"绿"在
        CI 界面上没有任何区别。所以 CI 的命令形态本身就是被校的对象。
        """
        text = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        m = re.search(r'"-m",\s*"pytest",(.*?)\]', text, re.S)
        assert m, "ci.yml 里找不到 pytest 调用 —— 形状变了，先更新本测试"
        for lit in re.findall(r'"([^"]+)"', m.group(1)):
            assert lit.startswith("-"), (
                f"CI 的 pytest 命令多了位置参数 {lit!r} —— "
                f"这条对账会在 CI 上静默失效（要么改判据，要么别在 CI 里传路径）")


# 覆盖率**不在**本文件校：pytest-cov 在 session 结束时才落 coverage.xml，
# 用例跑到时它还不存在 —— 硬写进去就是一条永远 skip 的假覆盖，看起来有
# 覆盖、实际不响，那比没有更糟。对账放在 CI 的 pytest 那一步之后
# （`.github/workflows/ci.yml`，`Unit & integration tests with coverage`），
# 那里是唯一拿得到它的时机。
