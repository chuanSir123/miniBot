"""拆解 TTFT：到底是"服务端慢"还是"链路上慢"。

对同一个 payload 分别用两条通道发：
- fresh  ：urllib 每次新建 TCP+TLS（和探针工具一致）
- pooled ：走 core/brain.py 生产路径的 keep-alive 连接池

记录四个时间点（毫秒，均相对请求开始）：
  headers  收到响应头
  sse      收到第一条 SSE data
  content  收到第一个非空 content / reasoning_content

同时对比带 tools / 不带 tools 两个变体，确认工具 schema 是否影响首 token。

用法: python tools/ttft_breakdown.py [session_id] [--rounds 4]
"""
import json
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.context_builder import (
    SYSTEM_PROMPT_CACHE,
    render_runtime_context,
    render_tail_context,
)
from core.harness import HARNESS_TOOLS
from core.network import direct_opener
from core.skill_manager import SkillManager

from cache_ttft_by_length import build
from real_cache_probe import load_history


def drain(resp, t0):
    marks = {"headers": round((time.monotonic() - t0) * 1000)}
    marks.update({"sse": None, "content": None})
    while True:
        line = resp.readline(1 << 20)
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        if marks["sse"] is None:
            marks["sse"] = round((time.monotonic() - t0) * 1000)
        raw = line[5:].strip()
        if raw == b"[DONE]":
            break
        try:
            ev = json.loads(raw)
        except Exception:
            break
        for c in ev.get("choices", []):
            d = c.get("delta") or {}
            if marks["content"] is None and (d.get("content") or d.get("reasoning_content")):
                marks["content"] = round((time.monotonic() - t0) * 1000)
    return marks


def make_request(endpoint, key, payload):
    return urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "User-Agent": "MiniBot-Harness/2.0"},
        method="POST")


def run_fresh(endpoint, key, payload, timeout=240):
    t0 = time.monotonic()
    try:
        with direct_opener().open(make_request(endpoint, key, payload), timeout=timeout) as resp:
            return {"mode": "fresh", **drain(resp, t0), "err": None}
    except Exception as exc:
        return {"mode": "fresh", "err": type(exc).__name__ + ": " + str(exc)[:120]}


def run_pooled(endpoint, key, payload, timeout=240):
    t0 = time.monotonic()
    try:
        resp = VLMBrain._open_request(make_request(endpoint, key, payload), timeout)
        try:
            return {"mode": "pooled", **drain(resp, t0), "err": None}
        finally:
            resp.close()  # 完整读完才会把连接还回池
    except Exception as exc:
        return {"mode": "pooled", "err": type(exc).__name__ + ": " + str(exc)[:120]}


def dns_probe(host, times=3):
    out = []
    for _ in range(times):
        t = time.monotonic()
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except Exception as exc:
            return {"err": str(exc)[:80]}
        out.append(round((time.monotonic() - t) * 1000))
    return out


def handshake_probe(host, times=3):
    out = []
    for _ in range(times):
        t = time.monotonic()
        try:
            raw = socket.create_connection((host, 443), timeout=20)
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            raw.close()
        except Exception as exc:
            return {"err": str(exc)[:80]}
        out.append(round((time.monotonic() - t) * 1000))
    return out


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:36b659ac"
    rounds = 4
    if "--rounds" in sys.argv:
        rounds = int(sys.argv[sys.argv.index("--rounds") + 1])

    brain = VLMBrain()
    base, key, model, _ = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()
    host = urllib.parse.urlsplit(endpoint).hostname

    print(json.dumps({
        "endpoint_host": host,
        "dns_ms": dns_probe(host),
        "tcp_tls_handshake_ms": handshake_probe(host),
    }, ensure_ascii=False), flush=True)

    hist, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fp = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names, skills=skills)
    comp = ContextCompactor(brain, None, sid)
    comp.summary = summary
    payload = build(sid, 50000, brain, settings, system, comp, hist)
    no_tools = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
    print(json.dumps({
        "payload_bytes": len(json.dumps(payload)),
        "tools": len(payload.get("tools") or []),
        "max_tokens": payload.get("max_tokens"),
        "stream_options": payload.get("stream_options"),
        "no_tools_bytes": len(json.dumps(no_tools)),
    }, ensure_ascii=False), flush=True)

    print(f"\n{'#':>2s} {'mode':7s} {'variant':9s} {'headers':>8s} {'sse':>7s} {'content':>8s}  err")
    for i in range(rounds):
        for mode, fn in (("fresh", run_fresh), ("pooled", run_pooled)):
            for label, pl in (("full", payload), ("no-tools", no_tools)):
                r = fn(endpoint, key, pl)
                print(f"{i + 1:2d} {mode:7s} {label:9s} "
                      f"{str(r.get('headers')):>8s} {str(r.get('sse')):>7s} "
                      f"{str(r.get('content')):>8s}  {r.get('err') or ''}", flush=True)
                time.sleep(0.4)


if __name__ == "__main__":
    main()
