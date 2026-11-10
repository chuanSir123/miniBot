# -*- coding: utf-8 -*-
r"""
svg_to_mp4: 将 HTML/SVG 动画页面转成 MP4 视频。

方案（2026-10 鹈鹕骑车动画验证成功）：
1. 截图在独立 Python 子进程中执行（自动探测装了 playwright 的解释器），
   用 Playwright 无头 Chromium + 虚拟时钟（clock.install + clock.run_for(整数毫秒)）逐帧截图，
   动画时间严格按帧推进；run_for 只接受 int 毫秒，用累计取整保证平均帧间隔恰为 1/fps
   （30fps 时序列为 33,34,33,33,34,...ms）；无 clock API 时回退真实时间 sleep。
2. ffmpeg 按指定 fps 合成 MP4；编码器自动回退 libx264 -> libopenh264 -> mpeg4
   （不同机器上的 ffmpeg 编译选项不一样，禁用了 libx264 的必须回退）。
3. ffprobe 验证 duration/size/codec/width/height/r_frame_rate 并随结果返回。
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time

PARAMETERS = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "description": "HTML/SVG动画文件路径（绝对或相对工作空间）"},
        "duration_sec": {"type": "number", "description": "录制时长秒数，默认12", "default": 12},
        "fps": {"type": "integer", "description": "帧率，默认30", "default": 30},
        "width": {"type": "integer", "description": "视口宽度，默认1280", "default": 1280},
        "height": {"type": "integer", "description": "视口高度，默认800", "default": 800},
        "output": {"type": "string", "description": "输出MP4路径，默认输入文件名+.mp4"},
    },
    "required": ["file_path"],
    "additionalProperties": False,
}

# 截图子进程脚本（在独立 Python 中执行，确保能 import playwright）
_CAPTURE_SCRIPT = r"""
import json, os, sys, time
from playwright.sync_api import sync_playwright

def main():
    cfg = json.loads(sys.argv[1])
    file_path = cfg["file_path"]
    duration = cfg["duration_sec"]
    fps = cfg["fps"]
    width = cfg["width"]
    height = cfg["height"]
    frames_dir = cfg["frames_dir"]
    n_frames = cfg["n_frames"]

    t0 = time.time()
    use_clock = False
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        pg.goto("file:///" + file_path.replace("\\", "/"))
        pg.wait_for_load_state("networkidle")
        use_clock = hasattr(pg, "clock")
        if use_clock:
            pg.clock.install()
        # 检测页面是否支持外部时间注入(window.__setAnimTime)。
        # 若支持, 每帧注入精确虚拟时间再截图, 彻底规避截图耗时导致
        # performance.now()/rAF 跟随真实墙钟被加速的问题(实测约2.4x)。
        use_ext_time = False
        try:
            use_ext_time = pg.evaluate('typeof window.__setAnimTime === "function"')
        except Exception:
            use_ext_time = False
        prev_ms = 0
        for i in range(n_frames):
            cur_ms = int(round((i + 1) * 1000.0 / fps))
            if use_ext_time:
                # 精确注入虚拟时间(同步渲染) -> 截图 -> 推进虚拟时钟
                pg.evaluate('window.__setAnimTime(%d)' % cur_ms)
                pg.screenshot(path=os.path.join(frames_dir, "frame_%04d.png" % i))
                if use_clock:
                    pg.clock.run_for(max(1, cur_ms - prev_ms))
                    prev_ms = cur_ms
            else:
                pg.screenshot(path=os.path.join(frames_dir, "frame_%04d.png" % i))
                if use_clock:
                    pg.clock.run_for(max(1, cur_ms - prev_ms))
                    prev_ms = cur_ms
                else:
                    target = t0 + (i + 1) / fps
                    delay = target - time.time()
                    if delay > 0:
                        time.sleep(delay)
        b.close()
    print(json.dumps({"ok": True, "use_clock": use_clock, "use_ext_time": use_ext_time, "wall_sec": round(time.time() - t0, 1)}))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": repr(e)}))
        sys.exit(1)
"""


def _project_root():
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def _tool_candidates(name):
    """按「环境变量 > PATH > 项目内置 > 常见安装目录」列出 ffmpeg/ffprobe 候选。

    ffmpeg 每家装的位置都不一样，写死盘符必然只能在本机跑：
    要临时指定可设 MINIBOT_FFMPEG / MINIBOT_FFPROBE（或 FFMPEG_BINARY / FFPROBE_BINARY）。
    """
    exe = name + ".exe" if os.name == "nt" else name
    root = _project_root()
    candidates = [
        os.environ.get("MINIBOT_" + name.upper(), ""),
        os.environ.get(name.upper() + "_BINARY", ""),
        shutil.which(name) or "",
        os.path.join(root, "tools", "bin", exe),
        os.path.join(root, "tools", "ffmpeg", "bin", exe),
        os.path.join(root, "tools", exe),
    ]
    # 常见安装位置：ffmpeg 官网压出来的是 <根>\ffmpeg\bin\ffmpeg.exe。
    bases = [os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
             os.environ.get("PROGRAMFILES(X86)")]
    if os.name == "nt":
        # 精简环境里 PROGRAMFILES 可能没设，按系统盘补回标准目录。
        # 盘符要带分隔符，否则 join("C:", "Program Files") 会变成盘符相对路径。
        drive = os.path.splitdrive(os.environ.get("WINDIR") or "C:\\Windows")[0] or "C:"
        drive = drive + os.sep
        bases += [os.path.join(drive, "Program Files"), os.path.join(drive, "Program Files (x86)"),
                  os.path.join(os.path.expanduser("~"), "AppData", "Local"), drive]
    for base in bases:
        if not base:
            continue
        candidates += [os.path.join(base, "ffmpeg", "bin", exe),
                       os.path.join(base, "ffmpeg", exe)]
    return [c for c in candidates if c]


def _find_tool(candidates):
    for c in candidates:
        if os.path.isfile(c) or shutil.which(c):
            return c
    return None


def _find_python_with_playwright():
    """探测装了 playwright 的 Python 解释器（不写死用户名下的安装路径）。"""
    candidates = [os.environ.get("MINIBOT_PYTHON") or os.environ.get("PLAYWRIGHT_PYTHON"),
                  sys.executable, shutil.which("python"), shutil.which("python3")]
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)")):
        if not base:
            continue
        candidates += sorted(glob.glob(os.path.join(base, "Programs", "Python", "Python3*", "python.exe")), reverse=True)
        candidates += sorted(glob.glob(os.path.join(base, "Python3*", "python.exe")), reverse=True)
    candidates += ["/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"]
    seen = set()
    for py in candidates:
        if not py or py in seen or not os.path.isfile(py):
            continue
        seen.add(py)
        try:
            r = subprocess.run([py, "-c", "import playwright"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def _get_args(context):
    if isinstance(context, dict):
        if isinstance(context.get("args"), dict):
            return context["args"]
        return context
    return {}


def execute(context):
    args = _get_args(context)
    file_path = args.get("file_path") or args.get("html") or ""
    duration = float(args.get("duration_sec", 12) or 12)
    fps = int(args.get("fps", 30) or 30)
    width = int(args.get("width", 1280) or 1280)
    height = int(args.get("height", 800) or 800)

    if not (1 <= fps <= 120):
        return {"ok": False, "error": "fps 需在 1~120 之间"}
    if not (0.2 <= duration <= 3600):
        return {"ok": False, "error": "duration_sec 需在 0.2~3600 之间"}

    ws = None
    try:
        ws = (context or {}).get("_minibot", {}).get("workspace")
    except Exception:
        ws = None
    if not ws:
        ws = os.getcwd()

    if not file_path:
        return {"ok": False, "error": "缺少 file_path 参数"}
    if not os.path.isabs(file_path):
        file_path = os.path.join(ws, file_path)
    if not os.path.isfile(file_path):
        return {"ok": False, "error": "文件不存在: %s" % file_path}

    ffmpeg_candidates, ffprobe_candidates = _tool_candidates("ffmpeg"), _tool_candidates("ffprobe")
    ffmpeg = _find_tool(ffmpeg_candidates)
    if not ffmpeg:
        return {"ok": False, "error": "未找到 ffmpeg（可用 MINIBOT_FFMPEG 指定路径；尝试过: %s）" % ", ".join(ffmpeg_candidates)}
    ffprobe = _find_tool(ffprobe_candidates)

    out = args.get("output") or (os.path.splitext(file_path)[0] + ".mp4")
    if not os.path.isabs(out):
        out = os.path.join(ws, out)

    n_frames = max(1, int(round(duration * fps)))
    frames_dir = os.path.join(ws, "_svg2mp4_frames_%d" % os.getpid())
    if os.path.isdir(frames_dir):
        shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir)

    # 探测装了 playwright 的 Python
    py = _find_python_with_playwright()
    if not py:
        shutil.rmtree(frames_dir, ignore_errors=True)
        return {"ok": False, "error": "未找到装了 playwright 的 Python 解释器"}

    # 在独立子进程中截图
    cfg = {
        "file_path": file_path,
        "duration_sec": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "frames_dir": frames_dir,
        "n_frames": n_frames,
    }
    t0 = time.time()
    r = subprocess.run([py, "-c", _CAPTURE_SCRIPT, json.dumps(cfg)],
                       capture_output=True, text=True, timeout=3600)
    cap_info = {}
    if r.stdout:
        try:
            cap_info = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
    if r.returncode != 0 or not cap_info.get("ok"):
        shutil.rmtree(frames_dir, ignore_errors=True)
        err = cap_info.get("error") or (r.stderr or "")[-800:]
        return {"ok": False, "error": "截图失败: %s" % err}

    use_clock = cap_info.get("use_clock", False)
    wall_capture = cap_info.get("wall_sec", 0)

    # 编码器自动回退（本机 ffmpeg 可能禁用了 libx264）
    encoders = [
        ("libx264", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium"]),
        ("libopenh264", ["-c:v", "libopenh264", "-pix_fmt", "yuv420p", "-b:v", "2500k"]),
        ("mpeg4", ["-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-q:v", "2"]),
    ]
    encoded_with = None
    last_err = ""
    for name, enc_args in encoders:
        cmd = [ffmpeg, "-y", "-framerate", str(fps),
               "-i", os.path.join(frames_dir, "frame_%04d.png")] + enc_args + [out]
        r2 = subprocess.run(cmd, capture_output=True, text=True)
        if r2.returncode == 0:
            encoded_with = name
            break
        last_err = (r2.stderr or "")[-800:]
    shutil.rmtree(frames_dir, ignore_errors=True)
    if not encoded_with:
        return {"ok": False, "error": "所有编码器均失败: %s" % last_err}

    info = {}
    if ffprobe:
        pr = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,size,bit_rate",
             "stream=codec_name,width,height,r_frame_rate",
             "-of", "default=noprint_wrappers=1", out],
            capture_output=True, text=True)
        for line in (pr.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()

    size = os.path.getsize(out) if os.path.isfile(out) else 0
    return {
        "ok": True,
        "output": out,
        "size_bytes": size,
        "encoded_with": encoded_with,
        "frames": n_frames,
        "duration_sec": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "use_virtual_clock": use_clock,
        "wall_capture_sec": wall_capture,
        "wall_total_sec": round(time.time() - t0, 1),
        "python_used": py,
        "ffprobe": info,
    }
DESCRIPTION = '把 HTML/SVG 动画页面转成 MP4 视频。主人要求"SVG/HTML动画转MP4/录制成视频/导出视频"时调用本技能。内部用 Playwright 无头 Chromium + 虚拟时钟逐帧截图（时间精确推进，平均帧间隔恰为1/fps），ffmpeg 编码器自动回退（libx264→libopenh264→mpeg4，兼容禁用libx264的ffmpeg），最后 ffprobe 验证时长/分辨率/编码/帧率并返回结果。截图在独立 Python 子进程中执行（自动探测装了 playwright 的解释器），避免宿主进程缺模块。'

PARAMETERS = {'type': 'object', 'properties': {'file_path': {'type': 'string', 'description': 'HTML/SVG动画文件路径（绝对或相对Harness工作空间）'}, 'duration_sec': {'type': 'number', 'description': '录制时长秒数，默认12', 'default': 12}, 'fps': {'type': 'integer', 'description': '帧率，默认30', 'default': 30}, 'width': {'type': 'integer', 'description': '视口宽度，默认1280', 'default': 1280}, 'height': {'type': 'integer', 'description': '视口高度，默认800', 'default': 800}, 'output': {'type': 'string', 'description': '输出MP4路径，默认输入文件名+.mp4'}}, 'required': ['file_path'], 'additionalProperties': False}

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from core.skill_cli import main

    raise SystemExit(main(execute))
