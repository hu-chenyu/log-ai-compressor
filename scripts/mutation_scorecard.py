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
        id="sast/documented-count-drifts-from-baseline",
        why="文档里写的豁免条数与基线对不上。**这一条是真的发生过**："
            "`tests/test_dep_audit_gate.py` 去掉一个用不上的 "
            "`import subprocess` 之后，基线里那条 B404 变成过期条目、门禁"
            "正确地红了；而我照着 `len(entries)` 把 DEVELOPMENT.md 的 43 改成"
            "44 —— **那 44 里有一条是过期的**。过期条目不是豁免，它是门禁"
            "正在报的债。照着它数，文档就会比门禁多报一条",
        target="docs/DEVELOPMENT.md",
        old="剩下 43 条才是真该留下的。",
        new="剩下 44 条才是真该留下的。",
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
        why="负载改用 localtime。同一 seed 在不同���区生成不同字节的日志，"
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


def _precondition(m: Mutation) -> None:
    p = m.path()
    if not p.is_file():
        raise SystemExit(f"[{m.id}] 目标文件不存在：{m.target}")
    hits = p.read_text(encoding="utf-8").count(m.old)
    if hits == 0:
        raise SystemExit(
            f"[{m.id}] 锚点在 {m.target} 里找不到了 —— 代码已经变了，"
            f"这条变异要么已无意义、要么需要重写。\n"
            f"  找不到：{m.old[:80]!r}\n"
            f"  **不要**为了让脚本跑通就把锚点改松。")
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
