# -*- coding: utf-8 -*-
"""性能基准：生成合成日志并测量处理速度与峰值内存。

用法：
    python scripts/benchmark.py [--lines 1000000]

指标口径：
- 速度：混合级别真实分布（INFO/DEBUG/WARN/ERROR + 堆栈）下行/秒；
- 内存：tracemalloc 统计的 Python 堆峰值（不含解释器自身开销）。
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from log_ai_compressor.core.pipeline import analyze_file  # noqa: E402


def generate_log(path: Path, lines: int, seed: int = 42) -> None:
    """生成合成日志：60% INFO / 15% DEBUG / 5% WARN / 18% ERROR / 2% FATAL。

    错误含 5 类模板 + 参数变化 + 周期性堆栈，贴近真实分布。
    """
    rng = random.Random(seed)
    error_templates = [
        "ERROR [db] connection refused to db-{shard}:5432",
        "ERROR [api] request {req} failed: upstream unavailable",
        "ERROR [cache] lock contention timeout: key=job:{key}",
        "ERROR [auth] token expired for session {sess}",
        "ERROR [task] worker {wid} aborted after retries",
    ]
    stack = [
        "java.net.ConnectException: Connection refused",
        "\tat com.app.db.ConnectionPool.init(ConnectionPool.java:142)",
        "\tat java.base/java.net.Socket.connect(Socket.java:606)",
        "\tat com.app.core.ServiceManager.start(ServiceManager.java:88)",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        base = 1710460800  # 2024-03-15 00:00:00
        for i in range(lines):
            ts = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(base + i // 100))
            roll = rng.random()
            if roll < 0.60:
                fh.write(f"{ts} INFO [api] GET /api/items 200 {rng.randint(1, 99)}ms\n")
            elif roll < 0.75:
                fh.write(f"{ts} DEBUG [db] query plan cached rows={rng.randint(10, 9999)}\n")
            elif roll < 0.80:
                fh.write(f"{ts} WARN [pool] connection pool {rng.randint(50, 95)}% utilized\n")
            elif roll < 0.98:
                tpl = error_templates[rng.randrange(len(error_templates))]
                msg = tpl.format(shard=rng.randint(0, 7), req=rng.randint(10000, 99999),
                                 key=rng.randint(1, 9999), sess=f"{rng.randint(1, 999999):06x}",
                                 wid=rng.randint(1, 16))
                fh.write(f"{ts} {msg}\n")
                if rng.random() < 0.02:   # 少量带堆栈
                    fh.write("\n".join(stack) + "\n")
            else:
                fh.write(f"{ts} FATAL [core] out of memory in worker {rng.randint(1, 16)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="log-ai-compressor 性能基准")
    parser.add_argument("--lines", type=int, default=1_000_000,
                        help="合成日志行数（默认 100 万）")
    args = parser.parse_args()

    tmp = Path(tempfile.gettempdir()) / "log_ai_compressor_bench.log"
    print(f"生成 {args.lines:,} 行合成日志 ...")
    t0 = time.perf_counter()
    generate_log(tmp, args.lines)
    gen_cost = time.perf_counter() - t0
    size_mb = tmp.stat().st_size / 1024 / 1024
    print(f"  文件大小 {size_mb:.1f} MB（生成耗时 {gen_cost:.1f}s）")

    tracemalloc.start()
    t0 = time.perf_counter()
    result = analyze_file(tmp)
    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tmp.unlink(missing_ok=True)

    s = result.stats
    print(f"处理完成：{s.total_lines:,} 行 / {elapsed:.2f}s"
          f" = {s.total_lines / elapsed:,.0f} 行/秒")
    print(f"  错误 {s.error_lines:,} 行，去重后 {len(result.clusters)} 种")
    print(f"  峰值内存（Python 堆）: {peak / 1024 / 1024:.1f} MB")
    print(f"  智能分析耗时: {s.analysis_cost * 1000:.1f} ms"
          f"（占 {s.analysis_cost / elapsed * 100:.1f}%）")
    print("结论："
          f"{'✅' if s.total_lines / elapsed >= 30000 else '⚠️'} "
          f"速度 {'达标' if s.total_lines / elapsed >= 30000 else '未达标'}（目标 ≥ 3 万行/秒），"
          f"{'✅' if peak / 1024 / 1024 <= 120 else '⚠️'} "
          f"内存 {'达标' if peak / 1024 / 1024 <= 120 else '未达标'}（目标 ≤ 120MB）")


if __name__ == "__main__":
    main()
