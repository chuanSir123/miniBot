"""复刻生产对话模式：每轮追加新消息，验证前缀缓存是否持续命中。

同时交错 A/B max_tokens=16 / 32768，排除「后端负载随时间漂移」造成的假象。

用法: python tools/turn_incremental_probe.py <session_id> [--turns N]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.brain import VLMBrain
from core.harness import HARNESS_TOOLS, build_structured_system_prompt
from core.compaction import ContextCompactor
from core.network import direct_opener
from core.skill_manager import SkillManager

from real_cache_probe import load_history


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


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    turns = 4
    if "--turns" in sys.argv:
        turns = int(sys.argv[sys.argv.index("--turns") + 1])

    from core.context_builder import (
        SYSTEM_PROMPT_CACHE,
        render_runtime_context,
        render_tail_context,
    )

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    hist, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    system, fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope="web:main",
        channel="web",
        selected_tools=[t["function"]["name"] for t in HARNESS_TOOLS],
        skills=skills,
    )
    compactor = ContextCompactor(brain, None, sid)
    compactor.summary = summary

    settings = brain._settings()
    print(json.dumps({"session": sid, "turns": turns, "system_fp": fingerprint,
                      "history": len(hist), "has_summary": bool(summary)}, ensure_ascii=False), flush=True)
    prev_prompt = None
    for turn in range(turns):
        # 每轮：固定的冻结前缀 + 一段**必然变化**的尾部运行数据（复刻真实生产）
        runtime = render_runtime_context(now=1793300000 + turn * 60, scope="web:main")
        tail = render_tail_context(runtime, {"original_goal": "测量缓存", "status": "active", "turn": turn})
        msgs = compactor.align_and_assemble_messages(system, hist, tail)
        hist = hist + [{"role": "user", "content": f"第{turn + 1}轮追问：继续喵。"}]

        payload = brain._build_payload(
            brain._prepare_messages(msgs), stream=True,
            tools=HARNESS_TOOLS, settings=settings)
        # max_tokens 固定：在 16/32768 之间跳会让 draft 模型按新 shape 重捕获计算图，
        # TTFT 抖动会被误读成缓存没命中。
        payload["max_tokens"] = 16
        res = post(endpoint, key, payload)
        u = res.get("usage") or {}
        det = u.get("prompt_tokens_details") or {}
        pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
        miss = (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None
        print(json.dumps({
            "turn": turn + 1,
            "ttft_ms": res.get("ttft_ms"),
            "prompt_tokens": pt,
            "cached_tokens": cached,
            "miss_tokens": miss,
            "hit_pct": round(cached * 100 / pt, 1) if pt and cached else None,
            "prev_prompt_covered": (miss is not None and prev_prompt is not None
                                    and (pt - miss) >= prev_prompt),
            "created_cache_tokens": det.get("created_cache_tokens"),
            "err": res.get("err"),
        }, ensure_ascii=False), flush=True)
        prev_prompt = pt
        time.sleep(0.5)


if __name__ == "__main__":
    main()
