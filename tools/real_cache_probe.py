"""用真实会话历史重建 VLM 请求，实测前缀缓存命中与 TTFT。

只读探测：只从 memory.db 读历史，不执行任何工具、不打印凭据。

用法:
  python tools/real_cache_probe.py <session_id> [--rounds N]
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.harness import HARNESS_TOOLS, build_structured_system_prompt
from core.network import direct_opener
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens


def load_history(sid):
    db = sqlite3.connect("file:data/memory.db?mode=ro", uri=True)
    try:
        summary = db.execute(
            "SELECT covered_until_id,summary FROM summaries_v2 WHERE session_id=?", (sid,)
        ).fetchone()
        cutoff = summary[0] if summary else 0
        rows = db.execute(
            "SELECT id,payload FROM messages_v2 WHERE session_id=? AND id>? ORDER BY id",
            (sid, cutoff),
        ).fetchall()
    finally:
        db.close()
    hist = [dict(json.loads(r[1]), _id=r[0]) for r in rows]
    summary = (
        {"covered_until_id": summary[0], "summary": summary[1]} if summary else None
    )
    return hist, summary


def build_real_payload(sid, brain, stamp=None):
    """贴近 harness 的真实装配：冻结 system + 历史 + 单块尾部运行数据。"""
    from core.context_builder import (
        SYSTEM_PROMPT_CACHE,
        render_clock,
        render_runtime_context,
        render_tail_context,
    )

    hist, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names, skills=skills
    )
    compactor = ContextCompactor(brain, None, sid)
    compactor.summary = summary
    tail = render_tail_context(
        render_runtime_context(now=stamp, scope="web:main", include_clock=False),
        {"status": "running", "turn": 1},
        clock=render_clock(stamp),
    )
    return compactor.align_and_assemble_messages(system, hist, tail), fingerprint


def _post_pooled(endpoint, key, req, timeout=200):
    """走 core/brain.py 的 keep-alive 连接池发请求，与生产同链路。

    TTFT 必须在这条链路上量：新建连接的 TCP+TLS 握手（这台服务端 0.4–0.5s）
    和 243KB 请求体在新连接上的慢启动，会把结果抬高一到两倍。
    """
    started = time.monotonic()
    usage, ttft, text = {}, None, []
    try:
        resp = VLMBrain._open_request(req, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        return {"ttft_ms": None, "wall_ms": None, "usage": {},
                "text": "", "err": f"HTTP {exc.code}: " + detail.replace(key, "[redacted]")[:200]}
    except Exception as exc:
        return {"ttft_ms": None, "wall_ms": None, "usage": {}, "text": "",
                "err": type(exc).__name__ + ": " + str(exc).replace(key, "[redacted]")[:200]}
    try:
        while True:
            line = resp.readline(1 << 20)
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw == b"[DONE]":
                break
            try:
                event = json.loads(raw)
            except Exception:
                break
            if event.get("usage"):
                usage = event["usage"]
            for c in event.get("choices", []):
                d = c.get("delta") or {}
                if ttft is None and (d.get("content") or d.get("reasoning_content")):
                    ttft = round((time.monotonic() - started) * 1000)
                if d.get("content"):
                    text.append(d["content"])
    except Exception as exc:
        return {"ttft_ms": ttft, "wall_ms": None, "usage": usage, "text": "",
                "err": type(exc).__name__ + ": " + str(exc).replace(key, "[redacted]")[:200]}
    finally:
        resp.close()  # 完整读完才会把连接还回池
    return {"ttft_ms": ttft, "wall_ms": round((time.monotonic() - started) * 1000),
            "usage": usage, "text": "".join(text)[:60], "err": None}


def post(endpoint, key, payload, timeout=200, pooled=True):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "MiniBot-Harness/2.0",
        },
        method="POST",
    )
    # 必须与生产同链路：core/brain.py 用 keep-alive 连接池。
    # 每次新建 TCP+TLS 会给这台服务端额外加 1.5–2.5s（实测：14 token 的小请求
    # 走池化 0.6–0.7s，走新连接 2.0–3.2s），243KB 请求体还要在新连接上重新慢启动。
    # 用 urllib 的 fresh 连接测出来的 TTFT 会严重虚高，不能用来判断缓存好坏。
    if pooled:
        return _post_pooled(endpoint, key, req, timeout)
    started = time.monotonic()
    usage = {}
    ttft = None
    text = []
    try:
        with direct_opener().open(req, timeout=timeout) as resp:
            while True:
                line = resp.readline(1 << 20)
                if not line:
                    break
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                if raw == b"[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except Exception:
                    break
                if event.get("usage"):
                    usage = event["usage"]
                for c in event.get("choices", []):
                    d = c.get("delta") or {}
                    if ttft is None and (d.get("content") or d.get("reasoning_content")):
                        ttft = round((time.monotonic() - started) * 1000)
                    if d.get("content"):
                        text.append(d["content"])
                # 不在 finish_reason 处提前 break：usage 是独立的尾部 chunk，
                # 提前 break 会把缓存统计丢掉（这正是要观察的指标）。
        return {"ttft_ms": ttft, "wall_ms": round((time.monotonic() - started) * 1000),
                "usage": usage, "text": "".join(text)[:60], "err": None}
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        return {"ttft_ms": None, "wall_ms": None, "usage": {},
                "text": "", "err": f"HTTP {exc.code}: " + detail.replace(key, "[redacted]")[:200]}
    except Exception as exc:
        return {"ttft_ms": None, "wall_ms": None, "usage": {}, "text": "",
                "err": type(exc).__name__ + ": " + str(exc).replace(key, "[redacted]")[:200]}


def report(label, res):
    u = res.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    pt = u.get("prompt_tokens")
    cached = det.get("cached_tokens")
    created = det.get("created_cache_tokens")
    hit = round(cached * 100 / pt, 1) if isinstance(cached, int) and pt else None
    print(json.dumps({
        "case": label,
        "ttft_ms": res.get("ttft_ms"),
        "wall_ms": res.get("wall_ms"),
        "prompt_tokens": pt,
        "cached_tokens": cached,
        "created_cache_tokens": created,
        "hit_pct": hit,
        "uncached_tokens": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
        "text": res.get("text"),
        "err": res.get("err"),
    }, ensure_ascii=False), flush=True)


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:0428eec0"
    rounds = 3
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, cfg = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"

    msgs, fingerprint = build_real_payload(sid, brain)
    print(json.dumps({
        "session": sid,
        "messages": len(msgs),
        "roles": [m.get("role") for m in msgs],
        "system_fp": fingerprint,
        "est_tokens": estimate_text_tokens({"messages": msgs, "tools": HARNESS_TOOLS}),
        "tools": len(HARNESS_TOOLS),
        "model": model,
    }, ensure_ascii=False), flush=True)

    settings = brain._settings()
    payload = brain._build_payload(
        brain._prepare_messages(msgs), stream=True, tools=HARNESS_TOOLS, settings=settings
    )
    # 固定一个 max_tokens：在 16 / 32768 之间来回跳会让 draft 模型按新输入 shape
    # 重新捕获计算图，TTFT 抖动会被误读成"缓存没命中"。
    payload["max_tokens"] = 16
    print(json.dumps({"prepared_roles": [m.get("role") for m in payload["messages"]]}, ensure_ascii=False), flush=True)

    for i in range(rounds):
        res = post(endpoint, key, payload)
        report(f"identical_run{i + 1}", res)
        time.sleep(0.8)


if __name__ == "__main__":
    main()
