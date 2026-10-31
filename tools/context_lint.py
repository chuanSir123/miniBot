"""上下文装配静态检查：不需要模型服务，直接验证前缀不变量。

检查项（任一失败即退出码 1）：
  1. 首条消息必须是 system，且全局只有一条 system（vLLM/Gemma 的模板限制）
  2. 冻结区里不得出现秒级时间、Unix 时间戳随着调用变化
  3. 同一组输入两次装配，冻结前缀必须逐字节一致
  4. 尾块必须是最后一条消息，且只有一条
  5. 尾部变化不得影响冻结前缀（模拟下一轮：历史 +2 条 + 新时间）

用法:
  python tools/context_lint.py [session_id]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import (
    SYSTEM_PROMPT_CACHE,
    render_clock,
    render_runtime_context,
    render_tail_context,
)
from core.harness import HARNESS_TOOLS
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens

SCOPE = "web:main"


def load_history(sid):
    """从 memory.db 只读加载历史；库不存在时返回空历史。"""
    import sqlite3

    db_path = Path("data/memory.db")
    if not db_path.exists():
        return [], None
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT covered_until_id,summary FROM summaries_v2 WHERE session_id=?", (sid,)
        ).fetchone()
        cutoff = row[0] if row else 0
        rows = db.execute(
            "SELECT id,payload FROM messages_v2 WHERE session_id=? AND id>? ORDER BY id",
            (sid, cutoff),
        ).fetchall()
    finally:
        db.close()
    hist = [dict(json.loads(payload), _id=mid) for mid, payload in rows]
    summary = {"covered_until_id": row[0], "summary": row[1]} if row else None
    return hist, summary


def build(sid, stamp=None, history=None, summary=None):
    """复刻画 harness 的装配顺序，返回 (messages, system, fingerprint)。"""
    if history is None:
        history, summary = load_history(sid)
    catalog = SkillManager().describe("", stable=True)
    names = [tool["function"]["name"] for tool in HARNESS_TOOLS]
    system, fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope=SCOPE, channel="web", selected_tools=names, skills=catalog
    )
    compactor = ContextCompactor(VLMBrain(), None, sid)
    compactor.summary = summary
    runtime = render_runtime_context(now=stamp, scope=SCOPE, include_clock=False)
    tail = render_tail_context(
        runtime,
        {"status": "running", "turn": 1},
        clock=render_clock(stamp),
    )
    return (
        compactor.align_and_assemble_messages(system, history, tail),
        system,
        fingerprint,
    )


def first_divergence(a, b):
    """两条消息序列逐条比对，返回第一个不同的下标。"""
    for index, (left, right) in enumerate(zip(a, b)):
        if json.dumps(left, ensure_ascii=False, sort_keys=True) != json.dumps(
            right, ensure_ascii=False, sort_keys=True
        ):
            return index
    return min(len(a), len(b)) if len(a) != len(b) else -1


def check(condition, label, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return condition


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:default"
    failures = 0

    msgs, system, fp = build(sid)
    roles = [m.get("role") for m in msgs]

    print(f"session={sid}  messages={len(msgs)}  system_fp={fp}")
    print(f"system chars={len(system)}  tok~{estimate_text_tokens({'messages': [{'role': 'system', 'content': system}]})}")
    print(f"tools  chars={len(json.dumps(HARNESS_TOOLS, ensure_ascii=False))}"
          f"  tok~{estimate_text_tokens({'messages': HARNESS_TOOLS})}")
    print(f"roles 前6={roles[:6]} 后3={roles[-3:]}")
    print()

    failures += not check(bool(msgs) and roles[0] == "system", "首条是 system")
    # 冻结区 = 开头的 system 段（system + 压缩摘要），遇到历史或尾块即结束
    leading_system = 0
    for msg in msgs:
        if msg.get("role") != "system" or msg.get("_volatile_context"):
            break
        leading_system += 1
    failures += not check(leading_system <= 2, "前置 system 不超过 2 段（system + 压缩摘要）", f"实际 {leading_system}")

    tail_marks = [i for i, m in enumerate(msgs) if m.get("_volatile_context")]
    failures += not check(tail_marks == [len(msgs) - 1], "尾块是最后一条且只有一条", f"位置 {tail_marks}")

    # 时钟必须是整条尾部的最后一段：它每轮必变，放在中间会把后面的内容一起打穿。
    # 实测同体积尾部，时间在开头 uncached 2004 token / 首 token 1187ms，
    # 时间在结尾 660 token / 459ms。
    tail_text = str(msgs[-1].get("content") or "") if msgs else ""
    clock_last = tail_text.rstrip().endswith("</runtime_clock>")
    failures += not check(
        clock_last, "时钟排在尾部最末（越慢变的越靠前）",
        f"末尾 40 字符={tail_text[-40:]!r}",
    )

    # 冻结前缀：system + summary（压缩摘要若存在）
    frozen = [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in msgs[:leading_system]]

    # 2. 同一输入两次装配必须一致
    msgs2, system2, fp2 = build(sid, stamp=msgs and None)
    same = msgs2[:leading_system] == msgs[:leading_system] and fp == fp2
    failures += not check(same, "同输入重复装配，冻结前缀逐字节一致", f"{fp} vs {fp2}")

    # 3. 秒级时间不得进入冻结区
    now = time.time()
    sys_a, fp_a = SYSTEM_PROMPT_CACHE.render(
        scope=SCOPE, channel="web",
        selected_tools=[t["function"]["name"] for t in HARNESS_TOOLS],
        skills=SkillManager().describe("", stable=True),
    )
    time.sleep(1.1)
    sys_b, fp_b = SYSTEM_PROMPT_CACHE.render(
        scope=SCOPE, channel="web",
        selected_tools=[t["function"]["name"] for t in HARNESS_TOOLS],
        skills=SkillManager().describe("", stable=True),
    )
    failures += not check(fp_a == fp_b and sys_a == sys_b, "冻结区不含随时间变化的内容", f"{fp_a} vs {fp_b}")

    # 4. 模拟下一轮：历史 +2 条、时间推进 60s，冻结前缀必须不变
    grown = list(load_history(sid)[0]) + [
        {"role": "assistant", "content": "（模拟）已处理完这一步。", "_id": 10 ** 9},
        {"role": "user", "content": "（模拟）继续。", "_id": 10 ** 9 + 1},
    ]
    msgs3, system3, fp3 = build(sid, stamp=now + 60, history=grown, summary=None)
    div = first_divergence(msgs, msgs3)
    frozen_same = system3 == system
    failures += not check(frozen_same, "下一轮 system 不变", f"{fp} vs {fp3}")
    failures += not check(
        div >= leading_system,
        "下一轮不修改冻结前缀",
        f"首个分叉下标 {div}（冻结区结束于 {leading_system}，总 {len(msgs)}）",
    )

    # 5. 体积构成
    print()
    print("体积构成:")
    print(f"  system   {len(system):6d} chars  ~{estimate_text_tokens({'messages': [{'role': 'system', 'content': system}]}):5d} tok")
    print(f"tools    {len(json.dumps(HARNESS_TOOLS, ensure_ascii=False)):6d} chars  ~{estimate_text_tokens({'tools': HARNESS_TOOLS}):5d} tok")
    hist_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs[leading_system:len(msgs) - 1])
    print(f"  history  {hist_chars:6d} chars")
    print(f"  tail     {len(str(msgs[-1].get('content'))):6d} chars（每轮必然 miss）")
    print()
    print("装配顺序:", " -> ".join(roles[:leading_system] + ["history x%d" % max(0, len(msgs) - leading_system - 1)] + ["tail"]))

    print()
    print("OK" if failures == 0 else f"{failures} 项不变量失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
