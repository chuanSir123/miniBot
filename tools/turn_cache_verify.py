"""用**生产装配路径**验证重构后的缓存行为。

不自己拼消息：直接调 harness 用的那几个函数（SYSTEM_PROMPT_CACHE、
memory.recall_for_turn + memory_entry、ContextCompactor.align_and_assemble_messages、
render_tail_context），确保测的就是线上那一份字节。

模拟连续三轮，每轮只追加少量真实增量（一条记忆条目 / 一条 user / 一条 assistant），
打印 prompt/cached/uncached 与 TTFT。

    python tools/turn_cache_verify.py web:main:ebce6ed7 --rounds 4
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import (
    SYSTEM_PROMPT_CACHE,
    compact_checkpoint,
    render_clock,
    render_runtime_context,
    render_tail_context,
)
from core.harness import HARNESS_TOOLS
from core.memory import MiniMemory
from core.transcript import KIND_MEMORY, last_meta, memory_entry


def assemble(mem, sid, history, tail_clock, task_state, recall):
    """完全复刻 harness._run 里的装配顺序。"""
    compactor = ContextCompactor(None, mem, sid)
    runtime_context = render_runtime_context(scope=sid.split(":")[0] or "main",
                                             goals="", include_clock=False)
    pinned = compact_checkpoint(task_state)
    live_context = render_tail_context(runtime_context, pinned, events="", clock=tail_clock)
    system, fp = SYSTEM_PROMPT_CACHE.render(
        scope=sid,
        channel="web",
        selected_tools=[t["function"]["name"] for t in HARNESS_TOOLS],
        skills="",
    )
    messages = compactor.align_and_assemble_messages(system, history, live_context)
    return messages, fp, len(live_context)


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:ebce6ed7"
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 4

    mem = MiniMemory()
    brain = VLMBrain()
    _, key, model, _ = brain._settings()
    settings = brain._settings()

    history, summary = mem.get_history(sid)
    print(f"会话 {sid}: 历史 {len(history)} 条, 已有摘要={bool(summary)}", flush=True)

    recall = mem.recall_for_turn(scope="main", query="继续")
    task_state = mem.get_task_state(sid) or {}
    rows = []
    base_now = time.time()

    identical = "--identical" in sys.argv
    for i in range(rounds):
        # 每轮只追加真实增量：第 1 轮加记忆条目（若内容有变），之后各加一轮对话。
        if i == 0 and recall:
            entry = memory_entry(recall, last_meta(history, KIND_MEMORY))
            if entry:
                history = history + [entry]
        if i > 0 and not identical:
            history = history + [
                {"role": "assistant", "content": f"第 {i} 轮：已完成上一步并说明结果。"},
                {"role": "user", "content": f"继续，做第 {i + 1} 步。"},
            ]
        # --identical：时钟和历史都不动，payload 逐字节相同，用来量“结构性重算”本身有多大。
        tail_clock = render_clock(now=base_now + (0 if identical else i * 60))
        messages, fp, tail_chars = assemble(mem, sid, history, tail_clock, task_state, recall)

        payload = brain._build_payload(
            brain._prepare_messages(messages), stream=True, tools=HARNESS_TOOLS,
            settings=settings,
        )
        payload["max_tokens"] = 16
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        base_url = settings[0]
        endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        req = urllib.request.Request(
            endpoint, data=body,
            headers={
                "Authorization": f"Bearer {settings[1]}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-VLM/2.0",
            },
            method="POST",
        )

        t0 = time.monotonic()
        resp = brain._open_request(req, timeout=180)
        ttft = None
        usage = {}
        text = []
        while True:
            line = resp.readline(1 << 20)
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            chunk = line[5:].strip()
            if chunk == b"[DONE]":
                break
            try:
                ev = json.loads(chunk)
            except ValueError:
                break
            if ev.get("usage"):
                usage = ev["usage"]
            for choice in ev.get("choices", []):
                delta = choice.get("delta") or {}
                if ttft is None and (delta.get("content") or delta.get("reasoning_content")):
                    ttft = time.monotonic()
                if delta.get("content"):
                    text.append(delta["content"])
        total_ms = round((time.monotonic() - t0) * 1000)

        det = usage.get("prompt_tokens_details") or {}
        pt, cached = usage.get("prompt_tokens"), det.get("cached_tokens")
        row = {
            "round": i + 1, "body_kb": round(len(body) / 1024, 1),
            "messages": len(messages), "system_fp": fp, "tail_chars": tail_chars,
            "prompt_tokens": pt, "cached": cached,
            "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
            "ttft_ms": round((ttft - t0) * 1000) if ttft else None,
            "total_ms": total_ms,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
