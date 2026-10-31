"""量"结构性未命中下限"：尾部换成一两句话，看未命中量是否跟着掉。

用途：区分「我们自己的尾部太大」和「服务端固定会重算最后一段」。
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import SYSTEM_PROMPT_CACHE
from core.harness import HARNESS_TOOLS
from core.memory import MiniMemory


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:ebce6ed7"
    mem = MiniMemory()
    history, _ = mem.get_history(sid)
    brain = VLMBrain()
    settings = brain._settings()
    base = settings[0]
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    system, _ = SYSTEM_PROMPT_CACHE.render(
        scope=sid, channel="web",
        selected_tools=[t["function"]["name"] for t in HARNESS_TOOLS], skills="",
    )

    def post(tail):
        compactor = ContextCompactor(None, mem, sid)
        messages = compactor.align_and_assemble_messages(system, history, tail)
        payload = brain._build_payload(brain._prepare_messages(messages), stream=True,
                                       tools=HARNESS_TOOLS, settings=settings)
        payload["max_tokens"] = 16
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body,
            headers={"Authorization": "Bearer " + settings[1],
                     "Content-Type": "application/json",
                     "User-Agent": "MiniBot-VLM/2.0"},
            method="POST")
        t0 = time.monotonic()
        resp = brain._open_request(req, timeout=180)
        usage, ttft = {}, None
        while True:
            line = resp.readline(1 << 20)
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except ValueError:
                break
            if ev.get("usage"):
                usage = ev["usage"]
            for choice in ev.get("choices", []):
                delta = choice.get("delta") or {}
                if ttft is None and (delta.get("content") or delta.get("reasoning_content")):
                    ttft = time.monotonic()
        det = usage.get("prompt_tokens_details") or {}
        pt, cached = usage.get("prompt_tokens"), det.get("cached_tokens")
        return {"tail_chars": len(tail), "prompt_tokens": pt, "cached": cached,
                "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
                "ttft_ms": round((ttft - t0) * 1000) if ttft else None}

    tiny = "状态: 进行中；本地时间: 2026-10-30T10:30"
    for label, tail in (("极小尾部#1", tiny), ("极小尾部#2", tiny),
                        ("极小尾部#3", tiny)):
        print(label, json.dumps(post(tail), ensure_ascii=False), flush=True)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
