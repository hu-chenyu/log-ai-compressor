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
from collections import deque
from typing import List, TextIO

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


def open_binary_stream(path):
    """以**二进制**打开日志流（gzip/zip 透明解压）。

    为什么要二进制：逐行解码兜底必须拿到**原始字节** —— 文本流已经把
    解不出来的字节换成了替换符，那时已经分不出「这行本来就是这个编码」
    与「这行是另一种编码」。
    """
    if _is_gzip(path):
        return gzip.open(path, "rb")
    if _is_zip(path):
        zf = zipfile.ZipFile(path)
        return _ZipBinaryStream(zf, _zip_log_member(zf))
    return open(path, "rb", buffering=1 << 20)


class _ZipBinaryStream:
    """zip 条目的二进制流（关闭时连带释放 ZipFile 句柄）。"""

    def __init__(self, zf: zipfile.ZipFile, member: str):
        self._zf = zf
        self._fh = zf.open(member)

    def read(self, *args):
        return self._fh.read(*args)

    def readline(self, *args):
        return self._fh.readline(*args)

    def __iter__(self):
        return iter(self._fh)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            self._zf.close()


#: 这些编码**不是 ASCII 透明**的：换行符在字节层面是多字节的。
#:
#: utf-16-le 的换行是 ``0A 00``，按 ``b"\n"`` 切行会把一个字符劈成两半
#: （实测：2 行的 utf-16 日志被切成 3 行，每行末尾多一个 NUL，
#: 且行内出现 4 个 U+FFFD）。所以这一族必须**先解码再切行**。
_WIDE_ENCODING_PREFIXES = ("utf-16", "utf-32")

#: utf-8 BOM。带 BOM 的文件按 "utf-8-sig" 探测出来，但解码时若用普通
#: "utf-8"，BOM 会变成一个 U+FEFF 字符留在首行开头（实测：首行摘要前缀
#: 多一个 ``﻿``）。基线用 utf-8-sig 解码会把它吃掉，所以这里显式剥。
_UTF8_BOM = b"\xef\xbb\xbf"


def is_wide_encoding(encoding: str) -> bool:
    """是否 utf-16/utf-32 一族（换行符多字节，不能按字节切行）。"""
    enc = (encoding or "").lower().replace("_", "-")
    return enc.startswith(_WIDE_ENCODING_PREFIXES)


class LogDecoder:
    """统一的日志行解码器 —— **摄入与回读必须走同一条路径**。

    为什么必须是同一个东西
    ----------------------
    证据摘要在**摄入时**按「解码后的文本」算；复核时再读一遍原文件、
    用**同一个**规则解码，才能重算出同样的摘要。一旦两处规则不同
    （例如摄入逐行兜底、回读整文件单编码），同一份没被改过的文件会算出
    两个摘要 ⇒ 复核必然失配 ⇒ 把「日志没被动过」报成 TAMPERED。

    这不是理论风险：2026-10-21 引入逐行兜底后，混合编码日志的
    `verify --report` 实测报出 TAMPERED（2 条引用里 1 条失配）——
    **指控了一个没有发生的事**，而这正是本工具最不该出的错。
    所以解码规则收敛到这一个类里，摄入与回读都调它。

    窄编码（utf-8 / gb18030 等 ASCII 透明族）
    ------------------------------------------
    换行符是单字节 ``0A``，可以按字节切行，再**逐行**决定用哪个编码解：

      * 主编码能严格解出来 → 用主编码；
      * 主编码解不出来、utf-8 能解 → 这一行其实是 utf-8（**混合编码**）；
      * utf-8 也解不出来、gb18030 能解 → 这一行其实是 GBK；
      * 都解不出来 → 记 ``undecodable``，按替换符保留，不丢行。

    utf-8 优先于 gb18030 的理由：utf-8 严格得多，能通过 utf-8 严格校验
    的行几乎必然本来就是 utf-8；而 gb18030 几乎什么字节序列都能「解成功」，
    拿它当首选会把 utf-8 的行解成乱码且**毫无征兆**。

    宽编码（utf-16 / utf-32）
    ------------------------
    换行符本身是多字节的，必须**整块解码后再切行**（用 open_text_stream）。
    这一族不做逐行兜底：宽编码文件里混进一行窄编码是病态场景，
    而错误的兜底比不做更糟（它会把整行拆坏）。

    三种结果都要**记账**（不是丢弃，是说清）：
      * 用主编码解出来 → 正常；
      * 换了别的编码解出来 → ``repaired``（这份文件是混合编码）；
      * 谁都解不出来 → ``undecodable``（多半不是文本日志）。
    """

    def __init__(self, encoding: str):
        self.encoding = encoding or "utf-8"
        self.repaired = 0
        self.undecodable = 0
        self.nul_lines = 0
        # utf-8-sig 在窄编码路径里等价于 utf-8 + 剥掉 BOM
        self._narrow_primary = ("utf-8" if self.encoding == "utf-8-sig"
                                else self.encoding)
        self._wide = is_wide_encoding(self.encoding)

    # -- 窄编码 ----------------------------------------------------------
    @staticmethod
    def _try(raw: bytes, enc: str):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            return None

    def _decode_narrow(self, raw: bytes) -> str:
        if b"\x00" in raw:
            self.nul_lines += 1
        primary = self._narrow_primary
        u8 = self._try(raw, "utf-8")
        if primary == "utf-8":
            if u8 is not None:
                return u8
            alt = self._try(raw, "gb18030")
            if alt is not None:
                self.repaired += 1
                return alt
            self.undecodable += 1
            return raw.decode("utf-8", errors="replace")
        # 主编码不是 utf-8：utf-8 能严格解出来且与主编码结果不同 ⇒
        # 这一行是 utf-8（主编码把它解成了别的样子）。
        pri = self._try(raw, primary)
        if u8 is not None and (pri is None or u8 != pri):
            self.repaired += 1
            return u8
        if pri is not None:
            return pri
        alt = self._try(raw, "gb18030")
        if alt is not None:
            self.repaired += 1
            return alt
        self.undecodable += 1
        return raw.decode(primary, errors="replace")

    def _iter_narrow(self, path):
        # BOM 出现在**任何**一行都剥掉：utf-8-sig 只影响首行，但轮转合并
        # 时每个文件都有自己的首行，逐行剥才不依赖「第几行」。
        # （BOM 出现在文件中段是极罕见的合法场景，剥掉它没有代价。）
        with open_binary_stream(path) as raw:
            for line in raw:
                if line.startswith(_UTF8_BOM):
                    line = line[len(_UTF8_BOM):]
                yield self._decode_narrow(line)

    # -- 宽编码 ----------------------------------------------------------
    def _iter_wide(self, path):
        # 整块解码后再切行（换行符多字节，按字节切会劈坏字符）。
        # errors="replace" 与基线一致，宽编码不做逐行兜底。
        with open_text_stream(path, self.encoding) as stream:
            for line in stream:
                if "\ufffd" in line:
                    self.undecodable += 1
                yield line

    # -- 对外接口 --------------------------------------------------------
    def decode_bytes(self, raw: bytes) -> str:
        """解一段**已经按行切好的**字节（接入层用；如 stdin）。

        与 :meth:`iter_lines` 走同一套判定规则，所以「管道进来的
        GBK 日志」和「文件里的 GBK 日志」解出来一样。
        """
        if self._wide:
            return raw.decode(self.encoding, errors="replace")
        if raw.startswith(_UTF8_BOM):
            raw = raw[len(_UTF8_BOM):]
        return self._decode_narrow(raw)

    def iter_lines(self, path):
        """按日志行产出解码后的文本（摄入与回读共用）。"""
        if self._wide:
            return self._iter_wide(path)
        return self._iter_narrow(path)

    def read_range(self, path, line_start: int, line_end: int) -> List[str]:
        """回读指定行区间（1-based，闭区间）—— 复核用。"""
        out: List[str] = []
        for i, line in enumerate(self.iter_lines(path), 1):
            if i < line_start:
                continue
            if i > line_end:
                break
            out.append(line)
        return out


def read_last_lines(path, n: int, encoding: str):
    """读文件**最后 n 行**，返回 (文本行列表, 起始行号, 文件总行数, 解码器)。

    解码器一并返回：混合编码 / 非文本的计数在它身上，调用方要拿它
    去写进统计（否则「这份文件解得有多干净」就没人记账了）。

    为什么要有它：`--tail N`（只看最近 N 行）要能用在几十 GB 的日志上，
    而证据引用必须指向**原文件的真实行号** —— 尾部窗口的「第 1 行」
    在原文件里可能是第 9,999,991 行。行号错了，整份报告的可复核性
    就没了（那正是本工具的核心承诺）。

    实现：一次正向扫描，环形缓冲留最后 n 行，同时数总行数。
    代价是**整个文件要读一遍**（但不解码、不解析、不聚类）——
    比全量分析便宜两个数量级（实测 149 MB 文件：全量分析 45 s，
    本步骤 0.3 s 量级），换来的是行号绝对正确。

    为什么不倒着读：倒着读要先知道「文件有多少行」才能算出窗口的
    起始行号，于是要么多扫一遍（等于还是读两遍），要么给出错误的
    行号 —— 而错误的行号会让报告**无法复核**，那是本工具最不该
    丢的承诺。一次读完 + 环形缓冲是唯一不牺牲正确性的简单做法。

    压缩包（.gz/.zip）：压缩流本来就得顺序解压，同一条路径即可。
    """
    if n <= 0:
        raise ValueError("n 必须为正")
    tail = deque(maxlen=n)
    total = 0
    with open_binary_stream(path) as raw:
        for line in raw:
            total += 1
            tail.append(line)
    lines = list(tail)
    start = max(1, total - len(lines) + 1)
    dec = LogDecoder(encoding)
    return [dec.decode_bytes(b) for b in lines], start, total, dec


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
