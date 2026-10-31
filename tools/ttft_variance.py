"""分离「未命中前缀的 prefill 成本」与「网关/后端本身抖动」。

用法: python tools/ttft_variance.py <session_id> [--rounds N]
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]) if False else str(__import__("pathlib").Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from core.brain import VLMBrain
from core.harness import HARNESS_TOOLS
from core.network import direct_opener

from real_cache_probe import build_real_payload, load_history
from core.compaction import ContextCompactor
from core.harness import build_structured_system_prompt
from core.skill_manager import SkillManager


def post_verbose(endpoint, key, payload, timeout=220):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "User-Agent": "MiniBot-Harness/2.0"},
        method="POST")
    t0 = time.monotonic()
    usage, ttft = {}, None
    hdrs = {}
    try:
        with direct_opener().open(req, timeout=timeout) as r:
            hdrs = {k.lower(): v for k, v in r.headers.items()}
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
        return {"ttft_ms": ttft, "usage": usage, "err": None, "hdrs": hdrs}
    except Exception as exc:
        return {"ttft_ms": None, "usage": {}, "err": type(exc).__name__ + ": " + str(exc)[:120],
                "hdrs": hdrs}


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    rounds = 10
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    payload = brain._build_payload(
        brain._prepare_messages(build_real_payload(sid, brain)),
        stream=True, tools=HARNESS_TOOLS, settings=brain._settings())
    payload["max_tokens"] = 16

    print("=== 固定载荷重复 %d 次：看 TTFT 方差与缓存命中是否稳定 ===" % rounds, flush=True)
    ttfts = []
    for i in range(rounds):
        res = post_verbose(endpoint, key, payload)
        u = res.get("usage") or {}
        det = u.get("prompt_tokens_details") or {}
        pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
        unc = (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None
        if res.get("ttft_ms"):
            ttfts.append(res["ttft_ms"])
        print(json.dumps({
            "run": i + 1, "ttft_ms": res.get("ttft_ms"), "prompt_tokens": pt,
            "cached_tokens": cached, "uncached_tokens": unc,
            "created_cache_tokens": det.get("created_cache_tokens"),
            "cf_ray": res.get("hdrs", {}).get("cf-ray"),
            "server": res.get("hdrs", {}).get("server"),
            "err": res.get("err"),
        }, ensure_ascii=False), flush=True)
        time.sleep(0.6)

    if ttfts:
        s = sorted(ttfts)
        print(json.dumps({
            "summary": {"n": len(s), "min": s[0], "p50": s[len(s)//2], "max": s[-1],
                        "mean": round(sum(s)/len(s)),
                        "spread_max_over_min": round(s[-1]/max(s[0],1), 2)}
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
