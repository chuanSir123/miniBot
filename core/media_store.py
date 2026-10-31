"""Persistent scoped media references; original bytes never enter model history."""
import base64
import contextlib
import hashlib
import re
import secrets
import sqlite3
import time
from io import BytesIO
from pathlib import Path

MEDIA_ROOT = Path(__file__).resolve().parents[1] / "data" / "media"

# mime -> 落盘扩展名。下游客户端（NapCat / 浏览器）靠扩展名识别容器类型。
EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/x-flv": ".flv",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/silk": ".silk",
    "audio/aiff": ".aiff",
}

KINDS = ("image", "video", "audio")
REF_PREFIX = {"image": "img_", "video": "vid_", "audio": "aud_"}
REF_PATTERN = re.compile(r"(?:img|vid|aud)_[a-f0-9]{16}")
KIND_LABEL = {"image": "图片", "video": "视频", "audio": "音频"}
MAX_BYTES = {"image": 20_000_000, "video": 100_000_000, "audio": 50_000_000}


def sniff_media(data):
    """按文件头识别媒体类型，不依赖 Pillow。

    返回 (kind, mime)；无法识别时返回 (None, None)。
    """
    if not isinstance(data, bytes) or len(data) < 12:
        return None, None
    head = data[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[:2] == b"BM":
        return "image", "image/bmp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P ", b"F4A "):
            return "audio", "audio/mp4"
        if brand.startswith(b"qt"):
            return "video", "video/quicktime"
        return "video", "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        # EBML 容器：WebM 与 MKV 共用，靠 Doctype 区分。
        return ("video", "video/webm") if b"webm" in data[:256] else ("video", "video/x-matroska")
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video", "video/x-msvideo"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "audio", "audio/aiff"
    if head[:4] == b"OggS":
        return "audio", "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio", "audio/flac"
    if head[:5] == b"#!AMR":
        return "audio", "audio/amr"
    if head[:9] == b"#!SILK_V3":
        return "audio", "audio/silk"
    if head[:3] == b"ID3":
        return "audio", "audio/mpeg"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG 音频帧头带 layer 位；ADTS/AAC 的 layer 位为 0。
        return ("audio", "audio/mpeg") if (head[1] >> 1) & 0x03 else ("audio", "audio/aac")
    if head[:3] == b"FLV":
        return "video", "video/x-flv"
    return None, None


def _jpeg_size(data):
    """扫描 JPEG 段直到 SOF，取真实尺寸。"""
    import struct

    index, total = 2, len(data)
    while index + 9 < total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height
        index += 2 + length
    return 0, 0


def probe_image_size(data):
    """纯 Python 读取常见图片尺寸，缺少 Pillow 时的兜底。读不出返回 (0, 0)。"""
    import struct

    head = data[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
    elif head[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
    elif head[:2] == b"BM" and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        width, height = abs(width), abs(height)
    elif head[:2] == b"\xff\xd8":
        width, height = _jpeg_size(data)
    else:
        return 0, 0
    if not width or not height:
        return 0, 0
    if width * height > 30_000_000:
        raise ValueError("图片像素超过上限")
    return width, height


def media_scope(scope):
    scope = (scope or "main").strip()
    group = re.fullmatch(r"(qq:group:\d+)(?::user:\d+)?", scope)
    if group:
        return group[1]
    m_grp = re.fullmatch(r"qq_group_(\d+)", scope)
    if m_grp:
        return f"qq:group:{m_grp.group(1)}"
    m_priv = re.fullmatch(r"qq_private_(\d+)", scope)
    if m_priv:
        return f"qq:private:{m_priv.group(1)}"
    if re.fullmatch(r"qq:private:\d+", scope):
        return scope
    if scope in ("local", "main") or scope.startswith(("local:", "main:", "web:")):
        return "local"
    return re.sub(r"[^a-zA-Z0-9_-]", "_", scope)[:64] or "local"


def normalize_ref(ref):
    """容忍 ``media://img_xxx`` 包裹与首尾空白；模型常把聊天里的链接原样带过来。"""
    if not isinstance(ref, str):
        return ref
    ref = ref.strip()
    if ref.lower().startswith("media://"):
        ref = ref[8:].strip()
    return ref


def is_media_ref(ref):
    return isinstance(ref, str) and bool(REF_PATTERN.fullmatch(normalize_ref(ref)))


class MediaStore:
    def __init__(self, root=None):
        self.root = Path(root or MEDIA_ROOT).resolve()
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS media (
                    media_ref TEXT PRIMARY KEY, scope TEXT, digest TEXT, mime TEXT,
                    width INTEGER, height INTEGER, size INTEGER, origin TEXT,
                    message_id TEXT, sender_qq TEXT, label TEXT, created_at REAL, expires_at REAL);
                CREATE INDEX IF NOT EXISTS media_scope_time ON media(scope,created_at DESC);
                CREATE INDEX IF NOT EXISTS media_digest ON media(digest);
                CREATE TABLE IF NOT EXISTS media_messages (
                    scope TEXT, message_id TEXT, media_ref TEXT, created_at REAL,
                    PRIMARY KEY(scope,message_id,media_ref));
            """)

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.root / "index.sqlite3"), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _manifest(row):
        return {key: row[key] for key in ("media_ref", "mime", "width", "height", "size", "origin",
                                          "message_id", "sender_qq", "label", "created_at", "expires_at")}

    @staticmethod
    def _image_size(data):
        """校验图片并取尺寸；缺少 Pillow 时退回纯 Python 取尺寸，不拒绝入库。"""
        try:
            from PIL import Image
        except ImportError:
            return probe_image_size(data)
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if width * height > 30_000_000:
                    raise ValueError("图片像素超过上限")
                image.verify()
        except ValueError:
            raise
        except Exception:
            raise ValueError("图片内容损坏或无法解析") from None
        return width, height

    def add(self, data, scope, origin="received", message_id="", sender_qq="", label="", retention_days=None):
        if retention_days is None:
            from core.config import load_config
            retention_days = load_config()["memory"]["retention_days"]
        scope = media_scope(scope)
        if not isinstance(data, bytes) or not data:
            raise ValueError("媒体内容为空")
        if len(data) > MAX_BYTES["video"]:
            raise ValueError("媒体文件超过100MB上限")
        kind, mime = sniff_media(data)
        if not mime:
            raise ValueError("无法识别的媒体格式：仅支持图片、视频和音频")
        if len(data) > MAX_BYTES[kind]:
            raise ValueError(f"{KIND_LABEL[kind]}超过{MAX_BYTES[kind] // 1_000_000}MB上限")
        width, height = self._image_size(data) if kind == "image" else (0, 0)
        digest = hashlib.sha256(data).hexdigest()
        now = time.time()
        path = self.root / "blobs" / (digest + EXTENSIONS[mime])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not path.exists() or path.stat().st_size != len(data) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                temporary = path.with_suffix("." + secrets.token_hex(6) + ".tmp")
                temporary.write_bytes(data)
                temporary.replace(path)
            # Content dedup is internal; each scope has independent references.
            if message_id:
                existing = db.execute("SELECT * FROM media WHERE scope=? AND digest=? AND message_id=? AND origin=? AND expires_at>?",
                                      (scope, digest, str(message_id), origin, now)).fetchone()
                if existing:
                    return self._manifest(existing)
            ref = REF_PREFIX[kind] + secrets.token_hex(8)
            db.execute("INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ref, scope, digest, mime, width, height, len(data), origin, str(message_id),
                        str(sender_qq), str(label)[:300], now, now + max(1, retention_days) * 86400))
            manifest = self._manifest(db.execute("SELECT * FROM media WHERE media_ref=?", (ref,)).fetchone())
        self.prune()
        return manifest

    def _row(self, ref, scope):
        ref = normalize_ref(ref)
        if not is_media_ref(ref):
            raise ValueError("无效media_ref，请使用list_media查询，不要编造")
        mscope = media_scope(scope)
        now = time.time()
        with self._db() as db:
            row = db.execute("SELECT * FROM media WHERE media_ref=? AND scope=? AND expires_at>?",
                             (ref, mscope, now)).fetchone()
            if row:
                return row

            # 回退机制：如果属于主人或本地工作区 (local, main, 主人私聊)，允许在主人生态范围内查找
            try:
                from core.qq_authority import is_owner_scope
                owner = is_owner_scope(scope)
            except Exception:
                owner = False

            if owner:
                candidate_scopes = []
                if mscope != "local":
                    candidate_scopes.append("local")
                try:
                    from core.config import load_config
                    for oid in load_config().get("channels", {}).get("napcat", {}).get("owner_ids", []):
                        candidate_scopes.append(f"qq:private:{oid}")
                except Exception:
                    pass

                for s in candidate_scopes:
                    if s == mscope:
                        continue
                    row = db.execute("SELECT * FROM media WHERE media_ref=? AND scope=? AND expires_at>?",
                                     (ref, s, now)).fetchone()
                    if row:
                        return row

                # 如果主人尝试读取系统模型工具登记的媒体（origin in ('registered', 'generated')）
                row = db.execute("SELECT * FROM media WHERE media_ref=? AND expires_at>?",
                                 (ref, now)).fetchone()
                if row and row["origin"] in ("registered", "generated"):
                    return row

        raise ValueError("媒体引用不存在、已过期或不属于当前会话；请重新上传或查询list_media")

    def get(self, ref, scope):
        row = self._row(ref, scope)
        try:
            data = (self.root / "blobs" / (row["digest"] + EXTENSIONS[row["mime"]])).read_bytes()
        except FileNotFoundError:
            raise ValueError("媒体缓存文件已丢失，请重新上传") from None
        if hashlib.sha256(data).hexdigest() != row["digest"]:
            raise ValueError("媒体缓存校验失败，请重新上传")
        return self._manifest(row), data

    @staticmethod
    def kind_of(manifest):
        mime = (manifest or {}).get("mime") or ""
        return mime.split("/", 1)[0] if mime.split("/", 1)[0] in KINDS else "image"

    def data_url(self, ref, scope):
        manifest, data = self.get(ref, scope)
        return "data:" + manifest["mime"] + ";base64," + base64.b64encode(data).decode("ascii")

    def preview(self, ref, scope):
        """图片返回 JPEG 预览；视频/音频没有图片预览，返回 None。"""
        manifest, data = self.get(ref, scope)
        if not manifest["mime"].startswith("image/"):
            return None
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return data
        with Image.open(BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1536, 1536))
            output = BytesIO()
            image.save(output, "JPEG", quality=82)
        return output.getvalue()

    def path(self, ref, scope):
        row = self._row(ref, scope)
        # A typed extension makes the file recognizable to downstream clients.
        self.get(ref, scope)
        target = self.root / "blobs" / (row["digest"] + EXTENSIONS[row["mime"]])
        return target

    def list(self, scope, limit=12, message_id=""):
        mscope = media_scope(scope)
        now = time.time()
        try:
            from core.qq_authority import is_owner_scope
            owner = is_owner_scope(scope)
        except Exception:
            owner = False

        with self._db() as db:
            if owner:
                owner_scopes = {"local", mscope}
                try:
                    from core.config import load_config
                    for oid in load_config().get("channels", {}).get("napcat", {}).get("owner_ids", []):
                        owner_scopes.add(f"qq:private:{oid}")
                except Exception:
                    pass
                placeholders = ",".join("?" for _ in owner_scopes)
                sql = f"SELECT * FROM media WHERE scope IN ({placeholders}) AND expires_at>?"
                args = list(owner_scopes) + [now]
                if message_id:
                    sql += f" AND (message_id=? OR media_ref IN (SELECT media_ref FROM media_messages WHERE scope IN ({placeholders}) AND message_id=?))"
                    args.extend([str(message_id)] + list(owner_scopes) + [str(message_id)])
            else:
                sql = "SELECT * FROM media WHERE scope=? AND expires_at>?"
                args = [mscope, now]
                if message_id:
                    sql += " AND (message_id=? OR media_ref IN (SELECT media_ref FROM media_messages WHERE scope=? AND message_id=?))"
                    args.extend([str(message_id), mscope, str(message_id)])
            args.append(max(1, min(int(limit), 30)))
            return [self._manifest(r) for r in db.execute(sql + " ORDER BY created_at DESC,rowid DESC LIMIT ?", args)]

    def bind_message(self, ref, scope, message_id):
        row = self._row(ref, scope)
        canonical = row["media_ref"]
        with self._db() as db:
            for s in {row["scope"], media_scope(scope)}:
                db.execute("INSERT OR REPLACE INTO media_messages VALUES (?,?,?,?)", (s, str(message_id), canonical, time.time()))

    def prune(self, max_bytes=1024 * 1024 * 1024):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM media WHERE expires_at<=?", (time.time(),))
            rows = db.execute("SELECT digest,MAX(size) AS size,MAX(created_at) AS latest FROM media GROUP BY digest ORDER BY latest DESC").fetchall()
            total = 0
            for row in rows:
                total += row["size"]
                if total > max_bytes:
                    db.execute("DELETE FROM media WHERE digest=?", (row["digest"],))
            keep = {r[0] for r in db.execute("SELECT DISTINCT digest FROM media")}
            db.execute("DELETE FROM media_messages WHERE media_ref NOT IN (SELECT media_ref FROM media)")
            for path in (self.root / "blobs").iterdir():
                if path.name.split(".", 1)[0] not in keep and path.is_file():
                    path.unlink(missing_ok=True)
