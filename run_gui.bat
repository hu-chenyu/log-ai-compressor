@echo off
REM ============================================================
REM  log-ai-compressor 日志AI压缩器 - GUI 双击启动脚本
REM  自动定位 Python、自动补装依赖，任何 Windows 环境可用
REM ============================================================
setlocal
title 日志AI压缩器

REM 切换到脚本所在目录（保证能找到 run_gui.py 与源码包）
cd /d "%~dp0"

REM ---- 探测可用的 Python（优先常规 python，回退 py 启动器）----
REM 用真实执行校验排除 Windows 商店占位程序（它会静默失败）
set "PY="
python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [错误] 未检测到 Python 3.9+。
    echo        请从 https://www.python.org/downloads/ 安装，
    echo        安装时务必勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

REM ---- 依赖自检：缺失则自动安装 ----
%PY% -c "import customtkinter, matplotlib, yaml" >nul 2>nul
if errorlevel 1 (
    echo [初始化] 首次运行，正在安装依赖（约 1~2 分钟）...
    %PY% -m pip install customtkinter matplotlib PyYAML tkinterdnd2
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请手动执行：pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM ---- 启动 GUI ----
%PY% run_gui.py
if errorlevel 1 (
    echo.
    echo [错误] GUI 启动失败，请将上方报错信息反馈给开发者。
    pause
)
endlocal
