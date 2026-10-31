"""上下文增长压测：从真实历史出发，逐轮追加内容直到 prompt_tokens 达到目标值，
记录每一轮的 TTFT / cached_tokens / 命中率，观察前缀缓存在长上下文下的表现。

追加方式是**只在尾部增长**（append-only），这正是生产里多轮对话的形状；
每轮同时重发一次完全相同的 payload，用来区分「新内容必须 prefill」和
「vLLM 块对齐导致的结构性不可缓存」。

用法:
  python tools/cache_growth_probe.py [session_id] [--target 50000] [--step 2000]
"""
import json
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
from core.text_budget import estimate_text_tokens

from real_cache_probe import load_history, post

# 用来拼装"看起来像真实工作记录"的素材，避免大段重复文本影响分块。
TOPICS = [
    ("media_store", "媒体引用与去重", "MediaStore.add 以 sha256(digest)+message_id 作为唯一键"),
    ("browser_verifier", "无头浏览器核验", "capture_browser_or_screen 支持多帧采样并合成 GIF"),
    ("compaction", "上下文压缩", "对齐装配时按 covered_until_id 截断，摘要放在 system 之后"),
    ("skill_manager", "技能注册表", "describe(stable=True) 按名称排序，保证跨轮渲染一致"),
    ("task_manager", "后台任务", "wait_task 轮询后台 shell，超时后返回部分输出"),
    ("network", "出网策略", "direct_opener 绕过系统代理直连推理服务"),
    ("checkpoint", "断点回退", "消息回退会把 id 之后的行从 messages_v2 物理删除"),
    ("prompt_cache", "前缀缓存", "vLLM 以 16 token 为块做前缀哈希匹配，块内变化即整块失效"),
]

CODE_TEMPLATE = """def {fn}(payload, *, strict=False):
    \"\"\"{doc}\"\"\"
    if not payload:
        raise ValueError("empty payload")
    result = {{}}
    for key, value in payload.items():
        if strict and value is None:
            continue
        result[key] = transform(value, index={idx})
    return result
"""


def make_chunk(seed, approx_tokens):
    """生成一段约 approx_tokens 的真实感内容（中文说明 + 代码 + 观测记录）。

    注意：判断长度时必须把已生成正文算进去，否则循环永远不收敛。
    """
    name, title, note = TOPICS[seed % len(TOPICS)]
    parts = [f"### {seed:03d} {name} / {title}", note + f"（第 {seed} 次观测）"]
    body = []
    i = 0
    while True:
        text = "\n\n".join(parts + body)
        if estimate_text_tokens({"messages": [{"role": "user", "content": text}]}) >= approx_tokens:
            break
        fn = f"{name}_handler_{seed}_{i}"
        body.append(CODE_TEMPLATE.format(fn=fn, doc=f"{title} 的第 {i} 个分支处理", idx=seed * 100 + i))
        if i % 3 == 2:
            body.append(
                f"观测记录 {seed}-{i}：本轮 {name} 路径耗时 {12 + (seed * 7 + i * 3) % 40}ms，"
                f"命中缓存块 {40 + (seed + i) % 120} 个，未命中 {3 + i % 5} 块，"
                f"KV 占用 {(18 + (seed + i) % 30)}%。"
            )
        i += 1
        if i > 2000:  # 兜底，绝不无限增长
            break
    return "\n\n".join(parts + body)


def stat(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    return {
        "n": n,
        "min": vals[0],
        "p50": vals[n // 2],
        "p90": vals[min(n - 1, int(n * 0.9))],
        "max": vals[-1],
        "mean": round(sum(vals) / n, 1),
    }


def usage_of(res):
    u = res.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    pt = u.get("prompt_tokens")
    cached = det.get("cached_tokens")
    return pt, cached, det.get("created_cache_tokens")


def baseline_ttft(endpoint, key, model, rounds=2):
    """极小 prompt 的 TTFT，用来标定"服务端此刻的基础延迟"。

    缓存再满，TTFT 也不可能低于这个值；把它单独打出来，才不会被误读成
    "大上下文命中后依然要好几秒"。
    """
    vals = []
    for _ in range(rounds):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "说三个字"}],
            "max_tokens": 8, "stream": True, "temperature": 0,
        }
        res = post(endpoint, key, payload, timeout=120)
        if res.get("ttft_ms") and not res.get("err"):
            vals.append(res["ttft_ms"])
        time.sleep(0.3)
    return stat(vals)


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    target = 50000
    step = 2000
    if "--target" in sys.argv:
        target = int(sys.argv[sys.argv.index("--target") + 1])
    if "--step" in sys.argv:
        step = int(sys.argv[sys.argv.index("--step") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    hist, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names, skills=skills
    )
    compactor = ContextCompactor(brain, None, sid)
    compactor.summary = summary

    print(json.dumps({
        "session": sid, "model": model, "system_fp": fingerprint,
        "system_chars": len(system), "tools": len(HARNESS_TOOLS),
        "history_msgs": len(hist), "has_summary": bool(summary),
        "target": target, "step": step,
    }, ensure_ascii=False), flush=True)

    base_before = baseline_ttft(endpoint, key, model)
    print(json.dumps({"baseline_ttft_ms": base_before}, ensure_ascii=False), flush=True)
    print(" round  prompt  cached   miss   hit%   ttft_ms  miss/ttft   rep_ttft  rep_hit%", flush=True)

    rows = []
    growth = 0
    turn = 0
    while True:
        turn += 1
        runtime = render_runtime_context(scope="web:main")
        tail = render_tail_context(runtime, {"original_goal": "上下文增长压测", "status": "active", "turn": turn})
        msgs = compactor.align_and_assemble_messages(system, hist, tail)
        payload = brain._build_payload(
            brain._prepare_messages(msgs), stream=True, tools=HARNESS_TOOLS, settings=settings
        )
        payload["max_tokens"] = 16

        res = post(endpoint, key, payload)
        pt, cached, created = usage_of(res)
        if res.get("err"):
            print(json.dumps({"turn": turn, "err": res["err"]}, ensure_ascii=False), flush=True)
            break

        # 立刻重发一次完全相同的 payload：这一发没有任何新内容，
        # 它的 cached 就是上一发全部 prefill 完之后的真实可复用上限。
        rep = post(endpoint, key, payload)
        rpt, rcached, _ = usage_of(rep)

        miss = (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None
        hit = round(cached * 100 / pt, 1) if pt and isinstance(cached, int) else None
        rep_hit = round(rcached * 100 / rpt, 1) if rpt and isinstance(rcached, int) else None
        speed = round(miss / (res["ttft_ms"] / 1000)) if miss and res.get("ttft_ms") else None

        rows.append({
            "turn": turn, "prompt_tokens": pt, "cached_tokens": cached,
            "miss_tokens": miss, "hit_pct": hit, "ttft_ms": res.get("ttft_ms"),
            "wall_ms": res.get("wall_ms"), "prefill_tok_per_s": speed,
            "repeat_ttft_ms": rep.get("ttft_ms"), "repeat_hit_pct": rep_hit,
            "created_cache_tokens": created,
        })
        print(f"{turn:5d} {pt:7d} {cached or 0:7d} {miss or 0:6d} {hit or 0:5.1f}% "
              f"{res.get('ttft_ms') or 0:8d} {speed or 0:10d} {rep.get('ttft_ms') or 0:10d} "
              f"{rep_hit or 0:8.1f}%", flush=True)

        if pt and pt >= target:
            break
        # 追加一段内容 + 一条新的用户消息，保持 append-only
        chunk = make_chunk(turn, step)
        hist = hist + [
            {"role": "assistant", "content": chunk, "_id": 10**9 + turn * 2},
            {"role": "user", "content": f"继续，把 {TOPICS[turn % len(TOPICS)][0]} 这部分的第 {turn} 段也看一眼。",
             "_id": 10**9 + turn * 2 + 1},
        ]
        growth += 1
        time.sleep(0.4)

    ok = [r for r in rows if isinstance(r.get("prompt_tokens"), int)]
    base_after = baseline_ttft(endpoint, key, model)
    summary_block = {
        "rounds": len(ok),
        "final_prompt_tokens": ok[-1]["prompt_tokens"] if ok else None,
        "growth_rounds": growth,
        "baseline_ttft_ms_before": base_before,
        "baseline_ttft_ms_after": base_after,
        "ttft_ms": stat([r["ttft_ms"] for r in ok]),
        "repeat_ttft_ms": stat([r["repeat_ttft_ms"] for r in ok]),
        "hit_pct": stat([r["hit_pct"] for r in ok]),
        "repeat_hit_pct": stat([r["repeat_hit_pct"] for r in ok]),
        "miss_tokens": stat([r["miss_tokens"] for r in ok]),
        "prefill_tok_per_s": stat([r["prefill_tok_per_s"] for r in ok]),
    }
    print("\n=== 汇总 ===")
    print(json.dumps(summary_block, ensure_ascii=False, indent=1))

    out = Path("docs/cache_growth_probe.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"meta": {"session": sid, "target": target, "step": step,
                                        "system_fp": fingerprint},
                               "rounds": rows, "summary": summary_block},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
