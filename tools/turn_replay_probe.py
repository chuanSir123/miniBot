"""按 harness 的真实顺序逐轮重放一个会话，测每轮的前缀命中与 TTFT。

与 real_cache_probe 的区别：那个只重放"最后一轮"，这个重放**连续多轮**，
因此能看出「第 N 轮的缓存有多少被第 N+1 轮复用」——线上 TTFT 忽快忽慢时，
缺的就是这个逐轮视图。

用法:
  python tools/turn_replay_probe.py <session_id> [--max-tokens 16]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import (
    SYSTEM_PROMPT_CACHE,
    compact_checkpoint,
    render_runtime_context,
    render_tail_context,
)
from core.harness import HARNESS_TOOLS
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens
from real_cache_probe import load_history, post


def load_task_state(sid):
    """取该会话的 task_state —— 它会进每轮尾巴，是 TTFT 里最容易被忽略的一块。"""
    import sqlite3

    db = sqlite3.connect("file:data/memory.db?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT payload FROM task_states WHERE session_id=?", (sid,)
        ).fetchone()
    finally:
        db.close()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return {}


def char_len(msg):
    """消息在本轮新增的字符量（content + tool_calls 参数）。"""
    total = len(str(msg.get("content") or ""))
    for call in msg.get("tool_calls") or []:
        total += len((call.get("function") or {}).get("arguments") or "")
    return total


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:ebce6ed7"
    max_tokens = 16
    if "--max-tokens" in sys.argv:
        max_tokens = int(sys.argv[sys.argv.index("--max-tokens") + 1])

    brain = VLMBrain()
    settings = brain._settings()
    base, key, model, _cfg = settings
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    hist, summary = load_history(sid)
    state = load_task_state(sid)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names,
        skills=SkillManager().describe("", stable=True),
    )
    compactor = ContextCompactor(brain, None, sid)
    compactor.summary = summary

    print(json.dumps({
        "session": sid, "history_msgs": len(hist), "model": model,
        "system_fp": fingerprint, "system_chars": len(system),
        "tools": len(HARNESS_TOOLS),
        "roles": [m.get("role") for m in hist],
    }, ensure_ascii=False), flush=True)

    # 一轮请求发生在"最后一条是 user 或 tool"之后；assistant 带 tool_calls 时不发请求。
    turns = [i for i, m in enumerate(hist) if m.get("role") in ("user", "tool")]
    header = (f"{'turn':>4s} {'boundary':22s} {'msgs':>4s} {'new_chars':>9s} "
              f"{'prompt':>7s} {'cached':>7s} {'miss':>6s} {'hit%':>6s} {'d_prompt':>8s} {'ttft_ms':>7s}")
    print(header, flush=True)

    prev_prompt = None
    prev_sum = 0
    rows = []
    for n, idx in enumerate(turns, 1):
        part = hist[: idx + 1]
        # 本轮新增的字符量 = 上次边界之后的所有消息（含本轮这条）
        new_chars = sum(char_len(m) for m in hist[prev_sum: idx + 1])
        prev_sum = idx + 1

        tail = render_tail_context(
            render_runtime_context(scope="web:main"),
            compact_checkpoint(state) if state else {"status": "running", "turn": n},
        )
        msgs = compactor.align_and_assemble_messages(system, part, tail)
        payload = brain._build_payload(
            brain._prepare_messages(msgs), stream=True, tools=HARNESS_TOOLS, settings=settings
        )
        payload["max_tokens"] = max_tokens
        res = post(endpoint, key, payload)
        usage = res.get("usage") or {}
        det = usage.get("prompt_tokens_details") or {}
        pt = usage.get("prompt_tokens")
        cached = det.get("cached_tokens")
        if res.get("err"):
            print(f"{n:4d} ERROR {res['err'][:120]}", flush=True)
            continue
        dp = (pt - prev_prompt) if isinstance(pt, int) and prev_prompt else None
        hit = round(cached * 100 / pt, 1) if isinstance(cached, int) and pt else None
        line = (f"{n:4d} {hist[idx].get('role') + ':' + str(hist[idx].get('_id')):22s} "
                f"{len(msgs):4d} {new_chars:9d} {pt if pt else 0:7d} {cached if cached else 0:7d} "
                f"{(pt - cached) if isinstance(pt, int) and isinstance(cached, int) else 0:6d} "
                f"{hit if hit is not None else 0:6.1f} {dp if dp is not None else 0:8d} "
                f"{res.get('ttft_ms') or 0:7d}")
        print(line, flush=True)
        rows.append({"turn": n, "prompt": pt, "cached": cached, "miss": (pt or 0) - (cached or 0),
                     "new_chars": new_chars, "ttft_ms": res.get("ttft_ms"),
                     "est_new_tokens": estimate_text_tokens(
                         {"messages": hist[prev_sum - 1: idx + 1]})})
        prev_prompt = pt

    out = Path("docs/turn_replay.json")
    out.write_text(json.dumps({"session": sid, "system_fp": fingerprint, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved ->", out, flush=True)


if __name__ == "__main__":
    main()
