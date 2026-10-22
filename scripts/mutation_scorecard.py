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
import shutil
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
            "于是「登记生效」永远验不出来，而命令本身返回 0。"
            "锚点必须长到**唯一**：这个 URL 在 publish 的回查与 verify 的"
            "自检里各有一处，锚点短了就只会改到 publish，而判据读的是 verify",
        target=".github/workflows/publish-mcp.yml",
        old='body="$(curl -fsS --get \'https://registry.modelcontextprotocol.io/v0/servers\'',
        new='body="$(curl -fsS --get \'https://registry.modelcontextprotocol.io/v0/servers/$name\'',
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
    # ---- 发布路径遇到模糊失败（v2.11.3 批9） --------------------------
    Mutation(
        id="registry/no-retry-on-ambiguous-failure",
        why="发布失败不重试 —— Registry 实测会返回 504，"
            "单次尝试把一次瞬时网关故障当成永久失败，登记从此卡在旧版本",
        target=".github/workflows/publish-mcp.yml",
        old="for i in 1 2 3; do",
        new="for i in 1; do",
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    Mutation(
        id="registry/verify-skips-version",
        why="发布后自检只核对名字不核对版本 —— 同一个 name 挂着旧版本也放行。"
            "这正是 504 事故现场的样子：名字命中、版本停在 2.11.2，"
            "而「登记生效了」这句话的全部内容就是那个版本号",
        target=".github/workflows/publish-mcp.yml",
        old="want_ver not in versions",
        new="False",
        tests=["tests/test_publish_mcp_workflow.py"],
    ),
    # ---- 注释强于代码（v2.11.4 批10） ----------------------------------
    # 这一批的来源不是事故而是**外部评审**：它读代码时发现，docstring 里
    # 「CONFIRMED 的唯一来源是 Caused by 链」这条承诺，管线上不可达，
    # 可函数注释同时写着它也接受外部直接构造的簇 —— 于是那条兜底成了一条
    # 从没被验证过的不变量。门禁守得住行为，守不住注释与代码的差距。
    Mutation(
        id="analysis/fallback-mints-confirmed",
        why="兜底用 `CONF_CONFIRMED if c.is_root_cause` —— 「被标成根因」"
            "本身就能拿到 CONFIRMED。只要外部调用方预设 is_root_cause、"
            "不带任何因果链，本工具唯一敢说「就是它」的那一档就被白送了",
        target="log_ai_compressor/core/analysis.py",
        old="    for c in ordered:\n"
            "        if not c.root_cause_confidence:\n"
            "            c.root_cause_confidence = CONF_INSUFFICIENT",
        new="    for c in ordered:\n"
            "        if not c.root_cause_confidence:\n"
            "            c.root_cause_confidence = (\n"
            "                CONF_CONFIRMED if c.is_root_cause else CONF_INSUFFICIENT)",
        tests=["tests/test_analysis.py"],
    ),
    Mutation(
        id="analysis/confirmed-skips-citation-check",
        why="CONFIRMED 跳过出处校验 —— 「已定位根因」却没有一行原文可回读。"
            "报告里最硬的那句断言失去出处，比降级成 LIKELY 隐蔽得多。"
            "判据在 test_forensics 而非 test_analysis：第一版指错了文件，"
            "变异存活 —— 而存活的不是测试没牙齿，是判据找错了门",
        target="log_ai_compressor/core/analysis.py",
        old="                marked = _mark_causal_evidence(c, top)\n"
            "        if not marked:",
        new="                marked = _mark_causal_evidence(c, top)\n"
            "        if False:",
        tests=["tests/test_forensics.py"],
    ),
    # ---- 威胁模型：防御必须说清防不住谁（v2.11.4 批11） ------------------
    Mutation(
        id="security/docstring-reverts-to-security-boundary",
        why="docstring 小节标题改回「安全边界」—— 它守的只有 DNS rebinding "
            "一种，却重新对外做出「只有本机能访问」的整体断言。"
            "外部评审的原话：这是个对称的漏洞（拿「边界为什么成立」当卖点，"
            "却从没画过威胁模型）",
        target="log_ai_compressor/web/server.py",
        old="威胁模型\n--------\nv2.11.4：",
        new="安全边界\n--------\nv2.11.4：",
        tests=["tests/test_threat_model_wording.py"],
    ),
    Mutation(
        id="security/docstring-hides-undefended-case",
        why="docstring 删掉「不防本机同权限进程」这句承认 —— 那一类对手"
            "本来就能读当前用户能读的一切，服务没扩大它的能力；"
            "可一旦不写，基于这条边界做的安全推演就全错了",
        target="log_ai_compressor/web/server.py",
        old="**明确不防的：同一台机器上、同一权限级别的任何进程。**",
        new="**本服务仅监听回环，仅本机可访问。**",
        tests=["tests/test_threat_model_wording.py"],
    ),
    # ---- 三档表：逐格与汇总（v2.11.4 批12） ----------------------------
    # 这张表是 BENCHMARK.md 里唯一一张主基准脚本**不生成**的表，所以它
    # 只靠人抄就会变成 v2.9.9 那个事故的重演。判据反向：从原始 CSV 重算。
    Mutation(
        id="bench/tier-cell-drifts-from-csv",
        why="三档表的一格与 docs/similarity_tiers.csv 对不上 —— 逐格数字漂了。"
            "上一版（v2.9.9）就是在 README/BENCHMARK 的表漂了、"
            "文档原地不动、六道门禁全绿",
        target="docs/BENCHMARK.md",
        old="| Apache | 29 | 13.8% | 55.2% |",
        new="| Apache | 29 | 14.8% | 55.2% |",
        tests=["tests/test_similarity_tiers_table.py"],
    ),
    Mutation(
        id="bench/tier-summary-drifts-from-csv",
        why="逐格全对、**汇总的胜负被改错** —— 这是最容易被顺手改掉的一行。"
            "v2.9.9 就干过一次（把一处平局写成赢）",
        target="docs/BENCHMARK.md",
        old="| 全量口径独占最优 | **8 / 10** | 0 | 0 |",
        new="| 全量口径独占最优 | **9 / 10** | 0 | 0 |",
        tests=["tests/test_similarity_tiers_table.py"],
    ),
    Mutation(
        id="bench/tier-table-claims-retune-is-fine",
        why="删掉「不会因此改默认值」那句 —— 交付口径的赢家是 lenient 而当前"
            "默认值不是，读者于是读成「建议改默认值」。而改了正是 §2.3 禁止的"
            "对着评测集调参，且会让簇更粗、根因更不准（形态六）",
        target="docs/BENCHMARK.md",
        old="而本项目不会因此改默认值",
        new="而本项目应该改默认值",
        tests=["tests/test_similarity_tiers_table.py"],
    ),
    # ---- 认表判据的下限（v2.11.4 批13） --------------------------------
    # 收紧认表判据和放宽一样要有人看着：改坏了不会让哪条主张变红，
    # 只会在下次有人往 BENCHMARK.md 里加表时，把「认出几张表」这个
    # 数字悄悄改掉。这两条盯的正是收紧本身，不是收紧的对象。
    Mutation(
        id="bench/baseline-column-floor-never-fires",
        why="去掉「至少一列真的映射得到基线字段」这条下限 —— 三档相似度那张"
            "表重新被认成基准表。认进来之后它报的问题全部是「基线里没有这一"
            "列」，真漂移被埋在这类噪音里（v2.9.9 事故的形状，第二次）",
        target="scripts/prove.py",
        old="    if not _BENCH_COLUMN_KEYS & out.keys():\n        return {}",
        new="    if False and not _BENCH_COLUMN_KEYS & out.keys():\n        return {}",
        tests=["tests/test_benchmark_table_gate.py"],
    ),
    Mutation(
        id="bench/baseline-column-floor-over-tightened",
        why="把下限从「一列」改成「两列」 —— 收紧过头。真表照样有 5 列不受"
            "影响，但**只带一个口径列**的那张（docs/BENCHMARK.md 的达成度表）"
            "会被静默放过。只测负向用例的话，收紧过头和收紧到位长得一模一样",
        target="scripts/prove.py",
        old="    if not _BENCH_COLUMN_KEYS & out.keys():",
        new="    if len(_BENCH_COLUMN_KEYS & out.keys()) < 2:",
        tests=["tests/test_benchmark_table_gate.py"],
    ),

    # -----------------------------------------------------------------------
    # 批 6：SAST / 依赖扫描 / 性能回归 / PA-GA 这四组门禁
    #
    # 加这一批的原因与前面几批一样：记分卡守的是**历史批次**，
    # 新写的门禁如果只「跑着」而没人验证它有没有牙齿，那么半年后
    # 没人说得清它们是守着的还是只是占着位置。
    # -----------------------------------------------------------------------
    Mutation(
        id="sast/reason-becomes-optional",
        why="豁免的 reason 变成可选 —— 这道门禁就退化成「bandit 的发现"
            "都在名单里」。名单只增不减，最后是一张没人看过的旧账。"
            "**没有理由的豁免等于开后门**，这句话必须由门禁执行",
        target="scripts/sast_gate.py",
        old='        if not str(e.get("reason") or "").strip():',
        new='        if False and not str(e.get("reason") or "").strip():',
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="sast/stale-entry-ignored",
        why="不再检测**过期豁免**。少了这一条，基线只增不减：代码里早就"
            "不报的东西还留在名单上，没有人会去删，而它在纸面上看起来"
            "仍然是「已被复核过」",
        target="scripts/sast_gate.py",
        old="        if counts.get(key, 0) == 0:",
        new="        if False:",
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="sast/https-guard-removed",
        why="把 https 白名单守卫改成恒不拦 —— 于是 `file://` 之类的 URL 也能"
            "被 urlopen 取走。这一条守的是**守卫本身**：SAST 基线里那条 B310 的"
            "理由写的是「有 `_require_https()` 挡着」，守卫一被删，那句话就变成"
            "一句没人验的注释（而注释不会因为守卫被删而变红）。",
        target="scripts/audit_published_versions.py",
        old='    if not url.lower().startswith("https://"):',
        new="    if False:",
        tests=["tests/test_audit_published_versions_scheme_guard.py"],
    ),
    Mutation(
        id="sast/documented-count-drifts-from-baseline",
        why="文档里写的豁免条数与基线对不上。**这一条是真的发生过**："
            "`tests/test_dep_audit_gate.py` 去掉一个用不上的 "
            "`import subprocess` 之后，基线里那条 B404 变成过期条目、门禁"
            "正确地红了；而我照着 `len(entries)` 把 DEVELOPMENT.md 的 43 改成"
            "44 —— **那 44 里有一条是过期的**。过期条目不是豁免，它是门禁"
            "正在报的债。照着它数，文档就会比门禁多报一条",
        target="docs/DEVELOPMENT.md",
        old="剩下 57 条才是真该留下的。",
        new="剩下 58 条才是真该留下的。",
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="sast/exclude-count-hardcoded-again",
        why="排除理由里的条数又被写死。**这一条是真的发生过**：理由里写着"
            "「全量套件 2,307 条」，写死的当天是对的，之后每加一批测试它就"
            "悄悄变成一句假话 —— 而它出现在**门禁输出**里，读者会当成实测值。"
            "实测值早已漂到两千四百多",
        target="scripts/sast_gate.py",
        old='     "pytest 的断言就是这门语言的表达方式，逐条豁免既没意义也写不完"\n'
            '     "（本次扫描实测排除量见下方输出，不是写死的数字）。"',
        new='     "pytest 的断言就是这门语言的表达方式，逐条豁免既没意义也写不完，"\n'
            '     "全量套件 2,487 条。"',
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="sast/reported-exclusion-count-stopped-following-the-scan",
        why="输出里的排除条数不再跟着本次扫描走，改成一个常量。数字仍然"
            "「看起来是实测的」，但它已经与这一次运行无关 —— 这比完全不报"
            "这个数更糟，因为它让人以为有人量过",
        target="scripts/sast_gate.py",
        old='        print("  %s @ %s —— %s（本次实测 %d 条）" % (tid, prefix, why, n))',
        new='        print("  %s @ %s —— %s（本次实测 2487 条）" % (tid, prefix, why))',
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="ci/windows-console-encoding-crash",
        why="去掉 stdout/stderr 的 UTF-8 兜底。**这一条是真的发生过**："
            "2026-10-19 第一次把三道新门禁接进 CI，windows-3.12 在 SAST 这步"
            "红了，报 `UnicodeEncodeError: 'charmap' codec can't encode "
            "characters`，而 ubuntu 两条全绿。**那次红与代码里有没有洞毫无"
            "关系**，只是 Windows 控制台默认 cp1252 装不下中文。症状极具"
            "误导性：门禁全绿、step 红了，看起来像功能坏了",
        target="scripts/sast_gate.py",
        old='for _stream in (sys.stdout, sys.stderr):\n'
            '    try:\n'
            '        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+\n'
            '    except (AttributeError, ValueError, OSError):                # pragma: no cover\n'
            '        pass',
        new="pass  # 兜底被删掉了",
        tests=["tests/test_invariants.py"],
    ),
    Mutation(
        id="deps/dependabot-chokes-on-cheatsheet",
        why="把 requirements-optional.txt 里的安装命令取消注释。同一次提交里"
            "的另一个真实事故：Dependabot 的 pip 生态按 `requirements*.txt` "
            "通配扫**全部**文件（不区分 pip 会不会执行），于是报 "
            "`dependency_file_not_evaluatable: Invalid requirement: "
            "'pip install log-ai-compressor'`，整条 Dependabot run 变红。"
            "原判据只管「pip 会执行的那两个文件」，而漏掉的正是这一个",
        target="requirements-optional.txt",
        old="# pip install log-ai-compressor\n",
        new="pip install log-ai-compressor\n",
        tests=["tests/test_packaging.py"],
    ),
    Mutation(
        id="sast/unknown-finding-tolerated",
        why="新发现不再让门禁红。于是「代码里新冒出来的洞」被静默吸收 —— "
            "而这正是 SAST 存在的唯一理由",
        target="scripts/sast_gate.py",
        old="        if e is None:",
        new="        if e is None and False:",
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="sast/b101-exclude-widened-to-package",
        why="把 tests/ 下的 B101 排除写宽成**整个项目**都排除。于是"
            "log_ai_compressor/ 与 scripts/ 里的裸 assert 一起消失 —— "
            "而发布代码里的裸 assert 是真问题（-O 会整条删掉）。"
            "排除项和基线条目一样，是一种「我决定过」的记录，写宽了就没人再看",
        target="scripts/sast_gate.py",
        old='    ("B101", "tests/",',
        new='    ("B101", "",',
        tests=["tests/test_sast_gate.py"],
    ),
    Mutation(
        id="perf/counter-instrumentation-removed",
        why="**这一条是真的发生过。** 第一版把仪表装在 clustering."
            "similarity() 上，而热路径 _find_similar 直接用 SequenceMatcher，"
            "于是计数恒为 0、门禁每条都绿 —— 它守的路径一次都没被走到。"
            "报数为 0 的计数器等于没装",
        target="scripts/perf_gate.py",
        old='        def ratio(self):\n'
            '            counters["ratio"] += 1\n'
            '            return super().ratio()',
        new='        def ratio(self):\n'
            '            return super().ratio()',
        tests=["tests/test_perf_gate.py"],
    ),
    Mutation(
        id="perf/counters-no-longer-compared",
        why="只比簇数与条目数，不比工作量计数。于是真实的速度回归"
            "（多扫一遍级别桶、剪枝被提前去掉）完全不会被拦到",
        target="scripts/perf_gate.py",
        old='    for key in ("counters", "clusters", "entries"):',
        new='    for key in ("clusters", "entries"):',
        tests=["tests/test_perf_gate.py"],
    ),
    Mutation(
        id="perf/workload-switches-to-localtime",
        why="负载改用 localtime。同一 seed 在不同时区生成不同字节的日志，"
            "基线会以「平台差异」的名义变红 —— 而那是门禁自己的 bug。"
            "这一条守的是「跨平台可比」这个前提本身",
        target="scripts/perf_gate.py",
        old='ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(base + i // 100))',
        new='ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + i // 100))',
        tests=["tests/test_perf_gate.py"],
    ),
    Mutation(
        id="paga/ga-condition-b-dropped",
        why="GA 少掉条件 (b)「该组必须包含这个 EventId 的全部出现」。"
            "少了它，**聚类越碎分越高** —— 而聚得碎恰恰是聚类做坏了的形态。"
            "这条是 LogParser 原始公式里最容易被抄漏的一行",
        target="scripts/eval_logparser_pa_ga.py",
        old="        if len(log_ids) != gt_total[only]:",
        new="        if False:",
        tests=["tests/test_logparser_pa_ga.py"],
    ),
    Mutation(
        id="paga/pa-skips-shared-cleaning",
        why="PA 两侧不再过同一个归一化函数（改成直接比原始模板串）。"
            "归一化是让「同一条事件的不同写法」能被认出同一件事的那一步；"
            "去掉它，PA 只在字面完全相同时才给分 —— 一个更严、"
            "且**不可与任何论文数字比较**的口径",
        target="scripts/eval_logparser_pa_ga.py",
        old="        if clean_content(mine) == clean_content(template):",
        new="        if mine == template:",
        tests=["tests/test_logparser_pa_ga.py"],
    ),
    Mutation(
        id="dep/self-check-tolerates-a-silent-scan",
        why="阳性对照没报出漏洞时不再判定「门禁失能」。于是 pip-audit 哪天"
            "静默什么都不扫，CI 照样全绿 —— 而**一个报「检查通过」但其实"
            "什么都没扫的门禁，比没有门禁更糟**",
        target="scripts/dep_audit_gate.py",
        old="    if _control_name() not in ctl[\"vulns\"]:",
        new="    if False and _control_name() not in ctl[\"vulns\"]:",
        tests=["tests/test_dep_audit_gate.py"],
    ),
    Mutation(
        id="dep/self-referential-dep-audited",
        why="不再跳过 `log-ai-compressor[...]` 自引用。pip-audit 于是去解析"
            "PyPI 上**上次发版**的那个包 —— 本次提交的依赖树被换成了一个"
            "历史快照，而报告仍然看起来很正常",
        target="scripts/dep_audit_gate.py",
        old="            if name.replace(\"-\", \"_\").lower() == \"log_ai_compressor\":",
        new="            if False:",
        tests=["tests/test_dep_audit_gate.py"],
    ),
    # 下面三条记的是同一场事故的两面：把阳性对照混进本项目的扫描，
    # **省下了一次网络往返**，代价是本项目的结论开始依赖对照臂的约束。
    Mutation(
        id="dep/control-pin-merged-into-project-scan",
        why="阳性对照那条 `urllib3==1.25.8` 被塞进本项目的扫描（省一次网络"
            "往返，看起来是纯优化）。**这一条是真的发生过**：混扫时报出一条"
            "`requests PYSEC-2026-2275`，而单独扫本项目是干净的 —— 那条漏洞"
            "由对照臂的 pin 造成了解析回退，与本项目无关。让本项目的结论"
            "依赖于对照臂的约束，就是把探针装在了错误的地方",
        target="scripts/dep_audit_gate.py",
        old="    reqs: List[str] = list(project.get(\"dependencies\") or [])",
        new="    reqs: List[str] = [POSITIVE_CONTROL_PIN] + list(project.get(\"dependencies\") or [])",
        tests=["tests/test_dep_audit_gate.py"],
    ),
    Mutation(
        id="dep/project-scan-drops-transitive-deps",
        why="给本项目的扫描也加上 `--no-deps`（为了和对照臂对称）。传递依赖"
            "于是整个逃出扫描范围 —— 而传递依赖恰恰是最容易出事、也最不会"
            "被写进 `pyproject.toml` 的那一层。**对称在这里是错的**",
        target="scripts/dep_audit_gate.py",
        old="        return _parse(_run(req))\n\n\ndef audit_control",
        new="        return _parse(_run(req, \"--no-deps\"))\n\n\ndef audit_control",
        tests=["tests/test_dep_audit_gate.py"],
    ),
    Mutation(
        id="dep/json-output-dropped",
        why="`--format json` 被去掉（或换成文本输出）。`--format json` 是"
            "「扫不动」与「干净」唯一的分界：pip-audit 两种情况都返回非零，"
            "判据是**能不能解析出 JSON**。它一掉，判据就退化成看退出码，"
            "而第一版正是那样把一次网络超时报成了「依赖有已知漏洞」—— 假红",
        target="scripts/dep_audit_gate.py",
        old="            \"--progress-spinner\", \"off\", \"--format\", \"json\", *extra]",
        new="            \"--progress-spinner\", \"off\", *extra]",
        tests=["tests/test_dep_audit_gate.py"],
    ),
    Mutation(
        id="fetch/https-guard-bypassed",
        why="https 白名单被短路。SAST 基线里那两条 B310 的理由写的正是"
            "「`_require_https()` 挡着」—— 守卫一删，那条豁免立刻变成假话，"
            "而基线本身仍然绿。这正是「判据落在被守对象的邻居上」的形状",
        target="scripts/fetch_datasets.py",
        old='    if scheme != "https":',
        new="    if False:",
        tests=["tests/test_fetch_datasets_scheme_guard.py"],
    ),
    Mutation(
        id="ci/gate-step-replaced-with-echo",
        why="把 `run: python scripts/sast_gate.py` 换成 `run: echo`。"
            "仓库里仍然有那道门禁、仍然有它的测试、本地仍然跑得绿 —— "
            "而 GitHub 上它一天都不会跑。**判据若只认步骤名不认 run 正文，"
            "这条变异会存活**，正对着它想防的那件事（本项目已在 publish-mcp "
            "上栽过一次）",
        target=".github/workflows/ci.yml",
        old="        run: python scripts/sast_gate.py",
        new="        run: echo 'sast skipped'",
        tests=["tests/test_ci_gate_wiring.py"],
    ),
    Mutation(
        id="wording/render-count-differs-from-word-set",
        why="把渲染口径的判据从「次数差」改回「词集合差」 —— 于是同一个词"
            "「字面出现一次 + 被语法藏起来一次」时，藏起来的那次**被整条吞掉**。"
            "而工作区/历史的基线是 0，这条洞在报告里长得跟「一切正常」一模一样。"
            "2026-10-21 的外部复核抓到的就是这一条",
        target="tests/test_no_job_wording.py",
        old=("    plain = dict(_hits_in(text))\n"
             "    out = []\n"
             "    for w, n in _hits_in(_render_normalize(text)):\n"
             "        extra = n - plain.get(w, 0)\n"
             "        if extra > 0:\n"
             "            out.append((w, extra))\n"
             "    return out"),
        new=("    plain = {w for w, _n in _hits_in(text)}\n"
             "    return [(w, n) for w, n in _hits_in(_render_normalize(text))\n"
             "            if w not in plain]"),
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/render-reference-link-removed",
        why="去掉渲染口径里对**引用式链接**的处理 —— 于是 `[X][ref]Y` 这种写法"
            "整个看不见，而读者看到的是那个完整的词。基线 0 只说明**这一批**词"
            "没被藏起来，不说明这种藏法看得见",
        target="tests/test_no_job_wording.py",
        old='    t = re.sub(r"\\[([^\\]]*)\\]\\[[^\\]]*\\]", r"\\1", t)',
        new="    t = t",
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="ci/long-pole-job-gets-an-if-condition",
        why="给记分卡那个独立 job 加 `if: false` —— 它就**整条不跑**，而外面看，"
            "CI 照样是绿的（那个 job 被跳过，结论是 success）。"
            "独立 job 的跑哪条矩阵由 job 自己声明（runs-on + setup-python），"
            "所以这个 `if:` 只会是错的。2026-10-21 的第五轮外部复核补上的缺口",
        target=".github/workflows/ci.yml",
        old="  mutation:\n    runs-on: ubuntu-latest\n",
        new="  mutation:\n    runs-on: ubuntu-latest\n    if: false\n",
        tests=["tests/test_ci_long_pole.py"],
    ),
    Mutation(
        id="wording/render-invisible-chars-narrowed",
        why="把渲染口径的不可见字符表缩回旧版（去掉定向隔离符、组合用记号、"
            "变体选择符）—— 读者看不见它们，而「看不见」正是「藏起来」的定义。"
            "2026-10-21 的第五轮外部复核指出这三种漏了",
        target="tests/test_no_job_wording.py",
        old=("    t = re.sub(\"[\\u200b-\\u200f\\ufeff\\u00ad\\u061c\\u180e\\u202a-\\u202e\"\n"
             "               \"\\u2060-\\u2064\\u2066-\\u2069\\ufff9-\\ufffb\\u034f\\ufe00-\\ufe0f]\", \"\", t)"),
        new=("    t = re.sub(\"[\\u200b-\\u200f\\ufeff\\u00ad\\u061c\\u180e\\u202a-\\u202e\"\n"
             "               \"\\u2060-\\u2064\\ufff9-\\ufffb]\", \"\", t)"),
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="ci/long-pole-budget-shrunk-to-shared",
        why="把变异记分卡那个独立 job 的 timeout 调回跟 `test` job 一样 —— "
            "预算于是变回共享的，长杆照样把短杆挤爆。2026-10-21 那次"
            "实测就是 `test` job 的 20 分钟被记分卡吃穿、run 结论是 "
            "**cancelled**，而前面十道门禁全 success —— "
            "一个把「十道过了、一道超时」显示成「整个 run 取消」的结论，"
            "比红本身更费时间",
        target=".github/workflows/ci.yml",
        old="    timeout-minutes: 60",
        new="    timeout-minutes: 20",
        tests=["tests/test_ci_long_pole.py"],
    ),
    Mutation(
        id="ci/long-pole-shallow-clone",
        why="记分卡那个 job 不取完整历史。记分卡里有几条变异直接改 "
            "tests/test_no_job_wording.py，而那道门禁要扫「全部历史版本的"
            "文件内容」—— 浅克隆会让它退化成只看当前那一个版本。"
            "那正是「扫了 0 个文件却报通过」那个形状的翻版",
        target=".github/workflows/ci.yml",
        old="          # 版本 —— 那正是「扫了 0 个文件却报通过」那个形状的翻版。\n"
            "          fetch-depth: 0",
        new="          # 版本 —— 那正是「扫了 0 个文件却报通过」那个形状的翻版。",
        tests=["tests/test_ci_long_pole.py"],
    ),
    Mutation(
        id="ci/gate-marked-continue-on-error",
        why="给三道新门禁加 `continue-on-error`。门禁一旦可以失败后继续，"
            "它就不再是门禁 —— 而 CI 的 job 级结论仍然是 success，"
            "从外面完全看不出来",
        target=".github/workflows/ci.yml",
        old="        run: python scripts/perf_gate.py",
        new="        run: python scripts/perf_gate.py\n"
            "        continue-on-error: true",
        tests=["tests/test_ci_gate_wiring.py"],
    ),
    Mutation(
        id="paga/table-cell-drifts-from-csv",
        why="PA/GA 表里的一格与 docs/logparser_pa_ga.csv 对不上。这是本项目"
            "**第一次**把外部定义的指标写进文档，而 v2.9.9 的形状正是"
            "「代码改了、数字变了、表原地不动、全部门禁绿着」。"
            "那张表当时靠的是人的自觉",
        target="docs/BENCHMARK.md",
        old="| Apache | 51,978 | 29 | 33 | 99.10% | 99.31% |",
        new="| Apache | 51,978 | 29 | 33 | 99.90% | 99.31% |",
        tests=["tests/test_logparser_pa_ga_table.py"],
    ),
    Mutation(
        id="paga/summary-verdict-flipped",
        why="把汇总结论里的胜场数改错（真实值 GA 4 胜 6 负）。**这一条是"
            "真的发生过**：本文第一版手算成 3 胜 7 负，漏了 Zookeeper"
            "（99.48% vs 99.37%）。汇总口径比逐格数字更容易写错，"
            "因为没有任何一格会因为它错而变红 —— 只有专门重算它的那条"
            "判据抓得住",
        target="docs/BENCHMARK.md",
        old="GA 上 4 胜 6 负（Mac / OpenStack / Proxifier / Zookeeper 胜）",
        new="GA 上 3 胜 7 负（Mac / OpenStack / Proxifier 胜）",
        tests=["tests/test_logparser_pa_ga_table.py"],
    ),
    Mutation(
        id="paga/changelog-verdict-flipped",
        why="**同一个手算错漏在了 CHANGELOG 里**。docs/BENCHMARK.md 被逐格"
            "对账抓出来改了，同一句话在 CHANGELOG 里却原封不动留到了提交前 —— "
            "同一个 commit 里既改掉又留着。原因很简单：那句话在文档里出现了"
            "**四遍**，而只有一遍下面有测试",
        target="CHANGELOG.md",
        old="逐数据集：PA 上 6 胜 4 负，GA 上 4 胜 6 负。",
        new="逐数据集：PA 上 6 胜 4 负，GA 上 3 胜 7 负。",
        tests=["tests/test_logparser_pa_ga_table.py"],
    ),
    # ---- GA 失分分解（v2.11.6 批1） ---------------------------------
    Mutation(
        id="ga-gap/bucket-silently-dropped",
        why="分解表的三摊里吞掉一摊（未解析）。**没有任何一行会红** —— "
            "百分比照印，只是加起来不到 100%，而 97% 和 100% 在表格里"
            "长得几乎一样。这张表会被当成「GA 为什么低」的答案写进文档，"
            "所以漏掉的那一摊必须由恒等式守住，而不是靠人加一下",
        target="scripts/diagnose_ga_gap.py",
        old="    unparsed = total_lines - len(pred_group)",
        new="    unparsed = 0   # 把未解析那摊吞掉了",
        tests=["tests/test_diagnose_ga_gap.py"],
    ),
    Mutation(
        id="ga-gap/condition-b-boundary-moved",
        why="条件 (b) 的边界从「不等于全集」改成「大于全集」。分解表与 "
            "**被发布那个 GA** 不再是同一个量，而两边都照常输出正常数字 —— "
            "只有那条逐位对等的断言抓得住。这正是本项目记过多次的形状："
            "判据指错了对象，却看起来完全正常",
        target="scripts/diagnose_ga_gap.py",
        old="        if len(log_ids) != gt_total[only]:\n"
            "            over_split += len(log_ids)\n"
            "            continue",
        new="        if len(log_ids) > gt_total[only]:\n"
            "            over_split += len(log_ids)\n"
            "            continue",
        tests=["tests/test_diagnose_ga_gap.py"],
    ),
    Mutation(
        id="sweep/reaches-in-and-changes-the-default",
        why="阈值扫描脚本改成直接改 `CLUSTER_SIMILARITY_THRESHOLD` 再建"
            "不带参数的聚类器 —— 这是 AGENTS.md §2.3 明令禁止的那件事"
            "（对着评测集调参），而脚本读起来仍然完全正常：它照旧打印"
            "一张漂亮的表。产品默认值被顺手改掉，没有任何一行会红，"
            "下一个看那张表的人还会以为那个默认本来就存在",
        target="scripts/sweep_threshold_pa_ga.py",
        old="    clusterer = ErrorClusterer(similarity_threshold=threshold)",
        new="    import log_ai_compressor.core.clustering as _C\n"
            "    _C.CLUSTER_SIMILARITY_THRESHOLD = threshold\n"
            "    clusterer = ErrorClusterer()",
        tests=["tests/test_diagnose_ga_gap.py"],
    ),
    # ---- GA 失分分解的文档表（v2.11.6 批2） --------------------------
    Mutation(
        id="ga-gap/table-cell-drifts-from-csv",
        why="「结果四·五」表 D 里的一格与 docs/ga_gap_decomposition.csv "
            "对不上。这与 v2.9.9 那次事故**完全同形**：改了聚类、基准数字"
            "变了、表格原地不动、全部门禁绿着。而这张表是本项目对"
            "「能不能全胜」这个问题的**唯一**回答 —— 它错了，问题就"
            "变成了一个看起来有答案的错误答案",
        target="docs/BENCHMARK.md",
        old="| Apache | 99.31% | 100.00% | 0.0% | — | 100.0% | — | 360 |",
        new="| Apache | 99.91% | 100.00% | 0.0% | — | 100.0% | — | 360 |",
        tests=["tests/test_ga_gap_table.py"],
    ),
    Mutation(
        id="ga-gap/zero-loss-printed-as-zero-percent",
        why="把「零失分」写成 `0.0%`。Apache 的 Drain3 是 GA 100.00%，"
            "分母为 0，`_pct()` 返回 0.0 —— 而表格里那两格现在写的是"
            "破折号。写成 0.0% 会被读成「失分全在另一桶」，"
            "**而真相是根本没有失分**。这类格最容易被后来的人"
            "「补齐」成数字，而表格看起来完全正常",
        target="docs/BENCHMARK.md",
        old="| Apache | 99.31% | 100.00% | 0.0% | — | 100.0% | — | 360 |",
        new="| Apache | 99.31% | 100.00% | 0.0% | 0.0% | 100.0% | 0.0% | 360 |",
        tests=["tests/test_ga_gap_table.py"],
    ),
    Mutation(
        id="ga-gap/bucket-share-denominator-swapped",
        why="两桶占比的分母从**失分行数**换成**标注行数**。同一批分子，"
            "占比从 51.4% 掉到 20.0% 量级 —— 而三个百分比加起来"
            "从 100% 变成不到 40%，**没有任何一条判据会自己发现**"
            "「加起来不到 100」这件事，读者也不会去加。"
            "「几乎对半」这句话会整个反过来，而表格照常排版整齐",
        target="docs/BENCHMARK.md",
        old="| 过合并（条件 a 失败） | 438,085 | 51.4% |",
        new="| 过合并（条件 a 失败） | 438,085 | 20.0% |",
        tests=["tests/test_ga_gap_table.py"],
    ),
    # ---- SAST 条数对账的作用域（v2.11.6 批3） -----------------------
    Mutation(
        id="sast/count-scope-reverted-to-whole-changelog",
        why="把豁免条数对账的作用域从「CHANGELOG 顶格版本节」改回**整份文件**。"
            "这看起来是简化，实际是**判据读错了对象**：v2.11.5 那一节里"
            "写下的当时条数是历史记录（AGENTS.md §2.4 明写历史不重写），"
            "而整文件扫会把它当成对现状的声明。于是每次基线增长，"
            "唯一能过的改法变成篡改历史或让 CI 变红 —— 两条都是错的，"
            "而门禁**仍然是绿的**。这条是 2026-10-19 实测撞出来的："
            "我加豁免时它立刻把 v2.11.5 的旧数字判成了「写错」",
        target="tests/test_sast_gate.py",
        old='    if doc != "CHANGELOG.md":\n        return text\n',
        new='    if doc != "CHANGELOG.md":\n'
            '        pass  # 变异：作用域退回整份文件\n',
        tests=["tests/test_sast_gate.py"],
    ),
    # ---- 取消之后的进度事件（v2.11.6 批4） -------------------------
    Mutation(
        id="web-jobs/progress-emitted-after-cancel",
        why="`_progress_cb` 不再看取消标记。取消之后前端还在收进度事件 —— "
            "而这一行**过去只被一个竞态顺带覆盖**：test_cancel 拿 52 行的"
            "样本日志起任务再立刻取消，任务通常在 cancel 到达前就跑完了，"
            "于是 `cancel_event.is_set()` 从未为真。后果不是「覆盖率低一点」，"
            "而是 **README 里写死的「漏行 N / M 语句」会 ±1 抖动，"
            "而 CI 拿它当红线**。实测：加了一批测试改变机器负载后，"
            "ubuntu-3.12 的 158 变成 159，CI 立刻红 —— 而测试一条都没挂。"
            "现在有两条确定性的测试（取消后不发 / 未取消照发）钉住它",
        target="log_ai_compressor/web/jobs.py",
        old="        if self.cancel_event.is_set():\n            return\n"
            "        self._emit(\"progress\", **(info or {}))",
        new="        self._emit(\"progress\", **(info or {}))",
        tests=["tests/test_web.py"],
    ),
    # ---- 阈值断言超出采样范围（v2.12.0 发布前抓到的真错误） ------------
    # 这五条对应同一个事故：**正文里那句「OpenSSH 与 Hadoop 在 0.70 时分别
    # 掉到 60.20 与 49.36」把 Linux 的 60.20 写进了 OpenSSH 那一格**，
    # 而 OpenSSH **压根没被扫过**（五档曲线只扫了 4 个数据集）。
    # 补测之后真值是 34.68，**方向还是反的**（比 0.85 的 32.08 高 2.60pp）。
    #
    # 为什么前面四条表体守卫一条都没红：它们读的是**表 F 的表体**
    # （形状 / 加粗 / 均值 / PA 不敏感），而错的是**正文里的一句断言**。
    # 这与 `bench/tier-*` 那两条「从 CSV 重算」是不同的一维：
    # 那两条守「格里的数」，这五条守「**说这句话的那个数据集到底测没测过**」。
    Mutation(
        id="sweep/claim-about-unswept-dataset",
        why="正文里出现一条**点名了从未被扫过的数据集**的阈值断言"
            "（这里拿 Proxifier 代 OpenSSH —— 后者现在已被补测覆盖，"
            "前者确定不在任何一张受控表里）。这是本项目**第二次**栽在"
            "同一个形状上：第一次是「只跑了 0.85/0.90/0.95 就宣称 0.85 在"
            "曲线顶点」，这一次是「只扫了 4 个数据集就给第 5 个报了数」。"
            "错因都是**结论的适用范围比采样范围宽**，而两次都全绿",
        target="docs/BENCHMARK.md",
        old="也就是说 OpenSSH 换成 0.70 **只涨 2.60pp**",
        new="Proxifier 换成 0.70 掉到 **60.20**，也就是说 Proxifier 只涨 2.60pp",
        tests=["tests/test_ga_gap_table.py"],
    ),
    Mutation(
        id="sweep/supplement-control-value-broken",
        why="补测表的**阳性对照**那一格被改坏（0.85 不再复现已发布快照里的 "
            "32.08）。补测只有一档（0.70）时，一次跑歪的测量也能给出"
            "**结构完全正常**的数字 —— 而 0.85 那一档的全部意义就是"
            "「同一份代码在已知答案上必须答对」。阳性对照被改坏却没人红，"
            "等于补测本身不可信，而它照样排在文档里",
        target="docs/BENCHMARK.md",
        old="| 0.85 | 31.02 | 32.08 | 171 |",
        new="| 0.85 | 31.02 | 34.68 | 171 |",
        tests=["tests/test_ga_gap_table.py"],
    ),
    Mutation(
        id="sweep/supplement-direction-flipped",
        why="把补测的 0.70 那一格改成已经**追过 Drain3**。原文那句「OpenSSH "
            "换成 0.70 也追不上」是被写错过一次的方向性结论（原文说"
            "「都比 0.85 差」，而真值 34.68 比 32.08 **高** 2.60pp）。"
            "只守数值不够：数字对了而**方向反了**，读者照样会照着错的结论读下去",
        target="docs/BENCHMARK.md",
        old="| 0.70 | 31.02 | **34.68** | 34 |",
        new="| 0.70 | 31.02 | **75.00** | 34 |",
        tests=["tests/test_ga_gap_table.py"],
    ),
    Mutation(
        id="sweep/supplement-caption-removed",
        why="补测表的标题行不再点名数据集。这一条守的是**解析器自己的退化**："
            "数据集名是写在标题行上的（表 G 一行只描述一个数据集），"
            "标题一改，被扫过的集合就少一个成员，于是 OpenSSH 被误判成"
            "「没被扫过」。方向看着相反（红在一个**本来没问题**的地方），"
            "但那正是必要的：宁可红在误报上，也不许静默丢掉一个成员",
        target="docs/BENCHMARK.md",
        old="**表 G · OpenSSH 的阈值补测（表 F 没有扫它，故单列）**",
        new="**附录 · 两档补测**",
        tests=["tests/test_ga_gap_table.py"],
    ),
    Mutation(
        id="sweep/supplement-row-label-annotated",
        why="补测表的**行标签**从纯阈值变成带括号注脚。第一版判据直接"
            "`body[\"0.85\"]`，于是这一改抛的是 `KeyError` —— **那看起来像"
            "脚本崩了，不像一句判决**，读者会去改测试而不是改文档。"
            "所以判据现在先判「行标签必须是纯阈值」再取值：同一个缺陷"
            "要红成一句能照着修的话",
        target="docs/BENCHMARK.md",
        old="| 0.85 | 31.02 | 32.08 | 171 |",
        new="| 0.85（对照档） | 31.02 | 32.08 | 171 |",
        tests=["tests/test_ga_gap_table.py"],
    ),
    # ---- 措辞门禁（2026-10 批） ----------------------------------------
    Mutation(
        id="wording/job-word-written-into-doc",
        why="在文档里写下一个与个人就业语境相关的词。看起来只是一句普通的"
            "说明 —— 而**当时没有任何门禁会红**：CI 跑的是测试与数字对账，"
            "没有一条检查措辞。于是 2026-10 那次全历史重写的成果，"
            "下一次有人动笔就又写回来了。这正是 AUDIT.md 形态八那条："
            "「表里的数全对，而表外那句断言没人看守」",
        target="docs/FAQ.md",
        old="每一条都按「问题 → 实测依据 → 本项目做不到什么 → 怎么自己验」写。",
        # 那个词用码点拼出来 —— 这份文件本身也在扫描范围内，
        # 写成字面量就会让仓库里多出一处禁用词，而门禁会先把自己判红。
        new=("每一条都按「问题 → 实测依据 → 本项目做不到什么 → 怎么自己验」写。\n"
             "> 顺带一提，"
             + "".join(chr(c) for c in (0x9762, 0x8BD5))
             + "的时候也要注意这一条。"),
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/forbidden-list-emptied",
        why="把禁用词表清空 —— 门禁于是恒真：三个扫描面全都「通过」，"
            "而它**一个词都没查**。这条守的是门禁自己的牙齿："
            "「报数为 0 的计数器等于没装」是本项目最贵的一课"
            "（性能门禁的第一版就装在一条根本没被调用的路径上，"
            "计数恒为 0，于是每一条都绿，而它守的路径一次都没被走到）",
        target="tests/test_no_job_wording.py",
        old="FORBIDDEN = tuple(_decode(c) for c in _FORBIDDEN_CODES)",
        new="FORBIDDEN = ()",
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/tag-message-first-line-only",
        why="把面 4 退回「每个 tag 只扫第一行」。旧实现用 "
            "`for-each-ref --format='%(contents)'`，而 %(contents) 是"
            "**多行**值：一次 splitlines() 把说明拆成多行，"
            "「只认含 tab 的行」那一步把没有 tab 的**续行整条丢掉** —— "
            "于是每个 tag 只有第一行被检查。实测：本仓库 32 个 annotated "
            "tag 的**整条对象**共 270 行（其中说明文字 110 行），"
            "旧实现只扫到 33 行。2026-10 的外部复核抓到的就是这个洞 —— "
            "它当时没漏到东西，但那是运气，不是口径。",
        target="tests/test_no_job_wording.py",
        old='        msgs.append((name, _git("cat-file", "tag", sha, repo=repo)))',
        new=('        msgs.append((name, _git("cat-file", "tag", sha, repo=repo)'
             '.splitlines()[0]))'),
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/gb18030-fallback-removed",
        why="把解码的 GB18030 兜底去掉 —— 于是 GBK 编码的文件又被当成 UTF-8 "
            "解，中文变成一串 U+FFFD。**那不是「里面没有词」，那是「看不见」**："
            "2026-10 的外部复核在编码这一维找到一个我完全没想到的洞"
            "（工作区 1 个 GBK 文本、历史 2 个），当时都是 0 命中 —— "
            "但那是**因为里面本来就没有词**，不是因为门禁看得见。",
        target="tests/test_no_job_wording.py",
        old='        return raw.decode("gb18030")',
        new='        return raw.decode("utf-8", errors="replace")',
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/whitespace-split-detector-disabled",
        why="让「被空白拆开的词」这个探测器恒返回空 —— 于是「工作区 0 处、"
            "历史不超过基线 6」这两条断言**全都变成空跑**，而它们守的恰恰是"
            "那道「折行的词看不见」的缺口。报数为 0 与报数为错在输出里长得"
            "一模一样：**探测器恒返回空**与「真的没有」无法区分。"
            "这条守的是探测器自己的牙齿。",
        target="tests/test_no_job_wording.py",
        old=("    out = []\n"
             "    for w in FORBIDDEN:\n"
             "        if \"\".join(w.split()) not in cands:\n"
             "            continue\n"
             "        for m in re.finditer(_flex_pattern(w), text):\n"
             "            span = text[m.start():m.end()]\n"
             "            if span == w:                       # 字面 —— 红线会报它\n"
             "                continue\n"
             "            if not any(ch.isspace() for ch in span):   # 变体，不是「被拆开」\n"
             "                continue\n"
             "            out.append(w)\n"
             "            break\n"
             "    return out"),
        new="    return []",
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/whitespace-split-literal-not-excluded",
        why="去掉「字面命中不算被拆开」那一条 —— 于是**字面出现**也被算进"
            "「被拆开的词」里。第一版就是这么错的（`all([])` 对空串恒真），"
            "后果是那个数字不再是「缺口的大小」，而是「字面命中 + 拆开命中」"
            "的混账：一旦历史上出现一处字面命中，基线就会被调高去**掩护**它。"
            "2026-10 的外部复核把这一条复现了 —— 判据是对的，钉它的测试"
            "（`字面必须返回 []`）也是新加的。",
        target="tests/test_no_job_wording.py",
        old=("            if span == w:                       # 字面 —— 红线会报它\n"
             "                continue\n"),
        new="",
        tests=["tests/test_no_job_wording.py"],
    ),
    # ---- U+FFFD 这条（2026-10，v2.12.5 批） ------------------------------
    Mutation(
        id="wording/replacement-char-detector-returns-nothing",
        why="让 U+FFFD 探测器恒返回空 —— 于是「工作区 0 处」与「历史不超过基线 91」"
            "这两条断言**全都变成空跑**，而它们守的恰恰是「读出来的不是原文」"
            "那个缺口：一个汉字的 3 个字节被逐字节替换之后，那句话在扫描口径下"
            "根本不存在，而门禁还是绿的。报数为 0 与报数为错在输出里长得一模一样 ——"
            "**探测器恒返回空**与「真的没有」无法区分。这条守的是探测器自己的牙齿。",
        target="tests/test_no_job_wording.py",
        old='    return [i for i, c in enumerate(text) if c == "\\ufffd"]',
        new="    return []",
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/replacement-char-worktree-scan-skipped",
        why="把工作区那条扫描整个跳过 —— 于是「工作区 0 处」变成**空跑**："
            "一个文件都没读，而报告仍然说「0 处」。**报「检查通过」但其实"
            "什么都没扫**的门禁，比没有门禁更糟 —— 没有门禁，你知道那里没检查。",
        target="tests/test_no_job_wording.py",
        old="    for rel in paths:\n        raw = (ROOT / rel).read_bytes()",
        new="    for rel in []:\n        raw = (ROOT / rel).read_bytes()",
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/replacement-char-scan-narrowed-to-docs",
        why="把扫描范围缩到只剩文档 —— 于是源码注释与测试里的坏字**不再有人管**。"
            "这一类是实测踩过的：5 处坏字里有 4 处在 `scripts/` 与 `tests/` 下，"
            "只扫文档的话它们一条都抓不到，而门禁照样报「工作区 0 处」。"
            "只测「工作区 0 处」而不测**扫的是不是全部**，这条断言就是空的。",
        target="tests/test_no_job_wording.py",
        old="    for rel in paths:\n        raw = (ROOT / rel).read_bytes()",
        new=('    for rel in [p for p in paths if p.startswith("docs/")]:\n'
             "        raw = (ROOT / rel).read_bytes()"),
        tests=["tests/test_no_job_wording.py"],
    ),
    Mutation(
        id="wording/multi-matcher-boundary-check-dropped",
        why="把多模式匹配器的 ASCII 边界判定去掉 —— 于是英文缩写会出现在"
            "**更长的单词内部**。逐词口径下那不是命中："
            "`(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])` 是红线的一部分。"
            "换实现之后这条边界一度是**另写一遍**的 —— 两份实现只要有一处不同，"
            "「门禁看见的」与「门禁应该看见的」就不再是同一件事。"
            "这条守的是那个分叉：改坏边界，等价性测试必须变红。",
        target="tests/test_no_job_wording.py",
        old=('_ASCII_WORDCHARS = frozenset(\n'
             '    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")'),
        new="_ASCII_WORDCHARS = frozenset()",
        tests=["tests/test_wording_matcher_equivalence.py"],
    ),
    Mutation(
        id="wording/multi-matcher-non-overlap-check-dropped",
        why="把多模式匹配器的「非重叠」判定去掉 —— 于是同一个词在**部分重叠**的"
            "两次出现会被数成两次（逐词口径只数一次）。"
            "真实词表里 200 个词只有 **1 个**带非空 border，而只有这种词"
            "才可能与自身重叠 ⇒ **在真实语料上这条规则几乎从不触发**，"
            "删掉它，结果一模一样，测试也就不会红。"
            "规则要靠**构造出来的**用例守着，不能靠「恰好没人踩到」。",
        target="tests/test_no_job_wording.py",
        old="            if s < last_end[wi]:\n                continue\n",
        new="",
        tests=["tests/test_wording_matcher_equivalence.py"],
    ),
    Mutation(
        id="wording/multi-matcher-returns-nothing",
        why="让多模式匹配器恒返回空 —— 于是门禁**一个词都没查**却仍然全绿。"
            "这与 `wording/forbidden-list-emptied` 是同一族："
            "**报数为 0 的计数器等于没装**。"
            "2026-10-21 换匹配器的那一次，这条路径一度完全没有守卫 —— "
            "而换实现正是最容易「让门禁变瞎」的时刻。",
        target="tests/test_no_job_wording.py",
        old="    return [(w, n) for w, n in zip(FORBIDDEN, _MATCHER.counts(text)) if n]",
        new="    return []",
        tests=["tests/test_wording_matcher_equivalence.py"],
    ),
    Mutation(
        id="local-gates/runner-skips-a-gate-step",
        why="让本地门禁运行器**跳过**一条真正的门禁步骤 —— 于是它照样打印「全绿」，"
            "而那道门禁一次都没跑。2026-10-21 的事故就是这个形状：本地那份清单漏了"
            "pytest 全量与变异记分卡两道（最重的两道，也恰恰是能抓住那次事故的那两道），"
            "而 `main` 被推成了红的。**报「检查通过」但其实什么都没跑**的门禁，"
            "比没有门禁更糟 —— 没有门禁，你知道那里没检查。"
            "这条守的是那份清单自己的牙齿。",
        target="scripts/run_local_gates.py",
        old="            if name.lower().startswith(_SETUP_STEP):\n                continue\n",
        new=("            if name.lower().startswith(_SETUP_STEP)"
             " or \"coverage\" in name.lower():\n                continue\n"),
        tests=["tests/test_local_gate_runner_covers_ci.py"],
    ),
    # ---- 第九轮外部复核的修复（2026-10-22 批） -------------------------
    # 这一批的共同点：每一条都对应一个**真实被外部复核抓到的问题**，
    # 或者一个**我自己引入又自己抓到的回归**。其中三条（回读路径、
    # UTF-16、BOM）是「修 bug 2 时自己踩的坑」——
    # 混合编码的逐行兜底让摄入与回读走了两条路，于是同一份没被改过的
    # 文件被报成 TAMPERED（指控了一个没有发生的事）。
    Mutation(
        id="ninth/root-headline-uses-cluster-summary",
        why="根因标题改回用**簇摘要**（外层症状）而不是链尾真因 —— "
            "标题与簇的 reason 两处口径分叉，而标题那一处正是读者唯一会记住的",
        target="log_ai_compressor/export/reporters.py",
        old='    return f"{root_cause_name(top)[:120]}{others}"',
        new='    return f"{top.summary[:120]}{others}"',
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/root-headline-prefers-plain-cluster",
        why="标题不再优先点名**有 Caused-by 链**的簇 —— 证据最硬的那个被换成了"
            "「优先级最高但没有因果链」的那个",
        target="log_ai_compressor/export/reporters.py",
        old="    pool = with_chain or roots",
        new="    pool = roots",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/mixed-encoding-fallback-removed",
        why="混合编码的逐行兜底被去掉 —— GBK 行静默变成一堆 U+FFFD，"
            "而报告里看不出任何异常（**数据被改了**）",
        target="log_ai_compressor/core/encoding.py",
        old=('        alt = self._try(raw, "gb18030")\n'
            "            if alt is not None:\n"
            "                self.repaired += 1\n"
            "                return alt"),
        new=("        alt = None\n"
             "            if alt is not None:\n"
             "                self.repaired += 1\n"
             "                return alt"),
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/mixed-encoding-notice-removed",
        why="混合编码的提示被去掉 —— 「报不出数」与「报数为 0」又分不开了，"
            "读者以为这份日志是干净的",
        target="log_ai_compressor/core/pipeline.py",
        old="    if decoder.repaired:\n        notes.append",
        new="    if False and decoder.repaired:\n        notes.append",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/readback-uses-whole-file-decode",
        why="**回读改回「整文件单编码」** —— 摄入逐行兜底、回读整文件解，"
            "同一份没被改过的文件算出两个摘要 ⇒ 误报 TAMPERED ⇒ "
            "指控了一个没有发生的事（本轮自己引入、自己抓到的坑）",
        target="log_ai_compressor/core/evidence.py",
        old="        out = LogDecoder(enc).read_range(path, line_start, line_end)",
        new=("        import log_ai_compressor.core.encoding as _E\n"
             "        with _E.open_text_stream(path, enc) as _fh:\n"
             "            out = []\n"
             "            for _i, _l in enumerate(_fh, 1):\n"
             "                if _i < line_start:\n"
             "                    continue\n"
             "                if _i > line_end:\n"
             "                    break\n"
             "                out.append(_l)"),
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/wide-encoding-disabled",
        why="UTF-16/UTF-32 也按字节切行 —— 换行符本身是多字节的，"
            "一个字符被劈成两半（实测：2 行变 3 行、行尾多一个 NUL、"
            "出现 4 个 U+FFFD）",
        target="log_ai_compressor/core/encoding.py",
        old="        self._wide = is_wide_encoding(self.encoding)",
        new="        self._wide = False",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/bom-strip-removed",
        why="UTF-8 BOM 不再剥离 —— U+FEFF 留在首行文本里，"
            "用户看到的第一行会多一个看不见的字符",
        target="log_ai_compressor/core/encoding.py",
        old=("                if line.startswith(_UTF8_BOM):\n"
             "                    line = line[len(_UTF8_BOM):]"),
        new=("                if False:\n"
             "                    line = line[len(_UTF8_BOM):]"),
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/report-size-cap-removed",
        why="超长字段不再截断 —— 5 MB 的单行让报告膨胀到 10 MB，"
            "而报告是给人读的",
        target="log_ai_compressor/export/reporters.py",
        old="    if not isinstance(text, str) or len(text) <= MAX_SAMPLE_FIELD_CHARS:",
        new="    if True:",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/binary-refusal-disabled",
        why="二进制输入不再被拒绝 —— 拿 .bin 当日志喂进来会被「成功分析」一遍，"
            "报告写着「压缩 95%」，退出码 0",
        target="log_ai_compressor/cli.py",
        old="    if stats.nul_lines:\n        return True",
        new="    if False:\n        return True",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/output-error-swallowed",
        why="写报告失败时不再转成人话错误 —— 栈回溯糊一屏，"
            "而且是在「成功摘要」之后炸的，滚屏里两者混在一起",
        target="log_ai_compressor/cli.py",
        old=("    except OSError as exc:\n"
             "        raise _OutputError(str(exc), output, code=exc.errno) from exc"),
        new=("    except OSError as exc:\n"
             "        raise RuntimeError(str(exc)) from exc"),
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/json-to-stdout-removed",
        why="`-f json` 不带 `-o` 时 stdout 不再是 JSON —— "
            "接 jq 直接失败，而 `-f json` 看上去像是生效了的",
        target="log_ai_compressor/cli.py",
        old=('    if args.output is None and fmt == "json":\n'
             "        sys.stdout.write(content)"),
        new=('    if False:\n'
             "        sys.stdout.write(content)"),
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/json-fingerprint-removed",
        why="JSON 报告里没有指纹 —— 机器侧没法核对「这两份 JSON 是不是同一份结论」",
        target="log_ai_compressor/export/reporters.py",
        old='        "fingerprint": result_fingerprint(result),',
        new='        "fingerprint": "",',
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/json-notice-removed",
        why="JSON 报告里没有 notice —— 混合编码 / 只看尾部这些**限定了结论适用范围**"
            "的话只写给人看的格式，机器侧拿到的结论看起来与全量分析一模一样",
        target="log_ai_compressor/export/reporters.py",
        old='        "notice": getattr(result, "notice", "") or "",',
        new='        "notice": "",',
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/compare-quiet-removed",
        why="compare 不再认 --quiet —— 只有 run 认，argparse 直接退出码 2，"
            "两个子命令的行为不一致",
        target="log_ai_compressor/cli.py",
        old='    p_cmp.add_argument("--quiet", "-q", action="store_true",',
        new='    p_cmp.add_argument("--quiet-x", "-q", action="store_true",',
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/compare-trace-alignment-removed",
        why="compare 不再按 trace_id 兜底对齐 —— 同一次请求在两个文件里措辞不同"
            "时被算成「新增 1 种 + 消失 1 种」，读起来像「出了新问题、"
            "同时修好了老问题」",
        target="log_ai_compressor/core/comparator.py",
        old="    for tid, b_clusters in sorted(b_traces.items()):",
        new="    for tid, b_clusters in sorted(b_traces.items()):\n        continue",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/compare-matched-by-hardcoded",
        why="对齐依据恒写成「模板」—— 按 trace 对上的那一项看不出来了，"
            "读的人无法判断「共同」到底是怎么对上的",
        target="log_ai_compressor/core/comparator.py",
        old='                    change_rate=change, matched_by="trace",',
        new='                    change_rate=change, matched_by="template",',
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/empty-file-notice-removed",
        why="空文件不再明说「0 行」—— 空文件与「有日志但没定论」被混成同一句话",
        target="log_ai_compressor/core/pipeline.py",
        old="    if not stats.total_lines:\n        notes.append",
        new="    if False:\n        notes.append",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/stdin-dispatch-removed",
        why="`run -` 不再走 stdin —— 管道接不进来，"
            "用户必须先落盘才能分析（日常排障的动线被切断）",
        target="log_ai_compressor/cli.py",
        old='    if args.file == "-":\n        return _run_stdin(args)',
        new='    if False:\n        return _run_stdin(args)',
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/tail-line-offset-ignored",
        why="`--tail` 的行号不再偏移 —— 报告里的「第 1 行」在原文件里是第 96 行，"
            "证据引用指向无关的内容，「可复核」当场失效",
        target="log_ai_compressor/core/pipeline.py",
        old="        line_no = line_offset",
        new="        line_no = 0",
        tests=["tests/test_ninth_round_fixes.py"],
    ),
    Mutation(
        id="ninth/tail-notice-removed",
        why="`--tail` 不再说明「这次只看的是尾部」—— 报告覆盖的不是全量，"
            "而读者以为它覆盖了全部",
        target="log_ai_compressor/core/pipeline.py",
        old='        note = (f"只看最后 {len(lines)} 行（文件共 {total:,} 行，"',
        new=('        note = ("" if True else f"只看最后 {len(lines)} 行'
             "（文件共 {total:,} 行，\""),
        tests=["tests/test_ninth_round_fixes.py"],
    ),
]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def git_exe() -> str:
    """解析 git 的绝对路径，解析不到就明确报错。

    SAST（bandit B607）报「用部分路径启动进程」。这里修而不豁免：
    写死 `"git"` 在 git 不在 PATH 时抛的是 `FileNotFoundError`，
    看不出是「git 缺失」还是「工作区不干净」—— 而这个脚本的整个卖点
    就是工作区脏不脏，出错时说不清正是最不该含糊的地方。
    """
    exe = shutil.which("git")
    if exe is None:
        raise SystemExit("找不到 git —— 变异记分卡靠 git status 兜底还原工作区")
    return exe


def _run_tests(targets: List[str], full: bool) -> int:
    """跑判定用测试。rc == 0 意味着「这条变异活了下来」。"""
    args = [sys.executable, "-m", "pytest", "-q", "--no-header",
            "-p", "no:cacheprovider"]
    if not full:
        args += targets
    proc = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.returncode


def _is_dirty(status_text: str, rel: str) -> bool:
    """目标文件在工作区里是否已经被改过（相对 HEAD，含未跟踪）。

    用来把两种「锚点找不到」分开：**代码变了** 与 **工作区本来就被改脏了**。
    后者不是稀罕事 —— 上一轮运行崩了没还原，文件就留在变异态；再跑一次
    看到「锚点找不到了」，如果只说「代码已经变了」，下一个人会去查代码，
    而该查的是 `git status`。

    **不另起一次子进程**：scorecard() 开头本来就要跑 `git status --porcelain`
    来做「跑完工作区没变」的兜底检查，那份输出直接复用。少一次子进程调用，
    SAST 基线也不用因此多长一条豁免。
    """
    for line in status_text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:          # 重命名：porcelain 给的是「旧 -> 新」
            path = path.split(" -> ", 1)[1]
        if path.strip('"') == rel:
            return True
    return False


def _precondition(m: Mutation, status_text: str = "") -> None:
    p = m.path()
    if not p.is_file():
        raise SystemExit(f"[{m.id}] 目标文件不存在：{m.target}")
    hits = p.read_text(encoding="utf-8").count(m.old)
    if hits == 0:
        dirty = ""
        if _is_dirty(status_text, m.target):
            dirty = ("\n  **先看工作区**：目标文件在 `git status` 里已经被改过 —— "
                     "锚点找不到很可能是因为这个，而不是代码变了。\n"
                     f"  先 `git diff -- {m.target}` 看看你（或上一次运行）改了什么。")
        raise SystemExit(
            f"[{m.id}] 锚点在 {m.target} 里找不到了 —— 代码已经变了，"
            f"这条变异要么已无意义、要么需要重写。\n"
            f"  找不到：{m.old[:80]!r}\n"
            f"  **不要**为了让脚本跑通就把锚点改松。{dirty}")
    if hits > 1:
        # 施加时是 `text.replace(m.old, m.new, 1)` —— 只改**第一处**。
        # 锚点不唯一时，变异很可能落在一个判据根本不看的地方，于是报出
        # 「变异存活」。那个结论是错的：守卫没变弱，是变异压根没落到
        # 被读的那份上 —— 而**看起来**完全像「守卫已经失去牙齿」。
        #
        # 实测踩到过：publish-mcp.yml 里 `.../v0/servers'` 同时出现在
        # publish 的回查与 verify 的自检里；锚点从唯一变成两处之后，
        # 变异改到了 publish，而判据读 verify，于是「存活」。
        raise SystemExit(
            f"[{m.id}] 锚点在 {m.target} 里出现 {hits} 次 —— "
            "只替换第一处很可能改错地方，于是报出「变异存活」，"
            "而那个结论是假的（守卫没弱，是变异没落到被读的那份上）。\n"
            f"  锚点：{m.old[:80]!r}\n"
            "  把锚点加长到**唯一**，或显式改 replace 的语义；"
            "**不要**为了让脚本跑通就把锚点改短或改松。")
    if not m.tests:
        raise SystemExit(f"[{m.id}] 没声明判定它的测试 —— 那这条记的是谁的账？")


def _restore(p: Path, data: bytes, attempts: int = 4) -> None:
    """把目标文件还原回原样。

    **为什么要有重试**：本机实测两次「刚写过的文件立刻再打开写入」失败
    （`OSError: [Errno 22] Invalid argument`，路径完全正常、事后又能写）——
    一次失败就把**变异态留在工作区**，而这正是这个脚本最不能留下的东西。
    所以这里宁可多试几次，也不把「一次瞬时失败」变成「用户的仓库脏了」。
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            p.write_bytes(data)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.15 * (i + 1))
    raise last if last else OSError("还原失败：没有产生任何异常？")


def scorecard(*, full: bool = False,
              mutations: List[Mutation] | None = None) -> dict:
    """逐条施加变异，记录它是被抓住还是活下来。"""
    mutations = MUTATIONS if mutations is None else mutations
    git_before = subprocess.run(
        [git_exe(), "status", "--porcelain"], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

    results = []
    originals: dict[Path, bytes] = {}
    try:
        for m in mutations:
            _precondition(m, git_before)
            p = m.path()
            if p not in originals:
                originals[p] = p.read_bytes()          # 事先存下，不是事后读回
            text = originals[p].decode("utf-8")
            p.write_bytes(text.replace(m.old, m.new, 1).encode("utf-8"))

            t0 = time.monotonic()
            rc = _run_tests(m.tests, full)
            dt = time.monotonic() - t0

            # 立刻还原，不把脏状态带到下一条
            _restore(p, originals[p])
            results.append({
                "id": m.id, "why": m.why, "target": m.target,
                "tests": m.tests if not full else ["(全量)"],
                "killed": rc != 0, "seconds": round(dt, 1),
            })
    finally:
        for p, data in originals.items():
            try:
                _restore(p, data)
            except OSError as exc:
                rel = p.relative_to(ROOT) if p.is_relative_to(ROOT) else p
                raise SystemExit(
                    f"还原失败：{p} 已脏 —— 工作区被本脚本留在修改态。\n"
                    f"  {exc}\n"
                    f"  请用 `git checkout -- {rel}` 手动还原后再跑。") from exc
            # 同样不用 assert：`-O` 会删掉它，于是「还原失败」这条兜底在
            # 优化模式下直接消失，而兜底没了的后果是**把脏工作区留给用户**。
            if p.read_bytes() != data:
                raise SystemExit(
                    f"还原失败：{p} 已脏 —— 工作区被本脚本留在修改态，"
                    f"请用 git status 确认后手动还原")

    git_after = subprocess.run(
        [git_exe(), "status", "--porcelain"], cwd=str(ROOT),
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
