"""受控对照：把上下文固定成若干长度，每个长度连发多次**完全相同**的 payload。

目的：把"上下文总长度"和"每轮新增内容"两个因素拆开。
- 全部命中（第 2 发起）时，TTFT 只与总长度有关；
- 若同一长度下 TTFT 随重复次数持续下降，则是缓存预热未完成；
- 若同一长度稳态 TTFT 随总长度上升，则是 vLLM 侧与长度相关的固定成本。

为消除服务端负载随时间漂移，长度按 升序 → 降序 各跑一遍，取两遍的中位数。

用法: python tools/cache_ttft_by_length.py [session_id] [--rounds 5]
"""
import json
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import (
    SYSTEM_PROMPT_CACHE,
    render_runtime_context,
    render_tail_context,
)
from core.harness import HARNESS_TOOLS
from core.skill_manager import SkillManager

from cache_growth_probe import make_chunk, usage_of
from real_cache_probe import load_history, post

SIZES = [12000, 30000, 50000]


def build(sid, size, brain, settings, system, compactor, hist_base):
    """按目标 token 量拼历史：先按需追加 chunk，再走真实装配。"""
    hist = list(hist_base)
    turn = 0
    tail = render_tail_context(render_runtime_context(scope="web:main"),
                               {"original_goal": "长度对照", "status": "active"})
    msgs = compactor.align_and_assemble_messages(system, hist, tail)
    payload = brain._build_payload(
        brain._prepare_messages(msgs), stream=True, tools=HARNESS_TOOLS, settings=settings)
    payload["max_tokens"] = 16
    # 用字符数快速逼近目标，最后精确到请求实际返回的 prompt_tokens 为准
    while True:
        msgs = compactor.align_and_assemble_messages(system, hist, tail)
        payload = brain._build_payload(
            brain._prepare_messages(msgs), stream=True, tools=HARNESS_TOOLS, settings=settings)
        payload["max_tokens"] = 16
        est = sum(len(str(m.get("content"))) for m in msgs) // 3
        if est >= size or turn > 60:
            return payload
        turn += 1
        hist = hist + [{"role": "assistant", "content": make_chunk(turn, 2000),
                        "_id": 10**9 + turn * 2},
                       {"role": "user", "content": f"继续第 {turn} 段。",
                        "_id": 10**9 + turn * 2 + 1}]


def measure(endpoint, key, payload, rounds):
    out = []
    for _ in range(rounds):
        res = post(endpoint, key, payload)
        pt, cached, _ = usage_of(res)
        out.append({"ttft_ms": res.get("ttft_ms"), "prompt_tokens": pt,
                    "cached_tokens": cached,
                    "hit_pct": round(cached * 100 / pt, 1) if pt and cached else None,
                    "err": res.get("err")})
        time.sleep(0.5)
    return out


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:36b659ac"
    rounds = 5
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    hist_base, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fp = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names, skills=skills)
    compactor = ContextCompactor(brain, None, sid)
    compactor.summary = summary

    payloads = {s: build(sid, s, brain, settings, system, compactor, hist_base) for s in SIZES}
    results = {s: [] for s in SIZES}

    for order in (SIZES, list(reversed(SIZES))):
        for size in order:
            rows = measure(endpoint, key, payloads[size], rounds)
            results[size].extend(rows)
            print(json.dumps({"size": size, "pass": "asc" if order == SIZES else "desc",
                              "rows": rows}, ensure_ascii=False), flush=True)

    print("\n=== 稳态 TTFT（同长度连发，第 2 发起全部命中）===")
    print(f"{'目标':>7s} {'实际prompt':>10s} {'稳态hit%':>8s} {'TTFT中位':>9s} {'TTFT最小':>9s} {'首发TTFT':>9s}")
    table = []
    for size in SIZES:
        rows = [r for r in results[size] if r.get("ttft_ms") and not r.get("err")]
        if not rows:
            continue
        steady = rows[len(rows) // 2:]  # 两遍合起来，取后一半当稳态
        table.append({
            "target": size,
            "prompt_tokens": int(st.median([r["prompt_tokens"] for r in rows])),
            "first_ttft_ms": rows[0]["ttft_ms"],
            "steady_hit_pct": round(st.median([r["hit_pct"] for r in steady if r["hit_pct"]]), 1),
            "steady_ttft_ms": int(st.median([r["ttft_ms"] for r in steady])),
            "min_ttft_ms": min(r["ttft_ms"] for r in rows),
        })
        t = table[-1]
        print(f"{t['target']:7d} {t['prompt_tokens']:10d} {t['steady_hit_pct']:8.1f} "
              f"{t['steady_ttft_ms']:9d} {t['min_ttft_ms']:9d} {t['first_ttft_ms']:9d}")

    if len(table) >= 2:
        a, b = table[0], table[-1]
        d_tok = b["prompt_tokens"] - a["prompt_tokens"]
        d_ms = b["steady_ttft_ms"] - a["steady_ttft_ms"]
        print(f"\n从 {a['prompt_tokens']} 到 {b['prompt_tokens']} tok（+{d_tok}），"
              f"稳态 TTFT {a['steady_ttft_ms']} → {b['steady_ttft_ms']}ms（{d_ms:+d}ms）")
        print(f"即每 1000 tok 上下文约增加 {d_ms / d_tok * 1000:.1f}ms")

    Path("docs/cache_ttft_by_length.json").write_text(
        json.dumps({"table": table, "raw": {str(k): v for k, v in results.items()}},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved: docs/cache_ttft_by_length.json")


if __name__ == "__main__":
    main()
