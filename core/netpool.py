"""按 endpoint 复用 keep-alive 连接。

实测到网关的 TCP 握手 180ms + TLS 握手 188ms，合计约 370ms。urllib 每次请求
都重新建连，而一轮任务会连发多轮模型请求；决策服务（Jev）同样每次新建 TLS。
这里按 scheme://host:port 复用连接。

调用方通过 `_pool_acquire` / `_pool_release` 拿还连接，响应对象由
`_PooledResponse.close()` 在读完响应体后自动归还。
"""
import http.client
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
from typing import Dict

_POOL: Dict[str, tuple] = {}
_POOL_LOCK = threading.Lock()
_POOL_TTL_SEC = 45.0  # 网关空闲回收通常 60s 左右，留出安全余量
_POOL_MAX_IDLE = 2

_STALE_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    ssl.SSLError,
)


def _pool_key(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{host}:{port}"


def _pool_acquire(key: str, scheme: str, host: str, port: int, timeout: float):
    """取一条可用连接，返回 (connection, reused)。"""
    with _POOL_LOCK:
        entry = _POOL.pop(key, None)
    if entry is not None:
        conn, last_used = entry
        if time.monotonic() - last_used < _POOL_TTL_SEC and conn.sock is not None:
            # 复用连接时同步一次超时设置，避免沿用上一次请求的 timeout。
            conn.timeout = timeout
            try:
                conn.sock.settimeout(timeout)
            except OSError:
                pass
            return conn, True
        try:
            conn.close()
        except Exception:
            pass
    if scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port, timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, False


def _pool_release(key: str, conn) -> bool:
    """把干净读完的连接放回池里；返回是否成功归还。"""
    with _POOL_LOCK:
        if len(_POOL) >= _POOL_MAX_IDLE:
            return False
        old = _POOL.get(key)
        if old is not None and old[0] is not conn:
            try:
                old[0].close()
            except Exception:
                pass
        _POOL[key] = (conn, time.monotonic())
        return True


def _pool_discard(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


class _PooledResponse:
    """把 http.client 的响应包成 urllib 响应的样子（read/readline/close/headers）。"""

    def __init__(self, raw, conn, key: str):
        self._raw = raw
        self._conn = conn
        self._key = key
        self.status = raw.status
        self.reason = raw.reason
        self.headers = raw.headers
        self._closed = False

    def read(self, *args):
        return self._raw.read(*args)

    def readline(self, *args):
        return self._raw.readline(*args)

    def __iter__(self):
        return iter(self._raw)

    @property
    def fp(self):
        return getattr(self._raw, "fp", None)

    def isclosed(self) -> bool:
        try:
            return self._raw.isclosed()
        except Exception:
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        # 只有把响应体完整读完（含 chunked 终止块）的连接才能安全复用，
        # 否则残留字节会污染下一次响应。
        try:
            reusable = self.status == 200 and self._raw.isclosed()
        except Exception:
            reusable = False
        if not (reusable and _pool_release(self._key, self._conn)):
            _pool_discard(self._conn)
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_request(request, timeout: float):
    """用连接池发一次请求，返回类 urllib 的响应对象。"""
    url = request.full_url
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    body = request.data
    method = request.method or ("POST" if body else "GET")
    headers = {
        k: v for k, v in (request.headers or {}).items()
        if k.lower() not in ("host", "connection")
    }
    key = _pool_key(scheme, host, port)

    last_error = None
    for attempt in range(2):
        conn, _reused = _pool_acquire(key, scheme, host, port, timeout)
        try:
            conn.request(method, target, body=body, headers=headers)
            raw = conn.getresponse()
        except _STALE_ERRORS + (OSError, socket.error) as error:
            _pool_discard(conn)
            # 请求尚未拿到任何响应字节，重试不会重复执行业务。
            # 池里的连接被对端回收是最常见的情况。
            if attempt == 0:
                last_error = error
                continue
            raise
        if raw.status >= 400:
            # 保持与 urllib 一致：抛 HTTPError，让上层原有的错误处理生效。
            raise urllib.error.HTTPError(
                url, raw.status, raw.reason, raw.headers,
                _PooledResponse(raw, conn, key),
            )
        return _PooledResponse(raw, conn, key)

    if last_error is not None:
        raise last_error
    raise ConnectionError("无法建立连接")
