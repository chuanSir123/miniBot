"""在缓存状态相同的前提下，量化各因素对 TTFT 的贡献。

对照项:
  mt16 / mt32768  - 生产用 max_tokens=32768，探针用 16；看是否影响调度/排队
  tail_small/large- 缓存前缀相同、尾部未命中量不同；看 TTFT 是否随尾部线性上升

用法: python tools/ttft_attrib.py <session_id> [--rounds N]
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


def post(endpoint, key, payload, timeout=240):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "User-Agent": "MiniBot-Harness/2.0"},
        method="POST")
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


def run_case(label, endpoint, key, payload, rounds):
    ttfts = []
    for i in range(rounds):
        res = post(endpoint, key, payload)
        u = res.get("usage") or {}
        det = u.get("prompt_tokens_details") or {}
        pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
        if res.get("ttft_ms"):
            ttfts.append(res["ttft_ms"])
        print(json.dumps({
            "case": label, "run": i + 1, "ttft_ms": res.get("ttft_ms"),
            "prompt_tokens": pt, "cached_tokens": cached,
            "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
            "err": res.get("err"),
        }, ensure_ascii=False), flush=True)
        time.sleep(0.6)
    if ttfts:
        s = sorted(ttfts)
        print(json.dumps({"case": label, "summary": {
            "n": len(s), "min": s[0], "p50": s[len(s)//2], "max": s[-1],
            "mean": round(sum(s)/len(s))}}, ensure_ascii=False), flush=True)


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    rounds = 6
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    msgs = brain._prepare_messages(build_real_payload(sid, brain))
    settings = brain._settings()
    base_payload = brain._build_payload(msgs, stream=True, tools=HARNESS_TOOLS, settings=settings)

    p16 = dict(base_payload); p16["max_tokens"] = 16
    p32k = dict(base_payload); p32k["max_tokens"] = 32768

    run_case("mt16", endpoint, key, p16, rounds)
    run_case("mt32768", endpoint, key, p32k, rounds)


if __name__ == "__main__":
    main()
