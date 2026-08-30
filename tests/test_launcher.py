# -*- coding: utf-8 -*-
"""双击启动脚本 run_gui.bat 测试：编码、行尾、关键逻辑。"""
from __future__ import annotations

from pathlib import Path

import pytest

BAT_PATH = Path(__file__).resolve().parent.parent / "run_gui.bat"


@pytest.fixture(scope="module")
def bat_content() -> str:
    """读取 bat 内容（GBK 编码 + CRLF 行尾，Windows cmd 解析要求）。"""
    if not BAT_PATH.exists():
        pytest.skip("run_gui.bat 不存在")
    raw = BAT_PATH.read_bytes()
    # cmd 逐行解析要求 CRLF：不允许存在裸 LF
    assert raw.count(b"\n") == raw.count(b"\r\n"), "bat 必须使用 CRLF 行尾"
    return raw.decode("gbk")   # 中文 Windows 控制台默认代码页


class TestRunGuiBat:
    def test_file_exists(self):
        assert BAT_PATH.is_file()

    def test_echo_off_and_no_chcp_utf8(self, bat_content):
        # @echo off 首行；不使用 chcp 65001（UTF-8 模式会破坏 cmd 逐行解析）
        assert bat_content.splitlines()[0].strip().lower() == "@echo off"
        assert "chcp 65001" not in bat_content

    def test_cd_to_script_dir(self, bat_content):
        # 切换到脚本目录，保证任何工作目录下双击都能找到 run_gui.py
        assert 'cd /d "%~dp0"' in bat_content

    def test_python_fallback_detection(self, bat_content):
        # python 优先、py -3 回退（覆盖仅装 py 启动器的环境）
        assert 'set "PY=python"' in bat_content
        assert "py -3" in bat_content
        # 用真实执行校验排除 Windows 商店占位 python
        assert 'python -c "import sys"' in bat_content

    def test_dependency_auto_install(self, bat_content):
        # 依赖缺失时自动安装（tkinterdnd2 为拖拽所需）
        assert "pip install customtkinter matplotlib PyYAML tkinterdnd2" in bat_content

    def test_launches_run_gui(self, bat_content):
        assert "%PY% run_gui.py" in bat_content

    def test_failure_shows_pause(self, bat_content):
        # 失败分支必须 pause，避免双击后窗口闪退看不到错误
        assert bat_content.count("pause") >= 3
