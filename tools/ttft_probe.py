"""对比三层链路的首 token 延迟，定位是网关慢还是我方代码慢。

A. 裸 socket 非流式  -> 纯网关+服务端耗时
B. 裸 socket 流式    -> 首 chunk 到达时间
C. VLMBrain 流式     -> 含 payload 组装、图片处理、缓存日志的真实 TTFT
"""
import json
import socket
import ssl
import statistics
import sys
import time
import urllib.parse

from core.brain import VLMBrain
from core.config import load_config

CFG = load_config()["vlm"]
BASE, API_KEY, MODEL = CFG["base_url"], CFG["api_key"], CFG["model"]
url = urllib.parse.urlsplit(BASE)
HOST = url.hostname
PORT = url.port or 443
PATH = (url.path or "/v1").rstrip("/") + "/chat/completions"

MESSAGES = [{"role": "user", "content": "只回复OK"}]


def _headers(body: bytes) -> bytes:
    return (
        f"POST {PATH} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        f"Authorization: Bearer {API_KEY}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "User-Agent: MiniBot-Harness/2.0\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n\r\n"
    ).encode()


def _socket():
    infos = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)
    sock = socket.create_connection((infos[0][4][0], PORT), timeout=120)
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(sock, server_hostname=HOST)


def raw_stream():
    body = json.dumps(
        {"model": MODEL, "messages": MESSAGES, "temperature": 0, "max_tokens": 8, "stream": True}
    ).encode()
    t0 = time.monotonic()
    sock = _socket()
    sock.sendall(_headers(body) + body)
    connect = round((time.monotonic() - t0) * 1000)
    buf = b""
    first = None
    while True:
        chunk = sock.recv(8192)
        if not chunk:
            break
        buf += chunk
        if first is None and b"data:" in buf:
            first = round((time.monotonic() - t0) * 1000)
            break
    sock.close()
    return {"connect_ms": connect, "ttft_ms": first, "total_ms": round((time.monotonic() - t0) * 1000)}


def raw_nonstream():
    body = json.dumps(
        {"model": MODEL, "messages": MESSAGES, "temperature": 0, "max_tokens": 8, "stream": False}
    ).encode()
    t0 = time.monotonic()
    sock = _socket()
    sock.sendall(_headers(body) + body)
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf and b"prompt_tokens" in buf:
            break
    sock.close()
    return {"total_ms": round((time.monotonic() - t0) * 1000)}


def brain_stream(brain):
    t0 = time.monotonic()
    gen = brain.stream_chat_and_tools(list(MESSAGES))
    first = None
    while True:
        try:
            kind, _ = next(gen)
        except StopIteration as done:
            full, calls, usage = done.value
            break
        if first is None and kind in ("text_delta", "reasoning_delta"):
            first = round((time.monotonic() - t0) * 1000)
    return {
        "ttft_ms": first,
        "total_ms": round((time.monotonic() - t0) * 1000),
        "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "prompt": usage.get("prompt_tokens"),
    }


def stats(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return "-"
    return f"avg {int(statistics.mean(vals))}  min {min(vals)}  p50 {int(statistics.median(vals))}  max {max(vals)}"


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    brain = VLMBrain()
    print(f"endpoint = {BASE}  model = {MODEL}\n")

    print(f"--- A. 裸 socket 非流式（{rounds} 轮）---")
    a = []
    for i in range(rounds):
        r = raw_nonstream()
        a.append(r)
        print(json.dumps({"run": i + 1, **r}), flush=True)
    print("合计:", stats(a, "total_ms"))

    print(f"\n--- B. 裸 socket 流式首 chunk（{rounds} 轮）---")
    b = []
    for i in range(rounds):
        r = raw_stream()
        b.append(r)
        print(json.dumps({"run": i + 1, **r}), flush=True)
    print("建连:", stats(b, "connect_ms"))
    print("TTFT:", stats(b, "ttft_ms"))

    print(f"\n--- C. VLMBrain 流式 TTFT（{rounds} 轮）---")
    c = []
    for i in range(rounds):
        r = brain_stream(brain)
        c.append(r)
        print(json.dumps({"run": i + 1, **r}), flush=True)
    print("TTFT:", stats(c, "ttft_ms"))
    print("总计:", stats(c, "total_ms"))


if __name__ == "__main__":
    main()
