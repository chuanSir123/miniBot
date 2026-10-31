"""对照实验：Jev 的 state 体积 vs 问题数量，各自对延迟的贡献。"""
import json
import time

from core.memory import MiniMemory
from core.jev import JevClient
from core.jev_budget import fit_state
import tools.jev_probe as P

memory = MiniMemory()
jev = JevClient()
state, ctx = P.build_state(memory, "web:main:7df17c7d", "local")
raw = json.dumps({"current": state, "context": ctx}, ensure_ascii=False)
fitted, steps = fit_state(raw)
print(f"state chars {len(raw)} -> {len(fitted)}  steps={steps}")

base_q = {
    "verification_mode": {"type": "choice", "options": ["skip", "task", "static", "dynamic"], "instructions": "判断核验类型"},
    "reply_readiness": {"type": "choice", "options": ["ready", "pending_work", "unsupported_claim"], "instructions": "判断回复就绪度"},
}

print("\n--- A. 固定 state，改变问题数 ---")
for label, n_cmd in [("0 命令问题", 0), ("8 命令问题", 8), ("37 命令问题", 37)]:
    qs = dict(base_q)
    for c in state["commands"][-n_cmd:]:
        qs[f"test_{c['index']}"] = {"type": "choice", "options": ["test_execution", "other"],
                                    "instructions": "判断该命令是否运行带功能断言的测试。"}
    started = time.monotonic()
    jev.decide(fitted, qs)
    elapsed = round((time.monotonic() - started) * 1000)
    err = jev.last_error[:60] or "-"
    print(f"  {label:12} questions={len(qs):3} total_ms={elapsed:5} err={err}")
    time.sleep(0.6)

print("\n--- B. 固定 2 个问题，改变 state 体积 ---")
for budget in (8000, 16000, 32000, 49152):
    chunk, _ = fit_state(raw, budget=budget)
    started = time.monotonic()
    jev.decide(chunk, base_q)
    elapsed = round((time.monotonic() - started) * 1000)
    err = jev.last_error[:60] or "-"
    print(f"  budget={budget:6} chars={len(chunk):6} total_ms={elapsed:5} err={err}")
    time.sleep(0.6)
