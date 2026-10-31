"""Meme library: silent collection, VLM tagging, tag-summary driven selection.

链路分四段：
  收集  —— 本地目录扫描 + 群聊静默采集（保留原图，动图不会退化成首帧）
  打标  —— 优先读图片自带元数据/文件名；拿不到再调 VLM 看图出标签
  汇总  —— meme_tags 表维护「每个标签下有几张图、被发过几次」
  决策  —— Jev 根据当前消息与回复，从标签里挑一个，或明确不发表情

旧实现用 ocr_text 存"图片所在那条群消息的文字"、靠字符 Jaccard 猜标签，
两边都不成立：那串文字和图片内容无关，字符相似度也认不出「卖萌」和「撒娇」。
"""

import base64
import contextlib
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from core.config import load_config
from core.console import log
from core.media_store import sniff_media

DEFAULT_MEME_DIR = Path(__file__).resolve().parent.parent / "assets" / "memes"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "memes.db"

VALID_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
# 采集入库上限。表情包极少超过这个体积，超了多半是聊天里发的大图。
MAX_STORED_BYTES = 8_000_000
# 打标上限：base64 之后体积会涨 1/3，太大只会拖慢队列。
MAX_TAGGED_BYTES = 5_000_000
MAX_TAGS_PER_MEME = 8

# 情绪桶只在「Jev 不可用」的降级路径里用来把标签归到语气大类，不再写库。
EMOTION_KEYWORDS: Dict[str, List[str]] = {
    "happy": ["开心", "好耶", "太棒了", "嘻嘻", "哈哈", "喜欢", "快乐", "庆祝", "嘿嘿", "大笑"],
    "cute": ["撒娇", "可爱", "揉揉", "摸摸", "抱抱", "贴贴", "求求", "呜呜", "乖巧", "卖萌", "比心"],
    "confused": ["问号", "疑惑", "不懂", "什么", "咦", "发呆", "挠头", "懵", "无语"],
    "angry": ["生气", "哼", "炸毛", "不理你", "讨厌", "咬你", "凶", "傲娇", "白眼"],
    "praise": ["点赞", "厉害", "牛", "棒", "夸夸", "佩服", "支持", "强", "鼓掌"],
    "sad": ["难过", "委屈", "心碎", "哭了", "流泪", "伤心", "叹气", "累了"],
    "greet": ["早安", "晚安", "你好", "打招呼", "挥手", "嗨", "拜拜", "再见", "告辞"],
    "shocked": ["吃惊", "震惊", "吓到", "真的吗", "天哪", "目瞪口呆", "震惊"],
}

# 给 VLM 的受控词表。故意收得窄：标签一散，汇总表就没法拿给 Jev 选，
# 几百个近义词标签等于没有标签。允许它补 1 个词表外的词描述具体画面。
SUGGESTED_TAGS: Sequence[str] = (
    "开心", "大笑", "比心", "点赞", "鼓掌", "打call", "卖萌", "撒娇", "可爱", "害羞",
    "疑惑", "无语", "震惊", "生气", "傲娇", "委屈", "难过", "哭泣", "叹气", "累了",
    "睡觉", "吃饭", "加油", "摸鱼", "告辞", "谢谢", "拒绝", "赞同", "摇头", "盯",
    "猫", "狗", "抱抱", "摸摸", "贴贴", "打滚", "探头", "偷看", "吃瓜", "哈欠",
    "早安", "晚安", "打招呼", "再见", "打扰了", "求求", "凶", "白眼", "流汗", "捂脸",
)

TAG_VOCAB = "、".join(SUGGESTED_TAGS)

TAG_INSTRUCTION = (
    "你是表情包图库的标注员。看这张图，先判断它是不是表情包，再决定输出。\n"
    "只输出一个 JSON 数组，不要解释、不要 markdown、不要代码块。\n"
    "规则：\n"
    "0. 先判断这张图是不是「表情包」：表情包是用于聊天表达情绪/语气/笑点的图片，"
    "通常是卡通/动漫/截图配文字、动物拟人、夸张表情、梗图等。"
    "如果它只是普通照片、风景图、文档截图、代码截图、新闻图、商品图、头像等"
    "不适合当聊天表情发送的图，直接输出空数组 []，不要打任何标签。\n"
    "1. 如果是表情包，输出 3 到 6 个中文短标签，必须优先从这些常用标签里挑最贴切的："
    f"{TAG_VOCAB}；\n"
    "2. 标签要概括「什么情绪／什么语气／适合什么场合」，是 2 到 4 个字的短词，"
    "绝对不要照抄图上的整句文字，不要把一句话当成标签；\n"
    "3. 如果图上文字只是补充笑点，把它浓缩成一个 2 到 4 字的关键词（如「绷不住」「下次还敢」），"
    "不要保留完整句子；\n"
    "4. 不要输出英文、数字、标点堆砌、乱码、模型报错（如 Loss:NaN）这类内容；\n"
    "5. 不要描述画风、配色、构图；同一张图不要给意思重复的标签。\n"
    '示例（是表情包）：["卖萌","比心","贴贴"]\n'
    '示例（不是表情包，普通照片）：[]'
)

_TAG_NOISE = {"none", "null", "无", "标签", "tag", "tags", "json", "image", "meme", "png", "jpg", "gif", "webp"}
# 脏数据特征：模型报错、纯英文/数字、带特殊符号堆砌、含问号感叹号等。
# 标签应该是 2~6 个汉字的短词，出现这些特征的一律丢弃。
_DIRTY_PATTERNS = (
    re.compile(r"loss", re.I),
    re.compile(r"nan", re.I),
    re.compile(r"error", re.I),
    re.compile(r"[?？!！]{2,}"),
    re.compile(r"[\u4e00-\u9fa5]{0,1}[\x00-\x7f]{3,}"),  # 大量 ASCII 混入
)


def _clean_tag(raw) -> str:
    """归一化单个标签：去空白与包裹符号，丢掉无信息量、过长或脏数据的。"""
    text = str(raw or "").strip()
    text = text.strip("[]{}()\"'`，,。.、；;：:!！?？ \t\r\n")
    text = re.sub(r"[\s%]+", "", text)
    text = text.strip("#＃")
    if not 1 <= len(text) <= 12:
        return ""
    if text.lower() in _TAG_NOISE:
        return ""
    if not re.search(r"[\w\u4e00-\u9fa5]", text):
        return ""
    for pat in _DIRTY_PATTERNS:
        if pat.search(text):
            return ""
    return text


def normalize_tags(raw) -> List[str]:
    """把任何形态的标签输入收敛成去重后的干净列表。"""
    if isinstance(raw, str):
        if raw.strip().startswith("["):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = re.split(r"[,，、\s]+", raw.strip("[]"))
        else:
            raw = re.split(r"[,，、\s]+", raw)
    if not isinstance(raw, (list, tuple, set)):
        return []
    tags: List[str] = []
    for item in raw:
        tag = _clean_tag(item)
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:MAX_TAGS_PER_MEME]


# ---------------------------------------------------------------- 自带标签提取
# 优先级：文件里就写着的标签 > 文件名 > VLM 现看。前两者零成本且准确，
# 用户自己整理的表情包往往就是靠文件名分类的。

def _png_text_chunks(data: bytes) -> List[str]:
    import struct

    found: List[str] = []
    pos = 8
    while pos + 8 <= len(data) and len(found) < 40:
        try:
            length = struct.unpack(">I", data[pos:pos + 4])[0]
        except struct.error:
            break
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype in (b"tEXt", b"iTXt", b"zTXt"):
            key, _, value = body.partition(b"\x00")
            if key.lower() in (b"comment", b"description", b"keywords", b"title", b"tag", b"tags"):
                found.append(value.decode("utf-8", "ignore"))
        elif ctype in (b"IDAT", b"IEND"):
            break
        pos += 12 + length
    return found


def _gif_comments(data: bytes) -> List[str]:
    found: List[str] = []
    pos = 13
    if len(data) > 10 and data[10] & 0x80:  # 全局调色板
        pos += 3 * (2 ** ((data[10] & 0x07) + 1))
    while pos < len(data) - 1 and len(found) < 20:
        marker = data[pos]
        if marker == 0x21 and pos + 1 < len(data) and data[pos + 1] == 0xFE:
            pos += 2
            chunk = bytearray()
            while pos < len(data) and data[pos]:
                size = data[pos]
                chunk += data[pos + 1:pos + 1 + size]
                pos += size + 1
            text = bytes(chunk).decode("utf-8", "ignore")
            if text.strip():
                found.append(text)
            pos += 1
        elif marker == 0x3B:
            break
        else:
            break
    return found


def _xmp_tags(data: bytes) -> List[str]:
    try:
        text = data.decode("utf-8", "ignore")
    except Exception:
        return []
    found: List[str] = []
    for match in re.finditer(r"<dc:subject>(.*?)</dc:subject>", text, re.S):
        found.extend(re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", match.group(1), re.S))
    for tag in ("pdf:Keywords", "photoshop:Instructions"):
        for match in re.finditer(rf"<{tag}>(.*?)</{tag}>", text, re.S):
            found.append(match.group(1))
    return found


def embedded_tags(data: bytes) -> List[str]:
    """读图片容器里自带的标签字段，读不到返回空列表。"""
    if not data:
        return []
    raw: List[str] = []
    try:
        head = data[:8]
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            raw.extend(_png_text_chunks(data))
        elif head[:3] == b"GIF":
            raw.extend(_gif_comments(data))
        elif head[:3] == b"\xff\xd8\xff":
            # JPEG 的 EXIF 里偶尔有 ImageDescription／XPKeywords，直接按段扫，不引 Pillow。
            raw.extend(re.findall(rb"[\x00]([\u4e00-\u9fa5A-Za-z0-9 ,]{4,60})[\x00]", data[:65536]))
            raw = [item.decode("utf-8", "ignore") if isinstance(item, bytes) else item for item in raw]
        raw.extend(_xmp_tags(data))
    except Exception:
        return []
    return normalize_tags(raw)


def filename_tags(path: Path) -> List[str]:
    """从文件名/上级目录名取标签。assets/memes/happy/开心.gif → ['开心', 'happy']。

    采集库的文件名是 md5，切出来全是 "bbef"、"beedb" 这类残片，所以要过滤纯十六进制
    片段——宁可少一个真实英文词，也不要往汇总表里灌垃圾标签。
    """
    tags: List[str] = []
    stem = re.sub(r"[_\-—\s]+", " ", path.stem)
    for piece in stem.split(" "):
        for word in re.findall(r"[\u4e00-\u9fa5]{1,6}|[a-zA-Z]{3,12}", piece):
            if re.fullmatch(r"[0-9a-fA-F]{3,}", word):
                continue
            if re.fullmatch(r"\d+", word):
                continue
            tags.append(word)
    parent = path.parent.name
    if parent and parent.lower() not in ("memes", "collected"):
        tags.append(parent)
    return normalize_tags(tags)


class MemeManager:
    """表情包库：入库、打标、汇总、挑选。"""

    def __init__(self, meme_dir: Optional[Path] = None, db_path: Optional[Path] = None):
        # 路径在运行时解析而不是默认参数绑定：测试要能把库重定向到临时目录，
        # 否则跑一次单元测试就会往真实 data/memes.db 里写打标结果。
        self.meme_dir = Path(meme_dir or DEFAULT_MEME_DIR)
        self.meme_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tag_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue(maxsize=256)
        self._tag_thread: Optional[threading.Thread] = None
        self._tag_stop = threading.Event()
        # 会话级"最近发过"：chat_key -> 最近 N 张图的路径。
        # 只在内存里记，重启清零 —— 冷却窗口丢一次没什么代价，
        # 持久化反而要在清理旧会话上花心思。
        self._recent: Dict[str, deque] = {}
        self._init_db()
        self.scan_local_memes()
        self.rebuild_tag_summary()
        self._ensure_tagger()

    # ------------------------------------------------------------ 基础
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def _db(self):
        conn = self._get_conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._lock, self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS memes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT UNIQUE,
                    file_path TEXT,
                    source TEXT,
                    tags TEXT DEFAULT '[]',
                    tag_source TEXT DEFAULT '',
                    use_count INTEGER DEFAULT 0,
                    created_at REAL,
                    tagged_at REAL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS meme_tags (
                    tag TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    used INTEGER DEFAULT 0,
                    updated_at REAL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_memes_hash ON memes(file_hash)")
            self._migrate_legacy(db)

    @staticmethod
    def _migrate_legacy(db):
        """把带 ocr_text/category 的旧表迁到新结构。

        旧 ocr_text 存的是「图片所在那条群消息的文字」，与图内容无关，直接丢弃；
        category 和旧 tags（文件名 + 分类）保留成标签，不至于让已有图变空壳。
        """
        columns = {row["name"] for row in db.execute("PRAGMA table_info(memes)")}
        if "ocr_text" not in columns:
            return
        db.execute("ALTER TABLE memes RENAME TO memes_legacy")
        db.execute("""
            CREATE TABLE memes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT UNIQUE,
                file_path TEXT,
                source TEXT,
                tags TEXT DEFAULT '[]',
                tag_source TEXT DEFAULT '',
                use_count INTEGER DEFAULT 0,
                created_at REAL,
                tagged_at REAL
            )
        """)
        for row in db.execute("SELECT * FROM memes_legacy"):
            item = dict(row)
            carried = normalize_tags(item.get("tags") or [])
            category = (item.get("category") or "").strip()
            if category and category != "general":
                carried = normalize_tags(carried + [category])
            db.execute(
                "INSERT OR IGNORE INTO memes (file_hash, file_path, source, tags, tag_source,"
                " use_count, created_at, tagged_at) VALUES (?,?,?,?,?,?,?,NULL)",
                (item.get("file_hash"), item.get("file_path"), item.get("source") or "local",
                 json.dumps(carried, ensure_ascii=False),
                 "legacy" if carried else "", item.get("use_count") or 0, item.get("created_at") or time.time()),
            )
        db.execute("DROP TABLE memes_legacy")
        db.execute("CREATE INDEX IF NOT EXISTS idx_memes_hash ON memes(file_hash)")
        log("Meme", "schema.migrated")

    # ------------------------------------------------------------ 收集
    def scan_local_memes(self):
        """扫描本地目录，把新文件登记进库（已有记录不动，保住使用计数）。

        已登记的路径直接跳过，不再读整份字节算 md5：图库几百张时全量读盘会让每次
        启动都白等好几秒。文件被原地替换过的，用 retag_all() 重打一次即可。
        """
        with self._lock, self._db() as db:
            known = {row["file_path"] for row in db.execute("SELECT file_path FROM memes") if row["file_path"]}
            for root, _, files in os.walk(self.meme_dir):
                for name in files:
                    path = Path(root) / name
                    if path.suffix.lower() not in VALID_EXTS or str(path) in known:
                        continue
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    if not data or len(data) > MAX_STORED_BYTES:
                        continue
                    digest = hashlib.md5(data).hexdigest()
                    if db.execute("SELECT id FROM memes WHERE file_hash = ?", (digest,)).fetchone():
                        continue
                    tags = filename_tags(path) or embedded_tags(data)
                    db.execute(
                        "INSERT OR IGNORE INTO memes (file_hash, file_path, source, tags,"
                        " tag_source, use_count, created_at, tagged_at) VALUES (?,?,?,?,?,0,?,?)",
                        (digest, str(path), "local", json.dumps(tags, ensure_ascii=False),
                         "filename" if tags else "", time.time(), time.time() if tags else None),
                    )
                    if not tags:
                        self._enqueue_tag(digest, str(path))

    def add_from_bytes(self, image_bytes: bytes, source: str = "group") -> Optional[str]:
        """群聊偷图入库。原样保存字节，GIF 保留动图。"""
        if not image_bytes or len(image_bytes) > MAX_STORED_BYTES:
            return None
        kind, mime = sniff_media(image_bytes)
        if kind != "image":
            return None
        digest = hashlib.md5(image_bytes).hexdigest()
        collect_dir = self.meme_dir / "collected"
        collect_dir.mkdir(parents=True, exist_ok=True)
        target = collect_dir / f"{digest}{MIME_EXT.get(mime, '.png')}"

        with self._lock, self._db() as db:
            row = db.execute("SELECT id, file_path FROM memes WHERE file_hash = ?", (digest,)).fetchone()
            if row:
                # 重复收集不能重置 use_count：旧实现用 INSERT OR REPLACE，每收一次
                # 就把 id 和计数清零，"少发重复图"的机制于是永远不生效。
                db.execute("UPDATE memes SET file_path = ? WHERE id = ?", (str(target), row["id"]))
                if not target.exists():
                    try:
                        target.write_bytes(image_bytes)
                    except OSError:
                        return None
                return str(target)
            if not target.exists():
                try:
                    target.write_bytes(image_bytes)
                except OSError:
                    return None
            tags = embedded_tags(image_bytes)
            db.execute(
                "INSERT INTO memes (file_hash, file_path, source, tags, tag_source,"
                " use_count, created_at, tagged_at) VALUES (?,?,?,?,?,0,?,?)",
                (digest, str(target), source, json.dumps(tags, ensure_ascii=False),
                 "embedded" if tags else "", time.time(), time.time() if tags else None),
            )
            if not tags:
                self._enqueue_tag(digest, str(target))
        log("Meme", "collected", hash=digest[:8], mime=mime, bytes=len(image_bytes),
            tagged="embedded" if tags else "pending")
        return str(target)

    # ------------------------------------------------------------ 打标
    def _ensure_tagger(self):
        if self._tag_thread and self._tag_thread.is_alive():
            return
        self._tag_stop.clear()
        self._tag_thread = threading.Thread(target=self._tag_loop, name="meme-tagger", daemon=True)
        self._tag_thread.start()
        self._queue_pending()

    def _queue_pending(self, limit: int = 20):
        """补扫历史未打标的图。设上限：大库首次启动不该一口气把 VLM 打满。"""
        try:
            with self._lock, self._db() as db:
                rows = db.execute(
                    "SELECT file_hash, file_path FROM memes WHERE tagged_at IS NULL AND file_path IS NOT NULL LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return
        for row in rows:
            self._enqueue_tag(row["file_hash"], row["file_path"])

    def _enqueue_tag(self, digest: str, path: str):
        try:
            self._tag_queue.put_nowait((digest, path))
        except queue.Full:
            log("Meme", "tag.queue_full", hash=str(digest)[:8])

    def _tag_loop(self):
        while not self._tag_stop.is_set():
            try:
                digest, path = self._tag_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._tag_one(digest, path)
            except Exception as exc:
                log("Meme", "tag.error", hash=str(digest)[:8], error=str(exc)[:160])
            finally:
                self._tag_queue.task_done()

    def _tag_one(self, digest: str, path: str):
        auto_tag = bool(load_config().get("meme", {}).get("auto_tag", True))
        target = Path(path)
        data = b""
        if target.exists():
            try:
                data = target.read_bytes()
            except OSError:
                data = b""
        tags = normalize_tags(embedded_tags(data) + filename_tags(target))
        source = "embedded" if tags else ""
        vlm_rejected = False
        if not tags and auto_tag and data and len(data) <= MAX_TAGGED_BYTES:
            vlm_result = vlm_tags(data)
            if vlm_result is None:
                # VLM 明确判断「不是表情包」，标记为 rejected 并从库里移除
                vlm_rejected = True
            else:
                tags = normalize_tags(vlm_result)
                source = "vlm" if tags else ""
        if vlm_rejected:
            with self._lock, self._db() as db:
                db.execute(
                    "UPDATE memes SET tags = '[]', tag_source = 'rejected', tagged_at = ? WHERE file_hash = ?",
                    (time.time(), digest),
                )
            # 删除本地文件，不再占用表情库
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                pass
            log("Meme", "tag.rejected", hash=str(digest)[:8])
            return
        if not tags and auto_tag:
            # 打标没结果大多是临时故障（网络、限流、模型没配），不写 tagged_at，
            # 下次启动还能再试一次；写死了就永远是一张没标签的沉默图。
            with self._lock, self._db() as db:
                db.execute(
                    "UPDATE memes SET tags = '[]', tag_source = 'error', tagged_at = NULL WHERE file_hash = ?",
                    (digest,),
                )
            log("Meme", "tag.empty", hash=str(digest)[:8])
            return
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE memes SET tags = ?, tag_source = ?, tagged_at = ? WHERE file_hash = ?",
                (json.dumps(tags, ensure_ascii=False), source or "none", time.time(), digest),
            )
        if tags:
            self.rebuild_tag_summary()
        log("Meme", "tagged", hash=str(digest)[:8], source=source or "none", tags=tags)

    def rebuild_tag_summary(self):
        """重算标签汇总表。图库就几百张，全量重算比增量维护不容易出错。"""
        with self._lock, self._db() as db:
            counts: Counter = Counter()
            for row in db.execute("SELECT tags FROM memes"):
                for tag in normalize_tags(row["tags"] or "[]"):
                    counts[tag] += 1
            used = {row["tag"]: row["used"] for row in db.execute("SELECT tag, used FROM meme_tags")}
            now = time.time()
            db.execute("DELETE FROM meme_tags")
            db.executemany(
                "INSERT INTO meme_tags (tag, count, used, updated_at) VALUES (?,?,?,?)",
                [(tag, count, used.get(tag) or 0, now) for tag, count in counts.items()],
            )
        return dict(counts)

    def tag_summary(self) -> List[Dict[str, object]]:
        with self._lock, self._db() as db:
            rows = db.execute("SELECT tag, count, used FROM meme_tags WHERE count > 0 ORDER BY used ASC, count DESC").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 挑选
    def note_sent(self, chat_key: str, path: str):
        """记录某会话刚发过的图，供后续挑图避让。真正发出后才记。"""
        if not chat_key or not path:
            return
        limit = max(1, int(load_config().get("meme", {}).get("avoid_repeat_last", 8)))
        with self._lock:
            recent = self._recent.setdefault(chat_key, deque(maxlen=limit))
            if path in recent:
                recent.remove(path)      # 移到末尾：越近发的越不容易再被选中
            recent.append(path)

    def _cooldown_paths(self, chat_key: Optional[str]) -> Set[str]:
        if not chat_key:
            return set()
        with self._lock:
            return set(self._recent.get(chat_key) or ())

    def _tag_availability(self, cooldown: Set[str]) -> Dict[str, int]:
        """每个标签还有几张"不在冷却中"的图。图库几千张也只是毫秒级全量扫描。"""
        if not cooldown:
            return {}
        available: Counter = Counter()
        with self._lock, self._db() as db:
            for row in db.execute("SELECT file_path, tags FROM memes WHERE file_path IS NOT NULL"):
                path = row["file_path"]
                if path in cooldown or not Path(path).exists():
                    continue
                for tag in normalize_tags(row["tags"] or "[]"):
                    available[tag] += 1
        return available

    def _rank_tags(self, text: str, limit: int) -> List[str]:
        """按「字面相关 > 发得少 > 图多」排序，字面命中只是粗筛，最终由 Jev 定。"""
        summary = self.tag_summary()
        if not summary:
            return []
        lowered = (text or "").lower()

        def score(item):
            tag = str(item["tag"])
            hit = 2 if tag and tag.lower() in lowered else 0
            if not hit and len(tag) >= 2:
                # 用二元组而不是单字：单字命中太廉价（「开心」会被「小心」拉到前排），
                # 排序一乱，喂给 Jev 的第一批选项就不对了。
                hit = 1 if any(tag[i:i + 2] in lowered for i in range(len(tag) - 1)) else 0
            return (-hit, int(item["used"] or 0), -int(item["count"] or 0))

        return [str(item["tag"]) for item in sorted(summary, key=score)][:limit]

    def pick_by_tag(self, tag: str, exclude_paths: Sequence[str] = ()) -> Optional[str]:
        excluded = set(exclude_paths or ())
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id, file_path, tags FROM memes WHERE tags LIKE ? AND file_path IS NOT NULL"
                " ORDER BY use_count ASC, RANDOM() LIMIT 40",
                (f'%"{tag}"%',),
            ).fetchall()
            chosen = None
            for row in rows:
                path = row["file_path"]
                if path in excluded:
                    continue
                if tag not in normalize_tags(row["tags"] or "[]"):
                    continue
                if not Path(path).exists():
                    continue
                chosen = row
                break
            if chosen is None:
                return None
            db.execute("UPDATE memes SET use_count = use_count + 1 WHERE id = ?", (chosen["id"],))
            db.execute("UPDATE meme_tags SET used = used + 1, updated_at = ? WHERE tag = ?", (time.time(), tag))
        log("Meme", "selected", tag=tag, file=Path(chosen["file_path"]).name)
        return chosen["file_path"]

    def choose_meme(self, message: str, reply: str, exclude_paths: Sequence[str] = (),
                    chat_key: Optional[str] = None) -> Optional[str]:
        """为一轮回复挑一张表情包；返回 None 表示这轮不该发。

        chat_key（群号 / 私聊用户号）用于会话级避让：该会话最近发过的图
        这轮不再选，标签下所有图都在冷却中时整个标签不进 Jev 的候选——
        否则「赞同」这种只有一张图的标签每次选中都会发同一张。
        """
        cfg = load_config().get("meme", {})
        summary = self.tag_summary()
        if not summary:
            return None
        cooldown = self._cooldown_paths(chat_key) | set(exclude_paths or ())
        # 官方 limits.max_options = 255（含 none），所以一批最多 254 个标签。
        # 上限就按官方值来，没必要自己往下砍：实测 255 个选项分辨力和延迟都没退化。
        max_options = max(2, min(255, int(cfg.get("tag_options_per_call", 255))))
        per_batch = max_options - 1
        # 轮数上限只是兜底，防止图库膨胀到几千个标签时一次回复连发几十个请求。
        rounds_cap = max(1, min(8, int(cfg.get("tag_max_rounds", 4))))
        capacity = per_batch * rounds_cap
        tags = self._rank_tags((message or "") + " " + (reply or ""), capacity * 2)
        if cooldown:
            available = self._tag_availability(cooldown)
            usable = [tag for tag in tags if available.get(tag, 0) > 0]
            cooled = len(tags) - len(usable)
            if cooled:
                log("Meme", "tag_cooldown", cooled=cooled, usable=len(usable))
            tags = usable
        tags = tags[:capacity]
        if not tags:
            # 相关标签的图全在冷却：发得太频繁了，这轮本来就该歇着。
            log("Meme", "skip", reason="all_in_cooldown", chat=chat_key)
            return None
        if len(summary) > capacity:
            # 只有超过容量才会走到这：按字面相关度取前 capacity 个。
            # 真到这个规模该做的是标签归一（同义词合并），不是继续加大穷举。
            log("Meme", "tag_budget", total=len(summary), capacity=capacity)

        chosen_tag: Optional[str] = None
        if cfg.get("use_jev", True):
            try:
                from core.jev import JevClient

                chosen_tag = JevClient().select_meme_tag(
                    message=message,
                    reply=reply,
                    tags=tags,
                    batch=per_batch,
                    max_rounds=rounds_cap,
                    min_confidence=float(cfg.get("min_confidence", 0.35)),
                    # 达到这个把握就不再问后面的批次（省请求）；它不决定发不发。
                    decisive_confidence=float(cfg.get("decisive_confidence", 0.6)),
                    max_options=max_options,
                )
            except Exception as exc:
                log("Meme", "jev.error", error=str(exc)[:160])
                chosen_tag = None
        if chosen_tag is None and not cfg.get("use_jev", True):
            # Jev 不可用时的降级：只在标签字面出现在回复里时才发，宁缺毋滥。
            lowered = (reply or "").lower()
            chosen_tag = next((tag for tag in tags if len(tag) >= 2 and tag.lower() in lowered), None)
        if not chosen_tag:
            log("Meme", "skip", reason="no_tag_chosen")
            return None
        path = self.pick_by_tag(chosen_tag, cooldown)
        if not path:
            log("Meme", "skip", reason="tag_empty", tag=chosen_tag)
        return path

    def retag_all(self):
        """清空标签重新打一遍（换词表或模型后手动调用）。"""
        with self._lock, self._db() as db:
            db.execute("UPDATE memes SET tags = '[]', tag_source = '', tagged_at = NULL")
        self.rebuild_tag_summary()
        self._queue_pending()

    def stats(self) -> Dict[str, object]:
        with self._lock, self._db() as db:
            total = db.execute("SELECT COUNT(*) AS n FROM memes").fetchone()["n"]
            pending = db.execute("SELECT COUNT(*) AS n FROM memes WHERE tagged_at IS NULL").fetchone()["n"]
            tagged = db.execute("SELECT COUNT(*) AS n FROM memes WHERE tagged_at IS NOT NULL AND tags != '[]'").fetchone()["n"]
            by_source = {row["tag_source"] or "none": row["n"] for row in db.execute(
                "SELECT tag_source, COUNT(*) AS n FROM memes GROUP BY tag_source")}
            tags = db.execute("SELECT COUNT(*) AS n FROM meme_tags").fetchone()["n"]
        return {"total": total, "pending": pending, "tagged": tagged, "tags": tags, "by_source": by_source}


def vlm_tags(image_bytes: bytes) -> Optional[List[str]]:
    """让 VLM 看图出标签。

    返回值：
      - List[str]: 正常打标结果（可能为空列表，表示打标失败/无标签）
      - None: VLM 明确判断「不是表情包」，调用方应过滤该图
    """
    kind, mime = sniff_media(image_bytes)
    if kind != "image":
        return []
    cfg = load_config().get("vlm", {})
    key = (cfg.get("api_key") or "").strip()
    if not key:
        return []
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        return []
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    model = cfg.get("model") or "gpt-4o"
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 200,
        "stream": False,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": TAG_INSTRUCTION},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    import urllib.request

    from core.netpool import open_request

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "MiniBot-Meme/2.0"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with open_request(request, timeout=30) as response:
            body = json.loads(response.read(256 * 1024).decode("utf-8", "replace"))
    except Exception as exc:
        log("Meme", "vlm.error", error=str(exc)[:160])
        return []
    choices = body.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        text = (choices[0].get("message") or {}).get("content") or ""
    # 先尝试解析 JSON 数组
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            arr = json.loads(stripped)
            if isinstance(arr, list):
                tags = normalize_tags(arr)
                if not tags:
                    # VLM 明确输出空数组 → 不是表情包
                    log("Meme", "vlm.rejected", elapsed_ms=round((time.monotonic() - started) * 1000))
                    return None
                log("Meme", "vlm.tags", elapsed_ms=round((time.monotonic() - started) * 1000), tags=tags[:6])
                return tags
        except ValueError:
            pass
    # 兜底：从文本里提取中文短词
    tags = normalize_tags(text)
    if not tags:
        tags = normalize_tags(re.findall(r"[\u4e00-\u9fa5]{2,6}", text))
    log("Meme", "vlm.tags", elapsed_ms=round((time.monotonic() - started) * 1000), tags=tags[:6])
    return tags


def extract_meme_tag(text: str) -> Tuple[str, Optional[str]]:
    """剥离模型输出里的 [表情: xx] 标记。

    只做剥离：表情选择已经交给 Jev，模型不再通过标记下单。历史上生成的标记
    仍可能出现在上下文里，留在正文会让用户看到莫名其妙的中括号。
    """
    match = re.search(r"\[(?:表情|表情包|meme)\s*[:：]\s*([^\]]+)\]", text or "", re.IGNORECASE)
    if match:
        clean_text = (text[:match.start()] + text[match.end():]).strip().rstrip("，。！ ")
        return clean_text, match.group(1).strip()
    return text, None


# 单例管理
_instance = None
_instance_lock = threading.Lock()


def get_meme_manager() -> MemeManager:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MemeManager()
        return _instance
