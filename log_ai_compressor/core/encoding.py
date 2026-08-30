# -*- coding: utf-8 -*-
"""编码探测：UTF-8 / GBK / GB2312 / UTF-16 / UTF-32 自动适配。

设计思路
--------
- 基于文件头采样 + 严格解码验证，不引入 chardet 等重型依赖；
- GB2312 ⊂ GBK ⊂ GB18030，按超集（gb18030）验证即可同时覆盖 GBK/GB2312；
- 采样窗口预留尾部余量，避免多字节字符被采样边界截断导致误判；
- 探测失败时兜底 UTF-8 + 容错解码（errors='replace'），保证永不因编码崩溃。
"""
from __future__ import annotations

from typing import TextIO

# BOM 特征表（优先级从高到低）
_BOM_TABLE = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

# 严格验证候选编码顺序（ASCII 兼容 UTF-8 优先，中文超集 gb18030 其次）
_CANDIDATE_ENCODINGS = ("utf-8", "gb18030")

_SAMPLE_SIZE = 262144       # 采样 256KB，兼顾准确性与读取开销


def detect_encoding(path, sample_size: int = _SAMPLE_SIZE) -> str:
    """探测日志文件编码。

    返回值可直接用于 io.open(encoding=...)。
    """
    with open(path, "rb") as fh:
        head = fh.read(sample_size)
    return detect_encoding_from_bytes(head)


def _decodes_cleanly(data: bytes, enc: str) -> bool:
    """严格解码验证；容忍采样边界截断（逐字节回退重试）。

    编码判断依据：解码错误若仅出现在采样尾部（多字节字符被截断），
    可视为采样边界效应；错误出现在中间则判定该编码不匹配。
    """
    for trim in range(5):   # GB18030 最长序列 4 字节，回退 4 次足够
        chunk = data[: len(data) - trim] if trim else data
        try:
            chunk.decode(enc, errors="strict")
            return True
        except UnicodeDecodeError:
            continue
    return False


def detect_encoding_from_bytes(head: bytes) -> str:
    """基于字节采样探测编码（便于单元测试）。"""
    if not head:
        return "utf-8"

    # 1) BOM 优先
    for bom, enc in _BOM_TABLE:
        if head.startswith(bom):
            return enc

    # 2) UTF-16/32 无 BOM 特征：大量 NUL 字节
    probe = head[:4096]
    nul_count = probe.count(b"\x00")
    if nul_count > len(probe) // 4:
        # 根据奇偶位置判断字节序
        return "utf-16-le" if probe[0:1] != b"\x00" else "utf-16-be"

    # 3) 严格解码验证（容忍尾部截断）
    for enc in _CANDIDATE_ENCODINGS:
        if _decodes_cleanly(head, enc):
            return enc

    # 4) 兜底：流式读取时配合 errors='replace' 容错
    return "utf-8"


def open_text_stream(path, encoding: str) -> TextIO:
    """以指定编码打开文本流（未知字符以替换符容错，保证流不中断）。"""
    return open(path, "r", encoding=encoding, errors="replace",
                buffering=1 << 20, newline="")


def decode_text(text: str) -> str:
    """粘贴文本的清洗（GUI 文本粘贴模式入口，保留原样）。"""
    return text
