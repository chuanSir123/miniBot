import base64
import json
import time
import urllib.error
import urllib.request
from io import BytesIO
from typing import Any, Dict, Generator, List, Optional, Tuple

from core.config import load_config


class VLMBrain:
    """
    慢脑 (System 2) - OpenAI 兼容视觉语言模型 (VLM)
    支持：
      1. OpenAI / DeepSeek / vLLM 思考开关 (thinking toggle) 与思考强度 (reasoning_effort)
      2. 前缀缓存友好的多模态消息打包
      3. 流式同时解耦输出思考过程、回复文本、工具调用与精准 Token Usage
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None):
        cfg = load_config().get("vlm", {})
        self.base_url = (base_url or cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "gpt-4o"
        self.temperature = cfg.get("temperature", 0.7)
        self.max_tokens = cfg.get("max_tokens", 1024)
        self.enable_thinking = cfg.get("enable_thinking", False)
        self.reasoning_effort = cfg.get("reasoning_effort", "none")
        self.last_usage: Dict[str, Any] = {}

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def _build_payload(self, messages: List[Dict[str, Any]], stream: bool = False, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }

        if stream:
            payload["stream"] = True
            # 请求云端提供准确的 Token 统计
            payload["stream_options"] = {"include_usage": True}

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # 思考强度 (兼容 OpenAI o1/o3-mini 及 vLLM reasoning_effort)
        if self.reasoning_effort and self.reasoning_effort.lower() != "none":
            payload["reasoning_effort"] = self.reasoning_effort.lower()

        # 思考开关 (兼容 DeepSeek 与 vLLM 模板)
        if self.enable_thinking:
            payload["extra_body"] = {
                "thinking": {"type": "enabled"},
                "chat_template_kwargs": {"thinking": True}
            }

        return payload

    def _prepare_messages(self, messages: List[Dict[str, Any]], image_bytes: Optional[bytes] = None) -> List[Dict[str, Any]]:
        formatted = []
        for msg in messages:
            formatted.append(dict(msg))

        if image_bytes and formatted:
            last_msg = formatted[-1]
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
        return formatted

    def chat(self, messages: List[Dict[str, Any]], image_bytes: Optional[bytes] = None) -> str:
        """非流式标准聊天"""
        if not self.is_configured():
            return "【提示】未配置 VLM API Key，请先在配置中心填写。"

        endpoint = f"{self.base_url}/chat/completions" if not self.base_url.endswith("/chat/completions") else self.base_url
        formatted_messages = self._prepare_messages(messages, image_bytes)
        payload = self._build_payload(formatted_messages, stream=False)

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-VLM/2.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                if "usage" in resp_json:
                    self.last_usage = resp_json["usage"]
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

    def stream_chat_and_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        image_bytes: Optional[bytes] = None
    ) -> Generator[Tuple[str, Any], None, Tuple[str, List[Dict[str, Any]], Dict[str, Any]]]:
        """
        流式生成，同时解耦输出中间文本思考、思考过程 (DeepSeek/vLLM reasoning_content)、工具调用与精准 Token Usage。
        yield ("reasoning_delta", reasoning_str)
        yield ("text_delta", delta_str)
        最终 return (full_text, completed_tool_calls, usage_dict)
        """
        if not self.is_configured():
            yield ("text_delta", "【提示】未配置 VLM API Key。")
            return ("【提示】未配置 VLM API Key。", [], {})

        endpoint = f"{self.base_url}/chat/completions" if not self.base_url.endswith("/chat/completions") else self.base_url
        formatted_messages = self._prepare_messages(messages, image_bytes)
        payload = self._build_payload(formatted_messages, stream=True, tools=tools)

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-Harness/2.0"
            },
            method="POST"
        )

        full_content = []
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        usage_result: Dict[str, Any] = {}

        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    if line == "data: [DONE]":
                        break

                    data_str = line[5:].strip()
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue

                    # 捕获 Token Usage (来自 stream_options)
                    if "usage" in chunk and chunk["usage"]:
                        usage_result = chunk["usage"]
                        self.last_usage = usage_result

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})

                    # 1. 思考链过程流式输出 (DeepSeek-R1 / vLLM reasoning_content)
                    reasoning_delta = delta.get("reasoning_content")
                    if reasoning_delta:
                        yield ("reasoning_delta", reasoning_delta)

                    # 2. 中间文本流式输出 (思考/解释/回复)
                    content_delta = delta.get("content")
                    if content_delta:
                        full_content.append(content_delta)
                        yield ("text_delta", content_delta)

                    # 3. 工具调用流式聚合
                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": tc.get("id", f"call_{idx}_{int(time.time()*1000)}"),
                                    "name": "",
                                    "arguments": ""
                                }
                            if tc.get("id"):
                                tool_calls_map[idx]["id"] = tc["id"]
                            func = tc.get("function", {})
                            if func.get("name"):
                                tool_calls_map[idx]["name"] += func["name"]
                            if func.get("arguments"):
                                tool_calls_map[idx]["arguments"] += func["arguments"]

        except Exception as e:
            err_msg = f"【流式读取异常: {e}】"
            yield ("text_delta", err_msg)
            full_content.append(err_msg)

        final_tool_calls = []
        for idx in sorted(tool_calls_map.keys()):
            tc_data = tool_calls_map[idx]
            if tc_data["name"]:
                final_tool_calls.append({
                    "id": tc_data["id"],
                    "type": "function",
                    "function": {
                        "name": tc_data["name"],
                        "arguments": tc_data["arguments"]
                    }
                })

        return ("".join(full_content).strip(), final_tool_calls, usage_result or self.last_usage)

    def generate_bubble_reply(self, user_text: str, context: str = "", image_bytes: Optional[bytes] = None) -> str:
        """为桌宠生成轻量气泡回复 (前缀对齐架构)"""
        # 静态规范放置于 system
        system_prompt = (
            "你是一个桌宠助手（形象是IKun小鸡/坤坤）。性格活泼幽默、忠诚可爱、说话简洁清晰。\n"
            "【规则】你的回复将显示在桌宠头顶的小气泡中，务必精简（通常 1~2 句话，尽量在 50 字内），直接给重点。"
        )
        # 易变上下文放置于 user 消息尾部，保护前缀缓存
        user_content = user_text
        if context:
            user_content += f"\n\n[当前环境上下文: {context}]"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        return self.chat(messages, image_bytes=image_bytes)
