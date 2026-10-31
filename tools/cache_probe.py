"""前缀缓存探针：只读探测，不执行工具，不打印凭据。

用法:
  python tools/cache_probe.py base        # 同一请求重复两次，看 cached_tokens
  python tools/cache_probe.py matrix      # tools / volatile 尾部 / 时间戳 变量矩阵
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.brain import VLMBrain
from core.network import direct_opener


def post(endpoint, key, payload, timeout=120):
    """发一次流式请求，返回 (ttft_ms, usage, status, err)。"""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "MiniBot-Harness/2.0",
        },
        method="POST",
    )
    started = time.monotonic()
    usage = {}
    ttft = None
    try:
        with direct_opener().open(req, timeout=timeout) as resp:
            while True:
                line = resp.readline(1 << 20)
                if not line:
                    break
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                if raw == b"[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except Exception:
                    break
                if event.get("usage"):
                    usage = event["usage"]
                if ttft is None and any(c.get("delta") for c in event.get("choices", [])):
                    ttft = round((time.monotonic() - started) * 1000)
                if any(c.get("finish_reason") for c in event.get("choices", [])):
                    break
        return ttft, usage, 200, None
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        return None, {}, exc.code, detail.replace(key, "[redacted]")[:300]
    except Exception as exc:
        return None, {}, -1, (type(exc).__name__ + ": " + str(exc)).replace(key, "[redacted]")[:300]


def show(label, ttft, usage, status, err, wall):
    prompt = usage.get("prompt_tokens")
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens", usage.get("prompt_cache_hit_tokens", usage.get("cached_tokens")))
    hit = None
    if isinstance(cached, int) and isinstance(prompt, int) and prompt:
        hit = round(cached * 100 / prompt, 1)
    print(
        json.dumps(
            {
                "case": label,
                "status": status,
                "ttft_ms": ttft,
                "wall_ms": wall,
                "prompt_tokens": prompt,
                "cached_tokens": cached,
                "cache_hit_pct": hit,
                "completion_tokens": usage.get("completion_tokens"),
                "error": err,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main():
    brain = VLMBrain()
    settings = brain._settings()
    base, key, model, _ = settings
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    mode = sys.argv[1] if len(sys.argv) > 1 else "base"

    # 一段足够长的稳定前缀，模拟 system + 历史
    stable = "\n".join(
        f"{i}. 这是用于验证前缀缓存的稳定填充文本，内容固定不变，编号 {i}，"
        f"包含足够多的中文字符以形成可观测的 token 数量。" for i in range(1, 120)
    )
    system = "你是测试助手，只回复 OK。\n" + stable
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo_probe",
                "description": "测试用工具，回显输入。",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]

    def payload(messages, with_tools=True, extra=None):
        body = {"model": model, "messages": messages, "temperature": 0.0,
                "max_tokens": 16, "stream": True,
                "stream_options": {"include_usage": True}}
        if with_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if extra:
            body.update(extra)
        return body

    if mode == "base":
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": "只回复 OK"}]
        for i in range(2):
            p = payload(msgs)
            t = time.monotonic()
            ttft, usage, status, err = post(endpoint, key, p)
            show(f"identical_run_{i + 1}", ttft, usage, status, err,
                 round((time.monotonic() - t) * 1000))
            time.sleep(1)
        return

    if mode == "matrix":
        # 场景 1: 完全相同的两轮
        base_msgs = [{"role": "system", "content": system},
                     {"role": "user", "content": "只回复 OK"}]
        cases = []
        cases.append(("A_same_1", payload(base_msgs)))
        cases.append(("A_same_2", payload(base_msgs)))
        cases.append(("A_same_no_tools_1", payload(base_msgs, with_tools=False)))
        cases.append(("A_same_no_tools_2", payload(base_msgs, with_tools=False)))
        # 场景 2: 工具描述顺序不同（内容一致）
        reordered = [
            {
                "type": "function",
                "function": {
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "description": "测试用工具，回显输入。",
                    "name": "echo_probe",
                },
            }
        ]
        p2 = payload(base_msgs)
        p2["tools"] = reordered
        cases.append(("B_tools_key_order_changed", p2))
        # 场景 3: 尾部追加易变上下文（时间戳），前缀不变
        volatile1 = base_msgs + [{"role": "user", "content": "当前时间: 2026-10-29T00:00:00"}]
        volatile2 = base_msgs + [{"role": "user", "content": "当前时间: 2026-10-29T00:12:34"}]
        cases.append(("C_volatile_1", payload(volatile1)))
        cases.append(("C_volatile_2", payload(volatile2)))
        # 场景 4: 在历史中间插入易变内容（破坏前缀）
        broken = [{"role": "system", "content": system},
                  {"role": "user", "content": "当前时间: 2026-10-29T00:00:00"},
                  {"role": "user", "content": "只回复 OK"},
                  {"role": "user", "content": "当前时间: 2026-10-29T00:12:34"}]
        cases.append(("D_middle_injection", payload(broken)))

        for label, p in cases:
            t = time.monotonic()
            ttft, usage, status, err = post(endpoint, key, p)
            show(label, ttft, usage, status, err, round((time.monotonic() - t) * 1000))
            time.sleep(0.8)
        return

    if mode == "tools":
        # 真实 HARNESS_TOOLS 规模的 tools，测 tools 是否影响 TTFT / 缓存
        from core.harness import HARNESS_TOOLS
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": "只回复 OK"}]
        big = payload(msgs, with_tools=False)
        big["tools"] = HARNESS_TOOLS
        big["tool_choice"] = "auto"
        for i in range(2):
            t = time.monotonic()
            ttft, usage, status, err = post(endpoint, key, big)
            show(f"E_harness_tools_{i + 1}", ttft, usage, status, err,
                 round((time.monotonic() - t) * 1000))
            time.sleep(1)
        return


if __name__ == "__main__":
    main()
