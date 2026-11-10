"""
name: system_status
description: 检查本机系统基本状态与时间
"""
import time
import os

def execute(context: dict) -> str:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"【系统状态】当前本地时间: {now_str}，工作目录: {os.getcwd()}"

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from core.skill_cli import main

    raise SystemExit(main(execute))
