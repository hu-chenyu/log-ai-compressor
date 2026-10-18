#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能回归门禁：**可跨平台判定**的那一半 + **只能报告**的那一半。

**为什么分成两半**
------------------
性能回归最常见的做法是「记一个墙钟基线，超过阈值就红」。本项目**不做**
这件事作为硬门禁，原因是它会产生三类假红，而每一类都在 CI 上真实发生过：

1. **机器不同**。GitHub 的 hosted runner 与本地差几倍是常态，
   一个 120 秒的阈值在 4 核 runner 上永远绿、在本地永远红。
2. **邻居干扰**。跑基准时并发跑测试，两边的墙钟一起被拉长好几倍。
   这不是本项目的纪律能解决的，是 CI 的物理现实。
3. **噪声向上**。共享 runner 的墙钟只会**变慢**，不会变快 ——
   也就是说墙钟退化是一个**只会假红、不会假绿**的单边指标，
   但「假红太多」的门禁最后会被大家用 `--continue-on-error` 关掉，
   那才是真的假绿。

所以：

* **硬门禁 = 确定性工作量计数**（`similarity` 调用次数、`mask_text`
  次数、簇数）。同一份输入在任何机器、任何 OS、任何 Python 版本上
  都必须给出**同一个整数** —— 这是本项目「同输入同输出」承诺的
  性能版本。一次真实的性能回归（多扫一遍桶、把剪枝提前去掉、
  把 `_max_compare` 打开）必然让其中一个整数变大，而且是**必然**，
  不依赖机器快慢。
* **软报告 = 墙钟吞吐**。照实打出来，让人看见趋势；但**不判定**。
  把它写成红线的唯一结果就是有人加 `--continue-on-error`。

**负载是本文件自己生成的，不是 `scripts/benchmark.py` 那个**
------------------------------------------------------------
`benchmark.py::generate_log` 用 `time.localtime`，同一个 seed 在
UTC 机器和 UTC+8 机器上生成的是**不同字节**的日志。那样基线计数
在跨平台之间根本不可比 —— 门禁会以「平台差异」的名义变红。
本文件用 `time.gmtime` 且不碰本地时区，保证负载逐字节跨平台一致。

用法
----
    python scripts/perf_gate.py                  # 判硬门禁 + 打印吞吐
    python scripts/perf_gate.py --write-baseline # 刷新 docs/perf_baseline.json
    python scripts/perf_gate.py --json           # 只出 JSON，给 CI 读
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from log_ai_compressor.core import clustering as C                    # noqa: E402
from log_ai_compressor.core.pipeline import analyze_file              # noqa: E402

BASELINE = ROOT / "docs" / "perf_baseline.json"

#: 负载规模。刻意用**小到在任何 runner 上都是秒级**的数字：
#: 这道门禁每天都要跑，它的价值在于「每次都跑」，不在于「跑得久」。
#: 实测：20,000 行在本地约 6 秒（且那是与另一个满载任务并跑时的数字），
#: 降到 8,000 行后各计数器仍有四位数，够灵敏，门禁本身 1~2 秒。
LINES = 8000
SEED = 20241118

ERROR_TEMPLATES = [
    "ERROR [db] connection refused to db-{shard}:5432",
    "ERROR [api] request {req} failed: upstream unavailable",
    "ERROR [cache] lock contention timeout: key=job:{key}",
    "ERROR [auth] token expired for session {sess}",
    "ERROR [task] worker {wid} aborted after retries",
]
STACK = [
    "java.net.ConnectException: Connection refused",
    "\tat com.app.db.ConnectionPool.init(ConnectionPool.java:142)",
    "\tat java.base/java.net.Socket.connect(Socket.java:606)",
    "\tat com.app.core.ServiceManager.start(ServiceManager.java:88)",
]

# 错误负载的词表。**这些词是刻意选的**：它们让错误消息在 `mask_text`
# 之后仍然是**不同的模板**（数字被掩成 N 了，词还在），于是
# `(级别, 消息模板)` 精确索引必然 miss、每一条都要走 `_find_similar`
# 的相似度回退扫整个级别桶。
#
# 第一版负载只有 5 个固定模板，掩码后全撞同一索引，于是
# `_find_similar` 一次都没被调用 —— 门禁守着一条**空路径**，
# 计数恒为 0、恒等于基线、恒绿。**这就是「判据认错了东西」的活例子**：
# 仪表装在 `similarity()` 上，而热路径根本不调它（`_find_similar` 直接
# 用 `SequenceMatcher`）。报数不等于守住了东西。
_SVC = ["ingest", "billing", "search", "render", "sync", "shard", "router",
        "planner", "reaper", "sentry", "quota", "ledger", "mailer", "index",
        "cache", "proxy", "broker", "compactor", "scheduler", "rebalancer"]
_VERB = ["connect", "refresh", "commit", "flush", "expire", "rotate",
         "resolve", "publish", "reserve", "throttle", "compact", "replay"]
_NOUN = ["session", "channel", "handle", "cursor", "lease", "ticket",
         "journal", "segment", "record", "shard"]


def workload_lines(lines: int = LINES, seed: int = SEED) -> List[str]:
    """确定性合成负载：60% INFO / 15% DEBUG / 5% WARN / 18% ERROR / 2% FATAL。

    用 `time.gmtime` 而不是 `localtime` —— 见模块 docstring。
    """
    rng = random.Random(seed)
    base = 1710460800                       # 2024-03-15 00:00:00 UTC
    out: List[str] = []
    for i in range(lines):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(base + i // 100))
        roll = rng.random()
        if roll < 0.60:
            out.append("%s INFO [api] GET /api/items 200 %dms"
                       % (ts, rng.randint(1, 99)))
        elif roll < 0.75:
            out.append("%s DEBUG [db] query plan cached rows=%d"
                       % (ts, rng.randint(10, 9999)))
        elif roll < 0.80:
            out.append("%s WARN [pool] connection pool %d%% utilized"
                       % (ts, rng.randint(50, 95)))
        elif roll < 0.98:
            # 词级变化 -> 掩码后仍是不同模板 -> 必然走相似度回退（见 _SVC 上方）
            out.append("%s ERROR [%s] %s %s failed on %s %d"
                       % (ts, _SVC[rng.randrange(len(_SVC))],
                          _VERB[rng.randrange(len(_VERB))],
                          _NOUN[rng.randrange(len(_NOUN))],
                          _SVC[rng.randrange(len(_SVC))], rng.randint(1, 999)))
            if i % 50 == 0:
                out.extend("%s %s" % (ts, s) for s in STACK)
        else:
            out.append("%s FATAL [main] unrecoverable state, halted" % ts)
    return out


def measure(lines: int = LINES) -> dict:
    """跑一遍，量「确定性工作量」与「墙钟」。

    三个计数器各自对应一处真实开销，**装的都是热路径本身**：
      * `mask_text`   —— 每次指纹/模板构造都调一次；
      * `find_similar`—— 精确索引 miss 之后进入相似度回退的次数；
      * `ratio`       —— 真正建了 SequenceMatcher 并算了 ratio 的次数，
                        也就是整条链路上最贵的那一步。
    第一版只装 `similarity()`，结果恒为 0 —— `_find_similar` 直接用
    `SequenceMatcher`，根本不经过那个模块级函数。**报数为 0 的计数器
    等于没装。**
    """
    counters: Dict[str, int] = {"mask_text": 0, "find_similar": 0, "ratio": 0}

    orig_mask, orig_sm = C.mask_text, C.SequenceMatcher
    orig_find = C.ErrorClusterer._find_similar

    def mask(text: str, *a, **kw) -> str:
        counters["mask_text"] += 1
        return orig_mask(text, *a, **kw)

    class CountingMatcher(orig_sm):          # type: ignore[misc,valid-type]
        def ratio(self):
            counters["ratio"] += 1
            return super().ratio()

    def find_similar(self, msg_tp, level):    # noqa: ANN001
        counters["find_similar"] += 1
        return orig_find(self, msg_tp, level)

    C.mask_text = mask
    C.SequenceMatcher = CountingMatcher
    C.ErrorClusterer._find_similar = find_similar
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "perf.log"
            path.write_text("\n".join(workload_lines(lines)) + "\n",
                            encoding="utf-8")
            t0 = time.perf_counter()
            result = analyze_file(str(path))
            elapsed = time.perf_counter() - t0
    finally:
        C.mask_text = orig_mask
        C.SequenceMatcher = orig_sm
        C.ErrorClusterer._find_similar = orig_find

    return {
        "counters": counters,
        "clusters": len(result.clusters),
        "entries": int(getattr(result.stats, "error_entries", 0) or 0),
        "seconds": round(elapsed, 3),
        "lines_per_second": round(lines / elapsed) if elapsed > 0 else 0,
        "platform": "%s-%s" % (platform.system().lower(),
                               platform.python_version_tuple()[0]),
    }


def load_baseline() -> dict:
    if not BASELINE.is_file():
        print("缺 %s —— 没有基线就没有「回归」这个概念" % BASELINE)
        raise SystemExit(2)
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def write_baseline(m: dict) -> int:
    doc = load_baseline() if BASELINE.is_file() else {}
    doc.setdefault("_readme",
                   "确定性工作量计数 = 硬门禁（任何平台都必须相等）；"
                   "wall_clock = 只报告，不判定（见 scripts/perf_gate.py 顶部）。")
    doc["workload"] = {"lines": LINES, "seed": SEED}
    doc["counters"] = m["counters"]
    doc["clusters"] = m["clusters"]
    doc["entries"] = m["entries"]
    doc.setdefault("wall_clock", {})["%s-py%s" % (m["platform"],
                                                  platform.python_version_tuple()[0])] = {
        "lines_per_second": m["lines_per_second"],
        "seconds": m["seconds"],
        "note": "仅供参考，不参与判定：跨机器不可比，且噪声只会向下。",
    }
    BASELINE.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已刷新 %s" % BASELINE)
    return 0


def check(m: dict) -> int:
    base = load_baseline()
    problems: List[str] = []
    for key in ("counters", "clusters", "entries"):
        want, got = base.get(key), m[key]
        if want is None:
            problems.append("基线里没有 %s —— 跑一次 --write-baseline 生成" % key)
        elif want != got:
            if key == "counters":
                # 逐项点名。只丢一句「counters 回归」等于把 diff 两个
                # dict 的活儿推给看到红的人。
                diff = ", ".join(
                    "%s 基线 %s -> 实测 %s" % (k, want.get(k), got.get(k))
                    for k in sorted(set(want) | set(got))
                    if want.get(k) != got.get(k))
            else:
                diff = "基线 %r，实测 %r" % (want, got)
            problems.append(
                "%s 回归（%s）。同一份输入在任何机器上必须得到同一个整数；"
                "变了就是算法或剪枝被改过了（变大=变慢，变小=结果也变了），"
                "请确认是有意为之再刷新基线" % (key, diff))
    print("确定性工作量（硬门禁）")
    print("  %-14s %-14s %-14s" % ("计数", "基线", "实测"))
    for key in ("find_similar", "mask_text", "ratio"):
        print("  %-14s %-14s %-14s" % (key, base.get("counters", {}).get(key, "—"),
                                       m["counters"][key]))
    for key in ("clusters", "entries"):
        print("  %-14s %-14s %-14s" % (key, base.get(key, "—"), m[key]))
    print("\n墙钟（只报告，不判定）")
    print("  %s 行 %ss，%s 行/秒"
          % (LINES, m["seconds"], m["lines_per_second"]))

    if problems:
        print("\n性能回归门禁失败（%d 项）：" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("\n性能回归门禁通过：工作量与基线逐项相等。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="性能回归门禁")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    m = measure()
    if args.json:
        print(json.dumps(m, ensure_ascii=False))
        return 0
    if args.write_baseline:
        return write_baseline(m)
    return check(m)


if __name__ == "__main__":
    raise SystemExit(main())
