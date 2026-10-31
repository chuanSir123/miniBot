"""
name: system_status
description: 检查本机系统基本状态与时间
"""
import time
import os

def execute(context: dict) -> str:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"【系统状态】当前本地时间: {now_str}，工作目录: {os.getcwd()}"
