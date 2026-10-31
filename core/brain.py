import base64
import copy
import hashlib
import http.client
import json
import queue
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from core.config import load_config
from core.console import log

# 流读取线程的结束哨兵（标识对象，避免与真实数据行混淆）。
_STREAM_EOF = object()

# 判断"内容是不是已经说到句子结尾了"。上游半开/被 RST 时我们拿不到
# finish_reason，只能靠这个兜底：末尾是标点、右括号、颜文字、emoji 视为说完了；
# 末尾还挂在字/词中间（"…维持骨骼健康"）视为被截断，应当让 harness 重试。
_TAIL_MID_SENTENCE = set("，,、；;：:—－-+*/=（(【[「『《〈“‘\"'")


def _tail_looks_complete(text: str) -> bool:
    tail = (text or "").rstrip()
    if not tail:
        return False
    ch = tail[-1]
    if ch in _TAIL_MID_SENTENCE:
        return False
    if ch.isalnum() or ch == "_":
        return False
    return True

# ---------------------------------------------------------------------------
# HTTP 连接池
#
# 实测：到网关的 TCP 握手 180ms + TLS 握手 188ms，合计约 370ms。urllib 每次
# 请求都重新建连，而一次工具循环会连发好几轮模型请求，这笔开销被反复支付。
# 这里按 endpoint 复用 keep-alive 连接，把每轮 TTFT 从 ~1.1s 压到 ~0.7s。
# ---------------------------------------------------------------------------
_POOL: Dict[str, tuple] = {}
_POOL_LOCK = threading.Lock()
_POOL_TTL_SEC = 45.0  # 网关空闲回收通常 60s 左右，留出安全余量
_POOL_MAX_IDLE = 2


def _pool_key(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{host}:{port}"


def _pool_acquire(key: str, scheme: str, host: str, port: int, timeout: float):
    """取一条可用连接，返回 (connection, reused)。"""
    with _POOL_LOCK:
        entry = _POOL.pop(key, None)
    if entry is not None:
        conn, last_used = entry
        if time.monotonic() - last_used < _POOL_TTL_SEC and not conn.sock is None:
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
        reusable = False
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


class VLMBrain:
    """
    慢脑 (System 2) - OpenAI 兼容视觉语言模型 (VLM)
    支持：
      1. OpenAI / DeepSeek / vLLM 思考开关 (thinking toggle) 与思考强度 (reasoning_effort)
      2. 前缀缓存友好的多模态消息打包
      3. 流式同时解耦输出思考过程、回复文本、工具调用与精准 Token Usage
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._overrides = (base_url, api_key, model)
        cfg = load_config().get("vlm", {})
        self.base_url = (
            base_url or cfg.get("base_url") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "gpt-4o"
        self.temperature = cfg.get("temperature", 0.7)
        self.max_tokens = cfg.get("max_tokens", 1024)
        self.enable_thinking = cfg.get("enable_thinking", False)
        self.reasoning_effort = cfg.get("reasoning_effort", "none")
        self.last_usage: Dict[str, Any] = {}

    def is_configured(self) -> bool:
        return bool((self._overrides[1] or load_config()["vlm"]["api_key"]).strip())

    def _settings(self, model_override=None):
        from core.config import resolve_vlm_model_config
        cfg = load_config()["vlm"]
        base, key, model = self._overrides
        if model_override:
            if isinstance(model_override, dict):
                target_id = model_override.get("id") or model_override.get("model")
                m_cfg = dict(resolve_vlm_model_config(target_id))
                for k, v in model_override.items():
                    if k not in ("id",) and v is not None:
                        m_cfg[k] = v
            else:
                m_cfg = resolve_vlm_model_config(model_override)
            merged_cfg = {**cfg, **m_cfg}
            return (
                (m_cfg.get("base_url") or base or cfg["base_url"]).rstrip("/"),
                m_cfg.get("api_key") or key or cfg["api_key"],
                m_cfg.get("model") or model or cfg["model"],
                merged_cfg,
            )
        return (
            (base or cfg["base_url"]).rstrip("/"),
            key or cfg["api_key"],
            model or cfg["model"],
            cfg,
        )

    @staticmethod
    def _stream_socket(response):
        """取出流响应的底层 socket，用于在收尾阶段临时收紧读超时。"""
        for path in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock")):
            node = response
            for attr in path:
                node = getattr(node, attr, None)
                if node is None:
                    break
            if node is not None and hasattr(node, "settimeout"):
                return node
        return None

    @staticmethod
    def _enable_keepalive(sock, idle_sec: int = 30, interval_sec: int = 10, probes: int = 3):
        """打开 TCP keepalive，让内核替我们发现半开连接。

        反向代理（Cloudflare / Nginx / FRP）掉线时经常既不 FIN 也不 RST，
        应用层只会看到"读到一半突然没有新数据"。keepalive 能在 ~60 秒内
        让内核探测出连接已死，readline 立刻返回错误而不是无限阻塞。
        """
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):        # Linux
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle_sec)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_sec)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, probes)
            elif hasattr(socket, "TCP_KEEPALIVE"):     # macOS
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, idle_sec)
            elif hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
                sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                           (1, idle_sec * 1000, interval_sec * 1000))
        except (OSError, AttributeError):
            pass

    @staticmethod
    def _watch_cancel(cancel_event, cancelled, done, sockets, interval=0.12):
        """阻塞 readline 无法被 Event 唤醒，取消时直接 shutdown 底层 socket。

        这样"停止"能在 0.1 秒级生效，而不是等到下一个 SSE 分片（思考阶段可能几十秒）。
        """
        while not done.wait(interval):
            if cancel_event is None or not cancel_event.is_set():
                continue
            cancelled.set()
            for sock in sockets:
                if sock is None:
                    continue
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return

    @staticmethod
    def _open_request(request, timeout):
        """用连接池发请求，返回类 urllib 的响应对象。

        与 urllib 的差别：keep-alive 连接会被复用，省掉每轮约 370ms 的
        TCP+TLS 握手（实测 180ms + 188ms）。
        """
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
            conn, reused = _pool_acquire(key, scheme, host, port, timeout)
            try:
                conn.request(method, target, body=body, headers=headers)
                raw = conn.getresponse()
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
                ssl.SSLError,
            ) as error:
                _pool_discard(conn)
                # 请求尚未拿到任何响应字节，重试不会重复执行业务。
                # 池里的连接被对端回收是最常见的情况。
                if attempt == 0:
                    last_error = error
                    continue
                raise
            except (OSError, socket.error) as error:
                _pool_discard(conn)
                if attempt == 0:
                    last_error = error
                    continue
                raise

            if raw.status >= 400:
                # 保持与 urllib 一致：抛 HTTPError，让上层原有的错误处理生效。
                err = urllib.error.HTTPError(
                    url, raw.status, raw.reason, raw.headers,
                    _PooledResponse(raw, conn, key),
                )
                raise err
            return _PooledResponse(raw, conn, key)

        if last_error is not None:
            raise last_error
        raise ConnectionError("无法建立到模型服务的连接")

    def _build_payload(
        self,
        messages: List[Dict[str, Any]],
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None,
        settings=None,
    ) -> Dict[str, Any]:
        _, _, model, cfg = settings or self._settings()
        default_max_tokens = 32768 if cfg.get("enable_thinking") or cfg.get("reasoning_effort", "none") != "none" else 16384
        max_tokens = cfg.get("max_tokens") or default_max_tokens
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": cfg.get("temperature", 0.7),
            "max_tokens": max_tokens,
        }

        if stream:
            payload["stream"] = True
            # 请求云端提供准确的 Token 统计
            payload["stream_options"] = {"include_usage": True}

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # 思考强度 (兼容 OpenAI o1/o3-mini 及 vLLM reasoning_effort)
        effort = cfg.get("reasoning_effort", "none")
        if effort and effort != "none":
            payload["reasoning_effort"] = effort

        # 思考开关 (兼容 DeepSeek 与 vLLM 模板)
        # Raw HTTP has no SDK extra_body envelope. Providers declare their supported fields.
        if cfg.get("enable_thinking"):
            payload.update(
                cfg.get(
                    "thinking_fields",
                    {"chat_template_kwargs": {"enable_thinking": True}},
                )
            )
        elif cfg.get("enable_thinking") is False:
            if "thinking_fields" in cfg:
                payload.update({"chat_template_kwargs": {"enable_thinking": False}})
        for field in cfg.get("omit_fields", []):
            payload.pop(field, None)

        return payload

    def _prepare_messages(
        self, messages: List[Dict[str, Any]], image_bytes: Optional[bytes] = None
    ) -> List[Dict[str, Any]]:
        formatted = []
        system_parts = []
        for msg in messages:
            item = copy.deepcopy(msg)
            if item.pop("_volatile_context", False):
                # vLLM/Gemma 只接受一条前置 system，运行数据必须降级成尾部 user，
                # 否则每轮改写前置 system 会把整段历史一起打穿。
                # 内容本身已由 render_tail_context 界定为「数据而不是新指令」，这里不再重复声明。
                item["role"] = "user"
                formatted.append(item)
                continue
            if item.get("role") == "system":
                content = item.get("content", "")
                if not isinstance(content, str):
                    raise ValueError("system消息必须是文本")
                if formatted:
                    # Runtime recovery/checkpoint instructions appended after turns
                    # must not be moved ahead of already cached conversation history.
                    item["role"] = "user"
                    item["content"] = "[Harness续接指令]\n" + content
                    formatted.append(item)
                    continue
                if content:
                    system_parts.append(content)
            else:
                formatted.append(item)
        # Gemma and several OpenAI-compatible gateways accept exactly one system
        # message at position zero. Merge leading policy/summary only; preserve
        # the chronology of runtime context and recovery messages after turns.
        if system_parts:
            formatted.insert(
                0, {"role": "system", "content": "\n\n".join(system_parts)}
            )

        if image_bytes and formatted:
            last_msg = formatted[-1]
            if last_msg.get("role") == "user":
                orig_content = last_msg.get("content", "")

                content_list = []
                if isinstance(orig_content, str) and orig_content:
                    content_list.append({"type": "text", "text": orig_content})
                elif isinstance(orig_content, list):
                    content_list.extend(orig_content)

                if isinstance(image_bytes, list):
                    for img in image_bytes:
                        if not img:
                            continue
                        b64_img = base64.b64encode(img).decode("utf-8")
                        content_list.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64_img}",
                                    "detail": "low",
                                },
                            }
                        )
                else:
                    b64_img = base64.b64encode(image_bytes).decode("utf-8")
                    content_list.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_img}",
                                "detail": "low",
                            },
                        }
                    )
                last_msg["content"] = content_list
        return formatted

    @staticmethod
    def _fingerprint(value) -> str:
        """稳定指纹：用于观察前缀（tools / system）是否被意外改写。"""
        if value is None:
            return "none"
        raw = json.dumps(value, ensure_ascii=False, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _cache_report(usage, tools_fp="", prefix_fp="") -> Optional[Dict[str, Any]]:
        """把上游 usage 折算成缓存命中率；上游不返回 usage 时返回 None。"""
        if not isinstance(usage, dict) or not usage:
            return None
        prompt = usage.get("prompt_tokens")
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is None:
            cached = usage.get("prompt_cache_hit_tokens", usage.get("cached_tokens"))
        if not isinstance(prompt, int) or not isinstance(cached, int):
            return None
        return {
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "created_cache_tokens": details.get("created_cache_tokens"),
            "hit_pct": round(cached * 100 / prompt, 1) if prompt else 0.0,
            "tools_fp": tools_fp,
            "prefix_fp": prefix_fp,
        }

    @staticmethod
    def _api_error_message(error):
        for _ in range(4):
            if isinstance(error, str):
                try:
                    error = json.loads(error)
                    continue
                except (ValueError, TypeError):
                    return error[:500]
            if isinstance(error, dict):
                if "error" in error:
                    error = error["error"]
                elif "message" in error:
                    error = error["message"]
                else:
                    return json.dumps(error, ensure_ascii=False)[:500]
            else:
                return str(error)[:500]
        return str(error)[:500]

    def chat(
        self,
        messages: List[Dict[str, Any]],
        image_bytes: Optional[bytes] = None,
        model_override: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> str:
        """非流式标准聊天"""
        if not self.is_configured():
            return "【提示】未配置 VLM API Key，请先在配置中心填写。"

        settings = self._settings(model_override=model_override)
        base_url, api_key, _, _ = settings
        endpoint = (
            f"{base_url}/chat/completions"
            if not base_url.endswith("/chat/completions")
            else base_url
        )
        formatted_messages = self._prepare_messages(messages, image_bytes)
        payload = self._build_payload(
            formatted_messages, stream=False, settings=settings
        )

        req = urllib.request.Request(
            endpoint,
            # ensure_ascii=False：默认值会把每个中文字符转成 \uXXXX（6 字节 → 实际 3 字节）。
            # 人设 + 历史全是中文时请求体能小 17%~40%，而上传耗时直接计入 TTFT
            # （实测 243KB 的 50k 上下文，请求体每小 40KB 约省 0.3s）。
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-VLM/2.0",
            },
            method="POST",
        )

        try:
            with self._open_request(
                req, timeout=load_config()["runtime"]["request_timeout_sec"]
            ) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
            if "usage" in resp_json:
                self.last_usage = resp_json["usage"]
                report = self._cache_report(
                    resp_json["usage"], "none",
                    self._fingerprint(payload["messages"][0] if payload.get("messages") else None),
                )
                if report:
                    log("VLM", "cache.usage", mode="chat", **report)
            choices = resp_json.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "").strip()
            return ""
        except urllib.error.HTTPError as e:
            err = e.read(2048).decode("utf-8", errors="ignore")
            err = err.replace(api_key, "[redacted]")
            print(f"[VLM] HTTP Error {e.code}: {err}")
            return f"【VLM 报错】HTTP {e.code}: {err[:80]}"
        except Exception as e:
            print(f"[VLM] Request error: {e}")
            return f"【VLM 网络异常】{str(e)[:80]}"

    def stream_chat_and_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        image_bytes: Optional[bytes] = None,
        model_override: Optional[Union[str, Dict[str, Any]]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Generator[
        Tuple[str, Any], None, Tuple[str, List[Dict[str, Any]], Dict[str, Any]]
    ]:
        """
        流式生成，同时解耦输出中间文本思考、思考过程 (DeepSeek/vLLM reasoning_content)、工具调用与精准 Token Usage。
        yield ("reasoning_delta", reasoning_str)
        yield ("text_delta", delta_str)
        最终 return (full_text, completed_tool_calls, usage_dict)
        """
        if not self.is_configured():
            yield ("text_delta", "【提示】未配置 VLM API Key。")
            return ("【提示】未配置 VLM API Key。", [], {})

        settings = self._settings(model_override=model_override)
        base_url, api_key, _, _ = settings
        endpoint = (
            f"{base_url}/chat/completions"
            if not base_url.endswith("/chat/completions")
            else base_url
        )
        formatted_messages = self._prepare_messages(messages, image_bytes)
        payload = self._build_payload(
            formatted_messages, stream=True, tools=tools, settings=settings
        )
        # 前缀缓存只认"从第一个 token 起逐字相同"的前缀。tools 与首条 system
        # 是前缀的头，任何改写都会让整段缓存归零，因此单独打指纹便于对照。
        tools_fp = self._fingerprint(tools)
        head_fp = self._fingerprint(formatted_messages[0] if formatted_messages else None)
        cache_logged = False

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-Harness/2.0",
            },
            method="POST",
        )

        full_content = []
        in_think_tag = False
        has_reasoning = False
        reasoning_done = False
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        usage_result: Dict[str, Any] = {}
        complete = False
        failed = False
        finish_reason = None
        received = 0
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        last_data = started
        stage = "等待连接或响应头"
        runtime_cfg = load_config()["runtime"]
        timeout = runtime_cfg["request_timeout_sec"]
        # 网关给出 finish_reason 后本应补 usage chunk 与 data: [DONE]，但部分
        # 反向代理会保持连接不关也不补 [DONE]。这里只给一个很短的收尾窗口，
        # 窗口过后主动结束，避免 readline 永久阻塞（表现为"模型答完了还卡着"）。
        finish_grace = float(runtime_cfg.get("stream_finish_grace_sec") or 3.0)
        # 已经吐过内容后的"死流"止损阈值。上游半开连接（既不 FIN 也不 RST）
        # 在 TCP 层看不到任何异常，readline 会一直阻塞到 socket 超时；如果只
        # 依赖 request_timeout_sec，用户就要干等满 180 秒。实测首字之后的分片
        # 间隔 p90 仅 30~45ms、最差 873ms（工具调用参数也是约 30ms 一片的流式
        # 分片），所以 3 秒足够判断"这条流已经死了"；真正长的等待只发生在首字
        # 之前（prefill，实测最长 6.3s），那一段仍由 request_timeout_sec 兜。
        stall_sec = float(runtime_cfg.get("stream_idle_stall_sec") or 3.0)
        finish_seen_at: Optional[float] = None
        # 软收尾原因：上游没给终结信号就断了/哑了，但有效内容已经收全。
        soft_tail: Optional[str] = None
        # 是否已经吐出过真实内容（文本/思考/工具参数），空 delta 不算。
        saw_delta = False
        stream_socket = None
        # 取消信号：外部 Event -> watcher 线程 -> shutdown socket -> readline 立刻返回。
        cancelled = threading.Event()
        cancel_done = threading.Event()
        cancel_watcher = None
        pump_stop = None
        pump_thread = None
        parsed = urllib.parse.urlsplit(endpoint)
        safe_endpoint = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))

        def diagnostic(event, **fields):
            log("VLM", event, request_id=request_id, endpoint=safe_endpoint,
                model=settings[2], transport="direct", timeout_sec=timeout, stage=stage,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                received_bytes=received, **fields)


        try:
            resp = self._open_request(req, timeout=timeout)
            stage = "等待首行流数据"
            last_data = time.monotonic()
            stream_socket = self._stream_socket(resp)
            self._enable_keepalive(stream_socket)
            # readline 是阻塞调用，任何 Event 都叫不醒它。这里用独立读线程把数据
            # 投进队列，主循环以 150ms 粒度轮询，用户点"停止"才能真正立刻生效。
            line_queue: "queue.Queue" = queue.Queue()
            pump_stop = threading.Event()
            pump_errors: List[BaseException] = []

            def pump():
                try:
                    while not pump_stop.is_set():
                        raw = resp.readline(1048576)
                        if not raw:
                            break
                        line_queue.put(raw)
                except BaseException as exc:  # noqa: BLE001 - 原样抛回主循环
                    pump_errors.append(exc)
                finally:
                    # 关闭必须由读线程自己做：Windows 上 resp.close() 会等待
                    # 被阻塞的 readline 释放缓冲区锁，在主线程里关会卡住整轮任务。
                    try:
                        resp.close()
                    except Exception:
                        pass
                    line_queue.put(_STREAM_EOF)

            pump_thread = threading.Thread(
                target=pump, daemon=True, name=f"vlm-read-{request_id}"
            )
            pump_thread.start()
            if cancel_event is not None:
                cancel_watcher = threading.Thread(
                    target=self._watch_cancel,
                    args=(cancel_event, cancelled, cancel_done, (stream_socket,)),
                    daemon=True,
                    name=f"vlm-cancel-{request_id}",
                )
                cancel_watcher.start()
            while True:
                if cancelled.is_set():
                    diagnostic("stream.cancelled", reason="user_cancel")
                    break
                now = time.monotonic()
                if finish_seen_at is not None and now - finish_seen_at >= finish_grace:
                    # 终止语义已到，网关只是没补 [DONE] / 没关连接。
                    complete = True
                    diagnostic("stream.tail.closed", reason="finish_grace",
                               finish_reason=finish_reason)
                    break
                # 分级空闲超时：
                #   还没吐出过任何内容 -> 现在是 TTFT/prefill 阶段，给足
                #     request_timeout_sec（实测最长 6.3s，并发排队时更久）；
                #   已经吐过内容 -> 分片间隔只有几十毫秒，沉默 stall_sec 就是死流。
                # 例外：思考链还没结束时，部分后端会攒着 reasoning 不吐，
                # 那段时间是真空闲，给 30 秒（或 5 倍 stall）兜底。
                idle_limit = stall_sec if saw_delta else timeout
                if saw_delta and has_reasoning and not reasoning_done:
                    idle_limit = max(idle_limit, stall_sec * 5, 30.0)
                idle = now - last_data
                if idle > idle_limit:
                    if (saw_delta and full_content and not tool_calls_map
                            and _tail_looks_complete("".join(full_content))):
                        # 内容已经收全，只是上游没补 finish_reason/[DONE] 就哑了。
                        # 按正常结束处理，避免把一句已经答完的话变成失败重试。
                        soft_tail = "idle_stall"
                        complete = True
                        diagnostic("stream.tail.soft", reason="idle_stall",
                                   idle_ms=round(idle * 1000),
                                   chars=len("".join(full_content)))
                        break
                    # 心跳行不算数据，避免被 keep-alive 维持的假活流无限拖住。
                    if saw_delta:
                        raise TimeoutError(
                            f"连续 {round(idle)} 秒未收到任何新数据切片"
                        )
                    raise TimeoutError(
                        f"首字等待超过 {round(idle)} 秒仍未收到任何内容（TTFT 超时）"
                    )
                try:
                    raw_line = line_queue.get(timeout=0.15)
                except queue.Empty:
                    continue
                if raw_line is _STREAM_EOF:
                    if cancelled.is_set():
                        diagnostic("stream.cancelled", reason="socket_shutdown")
                        break
                    error = pump_errors[0] if pump_errors else None
                    if error is not None:
                        # 读线程抛超时/网络错：只要有效内容已经收到、也没有半截
                        # 工具调用，就按正常结束处理（上游多半是半开死连接）。
                        if isinstance(error, (TimeoutError, OSError)) and (
                            finish_seen_at is not None
                            or (full_content and not tool_calls_map
                                and _tail_looks_complete("".join(full_content)))
                        ):
                            complete = True
                            if finish_seen_at is not None:
                                diagnostic("stream.tail.closed", reason="read_error_after_finish",
                                           finish_reason=finish_reason)
                            else:
                                soft_tail = "read_error"
                                diagnostic("stream.tail.soft", reason="read_error",
                                           chars=len("".join(full_content)),
                                           error_type=type(error).__name__)
                            break
                        raise error
                    # 对端直接关了连接，且没给 finish_reason/[DONE]。只要内容
                    # 已经收到、也没有半截工具调用，就按正常结束处理。
                    if (full_content and not tool_calls_map
                            and _tail_looks_complete("".join(full_content))):
                        soft_tail = "peer_eof"
                        complete = True
                        diagnostic("stream.tail.soft", reason="peer_eof",
                                   chars=len("".join(full_content)))
                    break
                if not raw_line:
                    break
                received += len(raw_line)
                if len(raw_line) >= 1048576 or received > 8 * 1024 * 1024:
                    raise ValueError("模型响应超过大小上限")
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                if line[5:].strip() == "[DONE]":
                    complete = True
                    # 再多读一小段，把 chunked 终止块消费干净，连接才能安全回池复用。
                    drain_until = time.monotonic() + 0.2
                    while time.monotonic() < drain_until:
                        try:
                            nxt = line_queue.get(timeout=0.02)
                        except queue.Empty:
                            continue
                        if nxt is _STREAM_EOF or not nxt:
                            break
                    break
                data_str = line[5:].strip()
                try:
                    chunk = json.loads(data_str)
                except Exception as exc:
                    raise ValueError("模型返回无效SSE JSON") from exc

                if not isinstance(chunk, dict):
                    raise ValueError("模型返回无效SSE对象")
                if chunk.get("error"):
                    raise ValueError(
                        "模型接口错误：" + self._api_error_message(chunk["error"])
                    )

                # 捕获 Token Usage (来自 stream_options)
                if "usage" in chunk and chunk["usage"]:
                    usage_result = chunk["usage"]
                    self.last_usage = usage_result
                    report = self._cache_report(usage_result, tools_fp, head_fp)
                    if report and not cache_logged:
                        cache_logged = True
                        log("VLM", "cache.usage", request_id=request_id, **report)

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                finish_reason = choices[0].get("finish_reason") or finish_reason
                if finish_reason and finish_seen_at is None:
                    finish_seen_at = time.monotonic()
                    if stream_socket is not None and finish_grace > 0:
                        try:
                            stream_socket.settimeout(max(0.2, finish_grace))
                        except OSError:
                            pass
                delta = choices[0].get("delta", {})
                # 只有真正吐出内容（文本/思考/工具参数）才算"开始应答"。
                # vLLM 首个包通常是 {"delta":{"role":"assistant","content":""}}，
                # 空内容块不能当成已出字，否则 TTFT 会被误判成"流卡死"。
                if (delta.get("content") or delta.get("tool_calls")
                        or delta.get("reasoning_content") or delta.get("reasoning")
                        or delta.get("thought") or delta.get("thinking")):
                    last_data = time.monotonic()
                    if not saw_delta:
                        saw_delta = True
                        stage = "读取流数据"

                # 1. 思考链过程流式输出 (DeepSeek-R1 / vLLM reasoning_content / Ollama reasoning / thought / thinking)
                raw_reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thought")
                    or delta.get("thinking")
                )
                reasoning_delta = (
                    str(raw_reasoning)
                    if raw_reasoning is not None and raw_reasoning != ""
                    else None
                )
                if reasoning_delta:
                    has_reasoning = True
                    yield ("reasoning_delta", reasoning_delta)

                # 2. 中间文本流式输出 (支持自动解析模型 <think> 标签)
                content_delta = delta.get("content")
                if content_delta:
                    if "<think>" in content_delta:
                        parts = content_delta.split("<think>", 1)
                        if parts[0]:
                            full_content.append(parts[0])
                            yield ("text_delta", parts[0])
                        in_think_tag = True
                        has_reasoning = True
                        content_delta = parts[1]

                    if in_think_tag:
                        if "</think>" in content_delta:
                            parts = content_delta.split("</think>", 1)
                            if parts[0]:
                                yield ("reasoning_delta", parts[0])
                            in_think_tag = False
                            reasoning_done = True
                            yield ("reasoning_complete", "")
                            if parts[1]:
                                full_content.append(parts[1])
                                yield ("text_delta", parts[1])
                        else:
                            yield ("reasoning_delta", content_delta)
                    else:
                        if has_reasoning and not reasoning_done:
                            reasoning_done = True
                            yield ("reasoning_complete", "")
                        full_content.append(content_delta)
                        yield ("text_delta", content_delta)

                # 3. 工具调用流式聚合
                if "tool_calls" in delta and delta["tool_calls"] and has_reasoning and not reasoning_done:
                    reasoning_done = True
                    yield ("reasoning_complete", "")
                if "tool_calls" in delta and delta["tool_calls"]:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.get(
                                    "id", f"call_{idx}_{int(time.time() * 1000)}"
                                ),
                                "name": "",
                                "arguments": "",
                            }
                        if tc.get("id"):
                            tool_calls_map[idx]["id"] = tc["id"]
                        func = tc.get("function", {})
                        delta_name = func.get("name", "")
                        delta_args = func.get("arguments", "")
                        if delta_name:
                            tool_calls_map[idx]["name"] += delta_name
                        if delta_args:
                            tool_calls_map[idx]["arguments"] += delta_args
                        if delta_name or delta_args:
                            yield (
                                "tool_call_delta",
                                {
                                    "index": idx,
                                    "id": tool_calls_map[idx]["id"],
                                    "name": tool_calls_map[idx]["name"],
                                    "delta": delta_args,
                                    "arguments_so_far": tool_calls_map[idx]["arguments"],
                                },
                            )

        except urllib.error.HTTPError as e:
            failed = True
            diagnostic("request.error", exception_type=type(e).__name__, http_status=e.code,
                       cf_ray=e.headers.get("cf-ray") if e.headers else None)
            detail = e.read(4096).decode("utf-8", errors="replace")
            message = f"HTTP {e.code}: " + self._api_error_message(detail)
            message = message.replace(api_key, "[redacted]")
            log("VLM", "response.error_detail", request_id=request_id, http_status=e.code, detail=message)
            if e.code == 520:
                message = (f"HTTP 520：网关或源站返回了无效响应，耗时 {time.monotonic() - started:.1f} 秒；"
                           f"本机网络等待上限为 {timeout} 秒，并非本机等待超时。"
                           f"请检查反向代理和上游模型服务日志；请求编号 {request_id}。")
            yield ("error", message)
            err_msg = "【模型接口错误：" + message[:500] + "】"
            yield ("text_delta", err_msg)
            full_content.append(err_msg)
        except Exception as e:
            if cancelled.is_set():
                # 取消触发的 shutdown 会以各种异常形式冒出来，全部按"用户停止"静默处理。
                diagnostic("stream.cancelled", reason="shutdown_exception",
                           exception_type=type(e).__name__)
                failed = True
            else:
                failed = True
                message = str(e).replace(api_key, "[redacted]")
                reason = getattr(e, "reason", e)
                timed_out = isinstance(reason, TimeoutError)
                diagnostic("request.error", exception_type=type(e).__name__,
                           reason_type=type(reason).__name__, detail=message,
                           timeout=timed_out,
                           idle_ms=round((time.monotonic() - last_data) * 1000))
                is_ssl_eof = "EOF occurred in violation of protocol" in str(e) or "SSLEOFError" in type(reason).__name__
                is_conn_reset = "ConnectionResetError" in type(reason).__name__ or "10054" in str(e) or "RemoteDisconnected" in type(reason).__name__
                if timed_out:
                    idle_sec = round(time.monotonic() - last_data)
                    if saw_delta:
                        message = (f"对话模型请求超时：读取流数据期间连续 {idle_sec} 秒没有新数据"
                                   f"（空闲上限 {round(stall_sec)} 秒，已累计接收 {received} 字节；"
                                   f"请求编号 {request_id}）。这通常是上游在生成过程中把连接挂死，"
                                   "或反向代理中断；已生成的内容会保留。")
                    else:
                        message = (f"对话模型请求超时：等待首字已超过 {idle_sec} 秒，一个内容包都没收到"
                                   f"（等待上限 {timeout} 秒，已累计接收 {received} 字节；"
                                   f"请求编号 {request_id}）。这通常是上游 prefill 太慢、显存不足或网关排队，"
                                   "与 max_tokens 无关；可调大 runtime.request_timeout_sec。")
                elif is_ssl_eof or is_conn_reset:
                    elapsed_sec = round(time.monotonic() - started, 1)
                    message = (f"模型服务连接异常中断（{type(reason).__name__}）：在{stage}阶段（已等待 {elapsed_sec} 秒），远端模型服务或反向代理网关直接关闭了连接（TCP FIN/RST）。\n"
                               "【常见原因】：\n"
                               "1. 本会话历史上下文过长（当前已达数万 Tokens），服务端推理引擎 (如 vLLM) 在分配 KV Cache 显存或 Prefill 期间 GPU 显存溢出 (CUDA OOM) 导致服务进程崩溃；\n"
                               "2. 反向代理/网关（如 Cloudflare / Nginx / FRP）在前向计算超时或网络抖动时切断了连接；\n"
                               "3. 模型服务正在重启。\n"
                               "【建议方案】：在智能体设置中调低 context_window（例如设为 32768 或 65536）以触发历史压缩，或在左侧开启‘新对话’清空过长历史。")
                if full_content:
                    # 已经给用户看过内容了：诊断长文只留日志，聊天里给一句短的，
                    # 否则一条"喵~"后面会跟着几百字的运维说明。
                    message = (f"回复中断：{stage}阶段与模型服务的连接失联"
                               f"（请求编号 {request_id}），已保留上面已生成的内容。")
                yield ("error", message)
                err_msg = f"【{message}】"
                yield ("text_delta", err_msg)
                full_content.append(err_msg)
        finally:
            cancel_done.set()
            if pump_stop is not None:
                pump_stop.set()
            if cancel_watcher is not None and cancel_event is not None and not cancelled.is_set():
                cancel_watcher.join(0.3)
            # 读线程可能仍阻塞在 readline 上（上游 [DONE] 之后既不关连接也不补
            # 终止块）。先给它 0.4 秒自然退出以保住连接复用，退不掉就 shutdown
            # 读端强制唤醒——否则每轮都会泄漏一个线程并占着连接最长 180 秒。
            if pump_thread is not None and pump_thread.is_alive():
                pump_thread.join(0.4)
                if pump_thread.is_alive() and stream_socket is not None:
                    try:
                        stream_socket.shutdown(socket.SHUT_RD)
                    except OSError:
                        pass
                    pump_thread.join(0.3)

        if not cache_logged and not failed and not cancelled.is_set():
            # 部分 OpenAI 兼容网关会吞掉 stream_options.include_usage，
            # 显式记录一次，避免把"没有数据"误读成"缓存没命中"。
            log("VLM", "cache.usage", request_id=request_id, prompt_tokens=None,
                cached_tokens=None, hit_pct=None, tools_fp=tools_fp, prefix_fp=head_fp,
                note="上游流式响应未返回 usage，无法统计前缀缓存命中")

        if has_reasoning and not reasoning_done:
            reasoning_done = True
            yield ("reasoning_complete", "")

        if cancelled.is_set():
            # 用户主动停止：保留已生成的部分内容，不当作错误，也不提交任何未完成的工具调用。
            log("VLM", "stream.cancelled", request_id=request_id,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                chars=len("".join(full_content)))
            return ("".join(full_content).strip(), [], usage_result)

        terminal = finish_reason == ("tool_calls" if tool_calls_map else "stop")
        # 上游没给终结信号就断流/哑流，但有效内容已经收全：按正常结束处理。
        # 否则会把一句已经答完的回复误判成失败，触发 harness 重试并重复输出，
        # 用户还会收到一大段与聊天无关的诊断文字。
        if soft_tail and not failed and finish_reason is None:
            terminal = True
            log("VLM", "stream.tail.recovered", request_id=request_id, reason=soft_tail,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                received_bytes=received, chars=len("".join(full_content)))
        # A gateway may omit [DONE] after an explicit terminal finish_reason.
        # Conversely, a complete text-only stream can omit finish_reason, but
        # executable tool calls always require their explicit terminal marker.
        text_only_done = (
            complete
            and finish_reason is None
            and bool(full_content)
            and not tool_calls_map
        )
        if not failed and not (terminal or text_only_done):
            failed = True
            if finish_reason == "length":
                error = "【回复达到模型输出上限 (max_tokens)，因思考或生成过长被截断；请调大max_tokens或调低思考强度；未执行本轮工具。】"
            elif finish_reason == "content_filter":
                error = "【模型服务过滤了本次回复；未执行本轮工具。】"
            else:
                error = "【模型流未正常结束，请重试；未执行本轮工具。】"
            yield ("error", error)
            full_content.append(error)
            yield ("text_delta", error)
        final_tool_calls = []
        for idx in sorted(tool_calls_map.keys()):
            tc_data = tool_calls_map[idx]
            if tc_data["name"] and not failed:
                final_tool_calls.append(
                    {
                        "id": tc_data["id"],
                        "type": "function",
                        "function": {
                            "name": tc_data["name"],
                            "arguments": tc_data["arguments"],
                        },
                    }
                )

        return ("".join(full_content).strip(), final_tool_calls, usage_result)

    def generate_bubble_reply(
        self, user_text: str, context: str = "", image_bytes: Optional[bytes] = None
    ) -> str:
        """为桌宠生成轻量气泡回复 (前缀对齐架构)"""
        # 静态规范放置于 system
        system_prompt = (
            "你是一个桌宠助手（形象是IKun小鸡/坤坤）。性格活泼幽默、忠诚可爱、说话简洁清晰。\n"
            "【规则】你的回复将显示在桌宠头顶的小气泡中，务必精简（通常 1~2 句话，尽量在 50 字内），直接给重点。"
        )
        # 易变上下文放置于 user 消息尾部，保护前缀缓存
        user_content = user_text
        if context:
            user_content += f"\n\n[当前环境上下文: {context}]"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        return self.chat(messages, image_bytes=image_bytes)
