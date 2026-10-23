# -*- coding: utf-8 -*-
"""主编码必须由**行数**投票决定，而不是「整块能不能解通」。

这是第十一轮外部复核（2026-10-23）抓到的真缺陷。
外部复核方给的现场是「9 行 UTF-8 + 1 行 GBK ⇒ 报告说主编码是 gb18030」，
我**自己复现了一遍**（不转述他人结论），复现结果与它一致，并顺着它找到了
根因与更准确的形状。

缺陷长什么样
------------
`core/encoding.py::detect_encoding_from_bytes` 此前按候选顺序（utf-8 → gb18030）
做**整块**严格解码：谁能整块解通谁就是主编码。

问题在于 **gb18030 几乎什么字节序列都能「解成功」**：

* 一行 GBK 就足以让整块的 utf-8 严格解码失败；
* 而 UTF-8 的字节只要恰好能按 gb18030 两两配对，整块 gb18030 就解得通
  （实测：UTF-8 中文不在行尾时正好配得上）。

于是「9 行 UTF-8 + 1 行 GBK」的文件被判成主编码 gb18030，报告写
**「有 9 行不是主编码 gb18030」** —— **把多数派说成少数派**。
读者会据此以为自己的日志是 GBK，而 README 的错误表里写的恰恰是
「有 N 行不是主编码 utf-8」。

数据本身没坏（逐行兜底本来就优先 utf-8，中文解得对），坏的是这个**标签**
与它带出来的计数。

修法
----
主编码由**逐行投票**决定：utf-8 能严格解开的行 ≥ 一半 ⇒ utf-8；
否则才退到 gb18030。这不是新规矩 —— `LogDecoder` 的文档串里早就写着
「utf-8 优先于 gb18030」，此前只是**探测**没按它执行。

为什么这些断言有牙齿
--------------------
每条都标注了「改坏会红吗」。把 `core/encoding.py` 里投票那一段换回
「整块判定」，本文件第 1 条立刻红；把投票的门槛改成「只要有一行 utf-8
就算 utf-8」，第 2 条立刻红。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from log_ai_compressor import service as S
from log_ai_compressor.core import encoding as ENC

_ROOT = Path(__file__).resolve().parent.parent

#: UTF-8 行 —— 中文**不在行尾**（这样它的字节恰好能按 gb18030 两两配对，
#: 整块 gb18030 也能解通，正是缺陷的触发条件）。
_U8_LINE = "2024-03-15 08:00:00 ERROR 数据异常\n".encode("utf-8")
#: GBK 行
_GBK_LINE = "2024-03-15 08:00:09 ERROR GBK 行：连接失败\n".encode("gb18030")


def _mixed(tmp_path: Path, n_u8: int, n_gbk: int, name: str = "m.log") -> Path:
    p = tmp_path / name
    p.write_bytes(_U8_LINE * n_u8 + _GBK_LINE * n_gbk)
    return p


class TestMainEncodingIsDecidedByLines:
    def test_a_mostly_utf8_file_is_not_reported_as_gbk(self, tmp_path: Path):
        """9 行 UTF-8 + 1 行 GBK ⇒ 主编码必须是 utf-8，只有 1 行回退。

        **改坏会红吗**：把 `detect_encoding_from_bytes` 里的投票换回
        「整块判定」（候选按顺序试整块严格解码），本断言红 ——
        整块 gb18030 能解通，主编码变回 gb18030，混合行数从 1 变 9。
        """
        p = _mixed(tmp_path, 9, 1)
        result = S.analyze(paths=[str(p)])
        assert result.stats.encoding == "utf-8", (
            "9 行 UTF-8 + 1 行 GBK 的文件被判成主编码 %r —— "
            "把多数派说成少数派了" % result.stats.encoding)
        assert result.stats.mixed_encoding_lines == 1, (
            "混合编码行数是 %r，应该只有 1 行（那一行 GBK）"
            % result.stats.mixed_encoding_lines)
        assert "不是主编码 utf-8" in (result.notice or ""), (
            "提示没说主编码是 utf-8：%r" % result.notice)

    def test_the_notice_does_not_blame_the_majority(self, tmp_path: Path):
        """提示里的行数必须指向**真正的少数派**，而不是反过来。

        **改坏会红吗**：把投票门槛改成「只要有一行 utf-8 就算 utf-8 主编码」，
        本断言在 1 行 UTF-8 + 9 行 GBK 那组夹具上红（主编码会变成 utf-8，
        提示会说 9 行不是主编码）。
        """
        p = _mixed(tmp_path, 1, 9, name="mostly_gbk.log")
        result = S.analyze(paths=[str(p)])
        assert result.stats.encoding == "gb18030", (
            "1 行 UTF-8 + 9 行 GBK 的文件被判成主编码 %r —— "
            "多数派明明是 GBK" % result.stats.encoding)
        assert result.stats.mixed_encoding_lines == 1, (
            "混合编码行数是 %r，应该只有 1 行（那一行 UTF-8）"
            % result.stats.mixed_encoding_lines)

    def test_pure_files_are_unaffected(self, tmp_path: Path):
        """纯 UTF-8 / 纯 GBK / 纯 ASCII 的判定不能被投票改坏。

        **改坏会红吗**：把投票写成「只要有 utf-8 就选 utf-8」，纯 GBK 那条红。
        """
        u8 = tmp_path / "pure_u8.log"
        u8.write_bytes("2024-01-01 10:00:00 ERROR 数据库连接失败\n".encode("utf-8") * 5)
        assert ENC.detect_encoding_from_bytes(u8.read_bytes()) == "utf-8"

        gbk = tmp_path / "pure_gbk.log"
        gbk.write_bytes("2024-01-01 10:00:00 ERROR 数据库连接失败\n".encode("gb18030") * 5)
        assert ENC.detect_encoding_from_bytes(gbk.read_bytes()) == "gb18030"

        ascii_only = tmp_path / "pure_ascii.log"
        ascii_only.write_bytes(b"2024-01-01 10:00:00 ERROR plain line\n" * 5)
        assert ENC.detect_encoding_from_bytes(ascii_only.read_bytes()) == "utf-8"

    def test_the_decoded_text_is_the_same_whatever_the_label_says(self, tmp_path: Path):
        """改标签**不能**改数据：中文必须照样解对，且不得出现替换符。

        这是这条修复的底线 —— 缺陷只影响「主编码」这个标签与它带出来的计数，
        解出来的文本必须一模一样。

        **改坏会红吗**：把 `_decode_narrow` 里「utf-8 解得出来就用它」那条
        去掉，本断言红（9 行 UTF-8 会被按 gb18030 解成乱码）。
        """
        p = _mixed(tmp_path, 9, 1)
        result = S.analyze(paths=[str(p)])
        text = json.dumps(result.clusters, ensure_ascii=False, default=str)
        assert "\ufffd" not in text, "出现了替换符"
        assert "数据异常" in text, (
            "UTF-8 的行没解对（被按 gb18030 解成乱码了）：%s" % text[:300])

    def test_the_cli_output_says_utf8_not_gbk(self, tmp_path: Path):
        """命令行输出里的提示也要说 utf-8 —— 那是用户唯一会看到的一行。

        **改坏会红吗**：把投票换回整块判定，本断言红（提示变成
        「有 9 行不是主编码 gb18030」）。
        """
        import os
        p = _mixed(tmp_path, 9, 1)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "log_ai_compressor", "run", str(p), "-f", "json"],
            cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=300)
        assert proc.returncode == 0, "跑失败：%s" % proc.stderr[-300:]
        data = json.loads(proc.stdout)
        notice = data.get("notice") or ""
        assert "不是主编码 utf-8" in notice, (
            "CLI 提示没提 utf-8：%r" % notice)
        assert "gb18030" not in notice, (
            "CLI 提示把多数派说成 gb18030：%r" % notice)
