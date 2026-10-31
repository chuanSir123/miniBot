#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一锤定音: 证明"上报的 cached_tokens"是否等于 vLLM 真实命中的量。

核心对照:
  HEAD_MISS : 改 system 消息**头部**一个字符 -> 前缀 token 必然全变 -> 必然 0 命中 -> 全量 prefill
  TAIL_MISS : 只改**最后一条消息**尾部   -> 前缀 token 不变     -> 应当高命中 -> 只 prefill 尾部
  BASELINE  : 完全不改

若 TAIL_MISS 与 HEAD_MISS 的 TTFT 相近 -> 说明"命中"没有带来任何真实加速,
上报的 cached_tokens 是网关侧自己算的账, vLLM 实际仍在全量 prefill 47k token。

同时测角色结构假设 (用户怀疑):
  MIXED   : 当前角色 (system + user + assistant + tool)
  ALLUSER: 除 system 外全部改成 user
  NOTOOLS : 当前角色但不带 tools
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
        err = type(exc).__name__ + ": " + str(exc)[:120]
    pt = usage.get("prompt_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return {
        "ttft_ms": ttft,
        "prompt_tokens": pt,
        "cached_tokens": cached,
        "hit_pct": round(cached * 100.0 / pt, 1) if pt and cached else 0.0,
        "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
        "err": err,
    }


def to_all_user(msgs):
    """除 index 0 的 system 外, 全部改成 user; 去掉 tool_calls 以免模板出错。"""
    out = []
    for i, m in enumerate(msgs):
        m = copy.deepcopy(m)
        if i == 0 and m.get("role") == "system":
            out.append(m)
            continue
        m["role"] = "user"
        m.pop("tool_calls", None)
        m.pop("tool_call_id", None)
        m.pop("name", None)
        c = m.get("content")
        if not isinstance(c, str):
            m["content"] = json.dumps(c, ensure_ascii=False)
        out.append(m)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="web:main:0428eec0")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=16)
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    raw = brain._prepare_messages(build_real_payload(args.session, brain))

    def mk(msgs, tools=True, head_mutate=False, tail_mutate=False, mt=None):
        m = copy.deepcopy(msgs)
        if head_mutate:
            for x in m:
                if x.get("role") == "system":
                    x["content"] = "X" + x.get("content", "")
                    break
        if tail_mutate:
            for x in reversed(m):
                if x.get("role") == "user":
                    c = x.get("content")
                    x["content"] = (c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)) + " z"
                    break
        p = brain._build_payload(
            m, stream=True, tools=(HARNESS_TOOLS if tools else None), settings=settings
        )
        p["max_tokens"] = mt or args.max_tokens
        return p

    variants = {
        "BASELINE":      mk(raw),
        "HEAD_MISS":     mk(raw, head_mutate=True),
        "TAIL_MISS":     mk(raw, tail_mutate=True),
        "ALLUSER":       mk(to_all_user(raw)),
        "NOTOOLS":       mk(raw, tools=False),
        "ALLUSER_NOTOOLS": mk(to_all_user(raw), tools=False),
    }

    print(json.dumps({
        "model": model, "session": args.session, "msgs": len(raw),
        "max_tokens": args.max_tokens,
    }, ensure_ascii=False), flush=True)

    results = {k: [] for k in variants}
    for rep in range(args.reps):
        print(f"--- rep {rep + 1} ---", flush=True)
        for name, payload in variants.items():
            r = call(endpoint, key, payload)
            if r.get("ttft_ms"):
                results[name].append(r["ttft_ms"])
            print(json.dumps({"rep": rep + 1, "case": name, **r}, ensure_ascii=False), flush=True)
            time.sleep(0.4)

    print("\n=== SUMMARY ===", flush=True)
    for name, vals in results.items():
        if not vals:
            print(json.dumps({"case": name, "n": 0}, ensure_ascii=False), flush=True)
            continue
        s = sorted(vals)
        print(json.dumps({
            "case": name, "n": len(s), "min": s[0],
            "p50": int(statistics.median(s)), "max": s[-1],
            "mean": round(sum(s) / len(s)),
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
