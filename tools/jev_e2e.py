"""端到端验证：用真实 Jev + 真实长任务 trace 走一遍 review_completion。

复现改前的故障场景（state 163,984 字符触发 400 state_too_long），
确认现在能正常拿到核验结论、且不把"分类器不可用"当成"任务未完成"。
"""
import json
import time

from core.harness import AgentHarness
from core.jev import JevClient
from core.memory import MiniMemory


class DummyBrain:
    def is_configured(self):
        return True


def build_trace(task_state):
    """把 task_state.last_tools 还原成 harness 的 trace 结构。"""
    trace = []
    for item in task_state.get("last_tools", []):
        tool = item.get("tool")
        entry = {"tool": tool, "arguments": {"command": item.get("target", "")},
                 "result": {"ok": item.get("ok", False), "output": item.get("result_preview", "")}}
        if tool in ("write_file", "edit_file") and item.get("ok"):
            entry["result"]["path"] = item.get("target", "")
            entry["result"]["changed_files"] = [item.get("target", "")]
        trace.append(entry)
    return trace


def main():
    memory = MiniMemory()
    jev = JevClient()
    harness = AgentHarness(DummyBrain(), jev, memory, None, None)
    session = "web:main:7df17c7d"
    task_state = memory.get_task_state(session) or {}
    trace = build_trace(task_state)
    goal = task_state.get("original_goal") or "做一个鹈鹕骑自行车的动画页面"
    print(f"trace={len(trace)} 条，file_changes={len(task_state.get('file_changes') or [])} 条")

    from core.decision_context import build_decision_context
    started = time.monotonic()
    complete, feedback = harness.review_completion(
        reply="已完成鹈鹕骑车动画页面并跑了测试，验证通过。",
        user_prompt=task_state.get("latest_request") or goal,
        trace=trace,
        task_state=task_state,
        scope="local",
        decision_context=build_decision_context(memory, session, "local"),
        task_goal=goal,
        session_id=session,
    )
    elapsed = round((time.monotonic() - started) * 1000)
    print(f"\nreview_completion -> complete={complete}  elapsed={elapsed}ms")
    print(f"feedback={feedback[:200]!r}")
    print(f"jev.last_error={jev.last_error[:160]!r}")
    print("verification_requirements =",
          json.dumps(task_state.get("verification_requirements", {}), ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
