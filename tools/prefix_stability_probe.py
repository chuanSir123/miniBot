"""验证三件事：
A. 前缀在多轮之间是否逐字稳定（stable_prefix）：生产每轮追加消息，前缀不应变化
B. trim_old_outputs 是否会让「已缓存的历史」内容漂移（缓存失效元凶）
C. 缓存已热时，TTFT 是否随 prompt 长度上升（区分 prefill 成本 vs 后端排队）

用法: python tools/prefix_stability_probe.py <session_id>
"""
import hashlib
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
from core.memory import MiniMemory
from core.network import direct_opener
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens


def fp(obj):
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                   default=str).encode()).hexdigest()[:12]


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
    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    mem = MiniMemory()
    history, persisted = mem.get_history(sid)
    catalog = json.dumps(SkillManager().describe("", stable=True), ensure_ascii=False)
    system = build_structured_system_prompt(
        scope="web:main", custom_sections={"available_skills": catalog})
    compactor = ContextCompactor(brain, mem, sid)
    compactor.summary = persisted

    # ---------- A. 前缀逐字稳定性 ----------
    print("=== A. 多轮之间前缀指纹（system + 历史前 N 条）===", flush=True)
    prev = None
    for turn in range(3):
        live = (f"<dynamic_context>\n本地时间: 2026-10-29T21:0{turn}:00+08:00\n"
                f"</dynamic_context>")
        msgs = compactor.align_and_assemble_messages(system, history, live)
        formatted = brain._prepare_messages(msgs)
        # 前缀 = 除最后一条 volatile 之外的全部内容
        prefix = formatted[:-1]
        cur = fp(prefix)
        print(json.dumps({"turn": turn + 1, "prefix_fp": cur,
                          "same_as_prev": cur == prev if prev else None,
                          "prefix_msgs": len(prefix)}, ensure_ascii=False), flush=True)
        prev = cur
        history = history + [{"role": "user", "content": f"第{turn + 1}轮。"}]

    # ---------- B. trim_old_outputs 的前缀漂移 ----------
    print("=== B. trim_old_outputs 对历史内容的影响（缓存失效元凶）===", flush=True)
    live = "<dynamic_context>\n固定\n</dynamic_context>"
    msgs = compactor.align_and_assemble_messages(system, history, live)
    before = compactor.trim_old_outputs(msgs)
    after = compactor.trim_old_outputs(msgs)
    changed = []
    for i, (b, a) in enumerate(zip(before, after)):
        if fp(b) != fp(a):
            changed.append(i)
    print(json.dumps({
        "trim_is_stable_same_input": not changed,
        "changed_indices": changed[:10],
    }, ensure_ascii=False), flush=True)

    # 模拟「随着历史增长，protected_from 前移」→ 旧 tool 消息内容被改写
    print("=== B2. 历史增长时，旧 tool 消息是否被改写（=> 前缀漂移）===", flush=True)
    fingerprints = []
    for extra in range(0, 40, 8):
        hist = history + [{"role": "user", "content": f"填充{extra}"} for _ in range(extra)]
        m = compactor.align_and_assemble_messages(system, hist, live)
        trimmed = compactor.trim_old_outputs(m)
        tool_msgs = [x for x in trimmed if x.get("role") == "tool"]
        fingerprints.append({"extra": extra, "tool_msg_fp": fp(tool_msgs),
                             "tool_count": len(tool_msgs)})
    for row in fingerprints:
        print(json.dumps(row, ensure_ascii=False), flush=True)

    # ---------- C. 缓存已热时 TTFT vs prompt 长度 ----------
    print("=== C. 缓存热时，TTFT 与 prompt 长度的关系 ===", flush=True)
    base_history, _ = mem.get_history(sid)
    for frac in (0.25, 0.5, 1.0):
        cut = int(len(base_history) * frac)
        h = base_history[:cut]
        m = compactor.align_and_assemble_messages(system, h, live)
        p = brain._build_payload(brain._prepare_messages(m), stream=True,
                                 tools=HARNESS_TOOLS, settings=brain._settings())
        p["max_tokens"] = 16
        row = {"frac": frac, "msgs": cut}
        for i in range(3):
            res = post(endpoint, key, p)
            u = res.get("usage") or {}
            det = u.get("prompt_tokens_details") or {}
            row[f"run{i + 1}_ttft"] = res.get("ttft_ms")
            row["prompt_tokens"] = u.get("prompt_tokens")
            row["cached_tokens"] = det.get("cached_tokens")
            time.sleep(0.6)
        print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
