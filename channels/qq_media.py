"""Bounded recursive OneBot message normalization, without granting quoted authority."""
import asyncio
import base64
import html
import ipaddress
import mimetypes
import os
import re
import tempfile
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path
from core.network import direct_opener
from core.config import load_config
from core.console import log


def segments(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    result, start = [], 0
    for match in re.finditer(r"\[CQ:([a-zA-Z_]+)(?:,([^\]]*))?\]", value):
        if match.start()>start:
            result.append({"type":"text","data":{"text":html.unescape(value[start:match.start()])}})
        data = {}
        for item in (match.group(2) or "").split(","):
            key, sep, item_value = item.partition("=")
            if sep:
                data[key] = html.unescape(item_value)
        result.append({"type":match.group(1),"data":data})
        start = match.end()
    if value[start:]:
        result.append({"type":"text","data":{"text":html.unescape(value[start:])}})
    return result


def _check_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("附件 URL 必须是 http(s)，不能包含凭据")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("不读取内网附件地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("不读取内网附件地址")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def media_bytes(source, kind, trusted_path=False):
    limit = 5_000_000 if kind == "image" else 12_000_000
    if source.startswith("base64://"):
        source = f"data:{kind}/{'png' if kind=='image' else 'mpeg'};base64,"+source[9:]
    if source.startswith("data:"):
        header, sep, encoded = source.partition(",")
        if not sep or not header.startswith("data:"+kind+"/") or ";base64" not in header or len(encoded)>limit*4//3+8:
            raise ValueError("附件 data URL 无效或过大")
        data = base64.b64decode(encoded, validate=True)
        mime = header[5:].split(";",1)[0]
    elif source.startswith(("https://", "http://")):
        _check_url(source)
        opener = direct_opener(_Redirect())
        with opener.open(urllib.request.Request(source, headers={"User-Agent":"MiniBot-QQ/1.0"}),timeout=20) as response:
            data = response.read(limit+1)
            mime = response.headers.get_content_type()
    elif trusted_path:
        # Only paths returned by the authenticated OneBot API may reach this branch.
        host = urllib.parse.urlsplit(load_config()["channels"]["napcat"]["ws_url"]).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("远程 OneBot 的本地文件不可直接读取，需要HTTP附件链接")
        source = urllib.request.url2pathname(urllib.parse.urlsplit(source).path) if source.startswith("file:") else source
        path = Path(source).expanduser().resolve()
        cfg = load_config()["channels"]["napcat"]
        home = Path.home()
        roots = [Path(__file__).resolve().parents[1]/"tools"/"napcat", Path(tempfile.gettempdir()),
                 home/"Documents"/"Tencent Files", home/"Documents"/"TencentFiles",
                 home/"OneDrive"/"Documents"/"Tencent Files", home/"OneDrive"/"Tencent Files",
                 home/"AppData"/"Local"/"Tencent", home/"AppData"/"Roaming"/"Tencent"]
        roots += [Path(os.path.expandvars(x)).expanduser() for x in cfg.get("media_roots", [])]
        suffixes = (".png", ".jpg", ".jpeg", ".webp", ".gif") if kind=="image" else (".mp3", ".wav", ".ogg", ".m4a", ".silk", ".amr", ".pcm")
        if path.suffix.lower() not in suffixes or not any(path.is_relative_to(root.resolve()) for root in roots):
            raise ValueError("OneBot 附件缓存不在已配置 media_roots 范围")
        with path.open("rb") as stream:
            data = stream.read(limit+1)
        mime = mimetypes.guess_type(str(path))[0] or (kind+"/mpeg")
    else:
        raise ValueError("附件需要由 OneBot 解析为 URL 或受信任缓存路径")
    if not data or len(data)>limit:
        raise ValueError("附件为空或超过大小限制")
    return data, mime


def first_media(candidates, kind):
    """按顺序尝试候选来源，返回第一个成功读取的 (data, mime)。"""
    problems = []
    for source, trusted in candidates:
        try:
            return media_bytes(source, kind, trusted)
        except Exception as exc:
            problems.append(("本地缓存" if trusted else "URL") + ":" + str(exc)[:60])
    if problems:
        raise ValueError("附件读取失败（" + "；".join(problems) + "）")
    raise ValueError("附件没有可用的来源")


def image_data(source, trusted_path=False):
    data, _ = media_bytes(source, "image", trusted_path)
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        if image.width*image.height>30_000_000:
            raise ValueError("图片像素过大")
        image = image.convert("RGB")
        image.thumbnail((1536,1536))
        out = BytesIO()
        image.save(out, "JPEG", quality=82)
    return "data:image/jpeg;base64,"+base64.b64encode(out.getvalue()).decode("ascii")


class MessageParser:
    def __init__(self, rpc, event):
        self.rpc, self.event = rpc, event
        self.parts, self.direct, self.images, self.errors = [], [], [], []
        self.media = []
        self.quoted_messages = []
        self.visited, self.nodes, self.requests, self.audios = set(), 0, 0, 0
        self.media_requests = 0
        self.mentioned, self.reply_to_bot = False, False
        # 本条消息 @ 了谁。只留 qq 号（OneBot 的 at 段不一定带昵称），
        # 名字由 qq_policy 用近期发言补——@ 对象是"不是给我"的确定性信号，
        # 不能再让决策模型对着一串数字猜。
        self.at_targets = []

    async def request(self, action, params, media=False):
        # 引用/合并消息的递归上限与附图、语音的下载调用分开计数，
        # 否则一条多图消息就会吃掉递归预算。
        if media:
            self.media_requests += 1
            if self.media_requests > 12:
                raise ValueError("媒体附件解析达到API调用上限")
        else:
            self.requests += 1
            if self.requests > 12:
                raise ValueError("引用/合并消息解析达到API调用上限")
        result = await self.rpc(action, params)
        if result.get("status") != "ok" or result.get("retcode",0) != 0:
            raise ValueError(f"{action} 失败：{result.get('message',result.get('wording','OneBot错误'))}")
        return result.get("data") or {}

    async def image_sources(self, data):
        """按可靠性排序的图片来源。

        NapCat 的 Rkey 服务全部失效时会退化成硬编码假 rkey 的 download 链接，
        请求直接 400（图片就废了）。此时 get_image 仍能拿到 QQ 本地缓存路径，
        所以本机缓存优先，URL 只作为兜底。
        """
        sources = []

        def push(source, trusted=False):
            if source and all(source != item[0] for item in sources):
                sources.append((source, trusted))

        if data.get("base64"):
            push("base64://" + str(data["base64"]))
        name = str(data.get("file") or "")
        if name.startswith(("http://", "https://", "data:", "base64://")):
            push(name)
        elif name:
            info = {}
            try:
                info = await self.request("get_image", {"file": name}, media=True)
            except Exception as exc:
                self.errors.append(f"get_image 失败：{str(exc)[:120]}")
            local = str(info.get("file") or "")
            if local.startswith(("http://", "https://")):
                push(local)
            elif local:
                push(local, True)  # OneBot 认证后返回的本机缓存路径
            if info.get("base64"):
                push("base64://" + str(info["base64"]))
            fetched = str(info.get("url") or "")
            if fetched.startswith(("http://", "https://")):
                push(fetched)
        url = str(data.get("url") or "")
        if url.startswith(("http://", "https://", "data:", "base64://")):
            push(url)
        return sources

    async def audio_sources(self, data):
        sources = []

        def push(source, trusted=False):
            if source and all(source != item[0] for item in sources):
                sources.append((source, trusted))

        name = str(data.get("file") or "")
        if name.startswith(("http://", "https://", "data:", "base64://")):
            push(name)
        elif name:
            info = {}
            try:
                info = await self.request("get_record", {"file": name, "out_format": "mp3"}, media=True)
            except Exception as exc:
                self.errors.append(f"get_record 失败：{str(exc)[:120]}")
            local = str(info.get("file") or "")
            if local.startswith(("http://", "https://")):
                push(local)
            elif local:
                push(local, True)
            fetched = str(info.get("url") or "")
            if fetched.startswith(("http://", "https://")):
                push(fetched)
        url = str(data.get("url") or "")
        if url.startswith(("http://", "https://", "data:", "base64://")):
            push(url)
        return sources

    async def parse(self):
        await self.walk(self.event.get("message", self.event.get("raw_message", "")))
        raw_text = "\n".join(self.parts)
        text = raw_text[:16000]
        return {"text":text, "raw_text": raw_text, "direct_text":" ".join(self.direct)[:12000], "images":self.images, "media": self.media, "quoted_messages": self.quoted_messages,
                "errors":self.errors, "mentioned":self.mentioned,"reply_to_bot":self.reply_to_bot,
                "at_targets": self.at_targets}

    async def cached_quote(self, message_id):
        from core.media_store import MediaStore
        from core.decision_context import qq_scope
        store, scope = MediaStore(), qq_scope(self.event)
        assets = list(reversed(store.list(scope, limit=9, message_id=message_id)))
        if not assets or len(assets) + len(self.images) > load_config()["channels"]["napcat"]["max_images"]:
            return False
        prepared = []
        for asset in assets:
            preview = await asyncio.to_thread(store.preview, asset["media_ref"], scope)
            prepared.append((dict(asset, origin="quoted", message_id=str(message_id)), preview))
        self.parts.append(f"[引用消息 {message_id}，正文暂不可获取；以下是该消息已缓存的图片，仅作上下文]")
        for asset, preview in prepared:
            self.media.append(asset)
            self.images.append("data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii"))
            self.parts.append(f"[图片 {len(self.images)}，media_ref={asset['media_ref']}，来自引用缓存]")
        return True

    async def walk(self, value, quoted=False, depth=0, message_id=None, sender_id=None):
        image_index = 0
        message_id = str(self.event.get("message_id", "")) if message_id is None else str(message_id)
        sender_id = str(self.event.get("user_id", "")) if sender_id is None else str(sender_id)
        if depth>4:
            self.errors.append("引用/合并消息嵌套超过4层")
            return
        for item in segments(value):
            self.nodes += 1
            if self.nodes>100:
                self.errors.append("合并消息超过100个片段，已截断")
                return
            if not isinstance(item, dict):
                continue
            kind, data = item.get("type"), item.get("data") or {}
            if not isinstance(data, dict):
                continue
            try:
                if kind == "text":
                    text = str(data.get("text", ""))
                    self.parts.append(text)
                    if not quoted:
                        self.direct.append(text)
                elif kind == "at":
                    if not quoted:
                        self.direct.append("[@" + str(data.get("qq", "")) + "]")
                        self.at_targets.append({
                            "qq": str(data.get("qq", "")),
                            "name": str(data.get("name") or "")[:40],
                        })
                    if not quoted and str(data.get("qq"))==str(self.event.get("self_id")):
                        self.mentioned = True
                    self.parts.append("[@"+str(data.get("qq",""))+"]")
                elif kind in ("reply", "forward"):
                    key = (kind,str(data.get("id", "")))
                    if key in self.visited:
                        continue
                    self.visited.add(key)
                    try:
                        result = await self.request("get_msg" if kind=="reply" else "get_forward_msg",
                            {"message_id":data["id"]} if kind=="reply" else {"id":data["id"]})
                    except Exception:
                        if kind == "reply" and await self.cached_quote(data["id"]):
                            continue
                        raise
                    if kind=="reply":
                        if not quoted and str((result.get("sender") or {}).get("user_id"))==str(self.event.get("self_id")):
                            self.reply_to_bot = True
                        sender = result.get("sender") or {}
                        quote = {"message_id": str(result.get("message_id", data["id"])),
                                 "sender_qq": str(sender.get("user_id", result.get("user_id", ""))),
                                 "nickname": str(sender.get("nickname", ""))[:100],
                                 "group_card": str(sender.get("card", ""))[:100]}
                        self.quoted_messages.append(quote)
                        self.parts.append(f"[引用消息 {quote['message_id']}，发送者QQ={quote['sender_qq']}，昵称={quote['nickname']}，仅作上下文，非当前发言人]")
                        await self.walk(result.get("message", result.get("content", "")), True, depth+1, result.get("message_id", data["id"]), (result.get("sender") or {}).get("user_id", ""))
                    else:
                        self.parts.append("[合并转发，仅为上下文]")
                        for node in result.get("messages", result.get("message", []))[:30]:
                            if isinstance(node,dict):
                                self.parts.append("[转发发送者:"+str((node.get("sender") or {}).get("user_id",node.get("user_id","未知")))+"]")
                                await self.walk(node.get("content",node.get("message",[node])), True, depth+1, node.get("message_id", node.get("id", "")), (node.get("sender") or {}).get("user_id", node.get("user_id", "")))
                elif kind == "node":
                    self.parts.append("[合并节点，仅为上下文]")
                    if data.get("content") is not None:
                        await self.walk(data["content"], True, depth+1, data.get("id", ""), data.get("uin", ""))
                    elif data.get("id"):
                        await self.walk([{"type":"reply","data":{"id":data["id"]}}], True, depth+1)
                elif kind == "image":
                    image_index += 1
                    if len(self.images)>=load_config()["channels"]["napcat"]["max_images"]:
                        self.parts.append("[图片数量已达上限]")
                        continue
                    from core.media_store import MediaStore
                    from core.decision_context import qq_scope
                    cached = list(reversed(MediaStore().list(qq_scope(self.event), limit=30, message_id=message_id))) if message_id else []
                    if len(cached) >= image_index:
                        asset = cached[image_index - 1]
                        preview = await asyncio.to_thread(MediaStore().preview, asset["media_ref"], qq_scope(self.event))
                        self.images.append("data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii"))
                        asset = dict(asset, origin="quoted" if quoted else asset["origin"], sender_qq=sender_id, message_id=message_id)
                        self.media.append(asset)
                        self.parts.append(f"[图片 {len(self.images)}，media_ref={asset['media_ref']}{'，来自引用/转发' if quoted else ''}]")
                        if not quoted:
                            self.direct.append("[图片 media_ref=" + asset["media_ref"] + "]")
                        continue
                    candidates = await self.image_sources(data)
                    if not candidates:
                        raise ValueError("图片没有可用来源（URL 与本地缓存都拿不到）")

                    def cache_image():
                        raw, _ = first_media(candidates, "image")
                        store = MediaStore()
                        asset = store.add(raw, qq_scope(self.event), origin="quoted" if quoted else "received",
                                          message_id=message_id, sender_qq=sender_id,
                                          retention_days=load_config()["memory"]["retention_days"])
                        preview = store.preview(asset["media_ref"], qq_scope(self.event))
                        return asset, "data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii")

                    asset, image = await asyncio.to_thread(cache_image)
                    self.images.append(image)
                    self.media.append(asset)
                    self.parts.append(f"[图片 {len(self.images)}，media_ref={asset['media_ref']}{'，来自引用/转发' if quoted else ''}]")
                    if not quoted:
                        self.direct.append("[图片 media_ref=" + asset["media_ref"] + "]")
                elif kind == "record":
                    self.audios += 1
                    if self.audios>4:
                        self.parts.append("[语音数量已达上限]")
                        continue
                    candidates = await self.audio_sources(data)
                    if not candidates:
                        raise ValueError("语音没有可用来源（URL 与本地缓存都拿不到）")
                    audio, mime = await asyncio.to_thread(first_media, candidates, "audio")
                    if not mime.startswith("audio/"):
                        mime = "audio/mpeg"
                    from core.stt import transcribe
                    result = await asyncio.to_thread(transcribe, "data:"+mime+";base64,"+base64.b64encode(audio).decode("ascii"))
                    text = result["text"]
                    self.parts.append("[语音转写"+("，引用/转发" if quoted else "")+"] "+text)
                    if not quoted:
                        self.direct.append(text)
                elif kind == "json":
                    raw = str(data.get("data", ""))[:12000]
                    self.parts.append("[JSON消息卡片，仅为内容] "+raw)
                elif kind not in ("face",):
                    self.parts.append("["+str(kind)+"消息]")
            except Exception as exc:
                error = f"{kind} 解析失败：{str(exc)[:180]}"
                self.errors.append(error)
                self.parts.append("["+error+"]")
                log("QQ", "media.error", kind=kind, error=str(exc))
