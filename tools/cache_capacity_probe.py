#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 vLLM 前缀缓存的容量上限 / 驱逐行为。

用户的 vLLM 日志全是 30s, 但探针有时能命中 98.2% -> 8.3s。
本实验回答: 什么条件下会掉回 30s?

假设 A(缓存被别的请求挤掉): 在两次同载荷请求之间插入不同载荷的请求, 命中率应下降。
假设 B(缓存有大小上限): 47k 的前缀本身放不进缓存, 换个更短的就能命中。
"""
import argparse
import copy
import json
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from core.brain import VLMBrain
from core.harness import HARNESS_TOOLS
from core.network import direct_opener
from real_cache_probe import build_real_payload


def call(endpoint, key, payload, timeout=300):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "MiniBot-Harness/2.0",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    t0 = time.monotonic()
    ttft = None
    usage = {}
    err = None
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
                    ev = json.loads(raw)
                except Exception:
                    break
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices", []):
                    d = ch.get("delta") or {}
                    if ttft is None and (d.get("content") or d.get("reasoning_content")):
                        ttft = round((time.monotonic() - t0) * 1000)
    except Exception as exc:  # noqa: BLE001
        err = type(exc).__name__
    pt = usage.get("prompt_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return {
        "ttft_ms": ttft, "prompt_tokens": pt, "cached_tokens": cached,
        "hit_pct": round(cached * 100.0 / pt, 1) if pt and cached else 0.0,
        "err": err,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="web:main:0428eec0")
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()
    raw = brain._prepare_messages(build_real_payload(args.session, brain))

    def mk(sub):
        p = brain._build_payload(sub, stream=True, tools=HARNESS_TOOLS, settings=settings)
        p["max_tokens"] = 16
        return p

    def mk_noise(msgs):
        """插入一个不同前缀的请求, 用于挤掉缓存。"""
        m = copy.deepcopy(msgs)
        for x in m:
            if x.get("role") == "system":
                x["content"] = x.get("content", "") + "\nNOISE-" + str(time.time())
                break
        return mk(m)

    full = mk(raw)
    print(json.dumps({"model": model, "msgs": len(raw)}, ensure_ascii=False), flush=True)

    print("\n=== [A] 连续同载荷 3 次 (无干扰) ===", flush=True)
    for i in range(3):
        r = call(endpoint, key, full)
        print(json.dumps({"i": i + 1, **r}, ensure_ascii=False), flush=True)
        time.sleep(0.3)

    print("\n=== [B] 同载荷 -> 插入不同载荷 -> 再同载荷 ===", flush=True)
    for i in range(3):
        r = call(endpoint, key, full)
        print(json.dumps({"step": f"before_noise_{i+1}", **r}, ensure_ascii=False), flush=True)
        rn = call(endpoint, key, mk_noise(raw))
        print(json.dumps({"step": f"noise_{i+1}", "ttft_ms": rn.get("ttft_ms"),
                          "prompt_tokens": rn.get("prompt_tokens"),
                          "hit_pct": rn.get("hit_pct")}, ensure_ascii=False), flush=True)
        r2 = call(endpoint, key, full)
        print(json.dumps({"step": f"after_noise_{i+1}", **r2}, ensure_ascii=False), flush=True)
        time.sleep(0.3)

    print("\n=== [C] 短前缀能否稳定命中 (容量上限探测) ===", flush=True)
    for frac in (0.1, 0.25, 0.5, 0.75, 1.0):
        n = max(2, int(len(raw) * frac))
        p = mk(raw[:n])
        rs = [call(endpoint, key, p) for _ in range(3)]
        for j, r in enumerate(rs):
            print(json.dumps({"frac": frac, "msgs": n, "rep": j + 1, **r}, ensure_ascii=False), flush=True)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
