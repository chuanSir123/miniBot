#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取网关完整响应头 + 响应体首帧，用于定位上游路由与排队来源。"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from core.brain import VLMBrain
from core.harness import HARNESS_TOOLS
from core.network import direct_opener
from real_cache_probe import build_real_payload


def dump(endpoint, key, payload, label):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "MiniBot-Harness/2.0",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    t0 = time.monotonic()
    first_byte = None
    first_line = None
    usage = {}
    try:
        with direct_opener().open(req, timeout=240) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            while True:
                line = resp.readline(1 << 20)
                if not line:
                    break
                if first_byte is None:
                    first_byte = round((time.monotonic() - t0) * 1000)
                if first_line is None and line.strip():
                    first_line = line[:400].decode("utf-8", "replace")
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                if raw == b"[DONE]":
                    break
                try:
                    ev = json.loads(raw)
                except Exception:
                    break
                if ev.get("usage"):
                    usage = ev["usage"]
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"label": label, "err": type(exc).__name__ + ": " + str(exc)[:120]}, ensure_ascii=False))
        return

    print(json.dumps({
        "label": label,
        "first_byte_ms": first_byte,
        "first_line": first_line,
        "headers": headers,
        "usage": usage,
    }, ensure_ascii=False, indent=2))
    print("-" * 100)


def main():
    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    print("base_url =", base)
    print("model    =", model)
    print("=" * 100)

    tiny = brain._build_payload([{"role": "user", "content": "只回复OK"}], stream=True, settings=settings)
    tiny["max_tokens"] = 8
    dump(endpoint, key, tiny, "tiny-15tok")

    full = brain._build_payload(
        brain._prepare_messages(build_real_payload("web:main:0428eec0", brain)),
        stream=True,
        tools=HARNESS_TOOLS,
        settings=settings,
    )
    full["max_tokens"] = 16
    dump(endpoint, key, full, "full-cached-47k")


if __name__ == "__main__":
    main()
