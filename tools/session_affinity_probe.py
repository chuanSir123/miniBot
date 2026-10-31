"""验证「会话亲和路由」能否降低 TTFT 抖动。

对照:
  base    - 现状（无 prompt_cache_key / 无亲和头）
  keyed   - 带 prompt_cache_key + x-client-request-id + x-session-affinity

用法: python tools/session_affinity_probe.py <session_id> [--rounds N]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.brain import VLMBrain
from core.harness import HARNESS_TOOLS
from core.network import direct_opener

from real_cache_probe import build_real_payload


def post(endpoint, key, payload, extra_headers=None, timeout=220):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "User-Agent": "MiniBot-Harness/2.0"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    t0 = time.monotonic()
    usage, ttft = {}, None
    try:
        with direct_opener().open(req, timeout=timeout) as r:
            while True:
                line = r.readline(1 << 20)
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
                for c in ev.get("choices", []):
                    d = c.get("delta") or {}
                    if ttft is None and (d.get("content") or d.get("reasoning_content")):
                        ttft = round((time.monotonic() - t0) * 1000)
        return {"ttft_ms": ttft, "usage": usage, "err": None}
    except Exception as exc:
        return {"ttft_ms": None, "usage": {},
                "err": type(exc).__name__ + ": " + str(exc)[:120]}


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    rounds = 8
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    payload = brain._build_payload(
        brain._prepare_messages(build_real_payload(sid, brain)),
        stream=True, tools=HARNESS_TOOLS, settings=brain._settings())
    payload["max_tokens"] = 16

    scenarios = [
        ("base", None),
        ("keyed", {"prompt_cache_key": "minibot-" + sid.replace(":", "-"),
                   "session_id": "minibot-" + sid.replace(":", "-")}),
    ]
    headers_for = {
        "base": {},
        "keyed": {"x-client-request-id": "minibot-" + sid.replace(":", "-"),
                  "x-session-affinity": "minibot-" + sid.replace(":", "-")},
    }

    for label, body_extra in scenarios:
        p = dict(payload)
        if body_extra:
            p.update(body_extra)
        ttfts = []
        print(f"--- {label} ---", flush=True)
        for i in range(rounds):
            res = post(endpoint, key, p, headers_for[label])
            u = res.get("usage") or {}
            det = u.get("prompt_tokens_details") or {}
            pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
            if res.get("ttft_ms"):
                ttfts.append(res["ttft_ms"])
            print(json.dumps({
                "case": label, "run": i + 1, "ttft_ms": res.get("ttft_ms"),
                "prompt_tokens": pt, "cached_tokens": cached,
                "created_cache_tokens": det.get("created_cache_tokens"),
                "err": res.get("err"),
            }, ensure_ascii=False), flush=True)
            time.sleep(0.6)
        if ttfts:
            s = sorted(ttfts)
            print(json.dumps({"case": label, "summary": {
                "n": len(s), "min": s[0], "p50": s[len(s)//2], "max": s[-1],
                "mean": round(sum(s)/len(s))}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
