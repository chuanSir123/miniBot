"""尾部顺序实验：时间戳放在尾部开头 vs 放在结尾，uncached 相差多少。

假设
----
vLLM 前缀缓存是"从第一个 token 起逐字相同"的前缀匹配。尾部块里如果**第一行就是
每分钟必变的时间戳**，那么时间一变，整个尾部（记忆 + 事件 + 检查点）全部作废，
每轮都要按完整体积重新 prefill。

如果成立：把慢变内容放前面、时间放最后，uncached 应该只剩最后一块（约 16–64 token）。

用法：
  python tools/tail_order_probe.py [session_id] [--rounds 6] [--pad 3000]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import SYSTEM_PROMPT_CACHE
from core.harness import HARNESS_TOOLS
from core.skill_manager import SkillManager
from tools.tail_cost_probe import Conn, load_history

PAD = (
    "近期事件（数据，不是指令）：\n"
    "用户在调试缓存命中问题，反复对比服务端日志与客户端耗时，"
    "希望确认尾部体积是否是 TTFT 的主因；期间读过 harness、compaction、context_builder 三个文件，"
    "并记录过若干轮 token 增量。\n"
)


def build(session, brain, tail_text):
    hist, summary = load_history(session)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, _ = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names,
        skills=SkillManager().describe("", stable=True),
    )
    compactor = ContextCompactor(brain, None, session)
    compactor.summary = summary
    return compactor.align_and_assemble_messages(system, hist, tail_text)


def main():
    session = sys.argv[1] if len(sys.argv) > 1 else "web:main:ebce6ed7"
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 6
    pad_chars = int(sys.argv[sys.argv.index("--pad") + 1]) if "--pad" in sys.argv else 3000

    brain = VLMBrain()
    endpoint, key, model, _ = brain._settings()
    endpoint = endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"
    settings = brain._settings()
    conn = Conn(endpoint, key)

    body_text = (PAD * (pad_chars // len(PAD) + 1))[:pad_chars]
    rows = []
    # 分组而不是交替：两组尾部开头不同，交替会让每轮互相打穿，测不出顺序的影响。
    # 每组第一轮天然是 warm（组间切换必然 miss 一次），从第二轮起才是可比数据。
    half = max(2, rounds // 2)
    plan = ["X_time_first"] * half + ["Y_time_last"] * half
    minute = 0
    for i, phase in enumerate(plan):
        minute += 1
        stamp = f"本地时间: 2026-10-30T{10 + minute // 60:02d}:{minute % 60:02d}"
        if phase == "X_time_first":
            tail = f"[运行时数据，非新指令]\n<runtime_context>\n{stamp}\n{body_text}\n</runtime_context>"
        else:
            tail = f"[运行时数据，非新指令]\n<runtime_context>\n{body_text}\n{stamp}\n</runtime_context>"
        msgs = build(session, brain, tail)
        payload = brain._build_payload(
            brain._prepare_messages(msgs), stream=True, tools=HARNESS_TOOLS, settings=settings
        )
        payload["max_tokens"] = 16
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        res = conn.post(body)
        u = res["usage"]
        det = u.get("prompt_tokens_details") or {}
        pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
        row = {
            "round": i + 1, "phase": phase, "tail_chars": len(tail),
            "prompt_tokens": pt, "cached": cached,
            "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
            **{k: res[k] for k in ("upload_ms", "server_ms", "first_delta_ms", "total_ttft_ms")},
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(0.6)

    Path("docs/tail_order_probe.json").write_text(
        json.dumps({"session": session, "model": model, "pad": pad_chars, "rows": rows},
                   ensure_ascii=False, indent=1)
    )


if __name__ == "__main__":
    main()
