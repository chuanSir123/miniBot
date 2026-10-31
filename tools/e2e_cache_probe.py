"""用真实 Memory + 真实 harness 组装路径，端到端验证前缀缓存。

与 real_cache_probe 的区别：这里走真实的 Memory.get_history()、
ContextCompactor(history_for_model + summary)、以及 harness 的 assemble() 等价逻辑，
能覆盖「历史图片被改写」「摘要插在 system 之后」等真实副作用。

用法: python tools/e2e_cache_probe.py <session_id> [--rounds N]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.harness import HARNESS_TOOLS, build_structured_system_prompt
from core.memory import MiniMemory as Memory
from core.network import direct_opener
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens

from real_cache_probe import post, report


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    rounds = 4
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    mem = Memory()
    history, persisted = mem.get_history(sid)
    print(json.dumps({
        "session": sid, "history_msgs": len(history),
        "has_summary": bool(persisted),
        "covered_until_id": persisted.get("covered_until_id") if persisted else None,
    }, ensure_ascii=False), flush=True)

    catalog = json.dumps(SkillManager().describe("", stable=True), ensure_ascii=False)
    system = build_structured_system_prompt(
        scope="web:main", custom_sections={"available_skills": catalog})

    compactor = ContextCompactor(brain, mem, sid)
    compactor.summary = persisted

    # 生产 assemble() 等价物：volatile 尾巴每轮都变（时间戳）
    for turn in range(rounds):
        live = (
            f"<dynamic_context>\n本地时间: 2026-10-29T20:0{turn}:00+08:00; Unix秒: {2000000 + turn}\n"
            f"项目根目录: D:/github/miniBot; 当前Harness工作空间: D:/github/miniBot/data/workspace/main\n"
            f"</dynamic_context>\n\n<task_checkpoint>\n"
            f'{{"original_goal": "验证缓存", "status": "active", "last_tools": [], "artifacts": []}}\n'
            f"</task_checkpoint>"
        )
        msgs = compactor.align_and_assemble_messages(system, history, live)
        formatted = brain._prepare_messages(msgs)
        payload = brain._build_payload(
            formatted, stream=True, tools=HARNESS_TOOLS, settings=brain._settings())
        payload["max_tokens"] = 32768
        est = estimate_text_tokens({"messages": formatted, "tools": HARNESS_TOOLS})
        res = post(endpoint, key, payload)
        u = res.get("usage") or {}
        det = u.get("prompt_tokens_details") or {}
        pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
        print(json.dumps({
            "turn": turn + 1, "est_tokens": est, "prompt_tokens": pt,
            "cached_tokens": cached,
            "hit_pct": round(cached * 100 / pt, 1) if pt and cached else None,
            "created_cache_tokens": det.get("created_cache_tokens"),
            "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
            "ttft_ms": res.get("ttft_ms"), "err": res.get("err"),
        }, ensure_ascii=False), flush=True)
        time.sleep(0.8)


if __name__ == "__main__":
    main()
