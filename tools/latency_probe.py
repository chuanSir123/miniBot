"""拆解最小请求的耗时构成：DNS / TCP / TLS / 服务端首字节 / 下载。

用法：
    python tools/latency_probe.py 5            # 直连 + 127.0.0.1:7890，各 5 轮
    python tools/latency_probe.py 5 7890       # 指定代理端口
    python tools/latency_probe.py 5 none       # 只测直连
"""
import json
import re
import socket
import ssl
import sys
import time
import urllib.parse

from core.config import load_config

CFG = load_config()["vlm"]
BASE = CFG["base_url"]
API_KEY = CFG["api_key"]
MODEL = CFG["model"]

url = urllib.parse.urlsplit(BASE)
HOST = url.hostname
PORT = url.port or (443 if url.scheme == "https" else 80)
PATH = (url.path or "/v1").rstrip("/") + "/chat/completions"

PAYLOAD = json.dumps(
    {
        "model": MODEL,
        "messages": [{"role": "user", "content": "只回复OK"}],
        "temperature": 0,
        "max_tokens": 4,
        "stream": False,
    }
).encode()

HEADERS = (
    f"POST {PATH} HTTP/1.1\r\n"
    f"Host: {HOST}\r\n"
    f"Authorization: Bearer {API_KEY}\r\n"
    "Content-Type: application/json\r\n"
    f"Content-Length: {len(PAYLOAD)}\r\n"
    "User-Agent: MiniBot-Harness/2.0\r\n"
    "Accept: application/json\r\n"
    "Connection: close\r\n"
    "\r\n"
).encode()


def ms(t):
    return round(t * 1000)


def drain(sock, first_byte):
    """读完剩余响应，返回 (全部字节, 读完耗时)"""
    buf = first_byte
    t = time.monotonic()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if b"\r\n\r\n" in buf:
            head, _, body = buf.partition(b"\r\n\r\n")
            lowered = head.lower()
            if b"content-length:" in lowered:
                clen = int(
                    [h for h in lowered.split(b"\r\n") if h.startswith(b"content-length")][0].split(b":")[1]
                )
                if len(body) >= clen:
                    break
    return buf, ms(time.monotonic() - t)


def finish(m, first, total_start, sock):
    body, read_ms = drain(sock, first)
    m["read"] = read_ms
    m["total"] = ms(time.monotonic() - total_start)
    sock.close()
    text = body.decode("utf-8", "replace")
    ray = re.search(r"CF-Ray: ([0-9a-f]+)-([A-Z]{3})", text)
    m["colo"] = ray.group(2) if ray else "?"
    m["status"] = text.split("\r\n", 1)[0].split(" ")[1] if " " in text.split("\r\n", 1)[0] else "?"
    return m, text


def run_direct():
    m, t0 = {}, time.monotonic()

    t = time.monotonic()
    infos = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)
    m["dns"] = ms(time.monotonic() - t)
    m["ip"] = infos[0][4][0]

    t = time.monotonic()
    sock = socket.create_connection((infos[0][4][0], PORT), timeout=120)
    m["tcp"] = ms(time.monotonic() - t)
    m["rtt"] = round(m["tcp"] / 2)

    ctx = ssl.create_default_context()
    t = time.monotonic()
    sock = ctx.wrap_socket(sock, server_hostname=HOST)
    m["tls"] = ms(time.monotonic() - t)

    t = time.monotonic()
    sock.sendall(HEADERS + PAYLOAD)
    m["send"] = ms(time.monotonic() - t)

    t = time.monotonic()
    first = sock.recv(1)
    m["ttfb"] = ms(time.monotonic() - t)
    return finish(m, first, t0, sock)


def run_proxy(phost, pport):
    m, t0 = {}, time.monotonic()

    t = time.monotonic()
    sock = socket.create_connection((phost, pport), timeout=120)
    m["proxy_tcp"] = ms(time.monotonic() - t)

    t = time.monotonic()
    sock.sendall(f"CONNECT {HOST}:{PORT} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("proxy 在 CONNECT 阶段关闭连接")
        buf += chunk
    m["connect"] = ms(time.monotonic() - t)

    ctx = ssl.create_default_context()
    t = time.monotonic()
    sock = ctx.wrap_socket(sock, server_hostname=HOST)
    m["tls"] = ms(time.monotonic() - t)

    t = time.monotonic()
    sock.sendall(HEADERS + PAYLOAD)
    m["send"] = ms(time.monotonic() - t)

    t = time.monotonic()
    first = sock.recv(1)
    m["ttfb"] = ms(time.monotonic() - t)
    return finish(m, first, t0, sock)


COLS = ["dns", "tcp", "proxy_tcp", "connect", "tls", "send", "ttfb", "read", "total"]


def table(rows, label):
    keys = [k for k in COLS if any(k in r for r in rows)]
    print(f"\n=== {label} ===")
    print(f"{'run':<4}" + "".join(f"{k:>10}" for k in keys) + f"{'colo':>6}")
    for i, r in enumerate(rows, 1):
        print(f"{i:<4}" + "".join(f"{r.get(k, '-'):>10}" for k in keys) + f"{r.get('colo', '?'):>6}")
    print(f"{'avg':<4}" + "".join(f"{sum(r.get(k, 0) for r in rows) // len(rows):>10}" for k in keys))
    print(f"{'min':<4}" + "".join(f"{min(r.get(k, 0) for r in rows):>10}" for k in keys))


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    port_arg = sys.argv[2] if len(sys.argv) > 2 else "7890"

    print(f"endpoint = {BASE}")
    print(f"host = {HOST}:{PORT}")
    print(f"model  = {MODEL}")
    print(f"payload = {len(PAYLOAD)} bytes（15 token 级最小请求）")

    direct, proxied = [], []
    sample = {}
    for i in range(rounds):
        m, text = run_direct()
        direct.append(m)
        sample.setdefault("direct", text)

    if port_arg.lower() != "none":
        for i in range(rounds):
            m, text = run_proxy("127.0.0.1", int(port_arg))
            proxied.append(m)
            sample.setdefault("proxy", text)

    table(direct, "直连（项目当前行为：ProxyHandler({})）")
    if proxied:
        table(proxied, f"本地代理 127.0.0.1:{port_arg}")

    for k, v in sample.items():
        body = v.split("\r\n\r\n", 1)[-1]
        try:
            usage = json.loads(body).get("usage", {})
        except Exception:
            usage = {}
        print(f"\n{k} usage: {json.dumps(usage, ensure_ascii=False)[:200]}")


if __name__ == "__main__":
    main()
