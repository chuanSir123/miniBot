#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""决定性实验: 缓存的"账面命中"是否带来真实加速。

A 组: 原样前缀 (cached ≈ 46592)          -> 期望命中
B 组: 前缀首部插入随机串 (缓存必然失效)   -> 强制全量 prefill
C 组: 原样前缀但只保留尾部 2k (极短)      -> 对照

如果 A ≈ B, 说明网关报告的 cached_tokens 是"账面命中", 上游并未真正复用 KV cache,
或者该 relay 对前缀缓存不做任何优化 —— 这才是 30s TTFT 的真正根因。
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request
import uuid

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
        "ttft_ms": ttft,
        "prompt_tokens": pt,
        "cached_tokens": cached,
        "hit_pct": round(cached * 100.0 / pt, 1) if pt and cached else 0.0,
        "err": err,
    }


def stats(v):
    if not v:
        return None
    s = sorted(v)
    return {"n": len(s), "min": s[0], "p50": int(statistics.median(s)),
            "max": s[-1], "mean": round(sum(s) / len(s))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="web:main:0428eec0")
    ap.add_argument("--reps", type=int, default=6)
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    msgs = brain._prepare_messages(build_real_payload(args.session, brain))

    def mk(sub, salt=None):
        p = brain._build_payload(sub, stream=True, tools=HARNESS_TOOLS, settings=settings)
        p["max_tokens"] = 16
        if salt:
            # 在 system 前缀最前面插入唯一随机串 -> 前缀必然改变 -> 缓存失效
            p["messages"] = ([{"role": "system", "content": "nonce:" + salt}] + p["messages"])
        return p

    A, B, C = [], [], []
    print(json.dumps({"model": model, "session": args.session, "msgs": len(msgs)}, ensure_ascii=False))
    print("=== 交错 A(命中) / B(强制失效) / C(极短) ===")
    for i in range(args.reps):
        ra = call(endpoint, key, mk(msgs))
        rb = call(endpoint, key, mk(msgs, salt=uuid.uuid4().hex))
        rc = call(endpoint, key, mk(msgs[-2:]))
        for tag, r, bucket in (("A_hit", ra, A), ("B_nocache", rb, B), ("C_short", rc, C)):
            if r.get("ttft_ms"):
                bucket.append(r["ttft_ms"])
            print(json.dumps({"r": i + 1, "case": tag, **r}, ensure_ascii=False))
        time.sleep(0.3)

    print()
    print("SUMMARY_A_cached      " + json.dumps(stats(A), ensure_ascii=False))
    print("SUMMARY_B_nocache     " + json.dumps(stats(B), ensure_ascii=False))
    print("SUMMARY_C_short       " + json.dumps(stats(C), ensure_ascii=False))
    if A and B:
        print(json.dumps({
            "p50_hit_vs_nocache": [stats(A)["p50"], stats(B)["p50"]],
            "cache_saves_ms": stats(B)["p50"] - stats(A)["p50"],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
