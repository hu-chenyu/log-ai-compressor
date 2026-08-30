# -*- coding: utf-8 -*-
"""编码探测单元测试：UTF-8 / GBK / BOM / 无 BOM UTF-16。"""
from __future__ import annotations

from log_ai_compressor.core.encoding import (
    detect_encoding,
    detect_encoding_from_bytes,
    open_text_stream,
)


class TestDetectFromBytes:
    def test_ascii_treated_as_utf8(self):
        assert detect_encoding_from_bytes(b"INFO plain ascii line\n" * 100) == "utf-8"

    def test_utf8_chinese(self):
        data = "2024-01-01 错误：时钟初始化失败\n" * 100
        assert detect_encoding_from_bytes(data.encode("utf-8")) == "utf-8"

    def test_gbk_chinese_detected(self):
        data = "2024-01-01 错误：时钟初始化失败\n" * 100
        assert detect_encoding_from_bytes(data.encode("gbk")) == "gb18030"

    def test_gb2312_detected(self):
        data = "测试日志：模块错误\n" * 100
        assert detect_encoding_from_bytes(data.encode("gb2312")) == "gb18030"

    def test_utf8_sig_bom(self):
        data = "ERROR boom\n".encode("utf-8-sig")
        assert detect_encoding_from_bytes(data) == "utf-8-sig"

    def test_utf16_le_bom(self):
        data = "ERROR boom\n".encode("utf-16-le")
        assert detect_encoding_from_bytes(b"\xff\xfe" + data) == "utf-16-le"

    def test_utf16_be_bom(self):
        data = "ERROR boom\n".encode("utf-16-be")
        assert detect_encoding_from_bytes(b"\xfe\xff" + data) == "utf-16-be"

    def test_empty_bytes(self):
        assert detect_encoding_from_bytes(b"") == "utf-8"

    def test_invalid_bytes_fallback_utf8(self):
        # 既是非法 UTF-8 也是非法 gb18030 的字节（流式读取时容错替换）
        data = b"ERROR line\n\xc3\x28 bad \xff\xff tail\nINFO ok\n" * 50
        assert detect_encoding_from_bytes(data) == "utf-8"


class TestFileDetection:
    def test_detect_encoding_file(self, tmp_path):
        p = tmp_path / "gbk.log"
        p.write_bytes("错误：模块初始化失败\n".encode("gbk") * 100)
        assert detect_encoding(p) == "gb18030"

    def test_open_text_stream_decodes_gbk(self, tmp_path):
        p = tmp_path / "gbk.log"
        text = "2024-01-01 ERROR 中文错误消息\n"
        p.write_bytes(text.encode("gbk"))
        with open_text_stream(p, "gb18030") as fh:
            content = fh.read()
        assert "中文错误消息" in content

    def test_open_text_stream_replaces_invalid_bytes(self, tmp_path):
        p = tmp_path / "broken.log"
        p.write_bytes(b"ERROR line\n\xff\xfe broken bytes\nINFO ok\n")
        with open_text_stream(p, "utf-8") as fh:
            content = fh.read()
        # 容错解码：坏字节替换但流不中断
        assert "ERROR line" in content and "INFO ok" in content
