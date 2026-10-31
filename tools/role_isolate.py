#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分离两个变量: tool_calls 的丢失 vs role 的改写。

用户怀疑是 role 的问题, 但 ALLUSER 变体同时丢掉了 tool_calls —— 两者混在一起。
本实验拆开:
  V0 BASELINE          : 原样 (system + user + assistant(含 tool_calls) + tool)
  V1 DROP_TOOLCALLS    : 只丢 tool_calls, 角色不变        -> 隔离 tool_calls 的贡献
  V2 TOOL_TO_USER      : 只把 tool 角色改成 user, 保留 tool_calls -> 隔离 role 的贡献
  V3 ALLUSER_DROP_TC   : 既改 role 又丢 tool_calls (用户猜的那个)
  V4 TOOL_AS_TEXT      : tool 角色改成 user 且把内容包成 "工具 X 返回: ..." 文本
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


def v_baseline(msgs):
    return copy.deepcopy(msgs)


def v_drop_tc(msgs):
    out = copy.deepcopy(msgs)
    for m in out:
        m.pop("tool_calls", None)
    return out


def v_tool_to_user(msgs):
    """只改 role, 保留 tool_calls。"""
    out = copy.deepcopy(msgs)
    for m in out:
        if m.get("role") == "tool":
            m["role"] = "user"
            m.pop("tool_call_id", None)
            m.pop("name", None)
    return out


def v_alluser_drop(msgs):
    out = copy.deepcopy(msgs)
    for i, m in enumerate(out):
        if i == 0 and m.get("role") == "system":
            continue
        m["role"] = "user"
        m.pop("tool_calls", None)
        m.pop("tool_call_id", None)
        m.pop("name", None)
        c = m.get("content")
        if not isinstance(c, str):
            m["content"] = json.dumps(c, ensure_ascii=False)
    return out


def v_tool_as_text(msgs):
    """tool 角色 -> user, 内容包成可读文本; assistant 的 tool_calls 保留但改名到 content。"""
    out = copy.deepcopy(msgs)
    for m in out:
        if m.get("role") == "tool":
            m["role"] = "user"
            m.pop("tool_call_id", None)
            m.pop("name", None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="web:main:0428eec0")
    ap.add_argument("--reps", type=int, default=2)
    args = ap.parse_args()

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()
    raw = brain._prepare_messages(build_real_payload(args.session, brain))

    builders = {
        "V0_BASELINE": v_baseline,
        "V1_DROP_TC": v_drop_tc,
        "V2_TOOL_TO_USER": v_tool_to_user,
        "V3_ALLUSER_DROP_TC": v_alluser_drop,
        "V4_TOOL_AS_TEXT": v_tool_as_text,
    }

    print(json.dumps({"model": model, "msgs": len(raw)}, ensure_ascii=False), flush=True)
    res = {k: [] for k in builders}
    for rep in range(args.reps):
        print(f"--- rep {rep + 1} ---", flush=True)
        for name, fn in builders.items():
            msgs = fn(raw)
            payload = brain._build_payload(
                msgs, stream=True, tools=HARNESS_TOOLS, settings=settings)
            payload["max_tokens"] = 16
            r = call(endpoint, key, payload)
            if r.get("ttft_ms"):
                res[name].append(r["ttft_ms"])
            print(json.dumps({"rep": rep + 1, "case": name,
                              "json_chars": len(json.dumps(msgs, ensure_ascii=False)), **r},
                             ensure_ascii=False), flush=True)
            time.sleep(0.4)

    print("\n=== SUMMARY ===", flush=True)
    for name, vals in res.items():
        if not vals:
            print(json.dumps({"case": name, "n": 0}, ensure_ascii=False), flush=True)
            continue
        s = sorted(vals)
        print(json.dumps({"case": name, "n": len(s), "min": s[0],
                          "p50": int(statistics.median(s)), "max": s[-1]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
