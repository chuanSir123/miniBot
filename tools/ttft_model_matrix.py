#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定位 59241 token 对应的真实会话 + 同网关多模型延迟对照。

用法:
    python tools/ttft_model_matrix.py web:main:0428eec0
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


def stream_once(endpoint, key, payload, timeout=240):
    body = json.dumps(payload).encode("utf-8")
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
    t0 = time.monotonic()
    ttft = None
    usage = {}
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
        return {"err": type(exc).__name__ + ": " + str(exc)[:90], "ttft_ms": None}
    pt = usage.get("prompt_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    hit = round(cached * 100.0 / pt, 1) if pt and cached else 0.0
    return {
        "ttft_ms": ttft,
        "prompt_tokens": pt,
        "cached_tokens": cached,
        "hit_pct": hit,
        "completion_tokens": usage.get("completion_tokens"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", default="web:main:0428eec0")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    full = brain._build_payload(
        brain._prepare_messages(build_real_payload(args.session, brain)),
        stream=True,
        tools=HARNESS_TOOLS,
        settings=settings,
    )
    full["max_tokens"] = 16

    print("=== [1] 目标会话真实 prompt_tokens 核对 ===")
    print(json.dumps({"session": args.session, "model": model}, ensure_ascii=False))
    for i in range(args.rounds):
        r = stream_once(endpoint, key, full)
        print(json.dumps({"round": i + 1, **r}, ensure_ascii=False))
        time.sleep(0.5)

    print()
    print("=== [2] 同网关多模型 15-token 基线 TTFT 对照 ===")
    models = [
        model,
        "google/gemma-4-26B-A4B-it",
        "google/gemma-4-26B-A4B-it-thinking",
        "tsinghua-sigs-robot-lab/VeriLoop-E2",
    ]
    tiny = {
        "model": None,
        "messages": [{"role": "user", "content": "只回复OK"}],
        "temperature": 0,
        "max_tokens": 8,
        "stream": True,
    }
    for mdl in models:
        ts = []
        for _ in range(3):
            p = dict(tiny)
            p["model"] = mdl
            r = stream_once(endpoint, key, p, timeout=200)
            if r.get("ttft_ms"):
                ts.append(r["ttft_ms"])
            time.sleep(0.4)
        if ts:
            print(json.dumps({
                "model": mdl,
                "n": len(ts),
                "min": min(ts),
                "p50": int(statistics.median(ts)),
                "max": max(ts),
                "all": ts,
            }, ensure_ascii=False))
        else:
            print(json.dumps({"model": mdl, "no_sample": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
