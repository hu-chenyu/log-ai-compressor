# -*- coding: utf-8 -*-
"""scripts/dep_audit_gate.py 的测试。

这道门禁要防的东西很具体：**它自己静默地什么都不检查**。

pip-audit 拿不到漏洞库时不一定报错；而一个报「检查通过」的门禁，
比没有门禁更糟 —— 没有门禁你知道那里没检查。所以：

* `--self-check` 用 `urllib3==1.25.8` 做**阳性对照**，要求它必须报出来，
  报不出来就退出码 2（门禁自身失能），而不是绿；
* 对照臂与本项目**分两次扫**，且这一点被测试钉住（理由见下）；
* 依赖清单与命令行拼装在这里被离线测掉，不依赖网络。

为什么要钉住「分两次」

--------------------
阳性对照那条 pin 一旦混进本项目的扫描，就会改变 pip 对**本项目依赖**的
解析结果 —— 实测凭空多出一条 `requests PYSEC-2026-2275`，而单独扫本项目
是干净的。**让本项目的结论依赖于对照臂的约束，就是把探针装在了错误的
地方。** 这类「为了省一次网络往返」的重构看起来是纯优化，所以必须有测试
挡着，否则早晚有人把它合回去。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "dep_audit_gate", ROOT / "scripts" / "dep_audit_gate.py")
assert _SPEC and _SPEC.loader
dep = importlib.util.module_from_spec(_SPEC)
sys.modules["dep_audit_gate"] = dep
_SPEC.loader.exec_module(dep)


def _vuln(vid: str, *fixes: str) -> dict:
    return {"version": "1.0", "ids": [vid], "fix": list(fixes)}


class _Proc:
    """`subprocess.CompletedProcess` 的替身 —— 只需要 `_parse` 读的那两个属性。

    这里刻意**不** `import subprocess`：本文件从头到尾没有真的起过进程
    （`_run` 那个接缝被换掉了），import 进来只会让 SAST 多一条 B404 豁免。
    按项目自己的规矩「能修的一律修，不进豁免表」，一条用不上的 import
    不该靠豁免留在基线里。
    """

    def __init__(self, args=(), returncode=0, stdout="", stderr=""):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _res(ok: bool, vulns: dict, error: str | None = None) -> dict:
    return {"ok": ok, "vulns": vulns, "error": error}


def _patch_run(monkeypatch, replies):
    """把 `_run` 这个接缝换成假的，记录 (requirements 文件内容, 额外参数)。

    刻意 patch `_run` 而不是 `subprocess.run` —— 后者是**全局**模块对象，
    在它上面打补丁会影响同一进程里的一切。
    """
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake(req: Path, *extra: str):
        calls.append((req.read_text(encoding="utf-8"), tuple(extra)))
        body = replies.pop(0) if replies else {"dependencies": []}
        code = 1 if body.get("dependencies") and any(
            d.get("vulns") for d in body["dependencies"]) else 0
        return _Proc([str(req)], code, json.dumps(body), "")

    monkeypatch.setattr(dep, "_run", fake)
    return calls


# ---------------------------------------------------------------------------
# 依赖清单
# ---------------------------------------------------------------------------
def test_runtime_dependency_is_included():
    reqs = dep.declared_requirements()
    assert "PyYAML>=6.0" in reqs, (
        "运行时依赖必须进扫描范围 —— 「生产环境只有一个第三方依赖」"
        "这句话指的就是它")


def test_all_extras_are_included():
    """web / mcp / ai 三个 extra 的依赖同样在用户的安装面上。"""
    reqs = dep.declared_requirements()
    for name in ("fastapi>=0.110", "mcp>=1.0", "httpx>=0.27"):
        assert name in reqs, "%s 不在扫描范围里" % name


def test_self_reference_is_skipped():
    """`log-ai-compressor[...]` 是本项目自己，要扫的是它的第三方依赖树。

    不跳过的话 pip-audit 会去解析 PyPI 上已发布的那个包 ——
    于是「本仓库这次提交的依赖」被换成了「上次发版时的依赖」。
    """
    reqs = dep.declared_requirements()
    assert not any("log-ai-compressor" in r.lower() for r in reqs)


def test_sast_tools_are_declared():
    """bandit / pip-audit 自己也要在依赖声明里。

    不在的话 CI 里 pip install -r requirements-dev.txt 不会装它们，
    dep_audit_gate.py 会返回 2 —— 而 2 在 CI 里同样是红，只是红得莫名其妙。
    """
    reqs = dep.declared_requirements()
    assert any(r.startswith("bandit") for r in reqs)
    assert any(r.startswith("pip-audit") for r in reqs)


def test_requirements_are_deduplicated_but_ordered():
    reqs = dep.declared_requirements()
    assert len(reqs) == len(set(reqs))
    assert reqs[0].startswith("PyYAML"), "顺序应当与 pyproject 的声明一致"


def test_dependency_list_is_not_empty():
    assert dep.declared_requirements(), "依赖清单为空 = 扫描了个寂寞"


def test_readme_dependency_count_matches_the_real_list():
    """README 写着「全部 N 条声明依赖」，N 必须等于实际扫的条数。

    这条纪律与本仓库的工程指标表一致：README 里的每个数字都要有人复算。
    """
    import re
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"全部\s*(\d+)\s*条声明依赖", readme)
    assert m, "README 里找不到「N 条声明依赖」这句话"
    assert int(m.group(1)) == len(dep.declared_requirements()), (
        "README 写 %s 条声明依赖，实测 %d 条 —— 加/减依赖时两边都要改"
        % (m.group(1), len(dep.declared_requirements())))


# ---------------------------------------------------------------------------
# 阳性对照本身
# ---------------------------------------------------------------------------
def test_positive_control_pin_is_known_vulnerable():
    """`--self-check` 的阳性对照必须真的是有漏洞的版本。

    这条不用网络，只断言常量本身。真正的「pip-audit 报不报得出来」
    由 --self-check 在有网时验证；没有阳性对照的话，那道门禁无法证明
    自己有牙齿。
    """
    assert dep.POSITIVE_CONTROL_PIN == "urllib3==1.25.8"
    pkg, _, version = dep.POSITIVE_CONTROL_PIN.partition("==")
    assert pkg and version
    assert version.count(".") == 2, "这条 pin 应当是固定版本，不是区间"


def test_control_name_is_the_package_of_the_pin():
    assert dep._control_name() == "urllib3"


# ---------------------------------------------------------------------------
# 两次扫描必须真的分开
# ---------------------------------------------------------------------------
def test_control_pin_never_enters_the_project_scan(monkeypatch):
    """阳性对照那条 pin **不许**出现在本项目的 requirements 文件里。

    混进去会让 pip 的解析结果被那条老 pin 拖偏，实测凭空多出一条
    `requests PYSEC-2026-2275` —— 一条与本项目完全无关的假红。
    """
    calls = _patch_run(monkeypatch, [
        {"dependencies": []},                                    # audit_control
        {"dependencies": []},                                    # audit_project
    ])
    dep.audit_control()
    dep.audit_project()
    control_body, project_body = calls[0][0], calls[1][0]
    assert dep.POSITIVE_CONTROL_PIN in control_body, "对照臂没拿到那条 pin"
    assert dep.POSITIVE_CONTROL_PIN not in project_body, (
        "阳性对照混进了本项目的扫描 —— 实测会多出一条假红")
    assert project_body.splitlines() == dep.declared_requirements()


def test_control_file_holds_only_the_control_pin(monkeypatch):
    """对照臂只扫那一条 pin，不搭便车带上别的东西。"""
    calls = _patch_run(monkeypatch, [{"dependencies": []}])
    dep.audit_control()
    assert calls[0][0].splitlines() == [dep.POSITIVE_CONTROL_PIN]


def test_control_scan_disables_dependency_resolution(monkeypatch):
    """对照臂带 `--no-deps`，本项目扫描**不带**。

    一头一尾都要钉：少了对照臂的 `--no-deps`，单次扫描会从十几秒涨到
    几分钟（早期版本两次连跑直接顶穿 CI 时限）；而给本项目加
    `--no-deps` 会让传递依赖整个逃出扫描范围 —— 那才是真正的漏检。
    """
    calls = _patch_run(monkeypatch, [
        {"dependencies": []}, {"dependencies": []},
    ])
    dep.audit_control()
    dep.audit_project()
    assert "--no-deps" in calls[0][1], "对照臂应当跳过依赖解析"
    assert "--no-deps" not in calls[1][1], (
        "本项目扫描加 --no-deps 会让传递依赖逃出扫描范围")


# ---------------------------------------------------------------------------
# 命令行拼装
# ---------------------------------------------------------------------------
def test_pip_audit_is_invoked_with_a_requirements_file(tmp_path):
    """依赖必须**写成文件**用 `-r` 传进去，不能拼进命令行。

    拼进去的话，一个依赖名里的空格就会变成参数注入。
    """
    req = tmp_path / "declared.txt"
    req.write_text("some pkg; rm -rf /\n", encoding="utf-8")
    args = dep._audit_args(req)
    assert "-r" in args, "没有 -r：依赖是拼进命令行的，那是一个注入面"
    assert args[args.index("-r") + 1] == str(req)
    assert "some pkg; rm -rf /" not in args, "依赖正文不许出现在 argv 里"
    assert args[args.index("--format") + 1] == "json", (
        "必须用 JSON 输出 —— 文本输出没法区分「有漏洞」与「没扫成」")
    assert args[args.index("--progress-spinner") + 1] == "off"


def test_extra_flags_are_appended_after_the_json_format(monkeypatch):
    """额外开关（`--no-deps`）要接在末尾，不能顶掉前面的固定参数。"""
    args = dep._audit_args(Path("x.txt"), "--no-deps")
    assert args[-1] == "--no-deps"
    assert args[args.index("--format") + 1] == "json"


# ---------------------------------------------------------------------------
# 「扫不动」与「干净」必须分开
# ---------------------------------------------------------------------------
def test_scan_failure_is_distinguished_from_a_clean_result(monkeypatch):
    """**扫不动**与**干净**必须分开。

    pip-audit 两种情况都返回非零，于是第一版把一次网络超时报成
    「本项目声明的依赖里存在已知漏洞」—— 一条假红。判据是
    「能不能解析出 JSON」。
    """
    monkeypatch.setattr(dep, "_run", lambda req, *extra: _Proc(
        [str(req)], 1, "", "ConnectionError: read timed out"))
    res = dep.audit_project()
    assert res["ok"] is False, "扫不动却报成 ok"
    assert res["vulns"] == {}, "扫不动时不得凭空长出漏洞"
    assert "ConnectionError" in res["error"]


def test_parse_keeps_version_ids_and_flattened_fix_versions():
    """漏洞记录要带**装着的版本**与**修复版本**，且修复版本是摊平的。

    `fix_versions` 是「每条 advisory 一个列表」，直接 `str()` 出来是
    `"['1.26.0', '2.0.0']"` —— 一层列表字面量把最有用的话包在壳里。
    """
    proc = _Proc([], 1, json.dumps({"dependencies": [
        {"name": "urllib3", "version": "1.25.8", "vulns": [
            {"id": "PYSEC-B", "fix_versions": ["1.26.0", "2.0.0"]},
            {"id": "PYSEC-A", "fix_versions": []},
        ]}]}), "")
    res = dep._parse(proc)
    assert res["ok"] is True
    info = res["vulns"]["urllib3"]
    assert info["version"] == "1.25.8"
    assert info["ids"] == ["PYSEC-A", "PYSEC-B"], "IDs 必须去重并排序"
    assert info["fix"] == ["1.26.0", "2.0.0"], (
        "修复版本被摊成了 %r（列表字面量没剥掉）" % (info["fix"],))
    assert not any(f.startswith("[") for f in info["fix"])


def test_report_names_the_installed_version_and_the_fix(capsys):
    """报出问题却不指出修法，等于把活推给读者，而读者多半只能 ignore。"""
    dep._print(_res(True, {"requests": _vuln("PYSEC-2026-2275",
                                             "2.32.5")}))
    out = capsys.readouterr().out
    assert "requests" in out and "PYSEC-2026-2275" in out
    assert "2.32.5" in out, "没说「升到哪个版本」"
    assert "修复版本" in out


def test_report_says_so_when_no_fix_version_exists(capsys):
    """没有修复版本时必须**说出来**，不能静默留空 ——
    留空的读起来像「这条没修版」，而真相是「暂时没修版」。"""
    dep._print(_res(True, {"foo": _vuln("PYSEC-9")}))
    assert "暂无修复版本" in capsys.readouterr().out


def test_report_surfaces_a_scan_error_alongside_vulns(capsys):
    res = _res(False, {"foo": _vuln("PYSEC-9", "1.2")}, error="read timed out")
    dep._print(res)
    out = capsys.readouterr().out
    assert "PYSEC-9" in out and "read timed out" in out


# ---------------------------------------------------------------------------
# 三种结局必须是三个不同的出口
# ---------------------------------------------------------------------------
def test_self_check_returns_2_when_control_scan_did_not_run(
        monkeypatch, capsys):
    """对照臂**自己没扫成** → 退出码 2，且**不得**说成「有漏洞」。"""
    monkeypatch.setattr(dep, "audit_control", lambda: _res(False, {}, "超时"))
    monkeypatch.setattr(
        dep, "audit_project",
        lambda: pytest_fail("对照臂都没扫成，不该继续扫本项目"))
    assert dep.self_check() == 2
    out = capsys.readouterr().out
    assert "没跑完" in out
    assert "存在已知漏洞" not in out


def test_self_check_returns_2_when_control_is_silent(monkeypatch, capsys):
    """阳性对照没被抓到 → 退出码 2（门禁自身失能），不是 0。

    返回 0 就等于宣布「依赖安全」，而那一刻它分明什么都没验成。
    """
    monkeypatch.setattr(dep, "audit_control", lambda: _res(True, {}))
    monkeypatch.setattr(dep, "audit_project", lambda: _res(True, {}))
    assert dep.self_check() == 2
    assert "此刻是装饰" in capsys.readouterr().out


def test_self_check_returns_1_when_project_itself_is_vulnerable(
        monkeypatch, capsys):
    """本项目有洞（对照同时被抓到）→ 退出码 1。

    注意这里那个有洞的包**不能叫 urllib3** —— 那是对照臂，它被抓到只
    说明门禁有牙齿，不算本项目的洞。
    """
    monkeypatch.setattr(dep, "audit_control",
                        lambda: _res(True, {"urllib3": _vuln("PYSEC-1")}))
    monkeypatch.setattr(dep, "audit_project", lambda: _res(True, {
        "requests": _vuln("PYSEC-2", "2.32.5")}))
    assert dep.self_check() == 1
    out = capsys.readouterr().out
    assert "存在已知漏洞" in out
    assert "requests" in out and "PYSEC-2" in out


def test_self_check_passes_only_when_both_arms_agree(monkeypatch, capsys):
    """对照被抓到 + 本项目扫描跑完 + 本项目干净 → 才算通过。"""
    monkeypatch.setattr(dep, "audit_control",
                        lambda: _res(True, {"urllib3": _vuln("PYSEC-1")}))
    monkeypatch.setattr(dep, "audit_project", lambda: _res(True, {}))
    assert dep.self_check() == 0
    out = capsys.readouterr().out
    assert "阳性对照成立" in out
    assert "自检通过" in out


def test_self_check_returns_2_when_the_project_scan_itself_fails(
        monkeypatch, capsys):
    """对照过了，但本项目那次扫描没跑完 → 仍然 2，不许说「通过」。"""
    monkeypatch.setattr(dep, "audit_control",
                        lambda: _res(True, {"urllib3": _vuln("PYSEC-1")}))
    monkeypatch.setattr(dep, "audit_project",
                        lambda: _res(False, {}, "ConnectionError"))
    assert dep.self_check() == 2
    out = capsys.readouterr().out
    assert "没跑完" in out
    assert "自检通过" not in out


def test_three_outcomes_are_three_different_exit_codes(monkeypatch):
    """三种结局必须是三个不同的出口。

    把它们混成一个就是**假红的制造机**：一次网络抖动会被报成
    「依赖有漏洞」，而真问题会淹没在这种噪音里。
    """
    def control(vulns, ok=True):
        return lambda: _res(ok, vulns, None if ok else "扫描没跑完")

    def project(vulns, ok=True):
        return lambda: _res(ok, vulns, None if ok else "扫描没跑完")

    cases = [
        # (对照臂, 本项目, 期望退出码, 用例说明)
        (control({"urllib3": _vuln("X")}), project({}), 0, "对照抓到+本项目干净"),
        (control({"urllib3": _vuln("X")}),
         project({"requests": _vuln("Y")}), 1, "本项目真有洞"),
        (control({}), project({}), 2, "对照没抓到 -> 门禁失能"),
        (control({}, ok=False), project({}), 2, "对照臂没扫成"),
        (control({"urllib3": _vuln("X")}), project({}, ok=False),
         2, "本项目没扫成"),
    ]
    codes = set()
    for ctl, prj, want, label in cases:
        monkeypatch.setattr(dep, "audit_control", ctl)
        monkeypatch.setattr(dep, "audit_project", prj)
        with contextlib.redirect_stdout(io.StringIO()):
            got = dep.self_check()
        assert got == want, "%s：得到 %s，期望 %s" % (label, got, want)
        codes.add(got)
    assert codes == {0, 1, 2}, "三种结局只落到了 %s" % sorted(codes)


def pytest_fail(msg):
    raise AssertionError(msg)
