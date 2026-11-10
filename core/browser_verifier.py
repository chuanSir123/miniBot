"""
Browser verification and multi-frame dynamic visual inspection.
Provides headless browser webpage rendering, multi-frame animation verification,
frame diffing, and animated GIF generation for closing the loop on UI/frontend code.
"""

import asyncio
import base64
import io
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union
from PIL import Image, ImageChops, ImageDraw, ImageFont

from core.workspace import resolve_file, workspace_for


def find_browser_executable() -> Optional[str]:
    """Find system Chromium-based browser (Chrome or Edge).

    Install roots come from the environment (PROGRAMFILES / PROGRAMFILES(X86) /
    LOCALAPPDATA) instead of a hardcoded drive path, so the same code works on
    machines where Chrome is per-user installed or Windows lives on another drive.
    """
    layouts = {
        "Google": ("Chrome", "Application", "chrome.exe"),
        "Microsoft": ("Edge", "Application", "msedge.exe"),
    }
    candidates: List[str] = []
    bases = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
             os.environ.get("PROGRAMW6432"), os.environ.get("LOCALAPPDATA")]
    if os.name == "nt":
        # 服务/精简环境里 PROGRAMFILES 可能没设，按系统盘补回标准目录。
        # 盘符要带分隔符，否则 join("C:", "Program Files") 会变成盘符相对路径。
        drive = os.path.splitdrive(os.environ.get("WINDIR") or "C:\\Windows")[0] or "C:"
        drive = drive + os.sep
        bases += [os.path.join(drive, "Program Files"), os.path.join(drive, "Program Files (x86)"),
                  os.path.join(os.path.expanduser("~"), "AppData", "Local")]
    for base in bases:
        if not base:
            continue
        for vendor, tail in layouts.items():
            candidates.append(os.path.join(base, vendor, *tail))
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("chrome.exe", "msedge.exe", "chrome", "msedge", "chromium"):
        p = shutil.which(name)
        if p:
            return p
    # macOS / Linux 常见安装位置，别人换平台也能直接跑。
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(c):
            return c
    return None


def resolve_web_target(target: str, scope: str = "main") -> Tuple[str, Optional[Path]]:
    """
    Resolve target string to a URL for browser navigation.
    Returns (url, local_file_path or None).
    """
    t_str = str(target).strip()
    if t_str.startswith(("http://", "https://", "file://")):
        return t_str, None

    # Treat as relative or absolute local file path
    p = resolve_file(t_str, scope)
    if not p.exists():
        p_abs = Path(t_str)
        if p_abs.exists():
            p = p_abs

    file_url = "file:///" + str(p.resolve()).replace("\\", "/")
    return file_url, p


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def _capture_cdp_async(
    browser_exe: str,
    url: str,
    num_frames: int,
    interval_sec: float,
    window_size: Tuple[int, int],
    diagnostics: Optional[Dict] = None,
) -> Tuple[List[bytes], List[float]]:
    """Capture true real-time sequential frames using Chrome DevTools Protocol."""
    try:
        import websockets
    except ImportError:
        return [], []

    port = _get_free_port()
    w, h = window_size
    user_data = Path(os.environ.get("TEMP", ".")) / f"mini_cdp_{port}"
    user_data.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser_exe,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        f"--window-size={w},{h}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={str(user_data)}",
        "about:blank",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frames_bytes: List[bytes] = []
    timestamps: List[float] = []

    try:
        target_ws_url = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as resp:
                    pages = json.loads(resp.read().decode())
                    for p in pages:
                        if p.get("type") == "page" and p.get("webSocketDebuggerUrl"):
                            target_ws_url = p.get("webSocketDebuggerUrl")
                            break
                    if target_ws_url:
                        break
            except Exception:
                await asyncio.sleep(0.1)

        if not target_ws_url:
            return [], []

        async with websockets.connect(target_ws_url, max_size=25 * 1024 * 1024) as ws:
            msg_id = 0

            async def send(method, params=None):
                nonlocal msg_id
                msg_id += 1
                req = {"id": msg_id, "method": method, "params": params or {}}
                await ws.send(json.dumps(req))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    res = json.loads(raw)
                    if diagnostics is not None:
                        if res.get("method") == "Runtime.exceptionThrown":
                            detail = res.get("params", {}).get("exceptionDetails", {})
                            error = detail.get("exception", {}).get("description") or detail.get("text", "JavaScript exception")
                            diagnostics.setdefault("runtime_errors", []).append(str(error)[:1000])
                        elif res.get("method") == "Log.entryAdded":
                            entry = res.get("params", {}).get("entry", {})
                            if entry.get("level") == "error":
                                diagnostics.setdefault("runtime_errors", []).append(str(entry.get("text", "Browser error"))[:1000])
                    if res.get("id") == msg_id:
                        if res.get("error"):
                            raise RuntimeError(str(res["error"]))
                        return res.get("result", {})

            await send("Page.enable")
            await send("Runtime.enable")
            await send("Log.enable")
            navigation = await send("Page.navigate", {"url": url})
            if navigation.get("errorText"):
                if diagnostics is not None:
                    diagnostics.setdefault("runtime_errors", []).append(navigation["errorText"])
                return [], []
            if diagnostics is not None:
                diagnostics["runtime_checked"] = True
            # Wait for layout & initial animation start
            await asyncio.sleep(0.35)

            start_t = time.monotonic()
            for i in range(num_frames):
                res = await send("Page.captureScreenshot", {"format": "png"})
                b64_data = res.get("data", "")
                if b64_data:
                    frames_bytes.append(base64.b64decode(b64_data))
                    timestamps.append(round(time.monotonic() - start_t, 2))
                if i < num_frames - 1:
                    await asyncio.sleep(interval_sec)

    except Exception as exc:
        print(f"[BrowserVerifier] CDP capture failed: {exc}")
        return [], []
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(user_data, ignore_errors=True)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    return frames_bytes, timestamps


def capture_cdp_sequential_frames(
    browser_exe: str,
    url: str,
    num_frames: int = 4,
    interval_sec: float = 0.4,
    window_size: Tuple[int, int] = (1280, 800),
    diagnostics: Optional[Dict] = None,
) -> Tuple[List[bytes], List[float]]:
    """Synchronous entry point to capture consecutive animation frames via CDP."""
    try:
        return asyncio.run(
            _capture_cdp_async(browser_exe, url, num_frames, interval_sec, window_size, diagnostics)
        )
    except Exception as e:
        print(f"[BrowserVerifier] CDP runner error: {e}")
        return [], []


def render_webpage_screenshot(
    url: str,
    out_path: str,
    window_size: str = "1280,800",
    virtual_time_budget_ms: int = 800,
    timeout_sec: int = 20,
) -> bool:
    """
    Render a webpage in a headless browser and capture a screenshot.
    """
    browser = find_browser_executable()
    if not browser:
        return False

    out_file = Path(out_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if out_file.exists():
        try:
            out_file.unlink()
        except Exception:
            pass

    cmd = [
        browser,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={window_size}",
        f"--virtual-time-budget={virtual_time_budget_ms}",
        f"--screenshot={str(out_file)}",
        url,
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            check=False,
        )
        return out_file.exists() and out_file.stat().st_size > 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[BrowserVerifier] Render error: {exc}")
        return False


def compare_frames(frame_paths: List[Path]) -> Dict:
    """
    Compare sequential frames to verify dynamic animation / movement.
    """
    if len(frame_paths) < 2:
        return {
            "dynamic_detected": False,
            "has_movement": False,
            "diff_count": 0,
            "diff_ratio": 0.0,
        }

    total_diff_pixels = 0
    max_diff_ratio = 0.0
    comparisons = []

    for i in range(len(frame_paths) - 1):
        try:
            im1 = Image.open(frame_paths[i]).convert("RGB")
            im2 = Image.open(frame_paths[i + 1]).convert("RGB")
            diff = ImageChops.difference(im1, im2)
            bbox = diff.getbbox()
            if bbox:
                # Calculate number of changed pixels
                diff_gray = diff.convert("L")
                data_iter = diff_gray.get_flattened_data() if hasattr(diff_gray, "get_flattened_data") else diff_gray.getdata()
                non_zero = sum(1 for p in data_iter if p > 15)  # threshold
                ratio = round(non_zero / (im1.width * im1.height), 4)
                total_diff_pixels += non_zero
                max_diff_ratio = max(max_diff_ratio, ratio)
                comparisons.append({
                    "from_frame": i + 1,
                    "to_frame": i + 2,
                    "changed_pixels": non_zero,
                    "changed_ratio": ratio,
                    "bbox": bbox,
                })
        except Exception as e:
            print(f"[BrowserVerifier] Diff error on frames {i}-{i+1}: {e}")

    dynamic_detected = bool(comparisons and (max_diff_ratio >= 0.0005 or total_diff_pixels >= 40))
    return {
        "dynamic_detected": dynamic_detected,
        "has_movement": dynamic_detected,
        "diff_count": len(comparisons),
        "max_diff_ratio": max_diff_ratio,
        "total_diff_pixels": total_diff_pixels,
        "comparisons": comparisons,
    }


def is_dynamic_web_content(target_path: Optional[Union[Path, str]], prompt: str = "") -> bool:
    """Detect if a webpage or prompt involves animation, games, canvas or dynamic loops."""
    if target_path:
        p = Path(target_path) if isinstance(target_path, str) else target_path
        if p and p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
                if re.search(
                    r"@keyframes|animation\s*:|requestAnimationFrame|setInterval|setTimeout|<canvas|transition\s*:",
                    text,
                    re.I,
                ):
                    return True
            except Exception:
                pass
    if prompt:
        if re.search(r"动画|动效|游戏|倒计时|旋转|跳跃|canvas|animation|keyframe", prompt, re.I):
            return True
    return False


def create_frame_filmstrip(
    frame_images: List[Union[Path, str, Image.Image]], timestamps: Optional[List[float]] = None
) -> bytes:
    """
    Stitch multiple captured frame images into a single comparative filmstrip image.
    Enables VLM to visually compare motion progression, limb positions, and dynamic continuity.
    """
    n = len(frame_images)
    if n == 0:
        return b""
    if n == 1:
        first = frame_images[0]
        if isinstance(first, (str, Path)):
            img = Image.open(first).convert("RGB")
        else:
            img = first.convert("RGB") if hasattr(first, "convert") else first
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # Layout:
    # 2 frames: 1x2 (horizontal side-by-side)
    # 3 or 4 frames: 2x2 grid
    # 5 or 6 frames: 3x2 grid
    if n == 2:
        cols, rows = 2, 1
    elif n <= 4:
        cols, rows = 2, 2
    else:
        cols, rows = 3, 2

    # Compact dimensions to prevent huge base64 token bloat on VLM context
    cell_w, cell_h = 420, 260
    banner_h = 26
    total_w = cols * cell_w
    total_h = rows * (cell_h + banner_h)

    montage = Image.new("RGB", (total_w, total_h), color=(15, 23, 42))
    draw = ImageDraw.Draw(montage)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for idx, item in enumerate(frame_images[: cols * rows]):
        c = idx % cols
        r = idx // cols
        x0 = c * cell_w
        y0 = r * (cell_h + banner_h)

        # Draw banner background
        draw.rectangle([x0, y0, x0 + cell_w, y0 + banner_h], fill=(30, 41, 59))
        t_sec = timestamps[idx] if timestamps and idx < len(timestamps) else idx * 0.5
        label = f"Frame {idx + 1} (T = {t_sec:.1f}s)"
        draw.text((x0 + 12, y0 + 8), label, fill=(56, 189, 248), font=font)

        # Obtain PIL image
        if isinstance(item, (str, Path)):
            img = Image.open(item).convert("RGB")
        else:
            img = item.convert("RGB") if hasattr(item, "convert") else item

        # Resize frame preserving aspect ratio
        thumb = img.copy()
        thumb.thumbnail((cell_w, cell_h), Image.Resampling.LANCZOS)
        offset_x = x0 + (cell_w - thumb.width) // 2
        offset_y = y0 + banner_h + (cell_h - thumb.height) // 2
        montage.paste(thumb, (offset_x, offset_y))

        # Cell border
        draw.rectangle(
            [x0, y0, x0 + cell_w, y0 + banner_h + cell_h],
            outline=(51, 65, 85),
            width=2,
        )

    buf = io.BytesIO()
    montage.save(buf, format="JPEG", quality=78, optimize=True)
    return buf.getvalue()


def _frame_path(ws_dir: Path, frames_dir: Path, run_id: int, idx: int, total: int) -> Path:
    """多帧采样落到 workspace/frames/ 下，用户能直接翻看这一轮采到了什么。

    以前这些文件带前导点、跑完就删，理由是"别弄乱工作空间"。代价是交付里
    只剩一张 GIF 和一张拼图，用户回看时找不到逐帧证据，出问题也无法判断是
    采样没采到还是判定没看清。
    """
    if total <= 1:
        return ws_dir / f"preview_{run_id}.png"
    frames_dir.mkdir(parents=True, exist_ok=True)
    return frames_dir / f"frame_{run_id}_{idx + 1}.png"


def create_animated_gif(frame_paths: List[Path], out_gif_path: Path, duration_ms: int = 500) -> bool:
    """
    Synthesize sequential frames into an animated GIF.
    """
    if not frame_paths:
        return False
    try:
        images = []
        for p in frame_paths:
            if p.exists():
                images.append(Image.open(p).convert("RGBA"))
        if not images:
            return False

        out_gif_path.parent.mkdir(parents=True, exist_ok=True)
        # Convert to P mode for GIF optimization
        p_images = [img.convert("RGB").convert("P", palette=Image.ADAPTIVE) for img in images]
        p_images[0].save(
            str(out_gif_path),
            save_all=True,
            append_images=p_images[1:],
            duration=duration_ms,
            loop=0,
            optimize=True,
        )
        return out_gif_path.exists() and out_gif_path.stat().st_size > 0
    except Exception as exc:
        print(f"[BrowserVerifier] Failed to create animated GIF: {exc}")
        return False


def capture_browser_or_screen(
    file_path: Optional[str] = None,
    url: Optional[str] = None,
    frames: int = 1,
    interval_seconds: float = 0.8,
    reason: str = "",
    scope: str = "main",
    session_id: str = "",
    fallback_desktop_capture: Optional[Callable[[], Optional[bytes]]] = None,
    width: int = 1280,
    height: int = 800,
) -> Dict:
    """
    Unified capture function:
    1. If file_path or url is provided, opens in headless browser and captures webpage screenshot.
    2. If frames > 1, captures sequential frames over time, checks frame diffs, and synthesizes an animated GIF!
    3. If neither is provided, falls back to full OS desktop capture.
    """
    from core.media_store import MediaStore

    ms = MediaStore()
    target_str = (url or file_path or "").strip()

    # Case A: Browser Webpage Capture
    if target_str:
        browser = find_browser_executable()
        if not browser:
            return {
                "ok": False,
                "error": "未在当前系统找到可用的无头浏览器 (Chrome / Edge)，无法进行网页渲染截图。",
            }

        nav_url, local_path = resolve_web_target(target_str, scope)
        ws_dir = workspace_for(scope)
        run_id = int(time.time() * 1000) % 1000000

        # The caller/Jev chooses sampling requirements. If frames=1 but target file is dynamic HTML, auto-upgrade frames.
        num_frames = max(1, min(int(frames), 6))
        if num_frames == 1 and local_path and is_dynamic_web_content(local_path):
            num_frames = 4
        interval_ms = int(max(0.2, min(float(interval_seconds), 5.0)) * 1000)

        w = max(320, min(int(width), 2560))
        h = max(240, min(int(height), 2160))
        window_size = f"{w},{h}"

        captured_frame_paths: List[Path] = []
        captured_pil_images: List[Image.Image] = []
        timestamps: List[float] = []
        diagnostics = {"runtime_checked": False, "runtime_errors": []}

        # For multi-frame dynamic captures, use real-time CDP capture so CSS keyframes & JS advance naturally
        if num_frames >= 1:
            cdp_frames, cdp_times = capture_cdp_sequential_frames(
                browser,
                nav_url,
                num_frames=num_frames,
                interval_sec=interval_ms / 1000.0,
                window_size=(w, h),
                diagnostics=diagnostics,
            )
            if cdp_frames:
                frames_dir = ws_dir / "frames"
                for idx, fb in enumerate(cdp_frames):
                    fpath = _frame_path(ws_dir, frames_dir, run_id, idx, num_frames)
                    fpath.write_bytes(fb)
                    captured_frame_paths.append(fpath)
                    try:
                        captured_pil_images.append(Image.open(io.BytesIO(fb)).convert("RGB"))
                        timestamps.append(cdp_times[idx] if idx < len(cdp_times) else idx * (interval_ms / 1000.0))
                    except Exception:
                        pass

        # Fallback to CLI screenshot loop if CDP was not used or returned no frames
        if not captured_frame_paths:
            for i in range(num_frames):
                budget_ms = 400 + i * interval_ms
                t_sec = round(budget_ms / 1000.0, 2)
                fpath = ws_dir / (f"preview_{run_id}.png" if num_frames == 1
                                  else _frame_path(ws_dir, ws_dir / "frames", run_id, i, num_frames))
                ok = render_webpage_screenshot(
                    nav_url,
                    str(fpath),
                    window_size=window_size,
                    virtual_time_budget_ms=budget_ms,
                    timeout_sec=20,
                )
                if ok and fpath.exists():
                    captured_frame_paths.append(fpath)
                    try:
                        captured_pil_images.append(Image.open(fpath).convert("RGB"))
                        timestamps.append(t_sec)
                    except Exception:
                        pass

        if not captured_frame_paths:
            return {
                "ok": False,
                "error": f"浏览器加载或截屏失败: {nav_url}",
            }

        # Multi-frame dynamic analysis
        diff_info = compare_frames(captured_frame_paths)
        primary_frame_path = captured_frame_paths[0]
        primary_bytes = primary_frame_path.read_bytes()

        # Register primary screenshot in MediaStore
        label = local_path.name if local_path else "webpage_screenshot"
        asset = ms.add(primary_bytes, scope=scope, origin="screenshot", label=label)
        primary_media_ref = asset["media_ref"]

        gif_path_str = None
        gif_media_ref = None
        filmstrip_bytes = None
        filmstrip_media_ref = None

        if num_frames > 1 and len(captured_frame_paths) > 1:
            # 1. Synthesize animated GIF
            gif_filename = f"dynamic_preview_{run_id}.gif"
            gif_path = ws_dir / gif_filename
            if create_animated_gif(captured_frame_paths, gif_path, duration_ms=interval_ms):
                gif_path_str = str(gif_path)
                gif_asset = ms.add(gif_path.read_bytes(), scope=scope, origin="tool", label=gif_filename)
                gif_media_ref = gif_asset["media_ref"]

            # 2. Synthesize multi-frame comparative filmstrip for VLM visual inspection
            if captured_pil_images:
                filmstrip_bytes = create_frame_filmstrip(captured_pil_images, timestamps)
                if filmstrip_bytes:
                    fs_asset = ms.add(filmstrip_bytes, scope=scope, origin="screenshot", label=f"filmstrip_{run_id}.jpg")
                    filmstrip_media_ref = fs_asset["media_ref"]

        # 逐帧 PNG 保留在 workspace/frames/：这是本轮唯一的原始证据，删掉之后
        # 既没法回看，也没法在下一轮判断"是没采到还是没看清"。

        # Synthesize multi-frame images for direct VLM visual multi-image input
        compressed_frame_bytes: List[bytes] = []
        if captured_pil_images:
            for p_img in captured_pil_images:
                buf = io.BytesIO()
                im_copy = p_img.copy()
                im_copy.thumbnail((800, 600), Image.Resampling.LANCZOS)
                im_copy.save(buf, format="JPEG", quality=75)
                compressed_frame_bytes.append(buf.getvalue())

        # Build comprehensive output message for visual model (VLM) and Jev evaluation
        is_ok = not diagnostics["runtime_errors"]
        msg_parts = [
            f"已使用浏览器打开并渲染核验: {target_str}",
            f"采样时间点: {len(captured_frame_paths)} 帧 (采样间隔 {interval_ms/1000:.1f}s)",
        ]
        if diagnostics["runtime_errors"]:
            msg_parts.append("【浏览器运行错误，必须修复后重测】" + "\n".join(diagnostics["runtime_errors"][:10]))
        elif not diagnostics["runtime_checked"]:
            msg_parts.append("浏览器运行错误检测不可用；截图成功仅证明捕获了画面，不能证明功能正常。")

        if gif_path_str:
            msg_parts.append(f"已生成动态验证动图 (供用户界面播放浏览): {Path(gif_path_str).name} (media://{gif_media_ref})")
        if compressed_frame_bytes and len(compressed_frame_bytes) > 1:
            msg_parts.append(
                f"多帧连续采样: {len(compressed_frame_bytes)} 帧图片已载入模型视觉中枢；"
                f"逐帧原始文件见 {captured_frame_paths[0].parent}")
        elif not gif_path_str:
            msg_parts.append(f"首帧截图: {primary_frame_path.name} (media://{primary_media_ref})")

        msg_parts.append(
            "【下一步核验】：截图只提供视觉证据，不代表任务完成。检查画面与原始需求，"
            "发现缺陷必须修改代码，再重跑功能测试与截图。功能测试尚未执行时立即调用工具测试，"
            "不要提前交付、罗列亮点或用‘稍后补测试’结束。全部通过后仅给一次简短结果。"
        )

        out_msg = "\n".join(msg_parts)

        # For VLM visual inspection: supply separate frame images as _images; fallback to filmstrip or primary
        final_image_bytes = filmstrip_bytes if (num_frames > 1 and filmstrip_bytes) else primary_bytes
        final_media_ref = filmstrip_media_ref if (num_frames > 1 and filmstrip_media_ref) else primary_media_ref

        result_dict = {
            "ok": is_ok,
            "runtime_checked": diagnostics["runtime_checked"],
            "runtime_errors": diagnostics["runtime_errors"][:10],
            "_image": final_image_bytes,
            "_images": compressed_frame_bytes if len(compressed_frame_bytes) > 1 else None,
            "media_ref": final_media_ref,
            "target": target_str,
            "frames_captured": len(captured_frame_paths),
            "filmstrip_generated": bool(filmstrip_bytes is not None),
            "dynamic_detected": diff_info["dynamic_detected"],
            "max_diff_ratio": diff_info.get("max_diff_ratio", 0.0),
            "output": out_msg,
        }
        if num_frames > 1:
            result_dict["frames_dir"] = str(captured_frame_paths[0].parent)
            result_dict["frame_paths"] = [str(p) for p in captured_frame_paths]
        if gif_path_str:
            result_dict["gif_path"] = gif_path_str
            result_dict["gif_media_ref"] = gif_media_ref
            result_dict["path"] = gif_path_str
        elif str(primary_frame_path).endswith(".png"):
            result_dict["screenshot_path"] = str(primary_frame_path)
            result_dict["path"] = str(primary_frame_path)

        return result_dict

    # Case B: Full OS Desktop Capture fallback
    if fallback_desktop_capture:
        image = fallback_desktop_capture()
        if image:
            media_ref = None
            try:
                asset = ms.add(image, scope=scope, origin="screenshot", label="desktop_screenshot")
                media_ref = asset.get("media_ref")
            except Exception:
                pass
            res = {
                "ok": True,
                "_image": image,
                "output": "已捕获当前桌面屏幕截图供视觉核验",
            }
            if media_ref:
                res["media_ref"] = media_ref
            return res
        return {"ok": False, "error": "桌面屏幕截取失败"}

    return {"ok": False, "error": "未提供待截屏的网页文件或桌面截图提供器"}
