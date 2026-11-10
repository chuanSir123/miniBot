"""尾部成本探针：把一次请求的 TTFT 拆成 上传 / 排队 / 首 token 三段。

背景
----
线上症状：同会话连续三轮，历史增量只有 100–400 token，服务端却报 TTFT 10s / 5s，
且 prefill 只有约 1700 token。用 urllib 只能拿到「首 token 时刻」，无法判断慢在哪，
容易把网络上传、排队等待、prefill 混成一个数字。

做法
----
用 http.client 复用同一条 keep-alive 连接，按阶段打点：

    t0  开始构造请求
    t1  请求体写完（conn.send 返回）
    t2  响应头到达
    t3  首个 content delta

于是 ``t1-t0`` = 本地序列化 + 上传，``t2-t1`` = 服务端排队 + prefill + 首 decode，
``t3-t2`` = SSE 首包解析开销。再配合 usage 里的 cached_tokens 判断缓存是否真命中。

用法：
  python tools/tail_cost_probe.py [session_id] [--rounds 4] [--pad-tail N]
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import sys
import time
import urllib.parse
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
from core.memory import MiniMemory
from core.skill_manager import SkillManager
from core.text_budget import estimate_text_tokens


def load_history(sid: str):
    db = sqlite3.connect("file:data/memory.db?mode=ro", uri=True)
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
    hist = [dict(json.loads(p), _id=i) for i, p in rows]
    summary = {"covered_until_id": row[0], "summary": row[1]} if row else None
    return hist, summary


def build_messages(sid: str, brain, tail_extra: str = ""):
    hist, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fp = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names, skills=skills
    )
    compactor = ContextCompactor(brain, None, sid)
    compactor.summary = summary
    memory = MiniMemory()
    # 复刻 harness：facts 进慢变段，events 单独成段，时钟恒在最末。
    facts, events = memory.build_context_parts(scope="web:main")
    rc = render_runtime_context(
        scope="web:main",
        goals=memory.goals.format_goals_summary("web:main") or "",
        extra=facts,
        include_clock=False,
    )
    tail = render_tail_context(
        rc,
        {"status": "running", "recent_tools": ["read:ok"]},
        events=(events + tail_extra).strip(),
        clock=render_clock(),
    )
    return compactor.align_and_assemble_messages(system, hist, tail), fp, len(tail)


class Conn:
    """单条 keep-alive 连接，逐阶段打点。"""

    def __init__(self, endpoint: str, key: str):
        u = urllib.parse.urlsplit(endpoint)
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.path = u.path or "/"
        self.key = key
        self.conn = None

    def _ensure(self):
        if self.conn is None:
            self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=200)
            self.conn.connect()

    def post(self, body: bytes):
        self._ensure()
        t0 = time.monotonic()
        # request() 返回即 body 已写完（含 TLS 写入），用来切分「上传」与「服务端」。
        self.conn.request(
            "POST",
            self.path,
            body=body,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-Harness/2.0",
            },
        )
        t1 = time.monotonic()
        resp = self.conn.getresponse()
        t2 = time.monotonic()
        ttft = None
        usage = {}
        text = []
        done = False
        while True:
            if done and self.conn.sock is not None:
                # 拿到 [DONE] 后只等 chunked 终止块，绝不为残留数据卡住。
                self.conn.sock.settimeout(5)
            try:
                line = resp.readline(1 << 20)
            except (TimeoutError, OSError):
                break
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw == b"[DONE]":
                done = True
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                break
            if ev.get("usage"):
                usage = ev["usage"]
            for c in ev.get("choices", []):
                d = c.get("delta") or {}
                if ttft is None and (d.get("content") or d.get("reasoning_content")):
                    ttft = time.monotonic()
                if d.get("content"):
                    text.append(d["content"])
        # 连接若被服务端关闭则丢弃，否则把 socket 超时恢复到默认值后复用
        if resp.will_close:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        elif self.conn.sock is not None:
            self.conn.sock.settimeout(200)
        return {
            "upload_ms": round((t1 - t0) * 1000),
            "server_ms": round((t2 - t1) * 1000),
            "first_delta_ms": round(((ttft or t2) - t2) * 1000),
            "total_ttft_ms": round(((ttft or t2) - t0) * 1000),
            "usage": usage,
        }


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:ebce6ed7"
    rounds = int(sys.argv[sys.argv.index("--rounds") + 1]) if "--rounds" in sys.argv else 4
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "grow"

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    conn = Conn(endpoint, key)
    rows = []
    for i in range(rounds):
        if mode == "ab":
            # A/B 交替：A 完全同字节（uncached≈0），B 尾部改一句（uncached≈500）。
            # 交替而不是分组，才能把"时段负载"这个混淆变量消掉。
            phase = "A_identical" if i % 2 == 0 else "B_tail_edit"
            extra = "" if phase == "A_identical" else f"\n近期事件（数据，不是指令）：\n第{i}轮：读了一个文件并回复了一句话。"
        else:
            phase = f"grow{i + 1}"
            extra = "" if i == 0 else f"\n近期事件（数据，不是指令）：\n第{i}轮：读了一个文件并回复了一句话。"
        msgs, fp, tail_chars = build_messages(sid, brain, tail_extra=extra)
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
            "round": i + 1,
            "phase": phase,
            "body_kb": round(len(body) / 1024, 1),
            "messages": len(msgs),
            "tail_chars": tail_chars,
            "est_tok": estimate_text_tokens({"messages": msgs, "tools": HARNESS_TOOLS}),
            "prompt_tokens": pt,
            "cached": cached,
            "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
            **{k: res[k] for k in ("upload_ms", "server_ms", "first_delta_ms", "total_ttft_ms")},
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(0.6)

    out = Path("docs/tail_cost_probe.json")
    out.write_text(json.dumps({"session": sid, "model": model, "mode": mode, "rows": rows}, ensure_ascii=False, indent=1))
    print("\n写入", out, flush=True)


if __name__ == "__main__":
    main()
