# -*- coding: utf-8 -*-
r"""diagram render: 把 SVG 渲染成高清 PNG（Playwright 无头 Chromium 截图）。

宿主解释器通常没装 playwright，沿用 svg_to_mp4 验证过的模式：
探测装了 playwright 的解释器 → 起子进程截图。截图用
device_scale_factor 放大出高清图，clip 到 viewBox 尺寸去掉白边。
"""
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

_CAPTURE = r'''
import sys
from playwright.sync_api import sync_playwright
svg_path, out_path, w, h, scale = (
    sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
)
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": w, "height": h}, device_scale_factor=scale
    )
    page.goto("file:///" + svg_path.replace("\\\\", "/").lstrip("/"))
    page.wait_for_timeout(300)
    page.screenshot(path=out_path, clip={"x": 0, "y": 0, "width": w, "height": h})
    browser.close()
print("CAPTURE_OK")
'''

_PY_CANDIDATES = [
    sys.executable,
    shutil.which("python"),
    shutil.which("python3"),
    r"C:\users\admin\appdata\local\programs\python\python310\python.exe",
]


def _python_with_playwright():
    seen = set()
    for py in _PY_CANDIDATES:
        if not py or py in seen:
            continue
        seen.add(py)
        try:
            r = subprocess.run([py, "-c", "import playwright"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def _svg_size(path):
    root = ET.parse(path).getroot()
    vb = root.get("viewBox") or "0 0 680 480"
    parts = vb.replace(",", " ").split()
    return float(parts[2]), float(parts[3])


def execute(context):
    if isinstance(context, dict) and isinstance(context.get("args"), dict):
        args = dict(context["args"])
    else:
        args = {k: v for k, v in (context or {}).items() if k != "_minibot"}
    ws = (context.get("_minibot") or {}).get("workspace") or os.getcwd()
    file_path = str(args.get("file_path") or "")
    if not file_path:
        return {"ok": False, "error": "缺少 file_path 参数"}
    if not os.path.isabs(file_path):
        file_path = os.path.join(ws, file_path)
    if not os.path.isfile(file_path):
        return {"ok": False, "error": f"文件不存在: {file_path}"}

    out = str(args.get("output") or (os.path.splitext(file_path)[0] + ".png"))
    if not os.path.isabs(out):
        out = os.path.join(ws, out)
    scale = max(1.0, min(4.0, float(args.get("scale", 2) or 2)))

    try:
        w, h = _svg_size(file_path)
    except Exception as exc:
        return {"ok": False, "error": f"SVG 解析失败: {exc}"}
    w, h = int(w), int(h)

    py = _python_with_playwright()
    if not py:
        return {"ok": False, "error": "未找到装 playwright 的 Python 解释器（尝试过: %s）" %
                ", ".join(p for p in _PY_CANDIDATES if p)}

    try:
        pr = subprocess.run(
            [py, "-I", "-c", _CAPTURE, os.path.abspath(file_path), os.path.abspath(out),
             str(w), str(h), str(scale)],
            capture_output=True, text=True, timeout=120, encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "渲染超时（120s）"}

    if pr.returncode != 0 or "CAPTURE_OK" not in (pr.stdout or ""):
        return {"ok": False, "error": f"渲染失败: {(pr.stderr or pr.stdout or '')[-400:]}"}

    size = os.path.getsize(out) if os.path.isfile(out) else 0
    return {
        "ok": True,
        "output": out,
        "size_bytes": size,
        "width": w,
        "height": h,
        "scale": scale,
        "python_used": py,
    }
