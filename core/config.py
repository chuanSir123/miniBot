import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "vlm": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o",
        "temperature": 0.7,
        "max_tokens": 1024
    },
    "jev": {
        "base_url": "https://api.bamboonode.dev/v1",
        "api_key": "",
        "model": "startlux-decision-27b-autothink",
        "portal_url": "https://bamboonode.dev/tokens"
    },
    "proactive": {
        "heartbeat_interval_sec": 30,
        "idle_threshold_sec": 600,
        "enable_window_listener": True,
        "enable_clipboard_listener": True,
        "interrupt_score_threshold": 0.65
    },
    "channels": {
        "pet": {
            "enabled": True,
            "scale": 1.0,
            "pet_dir": r"C:\Users\admin\.codex\pets\ikunchick",
            "bubble_duration_sec": 8
        },
        "napcat": {
            "enabled": False,
            "ws_url": "ws://127.0.0.1:3001",
            "access_token": "",
            "auto_launch": True,
            "qq_number": ""
        }
    }
}

CONFIG_FILE_PATH = Path(__file__).resolve().parent.parent / "config.json"


def get_config_path() -> Path:
    return CONFIG_FILE_PATH


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except Exception as e:
        print(f"[Config] Error reading {CONFIG_FILE_PATH}: {e}, using defaults.")
        return DEFAULT_CONFIG.copy()

    # Deep merge defaults
    merged = DEFAULT_CONFIG.copy()
    for section, values in DEFAULT_CONFIG.items():
        if section in user_cfg and isinstance(user_cfg[section], dict):
            merged[section] = {**values, **user_cfg[section]}
        elif section in user_cfg:
            merged[section] = user_cfg[section]

    return merged


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Config] Failed to save config: {e}")


def is_configured() -> bool:
    cfg = load_config()
    vlm_key = cfg.get("vlm", {}).get("api_key", "").strip()
    jev_key = cfg.get("jev", {}).get("api_key", "").strip()
    return bool(vlm_key or jev_key)
