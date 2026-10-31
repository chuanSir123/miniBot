"""同一份 50k payload 的变体矩阵 + 上传/排队拆解。

目的：回答"为什么 Postman 手动发只要 0.5s，探针却要 4.7s"。

两个可测量的嫌疑：
1. 上传耗时（243KB 请求体写入 socket 的时间）——
   用 http.client 把 conn.request() 和 getresponse() 分开计时即可分离。
2. 网关突发限流/排队 —— 观察"空闲后的第一发"与"紧接的第二发"是否差一个量级。

顺带把各变体导出成 docs/payloads/*.json，可直接拖进 Postman 手工复测。

用法: python tools/payload_variant_matrix.py [session_id]
"""
import json
import http.client
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
from core.context_builder import SYSTEM_PROMPT_CACHE
from core.harness import HARNESS_TOOLS
from core.skill_manager import SkillManager

from cache_ttft_by_length import build
from real_cache_probe import load_history

OUT = Path("docs/payloads")


def timed_post(endpoint, key, payload, timeout=240):
    """一次请求，拆成 upload / headers / 首 token 三段。"""
    parts = urllib.parse.urlsplit(endpoint)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    body = json.dumps(payload).encode()

    conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                       context=ssl.create_default_context())
    marks = {}
    t0 = time.monotonic()
    try:
        conn.request("POST", path, body=body, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "MiniBot-Harness/2.0",
            "Content-Length": str(len(body)),
        })
        marks["upload"] = round((time.monotonic() - t0) * 1000)
        resp = conn.getresponse()
        marks["headers"] = round((time.monotonic() - t0) * 1000)
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
                ev = json.loads(raw)
            except Exception:
                break
            for c in ev.get("choices", []):
                d = c.get("delta") or {}
                if "content" not in marks and (d.get("content") or d.get("reasoning_content")):
                    marks["content"] = round((time.monotonic() - t0) * 1000)
        marks["total"] = round((time.monotonic() - t0) * 1000)
        return marks
    except Exception as exc:
        return {"err": type(exc).__name__ + ": " + str(exc)[:100]}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    sid = sys.argv[1] if len(sys.argv) > 1 else "web:main:36b659ac"
    brain = VLMBrain()
    base, key, model, cfg = brain._settings()
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    settings = brain._settings()

    hist, summary = load_history(sid)
    skills = SkillManager().describe("", stable=True)
    names = [t["function"]["name"] for t in HARNESS_TOOLS]
    system, fp = SYSTEM_PROMPT_CACHE.render(
        scope="web:main", channel="web", selected_tools=names, skills=skills)
    comp = ContextCompactor(brain, None, sid)
    comp.summary = summary
    prod = build(sid, 50000, brain, settings, system, comp, hist)

    def variant(mt=None, tools=True, stream=True, usage=True):
        p = json.loads(json.dumps(prod))
        if mt is not None:
            p["max_tokens"] = mt
        if not tools:
            p.pop("tools", None)
            p.pop("tool_choice", None)
        if not stream:
            p["stream"] = False
            p.pop("stream_options", None)
        if not usage:
            p.pop("stream_options", None)
        return p

    variants = {
        "A_prod_16384_tools": variant(16384, True, True, True),
        "B_prod_16_tools": variant(16, True, True, True),
        "C_prod_16384_notools": variant(16384, False, True, True),
        "D_prod_16_notools": variant(16, False, True, True),
        "E_prod_16384_no_usage": variant(16384, True, True, False),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"endpoint": endpoint, "model": model, "system_fp": fp,
            "payload_bytes": len(json.dumps(prod)),
            "prompt_messages": len(prod["messages"]),
            "auth": "Header: Authorization: Bearer <在 config 里，别外传>"}
    for name, p in variants.items():
        (OUT / f"{name}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    (OUT / "_README.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False), flush=True)
    print("已导出:", ", ".join(sorted(variants)), flush=True)

    print(f"\n{'variant':22s} {'#'}  {'upload':>7s} {'headers':>8s} {'首token':>8s} {'total':>7s}  err")
    results = {}
    for name, p in variants.items():
        results[name] = []
        for i in range(3):
            if i == 0:
                time.sleep(6)  # 空闲 6 秒，测"冷链路第一发"
            r = timed_post(endpoint, key, p)
            results[name].append(r)
            print(f"{name:22s} {i + 1}  {str(r.get('upload')):>7s} {str(r.get('headers')):>8s} "
                  f"{str(r.get('content')):>8s} {str(r.get('total')):>7s}  {r.get('err') or ''}", flush=True)

    Path("docs/payload_variant_matrix.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
