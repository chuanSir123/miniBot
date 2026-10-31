"""按 endpoint 复用 keep-alive 连接（全项目唯一实现）。

实测到网关的 TCP 握手 180ms + TLS 握手 188ms，合计约 370ms。urllib 每次请求
都重新建连，而一轮任务会连发多轮模型请求；决策服务（Jev）同样每次新建 TLS。
这里按 scheme://host:port 复用连接。

与"固定空闲 TTL"做法的区别
-------------------------
早期版本靠"距上次使用超过 45 秒就不敢复用"来躲僵尸连接，等于用时间猜对端还
活着没有：猜早了白白丢掉一条好连接（多付 370ms），猜晚了照样踩到死连接。
现在改成直接问 socket：

  * `select(..., 0)` 没有可读事件 -> 内核没收到 FIN/数据，连接健康，直接用；
  * 有可读事件 -> 对端要么发了 FIN（peek 到 b""），要么留了脏字节，两种情况
    都不能安全复用，丢弃重建。

判断依据从"经验值"变成"事实"，因此连接空闲多久都能继续用。`_IDLE_HARD_TTL`
只是一个很大的 fd 卫生兜底，不再是 45 秒那种会打断复用的窗口。

调用方通过 `open_request` 发请求，响应对象由 `_PooledResponse.close()` 在把响应体
完整读完后自动归还连接。
"""
import http.client
import select
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
from typing import Dict, List, Tuple

# key = "scheme://host:port"，value = 空闲连接列表（末尾是最新归还的）。
_POOL: Dict[str, List[Tuple[object, float]]] = {}
_POOL_LOCK = threading.Lock()

# 同一个 endpoint 备几条空闲连接。并发两路请求时不再互相把对方的连接踢掉。
_MAX_IDLE_PER_KEY = 4
# 全局空闲连接上限，防止多个 endpoint 把 fd 攒爆。
_MAX_IDLE_TOTAL = 8
# 仅作 fd 卫生兜底：真正判断连接能否复用的是 `_socket_alive`，不是这个时间。
_IDLE_HARD_TTL = 300.0

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


def _socket_alive(sock) -> bool:
    """零成本探测连接是否已被对端关掉。

    只在"没有其他人在用这条连接"时调用（池里的空闲连接），所以临时切成非阻塞
    读不会影响任何正在进行的请求。
    """
    if sock is None:
        return False
    # TLS 层若还有没被消费掉的明文，说明上一次响应没读干净，不能复用。
    pending = getattr(sock, "pending", None)
    if pending is not None:
        try:
            if pending() > 0:
                return False
        except (OSError, ValueError):
            return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    if not readable:
        # 没有可读事件 = 对端没关、也没塞数据，连接健康。
        return True
    prev_timeout = None
    try:
        prev_timeout = sock.gettimeout()
        if prev_timeout != 0.0:
            sock.setblocking(False)
        sock.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, InterruptedError):
        return True
    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
        # 可读的是 TLS 控制记录（session ticket 之类），被 OpenSSL 消化后没有
        # 应用数据：连接本身还活着。
        return True
    except (OSError, ValueError):
        return False
    finally:
        if prev_timeout is not None and prev_timeout != 0.0:
            try:
                sock.settimeout(prev_timeout)
            except OSError:
                pass
    # 读到 EOF（对端已经关）或有残留脏字节：两种情况都不能安全复用。
    return False


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _purge_expired_locked(now: float) -> List[object]:
    """清掉超龄空闲连接。调用方必须持有 _POOL_LOCK。"""
    expired: List[object] = []
    for key in list(_POOL):
        bucket = _POOL[key]
        alive = [entry for entry in bucket if now - entry[1] < _IDLE_HARD_TTL]
        expired.extend(conn for conn, ts in bucket if now - ts >= _IDLE_HARD_TTL)
        if alive:
            _POOL[key] = alive
        else:
            _POOL.pop(key, None)
    return expired


def _pool_acquire(key: str, scheme: str, host: str, port: int, timeout: float):
    """取一条可用连接，返回 (connection, reused)。"""
    expired: List[object] = []
    while True:
        conn = None
        with _POOL_LOCK:
            now = time.monotonic()
            expired.extend(_purge_expired_locked(now))
            bucket = _POOL.get(key)
            if bucket:
                # LIFO：最近归还的那条最新鲜。
                conn, _last_used = bucket.pop()
                if not bucket:
                    _POOL.pop(key, None)
        if conn is None:
            break
        if _socket_alive(getattr(conn, "sock", None)):
            # 复用时同步一次超时设置，避免沿用上一次请求的 timeout。
            conn.timeout = timeout
            try:
                conn.sock.settimeout(timeout)
            except (OSError, AttributeError):
                pass
            for dead in expired:
                _close_quietly(dead)
            return conn, True
        # 探到是死连接：丢掉，继续找下一条。
        _close_quietly(conn)

    for dead in expired:
        _close_quietly(dead)
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
    evicted: List[object] = []
    with _POOL_LOCK:
        now = time.monotonic()
        evicted.extend(_purge_expired_locked(now))
        bucket = _POOL.get(key) or []
        if len(bucket) >= _MAX_IDLE_PER_KEY:
            # 该 endpoint 满了：淘汰最旧的一条，而不是把刚用完的这条丢掉。
            evicted.append(bucket.pop(0)[0])
        bucket.append((conn, now))
        _POOL[key] = bucket

        total = sum(len(entries) for entries in _POOL.values())
        while total > _MAX_IDLE_TOTAL:
            # 全局超限：淘汰全局最旧的一条（不一定是本 key 的）。
            oldest_key = None
            oldest_at = float("inf")
            for k, entries in _POOL.items():
                if entries and entries[0][1] < oldest_at:
                    oldest_at = entries[0][1]
                    oldest_key = k
            if oldest_key is None:
                break
            dropped = _POOL[oldest_key].pop(0)
            if not _POOL[oldest_key]:
                _POOL.pop(oldest_key, None)
            if dropped[0] is not conn:
                evicted.append(dropped[0])
            total -= 1
    for dead in evicted:
        _close_quietly(dead)
    return True


def _pool_discard(conn) -> None:
    _close_quietly(conn)


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
