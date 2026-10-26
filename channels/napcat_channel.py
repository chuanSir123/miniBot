import asyncio
import json
import threading
from typing import Any, Callable, Dict, Optional

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


class NapCatChannel:
    """
    OneBot v11 WebSocket 客户端交互通道
    负责：
      1. 与内嵌的 NapCat OneBot 建立双向 WebSocket 连接
      2. 监听私聊和群聊消息，转交给 Brain 处理
      3. 发送主动协同提醒或被动回答到 QQ
    """

    def __init__(
        self,
        ws_url: str = "ws://127.0.0.1:3001",
        access_token: str = "",
        on_message_callback: Optional[Callable[[str, Dict[str, Any]], str]] = None
    ):
        self.ws_url = ws_url
        self.access_token = access_token
        self.on_message_callback = on_message_callback
        self._ws: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._is_running = False

    def start(self):
        if not HAS_WEBSOCKETS:
            print("[NapCat Channel] 错误：未安装 websockets 库，请使用 launcher 安装。")
            return

        if self._is_running:
            return

        self._is_running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_and_listen())

    async def _connect_and_listen(self):
        headers = {}
        if self.access_token.strip():
            headers["Authorization"] = f"Bearer {self.access_token.strip()}"

        print(f"[NapCat Channel] 正在连接 OneBot 服务: {self.ws_url} ...")
        retry_delay = 3

        while self._is_running:
            try:
                async with websockets.connect(self.ws_url, extra_headers=headers) as ws:
                    self._ws = ws
                    print(f"[NapCat Channel] ✅ 成功连接至 NapCat OneBot WebSocket！")
                    retry_delay = 3

                    async for message in ws:
                        if not self._is_running:
                            break
                        try:
                            data = json.loads(message)
                            await self._handle_onebot_event(data)
                        except Exception as e:
                            print(f"[NapCat Channel] 处理事件异常: {e}")

            except Exception as e:
                self._ws = None
                if self._is_running:
                    # 等待重连（NapCat 启动/扫码通常需要一些时间）
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 1.5, 15)

    async def _handle_onebot_event(self, event: Dict[str, Any]):
        post_type = event.get("post_type")
        if post_type != "message":
            return

        msg_type = event.get("message_type")
        raw_msg = event.get("raw_message", "").strip()
        user_id = event.get("user_id")
        group_id = event.get("group_id")

        if not raw_msg:
            return

        print(f"[QQ 收到消息] [{msg_type}] ({user_id}): {raw_msg}")

        # 若配置了消息处理回调
        if self.on_message_callback:
            # 异步执行回答
            reply = self.on_message_callback(raw_msg, event)
            if reply:
                if msg_type == "private":
                    await self.send_private_msg(user_id, reply)
                elif msg_type == "group":
                    await self.send_group_msg(group_id, reply)

    async def send_private_msg(self, user_id: int, message: str):
        payload = {
            "action": "send_private_msg",
            "params": {
                "user_id": user_id,
                "message": message
            }
        }
        await self._send_json(payload)

    async def send_group_msg(self, group_id: int, message: str):
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": group_id,
                "message": message
            }
        }
        await self._send_json(payload)

    async def _send_json(self, payload: Dict[str, Any]):
        if self._ws:
            try:
                await self._ws.send(json.dumps(payload))
            except Exception as e:
                print(f"[NapCat Channel] 发送消息失败: {e}")

    def stop(self):
        self._is_running = False
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
