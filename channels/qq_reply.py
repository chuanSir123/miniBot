"""Turn explicit assistant media attachments into OneBot message segments."""
import base64
import re
import urllib.parse
import urllib.request
from pathlib import Path

from channels.qq_media import _check_url
from core.config import load_config

MEDIA_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
    "record": {".mp3", ".wav", ".ogg", ".m4a", ".silk", ".amr", ".flac", ".aac", ".aiff", ".opus"},
    "video": {".mp4", ".webm", ".mov", ".mkv", ".avi", ".flv"},
}
LINK = re.compile(r'!?\[([^\]\r\n]*)\]\((<[^>\r\n]+>|[^)\r\n]+)\)')
# media_ref 前缀直接标明类型：img_ / vid_ / aud_。
REF_KINDS = {"img": "image", "vid": "video", "aud": "record"}

def _resolve_video_cover(video_path: Path):
    """查找或从视频第1帧提取封面图文件。"""
    try:
        if not video_path or not video_path.is_file():
            return None
        # 1. 查找已生成的同名封面图
        for ext in (".cover.jpg", ".jpg", ".png"):
            cand = video_path.with_name(video_path.stem + ext)
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
            cand2 = video_path.with_suffix(ext)
            if cand2.is_file() and cand2.stat().st_size > 0:
                return cand2
        # 2. 调用 ffmpeg 快速提取第 1 帧存为封面
        import subprocess
        from skills.svg_to_mp4 import _find_tool, FFMPEG_CANDIDATES
        ffmpeg = _find_tool(FFMPEG_CANDIDATES)
        if ffmpeg:
            target = video_path.with_suffix(".cover.jpg")
            subprocess.run([ffmpeg, "-y", "-ss", "00:00:00.000", "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(target)],
                           capture_output=True, timeout=8)
            if target.is_file() and target.stat().st_size > 0:
                return target
    except Exception:
        pass
    return None



def _media_ref_segment(source, event):
    """按 media_ref 自身的 mime 决定段类型，不依赖模型写的标签。"""
    from core.media_store import MediaStore
    from core.decision_context import qq_scope
    store = MediaStore()
    scope = qq_scope(event)
    asset, raw = store.get(source[8:], scope)
    kind = REF_KINDS.get(asset["media_ref"].split("_", 1)[0], "image")
    local = urllib.parse.urlsplit(load_config()["channels"]["napcat"]["ws_url"]).hostname in ("127.0.0.1", "localhost", "::1")
    v_path = store.path(asset["media_ref"], scope)
    if local:
        file = v_path.as_uri()
    else:
        # 远程 NapCat 只能收 base64 或可访问 URL，大文件内嵌传输不现实。
        if len(raw) > 20_000_000:
            raise ValueError("远程NapCat无法接收超过20MB的内嵌媒体，请改用可访问的媒体URL")
        file = "base64://" + base64.b64encode(raw).decode("ascii")

    segment_data = {"file": file}
    if kind == "video":
        cover_path = _resolve_video_cover(v_path)
        if cover_path and cover_path.is_file():
            segment_data["cover"] = cover_path.as_uri() if local else ("base64://" + base64.b64encode(cover_path.read_bytes()).decode("ascii"))
    return {"type": kind, "data": segment_data, "_media": asset}


def media_segment(kind, source, event):
    kind = {"audio": "record", "voice": "record"}.get(kind, kind)
    if not isinstance(source, str):
        raise ValueError("不支持的媒体类型或来源")
    source = source.strip().strip("<>")
    if source.startswith("media://"):
        return _media_ref_segment(source, event)
    if kind not in MEDIA_EXTENSIONS:
        raise ValueError("不支持的媒体类型或来源")
    if source.startswith(("https://", "http://")):
        _check_url(source)
    elif source.startswith(("data:", "base64://")):
        if source.startswith("data:"):
            header, sep, encoded = source.partition(",")
            mime_kind = "audio" if kind == "record" else kind
            if not sep or not header.startswith("data:" + mime_kind + "/") or ";base64" not in header:
                raise ValueError("媒体 data URL 类型不匹配")
        else:
            encoded = source[9:]
        if len(encoded) > 28_000_000:
            raise ValueError("内嵌媒体超过大小限制，请使用文件或URL")
        data = base64.b64decode(encoded, validate=True)
        if not data:
            raise ValueError("媒体内容为空")
        source = "base64://" + encoded
    else:
        cfg = load_config()["channels"]["napcat"]
        # A local path is a file disclosure, so retain the owner's local-tools gate.
        from core.qq_authority import tool_access
        from core.decision_context import qq_scope
        actor_scope = qq_scope(event) + (":user:" + str(event.get("user_id", "")) if event.get("message_type") == "group" else "")
        if event.get("message_type") not in ("private", "group") or not (tool_access(actor_scope, "read_file") or tool_access(actor_scope, "register_media")):
            raise ValueError("当前会话无权发送本机媒体文件")
        if urllib.parse.urlsplit(cfg["ws_url"]).hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("远程NapCat无法读取本机路径，请使用媒体URL")
        if source.startswith("file:"):
            parsed = urllib.parse.urlsplit(source)
            if parsed.netloc not in ("", "localhost"):
                raise ValueError("不支持网络共享文件")
            source = urllib.request.url2pathname(parsed.path)
        if source.startswith(("\\\\", "//")):
            raise ValueError("不支持网络共享文件")
        path = Path(source).expanduser()
        if not path.is_absolute():
            raise ValueError("媒体文件必须使用绝对路径")
        path = path.resolve(strict=True)
        if path.suffix.lower() not in MEDIA_EXTENSIONS[kind] or not path.is_file():
            raise ValueError("媒体文件类型不匹配")
        if not 0 < path.stat().st_size <= 100_000_000:
            raise ValueError("媒体文件为空或超过100MB限制")
        source = path.as_uri()
        if kind == "image":
            from core.media_store import MediaStore
            from core.decision_context import qq_scope
            asset = MediaStore().add(path.read_bytes(), qq_scope(event), origin="sent", label=path.name)
            return {"type": kind, "data": {"file": source}, "_media": asset}
    return {"type": kind, "data": {"file": source}}


def reply_parts(reply, event):
    """Return text/media parts in order; ordinary file links remain text."""
    if isinstance(reply, dict):
        result = []
        if reply.get("text"):
            result.append({"type": "text", "data": {"text": str(reply["text"])}})
        attachments = reply.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 9:
            raise ValueError("回复附件必须是数组，最多9个")
        for item in attachments:
            result.append(media_segment(item["type"], item["file"], event))
        return result
    if not isinstance(reply, str):
        raise ValueError("回复必须是文本或带attachments的对象")
    result = []
    cursor = 0
    count = 0
    for match in LINK.finditer(reply):
        if reply[:match.start()].count("```") % 2:
            continue
        source = match.group(2).strip().strip("<>")
        suffix = Path(urllib.parse.unquote(urllib.parse.urlsplit(source).path)).suffix.lower()
        kind = next((k for k, extensions in MEDIA_EXTENSIONS.items() if suffix in extensions), None)
        if not kind:
            kind = {"图片": "image", "语音": "record", "音频": "record", "视频": "video"}.get(match.group(1))
        if not kind and match.group(0).startswith("!["):
            kind = "image"
        if source.startswith("data:"):
            prefix = source[5:].split("/", 1)[0]
            kind = {"image": "image", "audio": "record", "video": "video"}.get(prefix)
        if not kind and source.startswith("base64://"):
            kind = {"图片": "image", "语音": "record", "音频": "record", "视频": "video"}.get(match.group(1))
        if not kind or count >= 9:
            continue
        if match.start() > cursor:
            result.append({"type": "text", "data": {"text": reply[cursor:match.start()]}})
        try:
            result.append(media_segment(kind, source, event))
        except (ValueError, OSError) as exc:
            result.append({"type": "text", "data": {"text": f"[附件发送失败：{exc}]"}})
        count += 1
        cursor = match.end()
    if cursor < len(reply):
        result.append({"type": "text", "data": {"text": reply[cursor:]}})
    return result
