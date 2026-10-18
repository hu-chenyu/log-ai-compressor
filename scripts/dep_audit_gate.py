#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依赖漏洞门禁：pip-audit + 声明来源唯一。

**这个脚本为什么存在**
---------------------
`docs/THREAT_MODEL.md` 里记过一条行业标配缺口：没有依赖扫描。
「生产环境只有一个第三方依赖」是本项目最该拿出去讲的一句话之一，
而**那句话只有被扫过才成立** —— 没扫过的「只有一个依赖」和
「有一个我们从没看过 advisories 的依赖」在事实上没有区别。

**为什么不用 `.github/workflows/*.yml` 里直接跑 pip-audit**
--------------------------------------------------------
可以直接跑，但那样这道门禁会退化成「CI 里的一行命令」：
本地复现不了、失败了看不到是哪一条依赖、也没地方写「哪些是已知的、
为什么留着」。本脚本把三件事收在一处：

1. **依赖清单只有一个来源** —— `pyproject.toml` 的
   `[project].dependencies` 与 `[project.optional-dependencies]`。
   刻意**不**去读 `requirements*.txt`：那两个文件是给 pip 用的，
   已经在注释里声明「与 pyproject 保持一致」，再扫一遍就多一个
   可能漂移的来源。
2. **任何一条已知漏洞都让门禁红**，不做白名单。
   依赖面小到只有一条运行时依赖（PyYAML），白名单在这里没有存在的理由；
   真出现「有漏洞但我们不得不用」时，正确做法是在本文件里写一条
   带理由的豁免并写明复查日期，而不是默默忽略。
3. **`--self-check` 证明这道门禁真的会红**（见下）。

**「门禁能变红」怎么证明**
--------------------------
一道从没红过的门禁等于没有门禁（AGENTS.md §2.2）。本机/CI 上验证它
的办法不是「相信 pip-audit 能用」，而是**故意喂一份已知有漏洞的版本**：

    urllib3==1.25.8

阳性对照**单独扫一次**，不与本项目的扫描混在一起（理由见 `audit_control()`：
混扫会让那条 pin 改变 pip 对本项目依赖的解析结果）。两次扫描读出三件事：
扫描跑完了吗、对照被抓到了吗、本项目干净吗。

三种结局是**三个不同的出口**，不是同一个退出码：

| 结局 | 退出码 | 含义 |
| --- | ---: | --- |
| 扫描没跑完（网络 / pip-audit 崩） | 2 | **既不是干净也不是有漏洞** —— 明确说清，不给任何结论 |
| 阳性对照没被抓到 | 2 | 门禁此刻是装饰，它分辨不出「干净」和「根本没扫」 |
| 本项目有已知漏洞 | 1 | 门禁正常发挥作用 |

把它们混成同一个退出码就是**假红的制造机** —— 而一条会误报的门禁比
没有门禁更糟，因为它把真问题淹没在噪音里。

用法
----
    python scripts/dep_audit_gate.py             # 扫本项目声明的全部依赖
    python scripts/dep_audit_gate.py --self-check # 证明这道门禁有牙齿
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import List, Optional, Sequence

# Windows 控制台默认 cp1252/cp936，打印中文会抛 UnicodeEncodeError ——
# 而且症状极具误导性：门禁本身全绿，却因为「打印表格时编码失败」让整个
# step 变红，看起来像功能坏了。**这一条是真的发生过**：v2.11.5 首次把
# 三道新门禁接进 CI，windows-3.12 就在 SAST 这步红了，而 ubuntu 两条
# 全绿 —— 门禁必须只在**判定**失败时失败，不能因为控制台编码。
# 模式与 scripts/adversarial_eval.py 一致，不是新发明。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # py3.7+
    except (AttributeError, ValueError, OSError):                # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

#: `--self-check` 用的阳性对照：这条 pin 必须被 pip-audit 判为有漏洞。
#: 选 urllib3 而不是更冷门的包，是因为它的 advisories 足够多、足够老，
#: 不会因为某一条被撤就整条消失（实测 21 条）。
POSITIVE_CONTROL_PIN = "urllib3==1.25.8"


def _pip_audit_module() -> bool:
    try:
        __import__("pip_audit")
        return True
    except ImportError:
        print("缺 pip-audit：pip install pip-audit（CI 走 requirements-dev.txt）")
        return False


def declared_requirements() -> List[str]:
    """pyproject.toml 里声明的全部依赖（运行时 + 全部 extra），逐行。

    跳过自引用（`log-ai-compressor[...]`）—— 那是本项目自己，
    要扫的是它的第三方依赖树，不是本项目。
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    reqs: List[str] = list(project.get("dependencies") or [])
    for extra, items in (project.get("optional-dependencies") or {}).items():
        for item in items:
            name = item.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0]
            name = name.split("<", 1)[0].strip()
            if name.replace("-", "_").lower() == "log_ai_compressor":
                continue                 # 自引用，跳过
            reqs.append(item)
    # 去重但保持声明顺序（报出来的顺序和 pyproject 一致才好核对）
    seen, out = set(), []
    for r in reqs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _audit_args(req: Path, *extra: str) -> List[str]:
    """拼 pip-audit 的命令行。

    抽成纯函数只有一个理由：**它能被离线断言**。`--format json` 这一个
    开关决定「有漏洞」与「没扫成」能不能被分开（见 `_parse`），它一旦
    被改掉，`_parse` 的判据就静默失效，而那时测试是绿的。
    """
    return [sys.executable, "-m", "pip_audit", "-r", str(req),
            "--progress-spinner", "off", "--format", "json", *extra]


def _run(req: Path, *extra: str) -> "subprocess.CompletedProcess":
    return subprocess.run(
        _audit_args(req, *extra),
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _parse(proc: "subprocess.CompletedProcess") -> dict:
    """把 pip-audit 的 JSON 拆成「扫成了没有」与「有什么洞」。

    pip-audit 的**退出码分不清这两件事**：有漏洞是 1，扫描没跑完也是 1。
    第一版据此把一次网络超时报成「依赖里有已知漏洞」—— 一条假红，
    而本项目的纪律是「一条会误报的门禁比没有门禁更糟」。
    判据是**能不能解析出 JSON**：扫不动就压根没有 JSON。
    """
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"ok": False, "vulns": {}, "error": (proc.stderr or
                                                     proc.stdout or "")[:800]}
    vulns: dict = {}
    for dep in data.get("dependencies", []):
        items = dep.get("vulns") or []
        if not items:
            continue
        vulns[str(dep.get("name"))] = {
            "version": str(dep.get("version", "?")),
            "ids": sorted({str(v.get("id")) for v in items if v.get("id")}),
            # fix_versions 是**列表的列表**（每条 advisory 一个列表）。
            # 早先写的是 str(v.get("fix_versions"))，打出来是
            # "['1.26.0', '2.0.0']" —— 一层列表字面量，读者得自己剥。
            # 这里摊平，否则「升到哪个版本」这条最有用的话被包在壳里。
            "fix": sorted({str(ver) for v in items
                           for ver in (v.get("fix_versions") or [])}),
        }
    return {"ok": True, "vulns": vulns, "error": None}


def audit_project() -> dict:
    """扫本项目声明的全部依赖（含传递依赖）。"""
    with tempfile.TemporaryDirectory() as td:
        req = Path(td) / "declared.txt"
        req.write_text("\n".join(declared_requirements()) + "\n",
                       encoding="utf-8")
        return _parse(_run(req))


def audit_control() -> dict:
    """只扫阳性对照那一条 pin，**不做依赖解析**（`--no-deps`）。

    为什么必须分开，不能混进同一次扫描：把 `urllib3==1.25.8` 混进去会
    改变 pip 对**本项目依赖**的解析结果 —— 实测混扫时报出一条
    `requests PYSEC-2026-2275`，而单独扫本项目是干净的。那条漏洞
    是对照臂的 pin 造成的解析回退，与本项目无关。
    **让本项目的结论依赖于对照臂的约束，就是把探针装在了错误的地方。**

    `--no-deps` 同时让这一步从「几分钟」降到十几秒。
    """
    with tempfile.TemporaryDirectory() as td:
        req = Path(td) / "control.txt"
        req.write_text(POSITIVE_CONTROL_PIN + "\n", encoding="utf-8")
        return _parse(_run(req, "--no-deps"))


def _print(res: dict) -> None:
    """每条漏洞都要打出**版本**与**修复版本**。

    第一版只打印包名与 advisory ID，于是门禁报出了
    `requests PYSEC-2026-2275` 却不说「你现在装的哪个版本有问题、
    升到哪个版本好」—— 报出问题却不指出修法，等于把活推给读者，
    而读者多半只能选择「ignore 掉」。
    """
    for name, info in sorted(res["vulns"].items()):
        fix = " / ".join(info["fix"]) or "**暂无修复版本**"
        print("  %-24s %-12s %s" % (name, info["version"],
                                   ", ".join(info["ids"])))
        print("  %-24s %-12s 修复版本：%s" % ("", "", fix))
    if res["error"]:
        print("  扫描本身没跑完：\n%s" % res["error"])


def self_check() -> int:
    """先证明门禁会红，再扫本项目。

    三种结局是**三个不同的出口**，不是同一个退出码。
    """
    print("阳性对照：%s（--no-deps，不做依赖解析，不会影响下面的结论）"
          % POSITIVE_CONTROL_PIN)
    ctl = audit_control()
    if not ctl["ok"]:
        print("\n**阳性对照的扫描没跑完** —— 无法判断门禁有没有牙齿。")
        _print(ctl)
        return 2
    if _control_name() not in ctl["vulns"]:
        print("\n阳性对照**没有被报出来** —— 这道门禁此刻是装饰：")
        print("它分辨不出「干净」和「根本没扫」。")
        print("（也可能那条 advisory 被撤了；那就换一条阳性对照，"
              "理由写进本文件。）")
        return 2
    _print(ctl)
    print("阳性对照成立：门禁确实有牙齿。")

    print("\n扫本项目声明的 %d 条依赖：" % len(declared_requirements()))
    res = audit_project()
    if not res["ok"]:
        print("\n**扫描本身没跑完** —— 这不是「干净」，也不是「有漏洞」。")
        _print(res)
        return 2
    if res["vulns"]:
        print("\n本项目声明的依赖里存在已知漏洞：")
        _print(res)
        print("修法只有两条：升级依赖，或在本文件里写一条带理由、"
              "带复查日期的豁免。「先 ignore 掉」不在选项里。")
        return 1
    print("本项目声明的 %d 条依赖无已知漏洞" % len(declared_requirements()))
    print("\n自检通过：会红（阳性对照）+ 对本项目是绿的")
    return 0


def _control_name() -> str:
    return POSITIVE_CONTROL_PIN.split("==", 1)[0]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="依赖漏洞门禁（pip-audit）")
    ap.add_argument("--self-check", action="store_true",
                    help="先验阳性对照证明门禁有牙齿，再扫本项目依赖")
    args = ap.parse_args(argv)

    if not _pip_audit_module():
        return 2
    if args.self_check:
        return self_check()

    reqs = declared_requirements()
    print("依赖来源：pyproject.toml（唯一来源），%d 条" % len(reqs))
    for r in reqs:
        print("  - %s" % r)
    res = audit_project()
    if not res["ok"]:
        print("\n**扫描本身没跑完** —— 这既不是「干净」也不是「有漏洞」，"
              "别拿它当任何结论：")
        _print(res)
        return 2
    if res["vulns"]:
        print("\n依赖扫描失败，以下依赖有已知漏洞：")
        _print(res)
        print("修法只有两条：升级依赖，或在本文件里写一条带理由、"
              "带复查日期的豁免。「先 ignore 掉」不在选项里。")
        return 1
    print("\n依赖扫描通过：%d 条声明依赖无已知漏洞" % len(reqs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
