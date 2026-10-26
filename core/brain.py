import base64
import json
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any, Dict, List, Optional

from core.config import load_config


class VLMBrain:
    """
    慢脑 (System 2) - OpenAI 兼容视觉语言模型 (VLM)
    负责复杂深度推理、视觉观察、桌宠对话气泡生成与 Skill 总结
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None):
        cfg = load_config().get("vlm", {})
        self.base_url = (base_url or cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "gpt-4o"
        self.temperature = cfg.get("temperature", 0.7)
        self.max_tokens = cfg.get("max_tokens", 800)

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def chat(self, messages: List[Dict[str, Any]], image_bytes: Optional[bytes] = None) -> str:
        """
        发送聊天请求，若包含 image_bytes 则以 OpenAI 兼容的多模态视觉格式发送
        """
        if not self.is_configured():
            return "【提示】未配置 VLM API Key，请先在配置中心填写。"

        endpoint = f"{self.base_url}/chat/completions" if not self.base_url.endswith("/chat/completions") else self.base_url
        
        # 复制消息列表，避免修改入参
        formatted_messages = []
        for msg in messages:
            formatted_messages.append(dict(msg))

        # 若提供了图片，附加到最新一条 user 消息中
        if image_bytes and formatted_messages:
            last_msg = formatted_messages[-1]
            if last_msg.get("role") == "user":
                orig_content = last_msg.get("content", "")
                b64_img = base64.b64encode(image_bytes).decode("utf-8")
                
                content_list = []
                if isinstance(orig_content, str) and orig_content:
                    content_list.append({"type": "text", "text": orig_content})
                elif isinstance(orig_content, list):
                    content_list.extend(orig_content)
                
                content_list.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64_img}",
                        "detail": "low"
                    }
                })
                last_msg["content"] = content_list

        payload = {
            "model": self.model,
            "messages": formatted_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-VLM/1.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                choices = resp_json.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
                return ""
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="ignore")
            print(f"[VLM] HTTP Error {e.code}: {err}")
            return f"【VLM 报错】HTTP {e.code}: {err[:80]}"
        except Exception as e:
            print(f"[VLM] Request error: {e}")
            return f"【VLM 网络异常】{str(e)[:80]}"

    def generate_bubble_reply(self, user_text: str, context: str = "", image_bytes: Optional[bytes] = None) -> str:
        """
        专门为桌宠生成轻量、精炼、有性格的头顶短气泡回复（控制在 50~100 字内）
        """
        system_prompt = (
            "你是一个桌宠助手（形象是IKun小鸡/坤坤）。性格活泼幽默、忠诚可爱、说话简洁清晰。\n"
            "【规则】\n"
            "1. 你的回复将显示在桌宠头顶的小气泡中，请务必精简（通常 1~3 句话，尽量不超过 60 个字）！\n"
            "2. 避免冗长的排版和大段无意义寒暄，直接给重点或贴心提醒。\n"
            f"3. 当前环境上下文：{context}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ]

        return self.chat(messages, image_bytes=image_bytes)
