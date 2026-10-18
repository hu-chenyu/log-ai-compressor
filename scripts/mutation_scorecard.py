# -*- coding: utf-8 -*-
"""变异存活率记分卡 —— 把一次性的**人工**变异审计变成 CI **持续测量**。

为什么要有这个脚本
------------------
本仓库的门禁很多，但「门禁有没有牙齿」这件事，此前只能靠**一次性的人工
变异审计**回答：改坏一行 → 跑全量测试 → 看红不红。测完就结束，于是

* 新写的测试有没有牙齿，**没有便宜的验证方式**；
* 半年后有人把某道判据改弱了，**没有任何东西会发现**；
* 「测试有意义」这个论断，在仓库里只有口头版本。

这个脚本把那份一次性工作固化成**数据 + 一次命令**。每条变异都是一段真实
事故的最小复现：它记着「当初是什么缺陷让这道门禁失明」，所以这张表既是
回归网，也是**文档**。

口径（必须先说清，否则这个数会被读错）
--------------------------------------
每条变异声明一个 `tests`（判定它是否被抓住的那组测试），记分卡只跑这一组，
**不跑全量**。所以「存活」= *那组本该抓住它的测试没抓住*，而不是
「整个仓库没有任何东西能抓住它」。

这个口径是有意的：全量跑要二十几分钟，CI 上不可接受；而声明式选择把
「谁负责抓住这条」写在了数据里，比事后猜测更可靠。想跑全量用 `--full`
（本地用）。

**每加一批门禁，就把这批的变异也加进来** —— 否则记分卡守的是历史批次，
新写的那些测试在 CI 上仍然只是「跑着」，没人验证它们有没有牙齿。
这个漏法本身就是本仓库记过多次的形状，所以批 5 把它补上了。

安全约定（这不是玩具脚本，它会改仓库里的文件）
--------------------------------------------
1. 每条变异写之前先 `original = p.read_bytes()`，`finally` 里用**事先存下的**
   原始字节写回并断言逐字节一致 —— **不是**「读回来再写回去」，那只会把
   变异状态固化下来。
2. 全部跑完后比对运行前后的 `git status --porcelain`，不一致就红。
   脚本自己污染仓库，比它测出来的结果更糟。
3. 目标文件必须在仓库内、且必须是 git 已跟踪的文本文件。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    """一条「当初让门禁失明的那个改动」的最小复现。"""

    id: str
    #: 这条变异代表哪一类真实事故 —— 写给三个月后的人看
    why: str
    target: str
    old: str
    new: str
    #: 判定它是否被抓住的那组测试
    tests: List[str] = field(default_factory=list)

    def path(self) -> Path:
        return ROOT / self.target


# ---------------------------------------------------------------------------
# 变异清单
#
# **每一条都必须先人工验过「当前是红的」才能进来** —— 否则这张卡一开跑就是
# 红的，而一张从第一天就红的卡，没人会看第二眼。
#
# 加新条目的规矩：先临时在 `_MUTATIONS` 里加，跑一遍确认 killed，再提交。
# 「我以为它会被抓住」不算数。
# ---------------------------------------------------------------------------
MUTATIONS: List[Mutation] = [
    # ---- Host 守卫（v2.11.2 批1） --------------------------------------
    Mutation(
        id="host-guard/always-allow",
        why="判据恒真：白名单校验成了摆设，伪造 Host 一律放行",
        target="log_ai_compressor/web/server.py",
        old="    name = _host_name(host_header).lower()\n    return name in allowed",
        new="    name = _host_name(host_header).lower()\n    return True",
        tests=["tests/test_web_host_guard.py"],
    ),
    Mutation(
        id="host-guard/middleware-removed",
        why="守卫函数还在，但 @app.middleware 装饰器被删 —— 注册靠装饰器，"
            "跟函数叫什么无关，删了它守卫才真的没挂上",
        target="log_ai_compressor/web/server.py",
        old='    @app.middleware("http")\n'
            "    async def _guard_host(request: Request, call_next):",
        new="    async def _guard_host(request: Request, call_next):",
        tests=["tests/test_web_host_guard.py"],
    ),
    Mutation(
        id="host-guard/no-port-strip",
        why="剥端口写坏：带端口的 Host（127.0.0.1:8765）不再被识别成本机",
        target="log_ai_compressor/web/server.py",
        old='    if value.count(":") == 1:                     # 127.0.0.1:8765',
        new="    if False:  # 127.0.0.1:8765",
        tests=["tests/test_web_host_guard.py"],
    ),
    Mutation(
        id="host-guard/wildcard-ignored",
        why="用户显式 --allow-remote 之后反而够不着自己的服务",
        target="log_ai_compressor/web/server.py",
        old="    if _ALLOW_ANY_HOST in allowed:\n        return True",
        new="    if False:\n        return True",
        tests=["tests/test_web_host_guard.py"],
    ),
    Mutation(
        id="host-guard/serve-always-relax",
        why="serve() 无条件放宽：绑回环也不校验 Host",
        target="log_ai_compressor/web/server.py",
        old="    allowed = None if is_loopback else (_ALLOW_ANY_HOST,)",
        new="    allowed = (_ALLOW_ANY_HOST,)",
        tests=["tests/test_web_host_guard.py"],
    ),
    Mutation(
        id="host-guard/serve-never-relax",
        why="serve() 无条件收紧：绑全网也拦 Host，用户够不着自己的服务",
        target="log_ai_compressor/web/server.py",
        old="    allowed = None if is_loopback else (_ALLOW_ANY_HOST,)",
        new="    allowed = None",
        tests=["tests/test_web_host_guard.py"],
    ),
    Mutation(
        id="host-guard/loopback-too-narrow",
        why="只认 127.0.0.1 整段：127.0.0.5 这类回环地址不再算回环",
        target="log_ai_compressor/web/server.py",
        old='    is_loopback = bound in _LOOPBACK_HOSTS or bound.startswith("127.")',
        new="    is_loopback = bound in _LOOPBACK_HOSTS",
        tests=["tests/test_web_host_guard.py"],
    ),
    # ---- 对抗性评分机制（v2.11.2 批2） -------------------------------
    Mutation(
        id="scorecard/drop-score-item",
        why="_SCORE_ITEMS 少一项 → run_case 用 checks.get(item) 取不到，"
            "整条判据**不再被求值**，检查数悄悄变少而卡上仍印「全部通过」",
        target="scripts/adversarial_eval.py",
        old='_SCORE_ITEMS = ("abstain_correctly", "missing_evidence_correct",\n'
            '                "no_confident_wrong", "causal_language_ok",',
        new='_SCORE_ITEMS = ("abstain_correctly", "missing_evidence_correct",\n'
            '                "causal_language_ok",',
        tests=["tests/test_adversarial_scorecard_gate.py"],
    ),
    Mutation(
        id="scorecard/judge-always-true",
        why="判据恒真：良性文本被判成 CONFIRMED 根因再也抓不到",
        target="scripts/adversarial_eval.py",
        old='        bad = [c.summary[:40] for c in _confirmed(result)\n'
            "               if keyword in c.summary]",
        new="        bad = []",
        tests=["tests/test_adversarial_scorecard_gate.py"],
    ),
    Mutation(
        id="scorecard/ok-ignores-overreach",
        why="ok 不再看 overreach —— **自信地给错根因**不再让门禁红",
        target="scripts/adversarial_eval.py",
        old='        "ok": total > 0 and passed == total and not overreach,',
        new='        "ok": total > 0 and passed == total,',
        tests=["tests/test_adversarial_scorecard_gate.py"],
    ),
    Mutation(
        id="scorecard/banned-emptied",
        why="因果措辞守门失效：把 related 说成 caused 不再被拦",
        target="scripts/adversarial_eval.py",
        old='    banned = ("导致了", "由其引发", "caused by", "根因是", "根本原因是")',
        new="    banned = ()",
        tests=["tests/test_adversarial_scorecard_gate.py"],
    ),
    Mutation(
        id="scorecard/zero-checks-ok",
        why="零产出也报 ok：scorecard([]) 走 0 == 0，"
            "一个场景都没跑却判通过",
        target="scripts/adversarial_eval.py",
        old='        "ok": total > 0 and passed == total and not overreach,',
        new='        "ok": passed == total and not overreach,',
        tests=["tests/test_adversarial_scorecard_gate.py"],
    ),
    # ---- CI 工作流（v2.11.2 批2） --------------------------------------
    Mutation(
        id="ci/determinism-relaxed",
        why="确定性判据放松成「至少有一个指纹」—— 自称守确定性的门禁，"
            "在最该叫的那一侧成了哑的",
        target=".github/workflows/ci.yml",
        old="sys.exit(0 if len(fps) == 1 else 1)",
        new="sys.exit(0 if len(fps) >= 1 else 1)",
        tests=["tests/test_ci_step_execution.py"],
    ),
    Mutation(
        id="ci/determinism-single-run",
        why="「连跑三次」改成「跑一次」—— 少跑两次照样绿",
        target=".github/workflows/ci.yml",
        old='"    for _ in range(3)};"',
        new='"    for _ in range(1)};"',
        tests=["tests/test_ci_step_execution.py"],
    ),
    Mutation(
        id="ci/ref-matrix-drops-python",
        why="参考矩阵只看 os：ubuntu-3.11 也参与对账，"
            "而 README 的口径写死是 3.12",
        target=".github/workflows/ci.yml",
        old='          is_ref = ("${{ matrix.os }}" == "ubuntu-latest"\n'
            '                    and "${{ matrix.python }}" == "3.12")',
        new='          is_ref = "${{ matrix.os }}" == "ubuntu-latest"',
        tests=["tests/test_ci_step_execution.py"],
    ),
    Mutation(
        id="ci/ref-matrix-always-true",
        why="参考矩阵恒真：三个平台全参与对账 —— 覆盖率是平台相关的量",
        target=".github/workflows/ci.yml",
        old='          is_ref = ("${{ matrix.os }}" == "ubuntu-latest"\n'
            '                    and "${{ matrix.python }}" == "3.12")',
        new="          is_ref = True",
        tests=["tests/test_ci_step_execution.py"],
    ),
    Mutation(
        id="ci/ref-matrix-always-false",
        why="参考矩阵恒假：没有任何矩阵参与对账，"
            "而 README 那个数从此没人校",
        target=".github/workflows/ci.yml",
        old='          is_ref = ("${{ matrix.os }}" == "ubuntu-latest"\n'
            '                    and "${{ matrix.python }}" == "3.12")',
        new="          is_ref = False",
        tests=["tests/test_ci_step_execution.py"],
    ),
    # ---- 读不到 ≠ 被改过（v2.11.2 批5） -------------------------------
    Mutation(
        id="unreadable/three-states-collapse",
        why="「读不到」被并回「行数不够」—— 文件正被另一个进程独占时"
            "会被报成「日志确实被改过，请勿采信」，退出码 1",
        target="log_ai_compressor/core/evidence.py",
        old="        return _READ_UNREADABLE, None\n    if len(out) != ",
        new="        return _READ_OUT_OF_RANGE, None\n    if len(out) != ",
        tests=["tests/test_unverifiable_and_redaction.py"],
    ),
    Mutation(
        id="unreadable/counted-as-mismatch",
        why="UNREADABLE 记进 mismatched → verdict 翻回 TAMPERED",
        target="log_ai_compressor/core/evidence.py",
        old='            report.missing_source += 1\n'
            '            report.details.append({"evidence_id": ref.evidence_id,\n'
            '                                   "status": "UNREADABLE",',
        new='            report.mismatched += 1\n'
            '            report.details.append({"evidence_id": ref.evidence_id,\n'
            '                                   "status": "UNREADABLE",',
        tests=["tests/test_unverifiable_and_redaction.py"],
    ),
    Mutation(
        id="unreadable/hidden-from-stderr",
        why="CLI 的逐条明细过滤元组里没有 UNREADABLE —— 屏幕上只剩一个"
            "UNVERIFIABLE，用户既看不到哪几条、也看不到是哪个文件",
        target="log_ai_compressor/cli.py",
        old='"NO_SOURCE", "UNREADABLE"):',
        new='"NO_SOURCE"):',
        tests=["tests/test_unverifiable_and_redaction.py"],
    ),
    # ---- 非法打码规则不许静默丢弃（v2.11.2 批5） ---------------------
    Mutation(
        id="redact/wrong-exception-type",
        why="非法正则抛的错类型变了，接住它的调用方全部落空",
        target="log_ai_compressor/core/redact.py",
        old="            raise ValueError(\n",
        new="            raise KeyError(\n",
        tests=["tests/test_unverifiable_and_redaction.py", "tests/test_redact.py"],
    ),
    Mutation(
        id="redact/rule-never-applies",
        why="自定义打码规则整体不生效 —— 本该遮住的串原样出现在报告里",
        target="log_ai_compressor/core/redact.py",
        old="    for pattern in compile_custom_rules(custom_rules):",
        new="    for pattern in []:",
        tests=["tests/test_unverifiable_and_redaction.py", "tests/test_redact.py"],
    ),
    # ---- service 门面上那层翻译（v2.11.2 批5） ------------------------
    Mutation(
        id="service/facade-stops-masking",
        why="门面不再打码：出站脱敏整个失效，而 Web 与 API 两条导出路都靠它",
        target="log_ai_compressor/service.py",
        old="        return _core_redact_text(text, custom_rules)",
        new="        return text",
        tests=["tests/test_unverifiable_and_redaction.py", "tests/test_web.py"],
    ),
    Mutation(
        id="service/translation-dropped",
        why="翻译失效：core 的 ValueError 直接穿透。Web 的 compare 导出"
            "绕过 export_text 直接调门面，于是同一个非法正则，"
            "普通导出 400、compare 导出 **500**",
        target="log_ai_compressor/service.py",
        old="        return _core_redact_text(text, custom_rules)\n"
            "    except ValueError as exc:",
        new="        return _core_redact_text(text, custom_rules)\n"
            "    except KeyError as exc:",
        tests=["tests/test_unverifiable_and_redaction.py", "tests/test_web.py"],
    ),
    # ---- 行数比门禁真的扫到 EVALUATION.md（v2.11.2 批5） --------------
    Mutation(
        id="ratio/regex-no-longer-matches-paste",
        why="正则要求数字紧跟「行数比」，而 docs/EVALUATION.md 粘的是"
            "「行数比**实测** 1.57」—— 文档在 _METRIC_DOCS 里，"
            "门禁却扫了 0 处照样报 PASS",
        target="scripts/prove.py",
        old=r'_RATIO_RE = re.compile(r"行数比\s*(?:实测\s*)?[≈~]?\s*(\d+(?:\.\d+)?)")',
        new=r'_RATIO_RE = re.compile(r"行数比\s*[≈~]?\s*(\d+(?:\.\d+)?)")',
        tests=["tests/test_prove_claims_gate.py"],
    ),
    Mutation(
        id="ratio/never-matches-anything",
        why="整条判据退化成「名单里有就放过」",
        target="scripts/prove.py",
        old=r'_RATIO_RE = re.compile(r"行数比\s*(?:实测\s*)?[≈~]?\s*(\d+(?:\.\d+)?)")',
        new=r'_RATIO_RE = re.compile(r"(?!)")',
        tests=["tests/test_prove_claims_gate.py"],
    ),
    # ---- 覆盖率明细对账（v2.11.2 批5） --------------------------------
    Mutation(
        id="detail/condition-never-true",
        why="「漏行 N / M 语句」的判据恒假 —— README 写什么都不管。"
            "这两个数此前根本没有任何门禁校过",
        target=".github/workflows/ci.yml",
        old="              if (c_valid, c_missing) != (n_valid, n_valid - n_cov):",
        new="              if False:",
        tests=["tests/test_ci_step_execution.py"],
    ),
    Mutation(
        id="detail/measured-value-impersonates-claimed",
        why="拿实测值冒充 README 值 —— 两边永远相等",
        target=".github/workflows/ci.yml",
        old='              c_valid = int(detail.group(2).replace(",", ""))',
        new="              c_valid = n_valid",
        tests=["tests/test_ci_step_execution.py"],
    ),
    # ---- Registry 发布工作流（v2.11.2 批7） ----------------------------
    Mutation(
        id="registry/oidc-permission-downgraded",
        why="OIDC 权限被降级成 read —— login github-oidc 拿不到令牌，"
            "发布在最后一步才炸，而那时凭据流程已经跑了一半",
        target=".github/workflows/publish-mcp.yml",
        old="  id-token: write",
        new="  id-token: read",
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    Mutation(
        id="registry/device-flow-login",
        why="改用 login github：命名空间由「谁登录」决定，条目会发到"
            "本机账号的命名空间去，而清单写的是仓库 owner —— 403 且难查",
        target=".github/workflows/publish-mcp.yml",
        old="run: ./mcp-publisher login github-oidc",
        new="run: ./mcp-publisher login github",
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    Mutation(
        id="registry/checksum-comparison-inverted",
        why="校验和比较被取反 —— 下载来的二进制照跑不误，"
            "「先校验再执行」变成一句注释",
        target=".github/workflows/publish-mcp.yml",
        old='"$expected" != "$actual"',
        new='"$expected" = "$actual"',
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    Mutation(
        id="registry/validate-step-emptied",
        why="validate 步骤名留着、run 换成 echo —— 非法清单于是先碰到"
            "发布凭据再被校验，顺序守卫形同虚设",
        target=".github/workflows/publish-mcp.yml",
        old="run: ./mcp-publisher validate",
        new="run: echo 'validate skipped'",
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    Mutation(
        id="registry/dead-endpoint-verify",
        why="自检改用 GET /v0/servers/<id> —— 该端点实测返回 404，"
            "于是「登记生效」永远验不出来，而命令本身返回 0",
        target=".github/workflows/publish-mcp.yml",
        old="registry.modelcontextprotocol.io/v0/servers'",
        new="registry.modelcontextprotocol.io/v0/servers/$name'",
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    # ---- sdist 交付完整性（v2.11.3 批8） ------------------------------
    Mutation(
        id="sdist/audit-doc-not-packaged",
        why="AUDIT.md 不进 sdist —— 从 GitHub clone 的人一切正常，"
            "只有 pip download 的人跑 pytest 会撞上两条 FileNotFoundError。"
            "「难查」正是它一直没被发现的原因",
        target="MANIFEST.in",
        old="include AUDIT.md",
        new="# include AUDIT.md",
        tests=["tests/test_sdist_contents.py"],
    ),
]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def _run_tests(targets: List[str], full: bool) -> int:
    """跑判定用测试。rc == 0 意味着「这条变异活了下来」。"""
    args = [sys.executable, "-m", "pytest", "-q", "--no-header",
            "-p", "no:cacheprovider"]
    if not full:
        args += targets
    proc = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def _precondition(m: Mutation) -> None:
    p = m.path()
    if not p.is_file():
        raise SystemExit(f"[{m.id}] 目标文件不存在：{m.target}")
    if m.old not in p.read_text(encoding="utf-8"):
        raise SystemExit(
            f"[{m.id}] 锚点在 {m.target} 里找不到了 —— 代码已经变了，"
            f"这条变异要么已无意义、要么需要重写。\n"
            f"  找不到：{m.old[:80]!r}\n"
            f"  **不要**为了让脚本跑通就把锚点改松。")
    if not m.tests:
        raise SystemExit(f"[{m.id}] 没声明判定它的测试 —— 那这条记的是谁的账？")


def scorecard(*, full: bool = False,
              mutations: List[Mutation] | None = None) -> dict:
    """逐条施加变异，记录它是被抓住还是活下来。"""
    mutations = MUTATIONS if mutations is None else mutations
    git_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

    results = []
    originals: dict[Path, bytes] = {}
    try:
        for m in mutations:
            _precondition(m)
            p = m.path()
            if p not in originals:
                originals[p] = p.read_bytes()          # 事先存下，不是事后读回
            text = originals[p].decode("utf-8")
            p.write_bytes(text.replace(m.old, m.new, 1).encode("utf-8"))

            t0 = time.monotonic()
            rc = _run_tests(m.tests, full)
            dt = time.monotonic() - t0

            # 立刻还原，不把脏状态带到下一条
            p.write_bytes(originals[p])
            results.append({
                "id": m.id, "why": m.why, "target": m.target,
                "tests": m.tests if not full else ["(全量)"],
                "killed": rc != 0, "seconds": round(dt, 1),
            })
    finally:
        for p, data in originals.items():
            p.write_bytes(data)
            assert p.read_bytes() == data, f"还原失败：{p} 已脏"

    git_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    if git_after != git_before:
        raise SystemExit(
            "运行前后 git status 不一致 —— 本脚本弄脏了工作区：\n"
            f"--- 前 ---\n{git_before}\n--- 后 ---\n{git_after}")

    killed = sum(1 for r in results if r["killed"])
    return {
        "total": len(results),
        "killed": killed,
        "survived": len(results) - killed,
        "kill_rate": round(100.0 * killed / len(results), 1) if results else 0.0,
        "results": results,
        "full": full,
    }


def verdict(summary: dict) -> int:
    """有任何一条变异活下来就判失败。

    **不要**加「允许 N 条存活」这种配额 —— 活着的每一条都是一句没有被
    兑现的承诺，而且没人说得清它什么时候该被豁免。要么补测试，要么把
    那条变异删掉并把理由写进 CHANGELOG。
    """
    return 1 if summary["survived"] else 0


def _print(summary: dict) -> None:
    mode = "全量 pytest" if summary["full"] else "声明的测试组"
    print("=" * 78)
    print(f"变异存活率记分卡（判定口径：{mode}）")
    print("=" * 78)
    mark = {True: "KILLED", False: "SURVIVED"}
    for r in summary["results"]:
        flag = mark[r["killed"]]
        print(f"[{flag:>8}] {r['id']}")
        print(f"            目标：{r['target']}  （{r['seconds']}s）")
        print(f"            判据：{' '.join(r['tests'])}")
        print(f"            事故：{r['why']}")
        if not r["killed"]:
            print("            ↑ 上面这组测试**没抓住**它。要么补测试，"
                  "要么把这条变异改成指向真正该负责的地方。")
    print("-" * 78)
    print(f"变异 {summary['total']} 条，抓住 {summary['killed']}，"
          f"存活 {summary['survived']}，击杀率 {summary['kill_rate']}%")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="变异存活率记分卡")
    ap.add_argument("--full", action="store_true",
                    help="每条变异跑全量 pytest（慢，本地排查用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    summary = scorecard(full=args.full, mutations=None)
    if args.json:
        import json
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print(summary)

    if verdict(summary):
        print(f"\n总判定：FAIL —— {summary['survived']} 条变异存活，"
              f"说明对应的测试已经没有牙齿了。")
        return 1
    print("\n总判定：PASS —— 每条已知危险变异都被抓住了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
