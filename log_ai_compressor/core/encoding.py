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

import gzip
import io
import zipfile
from typing import TextIO

# 优化缺陷R87：压缩包魔数（gzip 1f 8b / zip PK\x03\x04）
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"

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


def _is_gzip(path) -> bool:
    """魔数判定 gzip（扩展名不可靠，轮转日志常改名）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == _GZIP_MAGIC
    except OSError:
        return False


def _is_zip(path) -> bool:
    """魔数判定 zip（jar/zip 同构，取首个日志条目即可）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == _ZIP_MAGIC
    except OSError:
        return False


def _zip_log_member(zf: zipfile.ZipFile) -> str:
    """选 zip 内最大非目录条目（日志包通常单文件；多文件取最大者）。"""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise ValueError("zip 压缩包内没有可分析的日志文件")
    return max(infos, key=lambda i: i.file_size).filename


def _read_sample_bytes(path, sample_size: int) -> bytes:
    """读取编码探测采样（优化缺陷R87：压缩包先解压头部再采样）。"""
    if _is_gzip(path):
        with gzip.open(path, "rb") as fh:
            return fh.read(sample_size)
    if _is_zip(path):
        with zipfile.ZipFile(path) as zf:
            with zf.open(_zip_log_member(zf)) as fh:
                return fh.read(sample_size)
    with open(path, "rb") as fh:
        return fh.read(sample_size)


def detect_encoding(path, sample_size: int = _SAMPLE_SIZE) -> str:
    """探测日志文件编码（优化缺陷R87：.gz/.zip 透明解压后探测）。

    返回值可直接用于 io.open(encoding=...)。
    """
    return detect_encoding_from_bytes(_read_sample_bytes(path, sample_size))


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


def is_compressed(path) -> bool:
    """是否受支持的压缩日志（优化缺陷R87：gzip/zip 魔数判定）。"""
    return _is_gzip(path) or _is_zip(path)


class _ZipTextStream(io.TextIOWrapper):
    """zip 条目文本流：关闭时连带释放 ZipFile 句柄。"""

    def __init__(self, zf: zipfile.ZipFile, member: str, encoding: str):
        super().__init__(zf.open(member), encoding=encoding,
                         errors="replace", newline="")
        self._zf = zf

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._zf.close()


def open_text_stream(path, encoding: str) -> TextIO:
    """以指定编码打开文本流（未知字符以替换符容错，保证流不中断）。

    优化缺陷R87：.gz/.zip 压缩包透明解压读取（轮转日志免手动解压）。
    """
    if _is_gzip(path):
        return gzip.open(path, "rt", encoding=encoding, errors="replace",
                         newline="")
    if _is_zip(path):
        zf = zipfile.ZipFile(path)
        return _ZipTextStream(zf, _zip_log_member(zf), encoding)
    return open(path, "r", encoding=encoding, errors="replace",
                buffering=1 << 20, newline="")


def decode_text(text: str) -> str:
    """粘贴文本的清洗（GUI 文本粘贴模式入口，保留原样）。"""
    return text
