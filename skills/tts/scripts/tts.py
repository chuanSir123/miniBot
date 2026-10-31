
import json
import os
import time
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/125.0.0.0 Safari/537.36")


def _tts_config():
    """服务地址来自配置（tts.base_url / tts.clone_base_url），不在源码里写死。"""
    try:
        from core.config import load_config

        cfg = load_config().get("tts") or {}
    except Exception:
        cfg = {}
    return {
        "base_url": str(cfg.get("base_url") or "").strip(),
        "clone_base_url": str(cfg.get("clone_base_url") or "").strip(),
    }


def execute(context):
    """
    TTS 语音合成技能
    - mode=design: VoiceDesign 设计声音，通过文字描述生成特定音色
    - mode=clone: 参考音色克隆，提供 reference_audio 生成同音色语音
    """
    mode = context.get("mode", "design")
    text = context.get("text", "")
    instructions = context.get("instructions", "")
    language = context.get("language", "Chinese")
    ref_audios = context.get("reference_audio", [])

    if not text or not text.strip():
        return {"ok": False, "error": "缺少 text 参数，请提供要合成的文本"}

    minibot = context.get("_minibot", {})
    workspace = minibot.get("workspace", ".")
    output_dir = os.path.join(workspace, "tts_output")
    os.makedirs(output_dir, exist_ok=True)

    tts_cfg = _tts_config()
    if mode == "design":
        url = tts_cfg.get("base_url", "")
        payload = {
            "model": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            "task_type": "VoiceDesign",
            "input": text,
            "instructions": instructions,
            "language": language,
            "response_format": "wav"
        }
    elif mode == "clone":
        url = tts_cfg.get("clone_base_url", "") or tts_cfg.get("base_url", "")

        if not ref_audios:
            return {"ok": False, "error": "clone 模式需要 reference_audio 参数（media_ref 数组）"}

        data_url_fn = minibot.get("data_url")
        if not data_url_fn:
            return {"ok": False, "error": "无法解析 reference_audio，缺少 data_url 工具"}

        ref_data_urls = []
        for ref in ref_audios:
            data_url = data_url_fn(ref)
            if not data_url:
                return {"ok": False, "error": f"无法解析 reference_audio: {ref}"}
            ref_data_urls.append(data_url)

        payload = {
            "model": "IndexTTS-2.0",
            "input": text,
            "reference_audio": ref_data_urls,
            "response_format": "wav"
        }
    else:
        return {"ok": False, "error": f"未知 mode: {mode}，支持 design 或 clone"}

    if not url:
        return {"ok": False, "error": "未配置 TTS 服务地址（config 的 tts.base_url）"}

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "audio/wav, audio/*, */*"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio_bytes = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        return {"ok": False, "error": f"API 返回 {e.code}: {err_body}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"网络错误: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"请求异常: {type(e).__name__}: {e}"}

    if not audio_bytes or len(audio_bytes) < 100:
        return {"ok": False, "error": f"返回数据异常（{len(audio_bytes)} bytes），可能不是有效音频"}

    ts = int(time.time())
    output_path = os.path.join(output_dir, f"tts_{mode}_{ts}.wav")
    with open(output_path, "wb") as f:
        f.write(audio_bytes)

    media_ref = None
    register_fn = minibot.get("register_media")
    if register_fn:
        try:
            media_ref = register_fn(output_path)
        except Exception:
            pass

    return {
        "ok": True,
        "mode": mode,
        "text": text,
        "file_path": output_path,
        "media_ref": media_ref,
        "size_bytes": len(audio_bytes)
    }

DESCRIPTION = 'TTS 语音合成，支持两种模式：design=通过文字描述设计特定音色（Qwen3 VoiceDesign），clone=提供参考音频克隆音色（IndexTTS-2.0）。当用户要求"说句话/念一段/读一下/用XX的声音/设计一个声音/克隆音色/语音合成/TTS/配音/朗读/用男声女声读/用XX的语气/把文字变成语音/模仿某人的声音"时调用本技能。即使用户没有明确说"TTS"，只要意图是把文字转成语音、用某种特定声音念、或模仿某段参考音频的音色，都应该使用本技能。'

PARAMETERS = {'type': 'object', 'properties': {'mode': {'type': 'string', 'enum': ['design', 'clone'], 'description': '合成模式：design=设计声音(VoiceDesign)，clone=参考音色克隆'}, 'text': {'type': 'string', 'description': '要合成的文本内容'}, 'instructions': {'type': 'string', 'description': "design模式专用：声音描述，如'年轻男性，声音清亮，语速自然'"}, 'language': {'type': 'string', 'description': 'design模式专用：语言，默认Chinese', 'default': 'Chinese'}, 'reference_audio': {'type': 'array', 'items': {'type': 'string'}, 'description': 'clone模式专用：参考音色的media_ref数组，按顺序传当前会话的media_ref'}}}
