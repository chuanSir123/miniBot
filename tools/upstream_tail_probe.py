#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探测上游是否存在「多副本 / 负载均衡」导致的 TTFT 长尾。

思路: 同一载荷连发 N 次, 记录 TTFT 序列。若呈双峰(快/慢两簇)且与上游实例相关,
说明请求被分发到不同后端, 其中部分后端冷启动/排队 —— 这才是 30s 的根因。
同时观察 cached_tokens 是否在慢请求上失效。
"""
import argparse
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
    ttft = None
    usage = {}
    hdr = {}
    err = None
    try:
        with direct_opener().open(req, timeout=timeout) as resp:
            hdr = {k.lower(): v for k, v in resp.headers.items()}
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
        "ttft_ms": ttft,
        "prompt_tokens": pt,
        "cached_tokens": cached,
        "hit_pct": round(cached * 100.0 / pt, 1) if pt and cached else 0.0,
        "cf_ray": hdr.get("cf-ray"),
        "fc_req": hdr.get("x-fc-request-id"),
        "err": err,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="web:main:0428eec0")
    ap.add_argument("--n", type=int, default=24)
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()
    payload = brain._build_payload(
        brain._prepare_messages(build_real_payload(args.session, brain)),
        stream=True, tools=HARNESS_TOOLS, settings=settings,
    )
    payload["max_tokens"] = 16

    rows = []
    print("=== 连续 24 次同载荷, 记录 TTFT / cached / cf-ray ===")
    for i in range(args.n):
        r = call(endpoint, key, payload)
        rows.append(r)
        print(json.dumps({"i": i + 1, **r}, ensure_ascii=False))
        time.sleep(0.2)

    vals = [r["ttft_ms"] for r in rows if r.get("ttft_ms")]
    fast = [v for v in vals if v < 15000]
    slow = [v for v in vals if v >= 15000]
    print()
    print("SUMMARY " + json.dumps({
        "n": len(vals),
        "p50": int(statistics.median(vals)) if vals else None,
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
        "fast_n": len(fast), "fast_mean": round(sum(fast) / len(fast)) if fast else None,
        "slow_n": len(slow), "slow_mean": round(sum(slow) / len(slow)) if slow else None,
        "slow_pct": round(len(slow) * 100.0 / len(vals), 1) if vals else None,
    }, ensure_ascii=False))
    slow_rows = [r for r in rows if (r.get("ttft_ms") or 0) >= 15000]
    fast_rows = [r for r in rows if (r.get("ttft_ms") or 0) < 15000]
    print("SLOW_CACHED " + json.dumps([{k: r.get(k) for k in ("ttft_ms", "cached_tokens", "hit_pct", "cf_ray")} for r in slow_rows], ensure_ascii=False))
    print("FAST_CACHED " + json.dumps([{k: r.get(k) for k in ("ttft_ms", "cached_tokens", "hit_pct", "cf_ray")} for r in fast_rows], ensure_ascii=False))


if __name__ == "__main__":
    main()
