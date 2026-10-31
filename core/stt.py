"""语音转写：云端 API（OpenAI 兼容 / 旧版 fileUrl）或本地 faster-whisper。

配置在 config.json 的 stt 段（含密钥，该文件不参与版本控制）。
mode=auto：填了 base_url 就走云端，失败或没填就回退本地模型。
"""
import base64
import io
import json
import time
import urllib.request
import uuid

from core.config import load_config
from core.network import urlopen

MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 2_000_000
MAX_DATA_URL_CHARS = 24_000_000

# 常见音频 MIME → 上传文件名后缀
_MIME_SUFFIX = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/amr": "amr",
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
}


def transcribe(file_url):
    """把 http(s) 或 data:audio/ URL 转写成文字，返回 {"ok": True, "text": ...}。"""
    cfg = load_config()["stt"]
    if not cfg.get("enabled"):
        raise ValueError("STT 已停用")
    if not isinstance(file_url, str) or not file_url.startswith(
        ("https://", "http://", "data:audio/")
    ):
        raise ValueError("STT fileUrl 需要 http(s) 或 data:audio/ URL")
    if len(file_url) > MAX_DATA_URL_CHARS:
        raise ValueError("音频超过大小限制")

    from core import local_stt

    mode = str(cfg.get("mode") or "auto")
    cloud_ready = _cloud_ready(cfg)
    local_ready = local_stt.is_available()
    if mode == "cloud":
        engines = ["cloud"]
    elif mode == "local":
        engines = ["local"]
    else:
        engines = []
        if cloud_ready:
            engines.append("cloud")
        if local_ready:
            engines.append("local")
    if not engines:
        raise ValueError(
            "STT 未配置：请在设置页填写 API 地址/密钥/模型，或安装本地转写模型"
        )

    from core.console import log

    errors = []
    for engine in engines:
        started = time.monotonic()
        try:
            if engine == "cloud":
                log("STT", "cloud.request", source=_source_kind(file_url))
                text = _cloud_transcribe(cfg, file_url)
            else:
                log("STT", "local.request", source=_source_kind(file_url))
                raw, _mime = _audio_bytes(file_url, cfg.get("timeout_sec", 60))
                text = local_stt.transcribe_bytes(raw, cfg["local"])
            text = (text or "").strip()
            if not text:
                raise ValueError("未返回可识别文字")
            log("STT", "response", engine=engine, characters=len(text),
                elapsed_ms=round((time.monotonic() - started) * 1000))
            return {"ok": True, "text": text[:16000], "engine": engine}
        except Exception as exc:  # 云端失败时允许 auto 模式回退本地
            errors.append(f"{engine}: {str(exc)[:300]}")
            if mode != "auto":
                break
    raise ValueError("STT 失败：" + "；".join(errors))


def _source_kind(file_url):
    return "audio data" if file_url.startswith("data:") else "audio URL"


def _cloud_ready(cfg):
    return bool(str(cfg.get("base_url") or "").strip())


def _endpoint(cfg):
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if cfg.get("format") == "file_url":
        return base
    if base.endswith("/audio/transcriptions") or base.endswith("/transcriptions"):
        return base
    if base.endswith("/v1"):
        return base + "/audio/transcriptions"
    return base + "/v1/audio/transcriptions"


def _headers(cfg):
    headers = {"User-Agent": "MiniBot-STT/1.0"}
    key = str(cfg.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _cloud_transcribe(cfg, file_url, allow_empty=False):
    endpoint = _endpoint(cfg)
    timeout = cfg.get("timeout_sec", 60)
    if cfg.get("format") == "file_url":
        payload = json.dumps({"fileUrl": file_url}).encode("utf-8")
        headers = {**_headers(cfg), "Content-Type": "application/json"}
    else:
        raw, mime = _audio_bytes(file_url, timeout)
        payload, content_type = _multipart(
            {k: v for k, v in (
                ("model", str(cfg.get("model") or "").strip()),
                ("language", str(cfg.get("language") or "").strip()),
                ("response_format", "json"),
            ) if v},
            "file",
            "audio." + _MIME_SUFFIX.get(mime, "wav"),
            raw,
            mime,
        )
        headers = {**_headers(cfg), "Content-Type": content_type}
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("转写响应过大")
    text_body = body.decode("utf-8", errors="replace").strip()
    if not text_body:
        return "" if allow_empty else ""
    try:
        result = json.loads(text_body)
    except ValueError:
        return text_body  # 有些服务直接返回纯文本
    if isinstance(result, dict) and (
        result.get("success") is False or result.get("error")
    ):
        detail = result.get("error") or result.get("message") or "未知错误"
        raise ValueError("服务返回失败：" + str(detail)[:300])
    text = _text(result)
    if not text and not allow_empty:
        raise ValueError("服务未返回文字")
    return text


def _audio_bytes(file_url, timeout=60):
    mime = "audio/wav"
    if file_url.startswith("data:"):
        header, _, payload = file_url.partition(",")
        declared = header[5:].split(";")[0].strip().lower()
        if declared:
            mime = declared
        try:
            data = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise ValueError("base64 音频解码失败") from exc
    else:
        request = urllib.request.Request(
            file_url, headers={"User-Agent": "MiniBot-STT/1.0"}
        )
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_AUDIO_BYTES + 1)
            mime = (response.headers.get("Content-Type") or mime).split(";")[0].strip()
    if not data:
        raise ValueError("音频内容为空")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError("音频超过 16MB 限制")
    return data, mime or "audio/wav"


def _multipart(fields, file_field, filename, content, mime):
    boundary = "----MiniBotSTT" + uuid.uuid4().hex
    buffer = io.BytesIO()
    for name, value in fields.items():
        buffer.write(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
                "utf-8"
            )
        )
    buffer.write(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
        ).encode("utf-8")
    )
    buffer.write(content)
    buffer.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return buffer.getvalue(), "multipart/form-data; boundary=" + boundary


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "transcript", "transcription", "recognized_text", "output_text"):
            if isinstance(value.get(key), str):
                return value[key]
        for key in ("data", "result", "segments", "results", "output"):
            if key in value:
                text = _text(value[key])
                if text:
                    return text
    if isinstance(value, list):
        return " ".join(filter(None, (_text(item) for item in value)))
    return ""


def silence_wav(seconds=1.0, rate=16000):
    """生成一段静音 WAV，仅用于连通性自检。"""
    import struct

    frames = int(rate * seconds)
    data = b"\x00\x00" * frames
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    return header + data


def test_connection(values=None):
    """用一段静音音频探测当前配置能否跑通，返回引擎与耗时。"""
    cfg = dict(load_config()["stt"])
    for key, value in (values or {}).items():
        if key == "local" and isinstance(value, dict):
            cfg["local"] = {**(cfg.get("local") or {}), **value}
        elif value not in (None, ""):
            cfg[key] = value
    data_url = "data:audio/wav;base64," + base64.b64encode(silence_wav()).decode("ascii")
    started = time.monotonic()
    engine = "cloud" if _cloud_ready(cfg) and cfg.get("mode") != "local" else "local"
    if engine == "cloud":
        text = _cloud_transcribe(cfg, data_url, allow_empty=True)
    else:
        from core import local_stt

        if not local_stt.is_available():
            raise ValueError("本地 STT 未安装，请先安装依赖并下载模型")
        raw, _mime = _audio_bytes(data_url, cfg.get("timeout_sec", 60))
        text = local_stt.transcribe_bytes(raw, cfg["local"])
    return {
        "engine": engine,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "text": (text or "").strip()[:200],
        "silent_ok": True,
    }
