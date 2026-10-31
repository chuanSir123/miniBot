"""OneBot channel with bounded async dispatch and acknowledged delivery."""

import asyncio
import difflib
import inspect
import json
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse
from core.config import load_config
from core.console import log
from core.meme import get_meme_manager
from channels.qq_text import TextStyle


def load_text_style() -> TextStyle:
    """读出站整理配置。load_config 自身带缓存与热重载通知，这里不必再缓存。"""
    return TextStyle.from_config(load_config())


try:
    from websockets.asyncio.client import connect
except ImportError:
    connect = None


# 比对前先抹掉标点、空白、emoji 与颜文字，只留下有信息量的汉字/字母/数字。
_PUNCT_RE = re.compile(r"[^\w一-龥]")

# 群职位/群名核验结果的内存缓存：(键, 过期时刻)。职位 5 分钟、群名 30 分钟，
# 避免每条群消息都打一次 NapCat 接口；进程内缓存即可，重启后自然重新核验。
_ROLE_TTL_SEC, _GROUP_INFO_TTL_SEC = 300.0, 1800.0


def split_chat_text(text, max_parts=3):
    """按句末标点切成气口，短碎片并回前段，超长时按字数均衡分配而不是堆到末尾。

    旧实现 `merged[:2] + ["\\n".join(merged[2:])]` 把第 3 段之后的全部用换行
    拼成一条，结果五句话的回复变成「两条短的 + 一大坨」，这正是长回复读起来
    像小作文的直接原因。现在改成按可见字数把尾部均匀并入前面的气口。
    """
    if not text or not text.strip():
        return []
    raw = [part.strip() for part in re.split(r"[\r\n]+|(?<=[。！？~…])(?=[^。！？~…\s])", text) if part.strip()]
    merged, prefix = [], ""
    for part in raw:
        if len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", part)) < 2:
            if merged:
                merged[-1] += part
            else:
                prefix += part
        else:
            merged.append(prefix + part)
            prefix = ""
    if prefix:
        merged.append(prefix)
    # 只剩一段（比如整条都是短句，或本来就是分段排布）时原样退回，
    # 保留原始换行；合并后的字符串会把段落结构压平。
    if len(merged) <= 1:
        return [text.strip()] if text.strip() else []
    if len(merged) <= max_parts:
        return merged
    # 超过上限时按长度均衡分组：保持原文顺序与完整文本，不产生空洞尾巴
    sizes = [len(part) for part in merged]
    remaining_chars = sum(sizes)
    start, groups = 0, []
    for remaining_groups in range(max_parts, 0, -1):
        if remaining_groups == 1:
            groups.append("".join(merged[start:]))
            break
        target = remaining_chars / remaining_groups
        latest_end = len(merged) - remaining_groups + 1
        accumulated, best_end, best_distance = 0, start + 1, None
        for end in range(start + 1, latest_end + 1):
            accumulated += sizes[end - 1]
            distance = abs(accumulated - target)
            if best_distance is None or distance < best_distance:
                best_end, best_distance = end, distance
        groups.append("".join(merged[start:best_end]))
        remaining_chars -= sum(sizes[start:best_end])
        start = best_end
    return [group for group in groups if group.strip()]


class NapCatChannel:
    def __init__(
        self,
        ws_url="ws://127.0.0.1:3001",
        access_token="",
        on_message_callback=None,
        on_observation=None,
        policy=None,
    ):
        self.ws_url, self.access_token = ws_url, access_token
        self.on_message_callback, self.on_observation = (
            on_message_callback,
            on_observation,
        )
        self.policy = policy
        self._parse_slots = None
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self._pending = {}
        self._jobs = set()
        self._seen = OrderedDict()
        self._generation = 0
        self.connected = threading.Event()
        self.last_error = ""
        self._self_id = ""
        self._last_error_log = 0.0
        # 群职位/群名核验缓存：{key: (value, expires_at)}，见模块头说明。
        self._role_cache = {}
        self._group_info_cache = {}

    @staticmethod
    def connection_options(cfg):
        headers = (
            {"Authorization": "Bearer " + cfg["access_token"]}
            if cfg.get("access_token")
            else {}
        )
        options = dict(
            additional_headers=headers,
            open_timeout=8,
            close_timeout=2,
            max_size=2 * 1024 * 1024,
            max_queue=8,
            compression=None,
        )
        # websockets 15 auto-detects system proxies; OneBot loopback must stay local.
        if connect is not None and "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
        return options

    def _report_connection_error(self, exc, url):
        parsed = urlparse(url)
        address = f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'wss' else 80)}"
        if isinstance(exc, OSError) and (
            getattr(exc, "winerror", None) in (10061, 1225, 10060)
            or getattr(exc, "errno", None) in (61, 111, 10061, 1225)
        ):
            message = f"等待 OneBot {address} 开始监听；QQ登录成功不代表NapCat/OneBot已经启动。"
        else:
            message = (
                f"OneBot {address} 连接失败：{type(exc).__name__} {str(exc)[:120]}"
            )
        now = time.monotonic()
        if message != self.last_error or now - self._last_error_log >= 60:
            print("[QQ]", message)
            self._last_error_log = now
        self.last_error = message

    def start(self):
        if connect is None:
            raise RuntimeError("请安装 websockets>=14,<16")
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop, daemon=True, name="bot-qq"
        )
        self._thread.start()

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_and_listen())
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
            self._loop.close()

    async def _fetch_and_remember_account(self):
        """登录成功后获取 QQ 号并自动保存到配置文件，下次启动免扫码自动快速登录。"""
        try:
            await asyncio.sleep(1.5)
            reply = await self.request("get_login_info", {})
            uin = str((reply.get("data") or reply or {}).get("user_id") or "")
            if uin and uin != "0" and uin.isdigit():
                cfg = load_config()
                old_uin = str(cfg["channels"]["napcat"].get("qq_number") or "").strip()
                if old_uin != uin:
                    from core.config import save_config
                    save_config({"channels": {"napcat": {"qq_number": uin}}})
                    log("QQ", "remember_login_account", uin=uin)
                    print(f"[NapCat] 已记住本次登录账号 {uin}，下次启动将自动快速登录，无需再次扫码！")
        except Exception as exc:
            log("QQ", "remember_account_error", error=str(exc))

    async def _connect_and_listen(self):
        delay = 1
        url = self.ws_url
        while self._running:
            try:
                cfg = load_config()["channels"]["napcat"]
                url = cfg.get("ws_url", self.ws_url)
                async with connect(url, **self.connection_options(cfg)) as ws:
                    self._ws = ws
                    self.connected.set()
                    self.last_error = ""
                    print("[QQ] OneBot WebSocket 已连接。")
                    asyncio.create_task(self._fetch_and_remember_account())
                    delay = 1
                    async for raw in ws:
                        data = json.loads(raw)
                        echo = data.get("echo")
                        if echo and echo in self._pending:
                            waiter = self._pending.pop(echo)
                            if not waiter.done():
                                waiter.set_result(data)
                        elif data.get("post_type") == "message":
                            if len(self._jobs) >= 32:
                                continue
                            task = asyncio.create_task(self._handle_onebot_event(data))
                            self._jobs.add(task)
                            task.add_done_callback(self._job_done)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    self._report_connection_error(exc, url)
            finally:
                self._ws = None
                self.connected.clear()
                for waiter in self._pending.values():
                    if not waiter.done():
                        waiter.set_exception(ConnectionError("OneBot连接中断"))
                self._pending.clear()
            if self._running:
                await asyncio.sleep(delay)
                delay = min(15, delay * 2)

    def _job_done(self, task):
        self._jobs.discard(task)
        if not task.cancelled() and task.exception():
            print("[QQ] 消息处理失败:", task.exception())

    @staticmethod
    def eligible(event):
        cfg = load_config()["channels"]["napcat"]
        uid = str(event.get("user_id", ""))
        if not uid or uid == str(event.get("self_id")) or uid in {str(x) for x in cfg.get("blacklisted_user_ids", [])}:
            return False
        owner_ids = {str(x) for x in cfg.get("owner_ids", [])}
        users = owner_ids | {str(x) for x in cfg.get("allowed_user_ids", [])}
        is_owner = uid in owner_ids
        from core.qq_authority import matching_grants
        actor_scope = f"qq:group:{event.get('group_id')}:user:{uid}" if event.get("message_type") == "group" else f"qq:private:{uid}"
        delegated = any(row["allow"] for row in matching_grants(actor_scope))
        if event.get("message_type") == "private":
            return cfg.get("private_allow_all", True) or uid in users or delegated
        if event.get("message_type") == "group":
            allowed_groups = {str(x) for x in cfg.get("allowed_group_ids", [])}
            group_ok = cfg.get("group_allow_all", False) or str(event.get("group_id")) in allowed_groups or is_owner
            return group_ok and (cfg.get("group_members_all", False) or uid in users or delegated)
        return False

    @staticmethod
    def allowed(event):
        if not NapCatChannel.eligible(event):
            return False
        if event.get("_qq_gate_decided"):
            return bool(event.get("_qq_should_reply"))
        cfg = load_config()["channels"]["napcat"]
        if event.get("message_type") != "group" or not cfg.get("group_require_at", True):
            return True
        from channels.qq_media import segments
        return any(
            s.get("type") == "at" and str((s.get("data") or {}).get("qq")) == str(event.get("self_id"))
            for s in segments(event.get("message", event.get("raw_message", "")))
            if isinstance(s, dict)
        )

    async def _try_collect_meme(self, asset, scope, source="group_stealer"):
        """静默收集表情包（群聊/私聊通用）：取原图字节，动图不会退化成首帧。"""
        try:
            ref = (asset or {}).get("media_ref")
            if not ref:
                return
            from core.media_store import MediaStore

            _, data = await asyncio.to_thread(MediaStore().get, ref, scope)
            if data:
                await asyncio.to_thread(get_meme_manager().add_from_bytes, data, source)
        except Exception as exc:
            log("QQ", "meme.collect_error", error=str(exc))

    async def _enrich_group_identity(self, event):
        """给群消息补「职位（群主/管理员/成员）」与群名。

        展示用职位可以来自事件自带的 ``sender.role``（OneBot 11 群消息标准字段）；
        但**用于授权**的职位必须经 ``get_group_member_info`` 向 NapCat 核验，
        结果写进 ``event["_role"]``，``actor_from_event`` 只认核验过的职位。
        接口不可用时退回声明值并标记未核验——职位只影响展示，不影响权限。
        """
        if event.get("message_type") != "group":
            return
        uid = str(event.get("user_id") or "")
        gid = str(event.get("group_id") or "")
        if not (gid.isdigit() and uid.isdigit()):
            return
        declared = str((event.get("sender") or {}).get("role") or "").lower()
        role, verified = declared, False
        cfg = load_config()["channels"]["napcat"]
        cache_key = ("role", gid, uid)
        cached = self._role_cache.get(cache_key)
        if cached and cached[1] > time.time():
            role, verified = cached[0]
        elif cfg.get("group_role_verify", True) and self._ws:
            try:
                data = await self.request(
                    "get_group_member_info",
                    {"group_id": int(gid), "user_id": int(uid), "no_cache": False},
                )
                api_role = str((data or {}).get("role") or "").lower()
                if api_role in ("owner", "admin", "member"):
                    role, verified = api_role, True
                    self._role_cache[cache_key] = ((role, verified), time.time() + _ROLE_TTL_SEC)
            except Exception as exc:
                log("QQ", "role.verify_failed", group_id=gid, user_id=uid, error=str(exc)[:120])
        event["_role"] = {"role": role, "verified": verified,
                          "declared": declared, "source": "api" if verified else "declared"}

        # 群名只是展示信息，30 分钟缓存一次就够。
        if not event.get("group_name"):
            info_key = ("group_info", gid)
            cached_info = self._group_info_cache.get(info_key)
            if cached_info and cached_info[1] > time.time():
                event["group_name"] = cached_info[0]
            elif self._ws:
                try:
                    info = await self.request("get_group_info", {"group_id": int(gid)})
                    name = str((info or {}).get("group_name") or "")[:100]
                    if name:
                        event["group_name"] = name
                        self._group_info_cache[info_key] = (name, time.time() + _GROUP_INFO_TTL_SEC)
                except Exception as exc:
                    log("QQ", "group_info.failed", group_id=gid, error=str(exc)[:120])

    async def _handle_onebot_event(self, event):
        pipeline_started = time.monotonic()
        queue_wait_ms = 0
        if event.get("post_type") != "message":
            return
        if event.get("user_id") is not None and str(event.get("user_id")) == str(
            event.get("self_id")
        ):
            return
        key = (
            event.get("self_id"),
            event.get("message_type"),
            event.get("group_id"),
            event.get("message_id"),
        )
        if event.get("message_id") is not None:
            if key in self._seen:
                return
            self._seen[key] = True
            if len(self._seen) > 2048:
                self._seen.popitem(last=False)

        uid = str(event.get("user_id", ""))
        log("QQ", "received", user_id=uid, group_id=event.get("group_id"), message_id=event.get("message_id"))
        self._self_id = str(event.get("self_id", self._self_id))

        # 1. 黑名单直接前置判定拦截（无论是主人拉黑还是 Jev 自动拉黑）
        if self.policy and self.policy.blocked(event):
            log("QQ", "ignored", reason="blacklisted", user_id=uid)
            return

        # 2. 基础白名单与群聊开启检查
        if not self.eligible(event):
            log("QQ", "ignored", reason="来源或群聊未启用，请检查主人/私聊/群聊配置", user_id=uid)
            return

        if not self.on_message_callback:
            log("QQ", "ignored", reason="没有消息处理回调")
            return

        # 3. 群成员职位（群主/管理员）与群名补全：在 observe 记录聊天流之前完成，
        #    这样聊天记录里的每条发言都自带职位；见 _enrich_group_identity。
        await self._enrich_group_identity(event)

        from channels.qq_media import MessageParser
        if self._parse_slots is None:
            self._parse_slots = asyncio.Semaphore(2)

        async with self._parse_slots:
            parsed = await asyncio.wait_for(MessageParser(self.request, event).parse(), timeout=100)
            event = dict(event)
            event["_parsed"] = parsed
            text = parsed.get("raw_text", parsed["text"]) or ("请查看这些图片。" if parsed["images"] else "")
            log("QQ", "parsed", text_chars=len(text), images=len(parsed["images"]), errors=parsed["errors"])
            if not text:
                log("QQ", "ignored", reason="无可解析内容")
                return

            # Include non-replied messages in subsequent reply decisions, too.
            if self.policy and hasattr(self.policy, "observe"):
                self.policy.observe(parsed.get("raw_text", text), event)

            # 静默表情包收集（群聊 + 私聊）。私聊里发的图往往就是主人/群友的表情包，
            # 之前只在群聊生效，私聊图片只进了媒体库、没进表情包库。
            # 不再只取前两张：表情包通常成串发，掐头两张会让后面更多的好图漏掉，
            # 上限交给 max_images 与入库体积把关。
            if parsed.get("media"):
                from core.decision_context import qq_scope

                scope = qq_scope(event)
                is_group = event["message_type"] == "group"
                meme_cfg = load_config().get("meme", {})
                switch = "collect_group" if is_group else "collect_private"
                if meme_cfg.get(switch, True):
                    source = "group_stealer" if is_group else "private_stealer"
                    current_id = str(event.get("message_id") or "")
                    limit = int(meme_cfg.get("max_collect_per_message", 9))
                    for asset in parsed["media"][:limit]:
                        # 只收这条消息自己带的图，引用/转发里那些是别人的图，重复入库没意义。
                        if current_id:
                            if str(asset.get("message_id") or "") != current_id:
                                continue
                        elif asset.get("origin") != "received":
                            continue
                        asyncio.create_task(self._try_collect_meme(asset, scope, source))

            result = None
            if self.policy:
                # 3. 主人指令前置处理（/拉黑、/取消拉黑、/黑名单等）
                result = await asyncio.to_thread(self.policy.owner_command, parsed["direct_text"], event)

            if result is None:
                cfg = load_config()["channels"]["napcat"]
                is_private = event["message_type"] == "private"
                is_mentioned = bool(parsed.get("mentioned") or parsed.get("reply_to_bot"))

                if is_private or is_mentioned:
                    # 4. 强交互信号（私聊或被 @、引用回复）：直接前置判断为回复！不走 Jev 回复意图门控
                    should_reply = True
                elif cfg.get("group_smart_reply", True):
                    # 5. 弱交互信号（未被 @ 的群消息）：调用 Jev 智能研判（是否喊了人设名字，或明确求助）
                    should_reply = await asyncio.to_thread(self.policy.decide_reply, text, event)
                else:
                    should_reply = False

                event["_qq_gate_decided"] = True
                event["_qq_should_reply"] = should_reply

                if self.on_observation and event["message_type"] == "group" and cfg.get("listen_groups"):
                    self.on_observation(text, event)

                if not should_reply:
                    log("QQ", "ignored", reason="jev reply gate", user_id=uid)
                    return

        sent_intermediate = set()
        sent_progress_history = []
        last_intermediate = None

        def _clean_norm(s: str) -> str:
            return _PUNCT_RE.sub("", s or "")

        def _similarity(old: str, new: str) -> float:
            """顺序敏感的重复度，0~1；共用长前缀或整句只改几个字都会拿到高分。"""
            if not old or not new:
                return 0.0
            return difflib.SequenceMatcher(None, old, new, autojunk=False).ratio()

        # 顺序相似度下限。0.78 能把「我先创建文件」→「我先创建一下文件」判重，
        # 又放过「正在读取 config.json」→「正在读取 package.json」这种换了对象的新进度。
        # 旧实现用字符集合 Jaccard，词序无关：「我给你点个赞」和「点个赞给你我」会被
        # 判成同一句，而改写式复述（换个说法把同一件事再讲一遍）反而能逃掉。
        _SIMILAR_RATIO = 0.78
        # 终稿与最后一条进度的比对要保守：中间进度多发一条只是吵，终稿漏发就是丢结果。
        _FINAL_RATIO = 0.92

        def _is_repeat(message: str, previous: str, ratio_floor: float = _SIMILAR_RATIO) -> bool:
            c_old, c_new = _clean_norm(previous), _clean_norm(message)
            if not c_old or not c_new:
                return False
            if c_old == c_new:
                return True
            return _similarity(c_old, c_new) >= ratio_floor

        def _carries_media(msg: str) -> bool:
            """含媒体引用的进度属于交付物，留给终稿统一发送。

            这是结构性判断，不是措辞匹配：中间消息本来就不发媒体附件，若文本里已经
            挂上 media:// 引用，说明模型在提前交付，先把整条压住。
            """
            return "media://" in msg

        if result is None:
            loop = asyncio.get_running_loop()
            progress = asyncio.Queue()
            accepting_progress = True

            def enqueue(message):
                if accepting_progress and isinstance(message, str) and message.strip():
                    progress.put_nowait(message.strip())

            def on_intermediate(message):
                if isinstance(event, dict):
                    event["_used_tools"] = True
                loop.call_soon_threadsafe(enqueue, message)

            async def send_progress():
                nonlocal last_intermediate
                while True:
                    message = await progress.get()
                    if message is None:
                        return
                    if message in sent_intermediate:
                        continue

                    # 1. 带媒体引用的进度不偷跑，留给最终答复统一交付
                    if _carries_media(message):
                        log("QQ", "intermediate.skip_media_claim", preview=message[:60])
                        continue

                    # 2. 与最近几条进度比对，改写式复述一并拦掉
                    if any(_is_repeat(message, prev) for prev in sent_progress_history[-3:]):
                        log("QQ", "intermediate.skip_similar", preview=message[:60])
                        continue

                    # 3. 截断内部核验过长细节，只保留首句或主要进度提示
                    trimmed = message.strip()
                    if "\n" in trimmed:
                        first_line = trimmed.split("\n", 1)[0].strip()
                        if len(first_line) >= 10:
                            trimmed = first_line
                    if len(trimmed) > 120:
                        trimmed = trimmed[:120] + "..."

                    try:
                        event["_is_intermediate"] = True
                        try:
                            delivered = await self.send_reply_segments(event, trimmed)
                        finally:
                            event.pop("_is_intermediate", None)
                        if delivered:
                            sent_intermediate.add(message)
                            sent_intermediate.add(trimmed)
                            sent_progress_history.append(trimmed)
                            last_intermediate = message
                    except Exception as exc:
                        log("QQ", "intermediate.error", error=str(exc))

            event["_on_intermediate"] = on_intermediate
            progress_task = asyncio.create_task(send_progress())
            try:
                # 回调里含 Jev 意图判定（一次 HTTP，超时 35s）和入队动作。直接在事件
                # 循环线程里调，会把**所有** QQ 发送人的消息一起卡住——群消息、私聊、
                # 心跳、媒体解析全在这一个 loop 上。丢到线程池里执行，loop 立刻返回。
                result = await asyncio.to_thread(self.on_message_callback, text, event)
                if isinstance(result, __import__("concurrent.futures", fromlist=["Future"]).Future):
                    pending_result = result
                    result = await asyncio.wrap_future(pending_result)
                    queue_wait_ms = getattr(pending_result, "queue_wait_ms", 0)
                elif inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                log("QQ", "reply.error", error=str(exc))
                # 走到这里模型通常已经不可用了，没法让人设来组织这句话，
                # 至少别用"请稍后再试"这种客服腔。
                result = "刚那条没处理成，你再说一遍？"
            finally:
                # Flush thread-safe enqueue callbacks before the sentinel, then
                # drain in order so the final reply cannot overtake progress.
                await asyncio.sleep(0)
                accepting_progress = False
                progress.put_nowait(None)
                try:
                    await progress_task
                finally:
                    if not progress_task.done():
                        progress_task.cancel()

        final_send_started = time.monotonic()
        should_send_final = True
        if not result or not isinstance(result, str) or not result.strip():
            should_send_final = False
        elif last_intermediate:
            clean_res = result.strip()
            # 只有在没有新媒体引用且内容与最后一次中间消息完全相同或高度雷同时才跳过
            if clean_res == last_intermediate.strip():
                should_send_final = False
            elif "media://" not in clean_res and _is_repeat(clean_res, last_intermediate, _FINAL_RATIO):
                should_send_final = False

        if should_send_final:
            await self.send_reply_segments(event, result)

        log("QQ", "reply.complete", message_id=event.get("message_id"),
            elapsed_ms=round((time.monotonic() - pipeline_started) * 1000),
            queue_wait_ms=queue_wait_ms,
            final_send_ms=round((time.monotonic() - final_send_started) * 1000))

    async def send_reply_segments(self, event, text_reply):
        is_intermediate = bool(isinstance(event, dict) and event.get("_is_intermediate"))
        from channels.qq_reply import reply_parts

        try:
            parts = reply_parts(text_reply, event)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            log("QQ", "reply.media_error", error=str(exc))
            parts = [{"type": "text", "data": {"text": "附件发送失败：" + str(exc)[:180]}}]

        # 中间进度消息不发送媒体附件，避免提前投递大文件和重复发送
        if is_intermediate:
            parts = [p for p in parts if p.get("type") == "text"]
            if not parts or not any(p.get("data", {}).get("text", "").strip() for p in parts):
                return False

        private = event.get("message_type") == "private"
        action = "send_private_msg" if private else "send_group_msg"
        target = event.get("user_id") if private else event.get("group_id")
        all_delivered = bool(parts)

        # 同一个媒体引用（media_ref）在单轮请求生命周期内严禁重复发送
        sent_media_refs = event.setdefault("_sent_media_refs", set())

        for part in parts:
            if part["type"] == "text":
                if part["data"]["text"].strip():
                    delivered = await self._send_text_reply(event, part["data"]["text"])
                    all_delivered = bool(delivered) and all_delivered and "[附件发送失败：" not in part["data"]["text"]
            else:
                media_info = part.get("_media") or {}
                ref = media_info.get("media_ref") or part.get("data", {}).get("file", "")
                if ref and ref in sent_media_refs:
                    log("QQ", "reply.skip_duplicate_media", media_ref=ref)
                    continue

                from core.decision_context import qq_scope
                delivered = await self._send_segments(action, target, [{"type": part["type"], "data": part["data"]}],
                                                      media_asset=part.get("_media"), asset_scope=qq_scope(event))
                if delivered and ref:
                    sent_media_refs.add(ref)
                all_delivered = bool(delivered) and all_delivered
                log("QQ", "reply.sent_media", user_id=str(event.get("user_id", "")), kind=part["type"], ok=delivered)
                if not delivered:
                    await self._send(action, target, "附件发送失败，请检查文件是否可访问，以及 NapCat 是否支持该媒体格式。")
                elif self.policy and hasattr(self.policy, "observe"):
                    sent_event = dict(event, _sent_media=[part["_media"]] if part.get("_media") else [])
                    self.policy.observe("[已发送" + {"image": "图片", "record": "语音", "video": "视频"}[part["type"]] + "]", sent_event, role="assistant")
        return all_delivered

    @staticmethod
    def _meme_chat_key(event) -> str:
        """会话标识：群聊按群、私聊按人。表情避让只看同一个会话，互不影响。"""
        if not isinstance(event, dict):
            return ""
        if event.get("message_type") == "private":
            return f"user:{event.get('user_id')}"
        return f"group:{event.get('group_id')}"

    async def _pick_meme_for_turn(self, event, reply_text: str):
        """一轮对话最多发一次表情；同一轮最多决策两次（进度一次、终稿一次）。

        进度的语境和终稿不一定相同，所以进度上"没选中"不该连累终稿；但真发出去
        一次之后就必须收手——同一轮里连发几张才是噪音的来源。
        """
        if not isinstance(event, dict) or event.get("_meme_sent"):
            return None
        if int(event.get("_meme_tries") or 0) >= 2:
            return None
        event["_meme_tries"] = int(event.get("_meme_tries") or 0) + 1
        parsed = event.get("_parsed") or {}
        incoming = parsed.get("direct_text") or parsed.get("text") or ""
        exclude = event.setdefault("_meme_paths", set())
        chat_key = self._meme_chat_key(event)
        try:
            path = await asyncio.to_thread(
                get_meme_manager().choose_meme, incoming, reply_text, list(exclude), chat_key
            )
        except Exception as exc:
            log("QQ", "meme.choose_error", error=str(exc)[:160])
            return None
        if path:
            event["_meme_sent"] = True
            exclude.add(path)
        return path

    async def _send_text_reply(self, event, text_reply: str):
        """拟人化分段发送回复，并附带一张与本轮语境匹配的表情包。"""
        uid = str(event.get("user_id", ""))
        is_private = event.get("message_type") == "private"
        target = event.get("user_id") if is_private else event.get("group_id")
        action = "send_private_msg" if is_private else "send_group_msg"

        from core.meme import extract_meme_tag

        # 模型不再通过 [表情: xx] 下单（选择权已交给 Jev），这里只负责把历史残留的
        # 标记从正文里剥掉，免得用户看到中括号。
        clean_text, _legacy_hint = extract_meme_tag(text_reply)
        # 报文类兜底文案也算错误：报错原样送出，不做任何「美化」。
        is_error = clean_text.lstrip().startswith(
            ("【模型接口错误", "【对话模型请求超时", "【VLM", "模型连接失败",
             "【[WinError", "【[Errno", "【网络", "【连接")
        )

        # 工具场景曾经整条不拆，727 字的搜索结果总结就是这样变成「一大筐」的——
        # 用了工具只说明内容是交付物，不代表它不该分条。现在只有代码块必须整体送达。
        used_tools = bool(isinstance(event, dict) and event.get("_used_tools"))
        has_code = "```" in clean_text
        if is_error:
            segments_to_send = [clean_text]
        else:
            # QQ 不渲染 Markdown，模型却会从搜索结果里把 **加粗**、# 标题一起抄过来。
            # 提示词是请求，这里是保证。规则本身来自 persona.style，不是渠道硬编码：
            # 剥格式是通道特性，砍客套/限装饰才是人设偏好，换人设只改配置。
            style = load_text_style()
            clean_text = style.tidy_tool_reply(clean_text) if used_tools else style.tidy(clean_text)
            segments_to_send = (
                [clean_text] if has_code
                else split_chat_text(clean_text, max_parts=style.parts_for(clean_text))
            )

        all_delivered = bool(segments_to_send)
        # 挑表情要问 Jev（一次 HTTP，通常 1~2s）。让它在正文发送的同时跑，
        # 而不是发完再问：多段消息本来就有打字间隔，这段时间正好用来做决策。
        meme_task = None if is_error else asyncio.create_task(
            self._pick_meme_for_turn(event, clean_text)
        )
        try:
            # 逐段模拟打字发送
            for i, segment in enumerate(segments_to_send):
                if i > 0:
                    delay = min(1.8, max(0.8, len(segment) * 0.05 + random.uniform(0.3, 0.7)))
                    await asyncio.sleep(delay)
                delivered = await self._send(action, target, segment)
                all_delivered = bool(delivered) and all_delivered
                if delivered and self.policy and hasattr(self.policy, "observe"):
                    self.policy.observe(segment, event, role="assistant")
                log("QQ", "reply.sent_segment", user_id=uid, segment=i + 1, ok=delivered)

            meme_file = await meme_task if meme_task else None
        finally:
            if meme_task and not meme_task.done():
                meme_task.cancel()
        if meme_file and Path(meme_file).exists():
            await asyncio.sleep(random.uniform(0.5, 1.0))
            file_url = Path(meme_file).resolve().as_uri()
            meme_payload = [{"type": "image", "data": {"file": file_url}}]
            delivered = await self._send_segments(action, target, meme_payload)
            log("QQ", "reply.sent_meme", user_id=uid, ok=delivered, meme=Path(meme_file).name)
            # 真发出去才记冷却：选中但发送失败的图不该被白白避开
            if delivered:
                get_meme_manager().note_sent(self._meme_chat_key(event), meme_file)
        return all_delivered

    async def request(self, action, params):
        if not self._ws:
            raise ConnectionError("OneBot未连接")
        echo = uuid.uuid4().hex
        waiter = asyncio.get_running_loop().create_future()
        self._pending[echo] = waiter
        try:
            await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            return await asyncio.wait_for(waiter, 10)
        finally:
            self._pending.pop(echo, None)

    async def _send_segments(self, action, target, message_segments, media_asset=None, asset_scope=None):
        """发送结构化消息片段（支持图片、文本混排）。"""
        if not self._ws:
            return False
        echo = uuid.uuid4().hex
        waiter = self._loop.create_future()
        self._pending[echo] = waiter
        field = "group_id" if action == "send_group_msg" else "user_id"
        payload = {
            "action": action,
            "params": {
                field: int(target),
                "message": message_segments,
            },
            "echo": echo,
        }
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            is_media = any(s.get("type") in ("image", "record", "video") for s in message_segments)
            reply = await asyncio.wait_for(waiter, 120 if is_media else 10)
            if reply.get("status") != "ok" or reply.get("retcode") != 0:
                log("QQ", "send.error", action=action, retcode=reply.get("retcode"),
                    detail=reply.get("message") or reply.get("wording"))
            elif media_asset and asset_scope and isinstance(reply.get("data"), dict) and reply["data"].get("message_id") is not None:
                try:
                    from core.media_store import MediaStore
                    MediaStore().bind_message(media_asset["media_ref"], asset_scope, reply["data"]["message_id"])
                except Exception as exc:
                    log("QQ", "media.bind_error", detail=str(exc))
            return reply.get("status") == "ok" and reply.get("retcode") == 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log("QQ", "send.error", action=action, exception_type=type(exc).__name__, detail=str(exc))
            return False
        finally:
            self._pending.pop(echo, None)

    async def _send(self, action, target, message):
        return await self._send_segments(
            action, target, [{"type": "text", "data": {"text": str(message)[:12000]}}]
        )

    async def send_private_msg(self, user_id, message):
        delivered = await self._send("send_private_msg", user_id, message)
        if delivered and self.policy and hasattr(self.policy, "observe"):
            self.policy.observe(message, {"message_type": "private", "user_id": user_id, "self_id": self._self_id}, role="assistant")
        return delivered

    async def send_group_msg(self, group_id, message):
        delivered = await self._send("send_group_msg", group_id, message)
        if delivered and self.policy and hasattr(self.policy, "observe"):
            self.policy.observe(message, {"message_type": "group", "group_id": group_id, "self_id": self._self_id}, role="assistant")
        return delivered

    def send_notification(self, channel, target, message):
        if not self._running or not self._loop or self._loop.is_closed():
            return False
        coro = (
            self.send_group_msg(target, message)
            if channel == "qq_group"
            else self.send_private_msg(target, message)
        )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(12)
        except Exception:
            future.cancel()
            return False

    def reload(self):
        if self._ws and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def stop(self):
        self._running = False
        if self._loop and not self._loop.is_closed():

            def cancel_all():
                for task in asyncio.all_tasks(self._loop):
                    task.cancel()

            self._loop.call_soon_threadsafe(cancel_all)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(4)
