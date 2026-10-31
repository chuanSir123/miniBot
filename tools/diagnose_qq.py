"""Read-only local QQ/OneBot diagnostics; does not send QQ messages or expose tokens."""

import json
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import load_config
from channels.napcat_manager import NapCatManager
from channels.napcat_channel import NapCatChannel, connect

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config()["channels"]["napcat"]
    manager = NapCatManager()
    state = manager.readiness()
    result = {
        "napcat_files_complete": manager.is_installed(),
        "onebot_tcp_listening": state["onebot_listening"],
        "webui_default_tcp_listening": state["webui_listening"],
        "websocket_proxy_disabled": (
            connect is not None
            and (
                "proxy" not in inspect.signature(connect).parameters
                or NapCatChannel.connection_options(cfg).get("proxy", True) is None
            )
        ),
    }
    try:
        result["qq_executable"] = str(manager._resolve_qq_executable())
    except (OSError, ValueError) as exc:
        result["qq_path_error"] = str(exc)
    if not state["onebot_listening"]:
        result["next_step"] = (
            "先确认控制台出现 NapCat Shell App Loading；若6099也未监听，属于核心尚未加载，不是QQ账号扫码失败。"
        )
    else:
        result["next_step"] = (
            "端口已监听；检查MiniBot是否显示OneBot WebSocket已连接，若鉴权失败请核对配置中的token。"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
