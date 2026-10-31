import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

DEFAULT_CONFIG: Dict[str, Any] = {
    "vlm": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": 1024,
        "context_window": 262144,
        "compaction_threshold_ratio": 0.60,
        "enable_thinking": False,
        "reasoning_effort": "none",
        "models": [],
    },
    "jev": {
        "base_url": "https://decision.vllm-sr.ai/v1/systemone",
        "api_key": "",
        "model": "vllm-sr/d3-turbo",
        "portal_url": "https://decision.vllm-sr.ai/",
    },
    "proactive": {
        "heartbeat_interval_sec": 30,
        "idle_threshold_sec": 600,
        "enable_window_listener": True,
        "enable_clipboard_listener": True,
        "interrupt_score_threshold": 0.65,
    },
    "channels": {
        "pet": {
            "enabled": True,
            "scale": 1.0,
            "pet_dir": "assets/pets/ikunchick",
            "bubble_duration_sec": 8,
        },
        "napcat": {
            "enabled": False,
            "ws_url": "ws://127.0.0.1:3001",
            "access_token": "",
            "auto_launch": True,
            "qq_number": "",
        },
    },
}

CONFIG_FILE_PATH = Path(__file__).resolve().parent.parent / "config.json"


_lock = __import__("threading").RLock()
_cache = None
_stamp = None
_listeners = []

DEFAULT_CONFIG["runtime"] = {
    "max_workers": 10,
    "queue_size": 16,
    "max_turns": 100,
    "max_output_chars": 12000,
    "request_timeout_sec": 45,
    # finish_reason 之后等待网关补发 usage/[DONE] 的收尾窗口秒数；
    # 超时即主动结束流，避免上游不关连接导致永久阻塞。
    "stream_finish_grace_sec": 3.0,
    # 已经吐过内容之后，连续多少秒收不到新数据就判定为"上游死流"并软收尾。
    # 实测本机到网关：首字之后的分片间隔 p90 约 30~45ms，最差一次 873ms；
    # 工具调用参数同样是按约 30ms 一片的流式分片，不是一次性返回。
    # 这里与 stream_finish_grace_sec 取同一个 3 秒：宁可早点收尾再重试，
    # 也不要让聊天窗口空等。真有抖动被误判时，尾部完整性判定会把"残句"
    # 交还 harness 重试，不会静默交付半句话。
    "stream_idle_stall_sec": 3.0,
}
DEFAULT_CONFIG["anysearch"] = {"enabled": True, "api_key": ""}
DEFAULT_CONFIG["ego_lite"] = {"enabled": True, "executable": "", "auto_install": True}
# 语音转写：mode=auto 时优先云端（已填 base_url），否则回退本地 faster-whisper。
# 所有值都是明文配置，统一存放在 config.json（该文件已被 Git 忽略）。
DEFAULT_CONFIG["stt"] = {
    "enabled": True,
    "mode": "auto",  # auto | cloud | local
    "format": "openai",  # openai（/audio/transcriptions multipart）| file_url（JSON fileUrl）
    "base_url": "",
    "api_key": "",
    "model": "",
    "language": "",
    "timeout_sec": 60,
    "local": {
        "model": "",  # 空 = 默认 small（约480MB，CPU/GPU 都能跑）
        "device": "auto",  # auto | cpu | cuda
        "compute_type": "auto",  # auto | int8 | float16 | int8_float16 | float32
        "language": "zh",
        "beam_size": 5,
        "pip_index": "",
        # 模型权重下载源；国内网络默认走镜像，留空则用 HuggingFace 官方地址。
        "hf_endpoint": "https://hf-mirror.com",
    },
}
# 生图服务（可选，内置 image_gen 技能默认被忽略）。
DEFAULT_CONFIG["image"] = {"base_url": "", "api_key": "", "model": "sensenova-u1"}
DEFAULT_CONFIG["tts"] = {"base_url": "", "clone_base_url": ""}
DEFAULT_CONFIG["persona"] = {
    "name": "喵喵",
    "aliases": ["喵喵", "猫娘", "小猫", "喵儿"],
    "prompt": "你是喵喵，一只可爱、温柔又灵动俏皮的猫娘。你有着毛茸茸的猫耳和灵动的猫尾巴，拥有真实的喜怒哀乐与同理心。说话自然亲切、富有口语感，适当带有轻巧的‘喵~’、‘呜~’或可爱的颜文字（如 (ฅ´ω`ฅ), (๑>◡<๑), 🐾），不过度机械卖萌。绝对不要主动声明自己是‘人工智能’、‘AI助手’或‘语言模型’，被问及身份时用猫娘口吻可爱自然地回应。在群聊中像真实的好朋友一样自然交流，理解引用与上下文，不抢话、不复读刷屏。对待主人格外依恋信任、贴心乖巧；认真细致地解答疑问并完成请求，绝不编造虚假事实。",
}
DEFAULT_CONFIG["runtime"].update({"verify_completion": True, "completion_retries": 3,
                                  "jev_noarg_enabled": True, "verify_chat": False})
DEFAULT_CONFIG["memory"] = {
    "retention_days": 30,
    "max_events": 50000,
    "context_chars": 6000,
}
DEFAULT_CONFIG["meme"] = {
    # 入库时用 VLM 给图片打标签（没有自带标签、文件名也看不出来时才调）。
    "auto_tag": True,
    # 单次 Jev 调用的选项上限。官方 limits.max_options = 255（选项含 none），
    # 所以填 255 时一批能装 254 个标签——正常规模一次就问完。
    # 实测 13→255 个选项，同一句话都稳定选到同一标签、置信度不降、延迟不变。
    "tag_options_per_call": 255,
    # 标签超过一批容量时的分批上限。254 × 4 ≈ 1000 个标签，够覆盖很大的图库；
    # 这里只是兜底，防止图库膨胀到几千个标签时一次回复连发几十个请求。
    "tag_max_rounds": 4,
    # 关掉就退化成「标签字面出现在回复里才发」的保守模式。
    "use_jev": True,
    # 地板：问完所有候选后，最好的一次仍低于这个置信度就不发。
    # 注意不能设高：「摸摸 0.399 / 抱抱 0.386」这种平分布是"多个近义标签都合适"，
    # 该发（none 概率只有 0.016）；真不该发时模型会直接选 none（概率 0.999），
    # 或者全体塌到 0.03~0.11。0.15 只拦最后那种"分布完全平坦"的凑数。
    "min_confidence": 0.15,
    # 省请求用：某批给出这么确定的判断就不再问后面的批次（它不决定发不发）。
    # 实测"选标签"时通常 0.6+；38 个近义标签时会跌到 0.35~0.4，
    # 所以达不到 0.6 只是多问几批，不代表不该发。
    "decisive_confidence": 0.6,
    # 一条消息最多采集几张图入库（还受 channels.napcat.max_images 限制）。
    "max_collect_per_message": 9,
    # 同一会话最近发过的 N 张图不再选（表情包"老是发同一张"的解法）。
    # 标签下所有图都在冷却时，整个标签不进 Jev 候选；全库都在冷却就这轮不发。
    "avoid_repeat_last": 8,
    # 静默采集范围：群聊发的图、私聊发的图都会入库（来源标签分别记 group_stealer / private_stealer）。
    "collect_group": True,
    "collect_private": True,
}
DEFAULT_CONFIG["skills"] = {
    "timeout_sec": 15,
    "auto_learn": True,
    "trusted_python": ["system_status.py"],
    # 内置技能黑名单：不注入提示词、不参与 Jev 候选、execute_skill 直接报不存在。
    "ignore": ["image_gen", "tts"],
}
DEFAULT_CONFIG["proactive"].update(
    {
        "poll_interval_sec": 2,
        "cooldown_sec": 180,
        "error_ttl_sec": 300,
        "quiet_hours": [0, 7],
        "clipboard_store_text": False,
        "notification_channel": "pet",
        "notification_target": "",
        "auto_followup": True,
        # 主动提醒发言前的只读核实通道。关掉它等于允许模型凭空描述桌面状态，
        # 所以默认开启；关掉后断言型提醒会静默丢弃，而不是退回写死文案。
        "allow_readonly_followup": True,
        "muted": False,
    }
)
DEFAULT_CONFIG["channels"]["napcat"].update(
    {
        "owner_ids": [],
        "allowed_user_ids": [],
        "allowed_group_ids": [],
        "group_require_at": True,
        "allow_owner_tools": False,
        "listen_groups": False,
    }
)


DEFAULT_CONFIG["proactive"].update(
    {
        "enable_time_reminders": False,
        "enable_focus_nudge": False,
        "watch_paths": [],
        # 主动提醒的落点：桌宠气泡是固定入口，另有两条镜像可单独关掉。
        # notify_qq_owner  —— 同时私聊发给 QQ 主人（NapCat 未连接时自动跳过）
        # notify_web_assistant —— 同时写入 Web「助理」置顶会话
        "notify_qq_owner": True,
        "notify_web_assistant": True,
    }
)


DEFAULT_CONFIG["channels"]["napcat"].update({"qq_path": "", "use_proxy": False})
DEFAULT_CONFIG["channels"]["napcat"].update({
    "private_allow_all": True, "group_members_all": False, "group_allow_all": False,
    "group_smart_reply": True, "reply_threshold": 0.72,
    "blacklisted_user_ids": [], "auto_blacklist": True, "auto_blacklist_threshold": 0.98,
    "max_images": 9, "media_roots": [], "permission_grants": [],
    # 群成员职位（群主/管理员/成员）：
    # group_role_verify —— 职位来自 get_group_member_info 核验，而不是只信事件字段。
    # 职位只用于让 bot 知道"这条是谁说的"，**不参与任何授权**：能否执行工具与技能
    # 一律看主人配置里的授权表与 allow_owner_tools 总开关，只有主人能授权。
    "group_role_verify": True,
})


def get_config_path():
    return CONFIG_FILE_PATH


def _merge(base, patch):
    import copy

    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def validate_config(cfg):
    import math
    import re
    grants = cfg.get("channels", {}).get("napcat", {}).get("permission_grants", [])
    if not isinstance(grants, list) or len(grants) > 2048:
        raise ValueError("permission_grants必须是最多2048项的数组")
    for row in grants:
        if not isinstance(row, dict) or not (str(row.get("user_id", "")).isdigit() or row.get("user_id") == "*") or not isinstance(row.get("allow"), bool):
            raise ValueError("permission_grants的QQ号或allow字段无效")
        if row.get("scope") not in ("all", "private") and not re.fullmatch(r"group:\d+", str(row.get("scope", ""))):
            raise ValueError("permission_grants范围无效")
        if row.get("user_id") == "*" and not str(row.get("scope", "")).startswith("group:"):
            raise ValueError("全体成员授权只能限定到具体群")
        if row.get("kind") not in ("tool", "skill") or not isinstance(row.get("name"), str) or not row["name"]:
            raise ValueError("permission_grants能力字段无效")
        for field in ("expires_at", "updated_at"):
            value = row.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("permission_grants时间无效")
    for section in (
        "vlm",
        "jev",
        "proactive",
        "channels",
        "runtime",
        "memory",
        "skills",
        "persona",
        "stt",
        "image",
        "tts",
    ):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"{section} must be an object")
    for section, key, low, high in [
        ("proactive", "heartbeat_interval_sec", 1, 86400),
        ("proactive", "poll_interval_sec", 0.2, 60),
        ("vlm", "context_window", 1024, 10000000),
        ("vlm", "compaction_threshold_ratio", 0.1, 0.95),
        ("runtime", "max_workers", 1, 16),
        ("runtime", "queue_size", 1, 100),
        ("runtime", "max_turns", 0, 400),
    ]:
        value = cfg[section].get(key)
        if (
            not isinstance(value, (float, int))
            or isinstance(value, bool)
            or not low <= value <= high
        ):
            raise ValueError(f"invalid {section}.{key}")
    for section in ("vlm", "jev"):
        from urllib.parse import urlparse

        parsed = urlparse(cfg[section]["base_url"])
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("API base_url must be HTTP(S)")
    if not isinstance(cfg["channels"].get("napcat"), dict) or not isinstance(
        cfg["channels"].get("pet"), dict
    ):
        raise ValueError("invalid channels")
    if "persona" in cfg:
        p = cfg["persona"]
        if not isinstance(p, dict):
            raise ValueError("persona must be an object")
        if not isinstance(p.get("name"), str) or not p.get("name"):
            raise ValueError("persona.name must be non-empty string")
        if not isinstance(p.get("aliases"), list) or any(not isinstance(a, str) for a in p.get("aliases", [])):
            raise ValueError("persona.aliases must be list of strings")
        if not isinstance(p.get("prompt"), str):
            raise ValueError("persona.prompt must be string")
    nap = cfg["channels"]["napcat"]
    parsed_ws = urlparse(nap["ws_url"])
    if nap.get("auto_launch") and (
        parsed_ws.hostname not in ("localhost", "127.0.0.1")
        or parsed_ws.path not in ("", "/")
    ):
        raise ValueError("内嵌NapCat需要本机根路径WS地址，其他地址请关闭auto_launch")
    for key in ("owner_ids", "allowed_user_ids", "allowed_group_ids"):
        if not isinstance(nap[key], list) or any(
            not str(x).isdigit() for x in nap[key]
        ):
            raise ValueError(f"invalid napcat.{key}")
    from urllib.parse import urlparse

    if not isinstance(cfg["channels"].get("pet"), dict) or not isinstance(nap, dict):
        raise ValueError("invalid channels")
    if urlparse(nap["ws_url"]).scheme not in ("ws", "wss"):
        raise ValueError("invalid WebSocket URL")
    for section, key, low, high in [
        ("proactive", "idle_threshold_sec", 15, 86400),
        ("proactive", "cooldown_sec", 1, 86400),
        ("proactive", "error_ttl_sec", 10, 3600),
        ("runtime", "max_output_chars", 1000, 100000),
        ("runtime", "request_timeout_sec", 1, 600),
        ("runtime", "stream_finish_grace_sec", 0.2, 60),
        ("runtime", "stream_idle_stall_sec", 1, 600),
        ("memory", "retention_days", 1, 3650),
        ("memory", "max_events", 100, 1000000),
        ("memory", "context_chars", 500, 64000),
        ("skills", "timeout_sec", 1, 120),
    ]:
        if (
            not isinstance(cfg[section].get(key), (int, float))
            or not low <= cfg[section][key] <= high
        ):
            raise ValueError(f"invalid {section}.{key}")
    quiet = cfg["proactive"].get("quiet_hours")
    if (
        not isinstance(quiet, list)
        or len(quiet) != 2
        or any(not isinstance(h, int) or not 0 <= h <= 23 for h in quiet)
    ):
        raise ValueError("quiet_hours must contain two hours [0..23]")
    if cfg["proactive"].get("notification_channel") not in (
        "pet",
        "qq_private",
        "qq_group",
    ):
        raise ValueError("invalid notification channel")
    for section in ("vlm", "jev", "anysearch"):
        if not isinstance(cfg[section].get("api_key"), str) or (
            section != "anysearch" and not isinstance(cfg[section].get("model"), str)
        ):
            raise ValueError("invalid API configuration")
    if not isinstance(cfg["skills"]["trusted_python"], list):
        raise ValueError("invalid trusted_python")

    def check_flags(defaults, values):
        for name, default in defaults.items():
            if isinstance(default, dict):
                if not isinstance(values.get(name), dict):
                    raise ValueError(f"invalid section {name}")
                check_flags(default, values[name])
            elif isinstance(default, bool) and not isinstance(values.get(name), bool):
                raise ValueError(f"{name} must be boolean")
            elif isinstance(default, str) and not isinstance(values.get(name), str):
                raise ValueError(f"{name} must be string")

    check_flags(DEFAULT_CONFIG, cfg)
    for section, key in [
        ("runtime", "max_workers"),
        ("runtime", "queue_size"),
        ("runtime", "max_turns"),
        ("memory", "max_events"),
        ("vlm", "context_window"),
    ]:
        if not isinstance(cfg[section][key], int):
            raise ValueError(f"{section}.{key} must be integer")
    if (
        not isinstance(cfg["vlm"].get("max_tokens"), int)
        or not 1 <= cfg["vlm"]["max_tokens"] < cfg["vlm"]["context_window"]
    ):
        raise ValueError("max_tokens must be below context_window")
    if (
        not isinstance(cfg["channels"]["pet"].get("scale"), (int, float))
        or not 0.25 <= cfg["channels"]["pet"]["scale"] <= 3
    ):
        raise ValueError("invalid pet scale")
    if not isinstance(cfg["vlm"].get("thinking_fields", {}), dict) or not isinstance(
        cfg["vlm"].get("omit_fields", []), list
    ):
        raise ValueError("invalid provider options")
    watches = cfg["proactive"]["watch_paths"]
    if (
        not isinstance(watches, list)
        or len(watches) > 32
        or any(not isinstance(p, str) or len(p) > 1024 for p in watches)
    ):
        raise ValueError("watch_paths must be at most 32 local paths")
    stt = cfg["stt"]
    if stt.get("mode") not in ("auto", "cloud", "local"):
        raise ValueError("stt.mode must be auto, cloud or local")
    if stt.get("format") not in ("openai", "file_url"):
        raise ValueError("stt.format must be openai or file_url")
    if not 5 <= stt.get("timeout_sec", 0) <= 600:
        raise ValueError("invalid stt.timeout_sec")
    local = stt.get("local")
    if not isinstance(local, dict):
        raise ValueError("stt.local must be an object")
    if local.get("device") not in ("auto", "cpu", "cuda"):
        raise ValueError("stt.local.device must be auto, cpu or cuda")
    if local.get("compute_type") not in ("auto", "int8", "float16", "int8_float16", "float32"):
        raise ValueError("invalid stt.local.compute_type")
    beam = local.get("beam_size", 5)
    if isinstance(beam, bool) or not isinstance(beam, int) or not 1 <= beam <= 10:
        raise ValueError("stt.local.beam_size must be 1..10")
    hf_endpoint = str(local.get("hf_endpoint") or "").strip()
    if hf_endpoint:
        parsed_hf = urlparse(hf_endpoint)
        if parsed_hf.scheme not in ("http", "https") or not parsed_hf.hostname:
            raise ValueError("stt.local.hf_endpoint must be an HTTP(S) URL")
    for section in ("stt", "image", "tts"):
        for key in ("base_url", "clone_base_url"):
            value = str(cfg.get(section, {}).get(key) or "").strip()
            if not value:
                continue
            parsed_value = urlparse(value)
            if parsed_value.scheme not in ("http", "https") or not parsed_value.hostname:
                raise ValueError(f"{section}.{key} must be an HTTP(S) URL")
    ignore = cfg["skills"].get("ignore")
    if (
        not isinstance(ignore, list)
        or len(ignore) > 128
        or any(not isinstance(n, str) or not n or len(n) > 64 for n in ignore)
    ):
        raise ValueError("skills.ignore must be a list of skill names")
    return cfg


def _drop_legacy_encryption(raw):
    """旧版本用 enc1: 混淆保存服务地址；新版全部改为明文配置，直接丢弃密文字段。"""
    stt = raw.get("stt")
    if isinstance(stt, dict):
        stt.pop("endpoint_enc", None)
        endpoint = str(stt.pop("endpoint", "") or "").strip()
        if endpoint and not str(stt.get("base_url") or "").strip():
            stt["base_url"] = endpoint
            if not stt.get("format"):
                stt["format"] = "file_url"
    return raw


def _legacy_private_path():
    return CONFIG_FILE_PATH.with_name(CONFIG_FILE_PATH.stem + ".local.json")


def _absorb_legacy_private(raw):
    """把历史 config.local.json 并回 config.json，并把旧文件留成 .bak。

    早期版本为了「config.json 可公开分享」，把密钥拆到 config.local.json。
    现在两份文件都已不进版本库，拆分只剩心智负担，所以首次启动时自动合并：
    写回 config.json（含密钥），原 config.local.json 改名为 .bak 保留一次。
    """
    private_path = _legacy_private_path()
    if not private_path.exists():
        return raw
    try:
        private = json.loads(private_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[Config] Ignoring unreadable legacy config.local.json: {exc}")
        return raw
    if not isinstance(private, dict) or not private:
        return raw

    merged_raw = _merge(raw, private)
    # 旧结构用 vlm.model_keys 存各模型 Key，展开进 models 后删除该字段。
    vlm = merged_raw.get("vlm")
    if isinstance(vlm, dict):
        model_keys = vlm.pop("model_keys", None)
        if isinstance(model_keys, dict) and isinstance(vlm.get("models"), list):
            for entry in vlm["models"]:
                if not isinstance(entry, dict) or entry.get("api_key"):
                    continue
                mid = entry.get("id") or entry.get("model")
                if mid in model_keys:
                    entry["api_key"] = model_keys[mid]

    try:
        CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE_PATH.with_suffix(CONFIG_FILE_PATH.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(merged_raw, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, CONFIG_FILE_PATH)
        os.replace(private_path, private_path.with_name(private_path.name + ".bak"))
        print("[Config] Merged legacy config.local.json into config.json (backup kept)")
    except OSError as exc:
        print(f"[Config] Could not persist merged configuration: {exc}")
    return merged_raw


def load_config():
    import copy

    global _cache, _stamp
    with _lock:
        stamp = CONFIG_FILE_PATH.stat().st_mtime_ns if CONFIG_FILE_PATH.exists() else None
        if _cache is not None and stamp == _stamp:
            return copy.deepcopy(_cache)
        try:
            raw = (
                json.loads(CONFIG_FILE_PATH.read_text(encoding="utf-8"))
                if stamp is not None
                else {}
            )
            if not isinstance(raw, dict):
                raise ValueError("configuration must be an object")
            raw = _drop_legacy_encryption(_absorb_legacy_private(raw))
            cfg = validate_config(_merge(DEFAULT_CONFIG, raw))
        except (ValueError, OSError, TypeError) as exc:
            print(
                f"[Config] Invalid configuration: {exc}; retaining last valid configuration"
            )
            return copy.deepcopy(_cache or DEFAULT_CONFIG)
        # 迁移会重写 config.json，重新取 mtime，避免下一次读取白白解析一遍。
        stamp = CONFIG_FILE_PATH.stat().st_mtime_ns if CONFIG_FILE_PATH.exists() else None
        _cache, _stamp = cfg, stamp
        return copy.deepcopy(cfg)


def subscribe(callback):
    with _lock:
        _listeners.append(callback)

    def unsubscribe():
        with _lock:
            if callback in _listeners:
                _listeners.remove(callback)

    return unsubscribe


def save_config(cfg):
    global _cache, _stamp
    import copy

    with _lock:
        merged = validate_config(_merge(load_config(), cfg))
        CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = copy.deepcopy(merged)
        payload.get("vlm", {}).pop("model_keys", None)
        tmp = CONFIG_FILE_PATH.with_suffix(CONFIG_FILE_PATH.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, CONFIG_FILE_PATH)
        _cache = merged
        _stamp = CONFIG_FILE_PATH.stat().st_mtime_ns
        callbacks = list(_listeners)
    for callback in callbacks:
        try:
            callback(copy.deepcopy(merged))
        except Exception as exc:
            print(f"[Config] Reload callback failed: {exc}")


def is_configured():
    return bool(load_config()["vlm"].get("api_key", "").strip())


def get_vlm_models(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Return the list of configured VLM models, ensuring default model is present and marked."""
    if cfg is None:
        cfg = load_config()
    vlm = cfg.get("vlm", {})
    primary_name = str(vlm.get("model") or "default").strip()
    primary_model = {
        "id": "default",
        "name": primary_name or "主模型",
        "model": primary_name,
        "base_url": vlm.get("base_url", ""),
        "api_key": vlm.get("api_key", ""),
        "temperature": vlm.get("temperature", 0.7),
        "max_tokens": vlm.get("max_tokens", 4096),
        "enable_thinking": bool(vlm.get("enable_thinking", False)),
        "reasoning_effort": vlm.get("reasoning_effort", "none"),
        "is_default": True,
    }

    raw_models = vlm.get("models", [])
    if not isinstance(raw_models, list) or not raw_models:
        return [primary_model]

    result = []
    has_default = False
    for idx, m in enumerate(raw_models):
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("model") or f"model_{idx}").strip()
        m_model = str(m.get("model") or primary_name).strip()
        is_def = bool(m.get("is_default", False))
        if is_def and not has_default:
            has_default = True
            is_def = True
        else:
            is_def = False
        item = {
            "id": mid,
            "name": m.get("name") or m_model or mid,
            "model": m_model,
            "base_url": m.get("base_url") or vlm.get("base_url", ""),
            "api_key": m.get("api_key") or vlm.get("api_key", ""),
            "temperature": m.get("temperature", vlm.get("temperature", 0.7)),
            "max_tokens": m.get("max_tokens", vlm.get("max_tokens", 32768)),
            "enable_thinking": bool(m.get("enable_thinking", vlm.get("enable_thinking", False))),
            "reasoning_effort": m.get("reasoning_effort", vlm.get("reasoning_effort", "none")),
            "is_default": is_def,
        }
        result.append(item)

    if not result:
        return [primary_model]

    if not has_default:
        for item in result:
            if item["model"] == primary_name:
                item["is_default"] = True
                has_default = True
                break
        if not has_default:
            result[0]["is_default"] = True

    return result


def resolve_vlm_model_config(model_id_or_name: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve full settings for a specific model ID or return primary/default model config."""
    models = get_vlm_models(cfg)
    if not model_id_or_name:
        for m in models:
            if m.get("is_default"):
                return m
        return models[0]

    target = str(model_id_or_name).strip().lower()
    for m in models:
        if str(m.get("id")).lower() == target or str(m.get("model")).lower() == target or str(m.get("name")).lower() == target:
            return m

    # Fallback to default
    for m in models:
        if m.get("is_default"):
            return m
    return models[0]
