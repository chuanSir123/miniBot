#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""区分 TTFT 瓶颈：上下文长度（注意力成本） vs 上游排队（噪声）。

做法:
  A. 长度缩放: 取真实会话历史的前 N 条消息, 每种长度先预热缓存, 再测 TTFT。
  B. 尾延迟分布: 同一 47k 已缓存载荷重复 N 次, 看 TTFT 分布形状。
  C. 同网关换模型: 47k 载荷在另一个模型上的 TTFT。
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


def call(endpoint, key, payload, timeout=240):
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


def stats(vals):
    if not vals:
        return None
    s = sorted(vals)
    return {
        "n": len(s), "min": s[0], "p50": int(statistics.median(s)),
        "p90": s[int(len(s) * 0.9) - 1] if len(s) > 1 else s[0],
        "max": s[-1], "mean": round(sum(s) / len(s)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="web:main:0428eec0")
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--tail-reps", type=int, default=20)
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    msgs = brain._prepare_messages(build_real_payload(args.session, brain))
    print(json.dumps({"session": args.session, "model": model, "total_msgs": len(msgs)}, ensure_ascii=False))

    def mk(sub_msgs, mdl=None):
        p = brain._build_payload(sub_msgs, stream=True, tools=HARNESS_TOOLS, settings=settings)
        p["max_tokens"] = 16
        if mdl:
            p["model"] = mdl
        return p

    # ---------- A. 长度缩放 ----------
    print("\n=== [A] 长度缩放: 同一真实历史截断到不同长度, 每档先预热再测 ===")
    buckets = []
    step = max(1, len(msgs) // 5)
    for n in range(step, len(msgs) + 1, step):
        buckets.append(n)
    if buckets[-1] != len(msgs):
        buckets.append(len(msgs))

    scale_rows = []
    for n in buckets:
        sub = msgs[:n]
        p = mk(sub)
        # 预热（建立缓存）
        for _ in range(2):
            call(endpoint, key, p)
        vals = []
        meta = None
        for _ in range(args.reps):
            r = call(endpoint, key, p)
            if r.get("ttft_ms"):
                vals.append(r["ttft_ms"])
            meta = r
            time.sleep(0.3)
        st = stats(vals)
        row = {
            "msgs": n, "prompt_tokens": meta.get("prompt_tokens"),
            "hit_pct": meta.get("hit_pct"), **st,
        }
        scale_rows.append(row)
        print(json.dumps(row, ensure_ascii=False))

    print("\n--- 缩放斜率 (TTFT vs prompt_tokens) ---")
    for row in scale_rows:
        print(json.dumps({
            "prompt_tokens": row["prompt_tokens"],
            "p50": row["p50"], "max": row["max"], "hit_pct": row["hit_pct"],
        }, ensure_ascii=False))

    # ---------- B. 尾延迟分布 ----------
    print("\n=== [B] 尾延迟分布: 47k 已缓存载荷重复 %d 次 ===" % args.tail_reps)
    full = mk(msgs)
    vals = []
    for i in range(args.tail_reps):
        r = call(endpoint, key, full)
        if r.get("ttft_ms"):
            vals.append(r["ttft_ms"])
        print(json.dumps({"i": i + 1, "ttft_ms": r.get("ttft_ms"), "hit_pct": r.get("hit_pct"), "err": r.get("err")}))
        time.sleep(0.25)
    print("DIST " + json.dumps(stats(vals), ensure_ascii=False))

    # ---------- C. 同网关换模型 ----------
    print("\n=== [C] 同网关换模型, 47k 载荷 ===")
    for mdl in ["tsinghua-sigs-robot-lab/VeriLoop-E2"]:
        p = mk(msgs, mdl)
        for _ in range(2):
            call(endpoint, key, p)
        vals = []
        for _ in range(3):
            r = call(endpoint, key, p)
            if r.get("ttft_ms"):
                vals.append(r["ttft_ms"])
            print(json.dumps({"model": mdl, **r}, ensure_ascii=False))
            time.sleep(0.3)
        print("MODEL " + json.dumps({"model": mdl, **stats(vals)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
