"""给 QQ 好友资料页点赞（OneBot send_like via NapCat，WS 连接带 access_token）。"""

import asyncio
import json
import time
import uuid


def _load_cfg():
    from core.config import load_config
    return load_config()["channels"]["napcat"]


def _pick_ws_url(cfg):
    return cfg.get("ws_url", "ws://127.0.0.1:3001")


def _headers(cfg):
    tok = cfg.get("access_token") or ""
    return {"Authorization": "Bearer " + tok} if tok else {}


def _do_onebot_request(ws_url, headers, action, params, timeout=8.0):
    """一次性连接 NapCat OneBot，发送 action 并等待 echo 匹配的响应。"""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        raise RuntimeError("websockets 未安装，无法调用 OneBot")

    async def _run():
        options = dict(
            additional_headers=headers,
            open_timeout=6,
            close_timeout=2,
            max_size=2 * 1024 * 1024,
            max_queue=8,
            compression=None,
        )
        import inspect
        if "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
        async with connect(ws_url, **options) as ws:
            echo = uuid.uuid4().hex
            payload = {"action": action, "params": params, "echo": echo}
            await ws.send(json.dumps(payload))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.monotonic()))
                data = json.loads(raw)
                if data.get("echo") == echo:
                    return data
            raise TimeoutError("OneBot 响应超时")

    return asyncio.run(_run())


def execute(context):
    args = context.get("arguments") or {}
    # 兼容多种传参路径：arguments / 顶层 / _minibot
    user_id = str(
        args.get("user_id")
        or context.get("user_id")
        or (context.get("_minibot") or {}).get("sender_qq")
        or (context.get("_minibot") or {}).get("user_id")
        or ""
    ).strip()
    count_raw = args.get("count") or context.get("count") or 1
    try:
        count = int(count_raw)
    except (TypeError, ValueError):
        count = 1

    if not user_id or not user_id.isdigit():
        return {"ok": False, "error": f"无法确定要点赞的 QQ 号（user_id={user_id!r}）"}

    if count < 1:
        count = 1
    if count > 10:
        count = 10  # 保护，避免异常批量

    cfg = _load_cfg()
    ws_url = _pick_ws_cfg(cfg)
    headers = _headers(cfg)

    # NapCat 给好友资料页点赞的 OneBot v11 标准 API 是 send_like（不是 set_friend_like）
    try:
        resp = _do_onebot_request(ws_url, headers, "send_like", {"user_id": int(user_id), "times": count})
    except Exception as exc:
        return {"ok": False, "error": f"调用 OneBot send_like 失败：{type(exc).__name__} {str(exc)[:160]}"}

    status = resp.get("status", "")
    ok = status in ("ok", "async")
    retcode = resp.get("retcode")
    msg = resp.get("msg") or resp.get("message") or ""
    data = resp.get("data") or {}

    if not ok:
        return {"ok": False, "error": f"OneBot 返回失败：status={status} retcode={retcode} msg={msg[:160]}"}

    return {
        "ok": True,
        "user_id": user_id,
        "times": count,
        "status": status,
        "data": data,
        "note": f"已给 QQ {user_id} 的资料页点 {count} 个赞",
    }


def _pick_ws_cfg(cfg):
    return cfg.get("ws_url", "ws://127.0.0.1:3001")

DESCRIPTION = '给 QQ 好友资料页点赞（OneBot send_like）。当主人或群友说「赞我/点赞/给我点赞/帮我点个赞/给xxx点赞」时使用。参数 user_id 为要点赞的好友 QQ 号（字符串或数字）；不传则默认给当前消息发送者点赞。通过 NapCat 的 OneBot WebSocket 通道（带 access_token 的 Authorization 头）发送 send_like 请求。'

PARAMETERS = {'type': 'object', 'properties': {'user_id': {'type': 'string', 'description': '要点赞的好友 QQ 号；不传则给当前消息发送者点赞'}, 'count': {'type': 'integer', 'description': '点赞次数，默认 1', 'default': 1}}, 'additionalProperties': False}
