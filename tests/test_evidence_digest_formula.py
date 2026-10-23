# -*- coding: utf-8 -*-
"""证据摘要的算法必须与文档里写的一致 —— 而且必须真的能自己算出来。

为什么要有这条
--------------
本工具的核心承诺之一是「**用户可脱离本工具自校验**」。而「脱离本工具」这一步
在此之前**没有任何文档写过算法**：README、docs/CLI.md、docs/FAQ.md 只说
「那段原文的 sha256」，没给规则。

承诺本身是成立的（实测每条引用都能自己对上），但读者只能逆推 ——
而「可脱离工具自校验」这句话的意思恰恰是「不用逆推」。
2026-10-23 第十一轮外部复核也独立逆推出过同一个规则。

规则（现已写进 docs/CLI.md）
---------------------------
取行区间 → **每行去掉行尾的 `\\r\\n`** → **用 `\\n` 连接** → 对 UTF-8 字节算 sha256。

**改坏会红吗**：把 `core/evidence.py` 里算摘要的那一步改掉（例如改成用
原始字节直接算、或把 `\\r` 也留下），本文件立刻红 ——
它是用**工具算出来的摘要**与**按文档规则自己算出来的摘要**对比。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _run(path: Path, out: Path) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "log_ai_compressor", "run", str(path),
         "-f", "json", "-o", str(out)],
        cwd=str(out.parent), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=300)
    assert proc.returncode == 0, "跑失败：%s" % proc.stderr[-300:]
    return json.loads(out.read_text(encoding="utf-8"))


def _documented_digest(raw_lines: list[bytes], start: int, end: int) -> str:
    """**照 docs/CLI.md 写的规则**自己算一遍摘要。"""
    block = b"\n".join(ln.rstrip(b"\r\n") for ln in raw_lines[start - 1:end])
    return hashlib.sha256(block).hexdigest()


def _all_refs(report: dict) -> list[dict]:
    refs = []
    for c in report.get("clusters") or []:
        for e in (c.get("evidence") or []):
            if e.get("digest") and e.get("line_start"):
                refs.append(e)
    return refs


def test_the_documented_formula_reproduces_every_digest(tmp_path: Path):
    """报告里每条引用的摘要，都能按**文档写的规则**自己对上。

    **改坏会红吗**：`core/evidence.py` 里算摘要的规则一变（多留一个 `\\r`、
    或改成不带换行连接），本条立刻红。
    """
    app = _ROOT / "examples" / "app.log"
    report = _run(app, tmp_path / "r.json")
    raw = app.read_bytes().splitlines(keepends=True)
    refs = _all_refs(report)
    assert refs, "报告里一条引用都没有 —— 这条断言就没东西可查了"
    for e in refs:
        want = _documented_digest(raw, e["line_start"], e["line_end"])
        assert want == e["digest"], (
            "第 %s~%s 行的摘要对不上：报告里 %s，按文档规则算 %s"
            % (e["line_start"], e["line_end"], e["digest"], want))


def test_the_digest_uses_the_raw_bytes_with_ansi(tmp_path: Path):
    """带 ANSI 转义序列的行，摘要按**原始字节**算（剥掉就不对）。

    README 那句「证据摘要只认文件原始字节，剥过 ANSI 的文本不算」，
    在这里被钉成一条可执行的断言。

    **改坏会红吗**：把摄入路径改成先剥 ANSI 再算摘要，本条立刻红。
    """
    log = tmp_path / "ansi.log"
    log.write_bytes(
        b"2024-03-15 08:00:00 ERROR \x1b[31mboom\x1b[0m happened here\n"
        b"2024-03-15 08:00:01 INFO plain line\n"
        b"2024-03-15 08:00:02 ERROR \x1b[31mboom\x1b[0m happened again\n")
    report = _run(log, tmp_path / "r.json")
    raw = log.read_bytes().splitlines(keepends=True)
    refs = _all_refs(report)
    assert refs, "报告里一条引用都没有"
    for e in refs:
        want = _documented_digest(raw, e["line_start"], e["line_end"])
        assert want == e["digest"], (
            "带 ANSI 的行摘要对不上：报告 %s，按原始字节算 %s"
            % (e["digest"], want))
        stripped = b"\n".join(
            ln.rstrip(b"\r\n").replace(b"\x1b[31m", b"").replace(b"\x1b[0m", b"")
            for ln in raw[e["line_start"] - 1:e["line_end"]])
        assert hashlib.sha256(stripped).hexdigest() != e["digest"], (
            "剥掉 ANSI 之后摘要竟然一样 —— 那说明摘要算的是剥过的文本，"
            "与 README 说的「只认文件原始字节」相反")


def test_the_line_endings_do_not_change_the_digest(tmp_path: Path):
    """同一份日志存成 LF 与 CRLF，摘要必须**相同**。

    这是「同一份日志」这个概念的前提 —— 换个平台检出的文件若摘要变了，
    复核就会把「没被改过的日志」报成 TAMPERED。

    **改坏会红吗**：把摘要改成按原始字节算（不剥行尾），本条立刻红。
    """
    app = (_ROOT / "examples" / "app.log").read_bytes()
    lf = tmp_path / "lf.log"
    crlf = tmp_path / "crlf.log"
    lf.write_bytes(app)
    crlf.write_bytes(app.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    rep_lf = _run(lf, tmp_path / "lf.json")
    rep_crlf = _run(crlf, tmp_path / "crlf.json")
    d_lf = sorted(e["digest"] for e in _all_refs(rep_lf))
    d_crlf = sorted(e["digest"] for e in _all_refs(rep_crlf))
    assert d_lf, "没有引用可比"
    assert d_lf == d_crlf, (
        "LF 与 CRLF 两份日志的摘要不一样：%s vs %s" % (d_lf[:2], d_crlf[:2]))
