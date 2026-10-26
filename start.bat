@echo off
chcp 65001 >nul
title MiniBot 极简主动交互智能体

cd /d "%~dp0"

echo =====================================================
echo           🤖 MiniBot 极简主动交互智能体
echo =====================================================

:: 1. 检查是否存在独立虚拟环境 .venv
if not exist ".venv\Scripts\python.exe" (
    echo [引导] 首次运行，正在创建项目独立虚拟环境 (.venv)...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确保系统已安装 Python 3.10+ 并加入 PATH。
        pause
        exit /b 1
    )
    echo [引导] 正在配置清华大学开源镜像源 (高速下载)...
    .venv\Scripts\python.exe -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>&1
    echo [引导] 虚拟环境初始化完成！
)

:: 2. 启动交互式引导程序
.venv\Scripts\python.exe launcher.py

if errorlevel 1 (
    echo.
    echo [MiniBot] 程序异常退出。
    pause
)
