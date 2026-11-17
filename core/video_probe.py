"""视频理解的前置处理：把一段视频拆成「带时间戳的关键帧 + 音轨」。

Gemini 3.8 Flash 本身支持视频输入，但本项目走的是 OpenAI 兼容网关，
content part 只认 text / image_url（实测 video_url、inline_data、input_video
都会被 400 拒掉），所以收到的视频统一在本地预处理：
画面抽成若干关键帧走既有的图片通道，声音走既有的 STT 通道。
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_FRAMES = 10
DEFAULT_WIDTH = 512
MAX_AUDIO_SEC = 120

# 音轨必须压成有损小体积再送 STT。云端 STT 在 Cloudflare 隧道后面，
# 请求体吞吐只有百来 KB/s：120 秒 PCM wav 光上传带 body 就要 45~60 秒，
# 直接撞满 stt.timeout_sec；同样的内容压成 opus 只剩 1/17（约 220KB），
# 端到端从 48 秒降到 11 秒。
AUDIO_TARGETS = (
    ("libopus", "ogg", "audio/ogg", ["-b:a", "16k"]),
    ("libmp3lame", "mp3", "audio/mpeg", ["-b:a", "32k"]),
)


def _candidates(name):
    """按「环境变量 > PATH > 项目内置 > 常见安装目录」列出 ffmpeg/ffprobe 候选。"""
    exe = name + (".exe" if os.name == "nt" else "")
    root = Path(__file__).resolve().parents[1]
    found = [
        os.environ.get("MINIBOT_" + name.upper(), ""),
        os.environ.get(name.upper() + "_BINARY", ""),
        shutil.which(name) or "",
        str(root / "tools" / "bin" / exe),
        str(root / "tools" / "ffmpeg" / "bin" / exe),
        str(root / "tools" / exe),
    ]
    bases = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
    ]
    if os.name == "nt":
        # 精简环境里 PROGRAMFILES 可能没设，按系统盘补回标准目录。
        drive = os.path.splitdrive(os.environ.get("WINDIR") or "C:\\Windows")[0] or "C:"
        drive = drive + os.sep
        bases += [
            os.path.join(drive, "Program Files"),
            os.path.join(drive, "Program Files (x86)"),
            drive,
        ]
    for base in bases:
        if base:
            found += [
                os.path.join(base, "ffmpeg", "bin", exe),
                os.path.join(base, "ffmpeg", exe),
            ]
    return [item for item in found if item]


def find_tool(name):
    for candidate in _candidates(name):
        if os.path.isfile(candidate) or shutil.which(candidate):
            return candidate
    return None


def available():
    return find_tool("ffmpeg") is not None


def _run(args, timeout):
    try:
        return subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def duration(path):
    """读取时长（秒）；读不到返回 0。"""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return 0.0
    result = _run(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ],
        15,
    )
    if result is None:
        return 0.0
    try:
        return max(0.0, float((result.stdout or b"").decode("utf-8", "replace").strip()))
    except ValueError:
        return 0.0


def timestamps(total, count):
    """把 [0, total) 均匀切成 count 个采样点，取每段中点，避开首尾黑帧。"""
    if count <= 0:
        return []
    if total <= 0:
        # 时长未知时按每秒一帧兜底，交付物仍然是「有画面」而不是空。
        return [float(index) for index in range(count)]
    step = total / count
    marks = []
    for index in range(count):
        second = round(min(step * (index + 0.5), max(0.0, total - 0.05)), 2)
        if not marks or second > marks[-1]:
            marks.append(second)
    return marks


def keyframes(path, count=DEFAULT_FRAMES, width=DEFAULT_WIDTH, timeout=15):
    """按时间顺序抽 count 帧，返回 [(秒, JPEG 字节)]。"""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg or count <= 0:
        return []
    marks = timestamps(duration(path), count)
    frames = []
    with tempfile.TemporaryDirectory(prefix="minibot-frame-") as workspace:
        for index, second in enumerate(marks):
            target = Path(workspace) / f"{index:02d}.jpg"
            _run(
                [
                    ffmpeg, "-v", "error",
                    "-ss", f"{second:.2f}",
                    "-i", str(path),
                    "-frames:v", "1",
                    "-vf", f"scale={width}:-2",
                    "-q:v", "4",
                    "-y", str(target),
                ],
                timeout,
            )
            if target.is_file():
                data = target.read_bytes()
                if data:
                    frames.append((second, data))
    return frames


def audio_track(path, max_seconds=MAX_AUDIO_SEC, timeout=25):
    """抽一条 16k 单声道音轨，返回 (字节, mime)；没有音轨返回 (None, "")。

    默认 opus（体积最小、STT 直接能收）；ffmpeg 没编进 libopus 时退到 32k mp3。
    """
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return None, ""
    with tempfile.TemporaryDirectory(prefix="minibot-audio-") as workspace:
        for codec, suffix, mime, tune in AUDIO_TARGETS:
            target = Path(workspace) / f"audio.{suffix}"
            _run(
                [
                    ffmpeg, "-v", "error",
                    "-i", str(path),
                    "-vn", "-ac", "1", "-ar", "16000",
                    "-t", str(int(max_seconds)),
                    "-c:a", codec, *tune,
                    "-f", suffix,
                    "-y", str(target),
                ],
                timeout,
            )
            if target.is_file() and target.stat().st_size > 1024:
                return target.read_bytes(), mime
    return None, ""


def analyze(path, frames=DEFAULT_FRAMES, width=DEFAULT_WIDTH, max_audio_sec=MAX_AUDIO_SEC):
    """一次性拿到时长、关键帧和音轨。缺 ffmpeg 时只返回时长（若 ffprobe 在）。"""
    path = Path(path)
    result = {
        "duration": 0.0, "frames": [], "audio": None, "audio_mime": "", "error": "",
    }
    if not path.is_file():
        result["error"] = "视频文件不存在"
        return result
    result["duration"] = duration(path)
    if not available():
        result["error"] = "未找到 ffmpeg，无法解析视频画面"
        return result
    result["frames"] = keyframes(path, frames, width)
    result["audio"], result["audio_mime"] = audio_track(path, max_audio_sec)
    return result


def clock(second):
    """把秒数写成 分:秒，给模型当时间锚点。"""
    second = max(0.0, float(second or 0))
    minutes, rest = divmod(second, 60)
    return f"{int(minutes):02d}:{rest:04.1f}"


def describe(path, frames=DEFAULT_FRAMES, width=DEFAULT_WIDTH):
    """调试用：返回不含二进制的分析摘要。"""
    data = analyze(path, frames, width)
    return {
        "duration": round(data["duration"], 2),
        "frames": [[clock(sec), len(jpeg)] for sec, jpeg in data["frames"]],
        "audio_bytes": len(data["audio"] or b""),
        "audio_mime": data.get("audio_mime", ""),
        "error": data["error"],
    }


if __name__ == "__main__":
    import sys

    print(json.dumps(describe(sys.argv[1]), ensure_ascii=False, indent=2))
