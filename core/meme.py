"""Meme library: silent collection, VLM tagging, tag-summary driven selection.

链路分四段：
  收集  —— 本地目录扫描 + 群聊静默采集（保留原图，动图不会退化成首帧）
  打标  —— 优先读图片自带元数据/文件名；拿不到再调 VLM 看图出标签
  汇总  —— meme_tags 表维护「每个标签下有几张图、被发过几次」
  打分  —— Jev 给每个标签打独立的分，取排名前三，图分 = 这三个标签的加权匹配度

第四步不能退化成「让模型挑一个标签」：一张图常带 3~6 个标签，选中「可爱」之后，
候选池里既有一张真的在卖萌的，也有一张贴着「可爱」但主体情绪是「疑惑」的，
两者在标签层面完全等价，随机抽一张就是错配。只有让每个标签各自带着分回来，
多出来的标签才真正参与决策。

取前三而不是只取第一名：只认一个标签时，其余标签全被丢掉，那个标签的分只要不够高
整轮就不发（线上表现就是几乎不发）。前三名一起参与匹配，发不发只由 none 决定。

用 noul 而不是 choice 也是为此：noul 是独立概率，跨批可直接合并比较，
所以超过 64 个标签时可以按 63 个一批切开问；choice 是批内相对的 softmax 分布，
一旦切开，不同批的分数就没有共同基准了。
"""

import base64
import contextlib
import hashlib
import json
import os
import queue
import random
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
PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
# 采集入库上限。表情包极少超过这个体积，超了多半是聊天里发的大图。
# 但 QQ 的动画表情动辄十几兆（几百帧 GIF），卡太紧会把它们全拒收。
MAX_STORED_BYTES = 12_000_000
# 打标上限：base64 之后体积会涨 1/3，太大只会拖慢队列。
MAX_TAGGED_BYTES = 5_000_000
MAX_TAGS_PER_MEME = 8
# 判「同一张图」的画面容忍度：64 位 dHash 的汉明距离上限。
# 取 6 是经验值——重编码、改尺寸一般落在 0~4 位，不同表情基本在 15 位以上。
BLACKLIST_SIMILARITY = 6


def _rel(path) -> str:
    """库里只存相对项目根的路径。

    以前存的是绝对路径，项目目录一搬整个图库就变成一堆死链，只能手工修。
    相对路径跟着项目走，怎么搬都不散，换台机器也能直接用。
    项目外的图（用户手动放进来的）没得相对，仍然存绝对。
    """
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def _abs(path) -> str:
    """把库里的路径还原成可打开的绝对路径（老的绝对路径记录照样能用）。"""
    candidate = Path(str(path))
    return str(candidate if candidate.is_absolute() else PROJECT_ROOT / candidate)

# 图分权重：标签在图上排第一就按它的原分算，往后依次衰减。
# 只加到第 4 个标签为止——再往后基本都是凑数的画风词，给了权重只会让
# "标签多但都弱"的图挤掉"只有一个标签但完全贴切"的图。
TAG_POSITION_WEIGHTS: Sequence[float] = (1.0, 0.5, 0.3, 0.15)

# Jev 排名前三的标签：第一名是它真正想表达的语气，第二、三名只是备选，
# 所以权重依次衰减，不能让"备选标签在图库里图多"反过来盖过第一名。
TOP_TAG_WEIGHTS: Sequence[float] = (1.0, 0.6, 0.35)

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

# 报文类兜底文案（模型/网络故障）原样送出，不配表情——配了像是在报错上贴笑脸。
# 「附件发送失败」同理：那条本身就是坏消息，后面跟张表情更像嘲讽。
FAILURE_PREFIXES = (
    "【模型接口错误", "【对话模型请求超时", "【VLM", "模型连接失败",
    "【[WinError", "【[Errno", "【网络", "【连接", "附件发送失败",
)

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


def chat_key_from_scope(scope) -> str:
    """把工具作用域换成表情避让用的会话键。

    同一个会话在不同层有三种写法：channel 层记的是 ``group:123`` / ``user:456``，
    main.py 传下来的是 ``qq_group_123``，媒体库内部又用 ``qq:group:123``。
    对不上就会出现"刚发的那张"永远找不到，所以这里把它们收敛成 channel 的写法。
    """
    match = re.match(r"qq[:_](group|private)[:_](\d+)", str(scope or ""))
    if not match:
        return ""
    return f"{'group' if match[1] == 'group' else 'user'}:{match[2]}"


def _perceptual_hash(data: bytes) -> str:
    """64 位 dHash：把图缩成 9×8 灰度，比相邻像素亮度，得到一个"长得像"的指纹。

    为什么不只用 md5：md5 认字节，认不出画面。用户在 QQ 里把一张图存下来再发，
    客户端会重新编码（去 EXIF、改压缩率、转格式），字节全变了，画面一点没变。
    要"这张图以后别发了"，必须按画面认。

    Pillow 是可选依赖（requirements-core 里标了 Optional）。装不上就返回空串，
    调用方退回 md5 精确匹配，功能仍在，只是认不出重编码过的图。
    """
    if not data:
        return ""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return ""
    try:
        with Image.open(BytesIO(data)) as image:
            # GIF 只看第一帧：表情包的主体信息几乎都在首帧，逐帧比会慢一个量级。
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            pixels = list(image.convert("L").resize((9, 8), resampling).getdata())
    except Exception:
        return ""
    value = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            value = (value << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return f"{value:016x}"


def _hamming(left: str, right: str) -> int:
    """两个 dHash 差了几位；任何一边为空都算"没法比"，返回一个大数。"""
    if not left or not right or len(left) != len(right):
        return 64
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def _phash_usable(phash: str) -> bool:
    """这个指纹能不能拿来判"是不是同一张图"。

    纯色图、纯渐变图的 dHash 会退化成全 0 或全 1 —— 两张颜色完全不同的纯色图
    指纹一模一样。按指纹删图的代价是删错人，所以这类图直接不参与画面比对，
    退回到 md5 精确匹配：宁可删不掉，也不能删错。
    """
    if not phash or len(phash) != 16:
        return False
    bits = bin(int(phash, 16)).count("1")
    return 6 <= bits <= 58


class MemeManager:
    """表情包库：入库、打标、汇总、挑选、删除。"""

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
        # 画面指纹缓存：file_hash -> dHash。删除是低频操作，按需算一次就够，
        # 不落库也就省了一次 schema 迁移。
        self._phash_cache: Dict[str, str] = {}
        self._init_db()
        self._normalize_paths()
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
            db.execute("""
                CREATE TABLE IF NOT EXISTS meme_blacklist (
                    file_hash TEXT PRIMARY KEY,
                    phash TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    created_at REAL
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

    def _normalize_paths(self):
        """把历史记录里的绝对路径就地改写成相对项目根（幂等，老库也能跑）。"""
        with self._lock, self._db() as db:
            rows = db.execute("SELECT id, file_path FROM memes WHERE file_path IS NOT NULL").fetchall()
            for row in rows:
                stored = str(row["file_path"])
                relative = _rel(stored)
                if relative != stored:
                    db.execute("UPDATE memes SET file_path = ? WHERE id = ?", (relative, row["id"]))

    # ------------------------------------------------------------ 收集
    def scan_local_memes(self):
        """扫描本地目录，把新文件登记进库（已有记录不动，保住使用计数）。

        已登记的路径直接跳过，不再读整份字节算 md5：图库几百张时全量读盘会让每次
        启动都白等好几秒。文件被原地替换过的，用 retag_all() 重打一次即可。
        """
        with self._lock, self._db() as db:
            known = {_abs(row["file_path"]) for row in db.execute("SELECT file_path FROM memes") if row["file_path"]}
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
                        (digest, _rel(path), "local", json.dumps(tags, ensure_ascii=False),
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
        # 用户明确说过「这张别发了」的图，再被人发回来也不能重新入库 —— 只删记录
        # 不拦采集的话，删掉的那张过一会儿就会自己长回来。
        if self.is_blacklisted(digest, image_bytes):
            log("Meme", "collect.skipped_blacklisted", hash=digest[:8])
            return None
        collect_dir = self.meme_dir / "collected"
        collect_dir.mkdir(parents=True, exist_ok=True)
        target = collect_dir / f"{digest}{MIME_EXT.get(mime, '.png')}"

        with self._lock, self._db() as db:
            row = db.execute("SELECT id, file_path FROM memes WHERE file_hash = ?", (digest,)).fetchone()
            if row:
                # 重复收集不能重置 use_count：旧实现用 INSERT OR REPLACE，每收一次
                # 就把 id 和计数清零，"少发重复图"的机制于是永远不生效。
                db.execute("UPDATE memes SET file_path = ? WHERE id = ?", (_rel(target), row["id"]))
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
                (digest, _rel(target), source, json.dumps(tags, ensure_ascii=False),
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
            self._enqueue_tag(row["file_hash"], _abs(row["file_path"]))

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

    # ------------------------------------------------------------ 删除与拉黑
    # 「有些不合适，以后别发了」是两件事，缺一件都不成立：
    #   删  —— memes 记录、文件、标签汇总一起清掉，候选池里再也没有它
    #   拉黑 —— 记住这张图长什么样，采集时再遇到直接拒收
    # 只删不拉黑，群友把它再发一遍就会被静默采集回来，等于白删。
    def _blacklist(self, digest: str, phash: str, reason: str = ""):
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO meme_blacklist (file_hash, phash, reason, created_at)"
                " VALUES (?,?,?,?)",
                (digest, phash or "", str(reason)[:200], time.time()),
            )

    def is_blacklisted(self, digest: str, data: bytes = b"") -> bool:
        """这张图是否被明确删过。

        先按 md5 查（命中就是原图，最便宜）；没中再按画面比一遍——用户把删掉的图
        重新发出来时，QQ 往往已经重新编码，md5 变了，只有 dHash 认得出来。
        黑名单只有几十条，逐条比不比建索引慢。
        """
        if not digest:
            return False
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM meme_blacklist WHERE file_hash = ?", (digest,)).fetchone():
                return True
            known = [row["phash"] for row in db.execute(
                "SELECT phash FROM meme_blacklist WHERE phash IS NOT NULL AND phash != ''").fetchall()]
        if not known or not data:
            return False
        phash = _perceptual_hash(data)
        if not _phash_usable(phash):
            return False
        return any(_phash_usable(other) and _hamming(phash, other) <= BLACKLIST_SIMILARITY
                   for other in known)

    def _phash_for(self, row) -> str:
        """取某条记录的画面指纹，按 file_hash 缓存。"""
        digest = str(row["file_hash"] or "")
        if digest in self._phash_cache:
            return self._phash_cache[digest]
        value = ""
        if row["file_path"]:
            try:
                value = _perceptual_hash(Path(_abs(row["file_path"])).read_bytes())
            except (OSError, ValueError):
                value = ""
        self._phash_cache[digest] = value
        return value

    def forget(self, path: str = "", digest: str = "", reason: str = "") -> Dict[str, object]:
        """按库内路径或 hash 删掉一张。找不到就如实说找不到。"""
        with self._lock, self._db() as db:
            row = None
            if digest:
                row = db.execute("SELECT * FROM memes WHERE file_hash = ?", (digest,)).fetchone()
            if row is None and path:
                # 库里存相对路径；调用方可能给绝对路径，两种都试一次。
                for candidate in (_rel(path), str(path)):
                    row = db.execute("SELECT * FROM memes WHERE file_path = ?", (candidate,)).fetchone()
                    if row is not None:
                        break
        if row is None:
            return {"ok": False, "error": "表情库里没有这张图"}
        return self._forget_row(row, "path", reason)

    def forget_last(self, chat_key: str, reason: str = "用户要求删除") -> Dict[str, object]:
        """删掉本会话最近一次发出去的那张表情包。"""
        path = self.last_sent(chat_key)
        if not path:
            return {"ok": False, "error": "这个会话里我还没发过表情包，没有可删的"}
        return self.forget(path=path, reason=reason)

    def forget_by_image(self, data: bytes, reason: str = "用户要求删除") -> Dict[str, object]:
        """按图片内容定位库里的那张并删掉。

        命中判断两级：md5 认字节（原图转发），dHash 认画面（存下来再发）。
        都不中也要拉黑——用户的意思本来就是"这张以后别发"，库里有没有是另一回事。
        """
        if not data:
            return {"ok": False, "error": "没有拿到图片内容"}
        digest = hashlib.md5(data).hexdigest()
        phash = _perceptual_hash(data)
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM memes WHERE file_hash = ?", (digest,)).fetchone()
            matched_by = "hash" if row is not None else ""
            if row is None and _phash_usable(phash):
                # 全库比一遍画面。图库几百张，删除是低频动作，按需算指纹就够，
                # 不值得为了省几毫秒往表里加一列再维护它。
                closest = None
                for candidate in db.execute(
                        "SELECT * FROM memes WHERE file_path IS NOT NULL").fetchall():
                    other = self._phash_for(candidate)
                    if not _phash_usable(other):
                        continue
                    distance = _hamming(phash, other)
                    if distance <= BLACKLIST_SIMILARITY and (closest is None or distance < closest[0]):
                        closest = (distance, candidate)
                if closest is not None:
                    row, matched_by = closest[1], "phash"
        if row is not None:
            return self._forget_row(row, matched_by, reason)
        self._blacklist(digest, phash, reason)
        return {"ok": True, "deleted": None, "matched_by": "none", "blacklisted": True,
                "message": "表情库里原本没有这张图；已记下来，以后收到也不会再收录。"}

    def _forget_row(self, row, matched_by: str, reason: str) -> Dict[str, object]:
        """真正执行删除：记录 + 黑名单 + 文件 + 标签汇总 + 会话冷却。"""
        digest = str(row["file_hash"] or "")
        path = _abs(row["file_path"]) if row["file_path"] else ""
        with self._lock, self._db() as db:
            db.execute("DELETE FROM memes WHERE id = ?", (row["id"],))
            db.execute(
                "INSERT OR REPLACE INTO meme_blacklist (file_hash, phash, reason, created_at)"
                " VALUES (?,?,?,?)",
                (digest, self._phash_for(row), str(reason)[:200], time.time()),
            )
        # 冷却队列里也要摘掉：留着它只会让"刚发的那张"指向一个已经不存在的文件。
        with self._lock:
            for recent in self._recent.values():
                with contextlib.suppress(ValueError):
                    recent.remove(path)
        removed_file = False
        if path:
            try:
                Path(path).unlink()
                removed_file = True
            except OSError:
                removed_file = False
        # rebuild 自己拿锁，不能在上面的 with 里面调，会自锁。
        self.rebuild_tag_summary()
        log("Meme", "forgotten", hash=digest[:8], by=matched_by, removed_file=removed_file,
            file=Path(path).name if path else "", reason=str(reason)[:60])
        return {
            "ok": True,
            "deleted": {
                "file_hash": digest,
                "file_path": _rel(path) if path else "",
                "tags": normalize_tags(row["tags"] or "[]"),
                "matched_by": matched_by,
            },
            "matched_by": matched_by,
            "file_removed": removed_file,
            "blacklisted": True,
            "message": "已从表情库删除，之后不会再发；这张图再被人发回来也不会被重新收录。",
        }

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

    def last_sent(self, chat_key: str) -> str:
        """本会话最近发出去的那张图（绝对路径）；没发过返回空串。

        「你刚刚发的那张删掉」全靠这一个指针：模型不需要知道文件名，
        也不需要用户重新发图来对齐内容。
        """
        if not chat_key:
            return ""
        with self._lock:
            recent = self._recent.get(chat_key)
            return str(recent[-1]) if recent else ""

    def _cooldown_paths(self, chat_key: Optional[str]) -> Set[str]:
        if not chat_key:
            return set()
        with self._lock:
            return set(self._recent.get(chat_key) or ())

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

    @staticmethod
    def _primary_rank(tags: Sequence[str], tag: str) -> int:
        """标签在这张图的标签序列里排第几，就是它有多"主张"。

        VLM 按贴切度顺序输出，第一位才是这张图真正要表达的东西。排序信息一直藏在
        tags 数组里，只是过去被当成无序集合用掉了。分三档而不是精确位次：第二位
        仍然算相关，第三位往后基本只是"顺带也有这个特征"，继续细分没有额外信息。

        这一步是 Jev 二次选图的兜底，也是短名单的排序依据——没有它，
        「可爱」桶里首位是「疑惑」的图和首位是「卖萌」的图完全等价。
        """
        if not tags:
            return 0
        if tags[0] == tag:
            return 3
        if len(tags) > 1 and tags[1] == tag:
            return 2
        return 1

    @staticmethod
    def _image_position_weight(image_tags: Sequence[str], tag: str) -> float:
        """标签在这张图上排得越前，这张图越"主张"这个情绪。"""
        rank = MemeManager._primary_rank(image_tags, tag)
        return TAG_POSITION_WEIGHTS[min(rank, len(TAG_POSITION_WEIGHTS)) - 1]

    def _candidates_for_tag(self, tag: str, exclude_paths: Sequence[str] = (),
                            limit: Optional[int] = None) -> List[Dict[str, object]]:
        """取该标签下的候选图，按「首位标签优先 > 发得少 > 随机」排好序。

        SQL 的 `tags LIKE '%"tag"%'` 只是粗筛（它认不出位置），真正的判定在
        normalize_tags 之后的精确成员检查与位置打分。
        """
        excluded = set(exclude_paths or ())
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id, file_path, tags, use_count FROM memes"
                " WHERE tags LIKE ? AND file_path IS NOT NULL",
                (f'%"{tag}"%',),
            ).fetchall()
        items: List[Dict[str, object]] = []
        for row in rows:
            path = _abs(row["file_path"])
            if path in excluded:
                continue
            tags = normalize_tags(row["tags"] or "[]")
            if tag not in tags:
                continue
            if not Path(path).exists():
                continue
            items.append({
                "id": row["id"],
                "path": path,
                "tags": tags,
                "rank": self._primary_rank(tags, tag),
                "use_count": int(row["use_count"] or 0),
            })
        items.sort(key=lambda item: (-int(item["rank"]), int(item["use_count"]), random.random()))
        if limit:
            return items[:max(1, int(limit))]
        return items

    def _take(self, candidate: Dict[str, object], tag: str) -> str:
        """认定发这张：加使用计数、加标签热度，返回路径。"""
        with self._lock, self._db() as db:
            db.execute("UPDATE memes SET use_count = use_count + 1 WHERE id = ?", (candidate["id"],))
            db.execute("UPDATE meme_tags SET used = used + 1, updated_at = ? WHERE tag = ?", (time.time(), tag))
        log("Meme", "selected", tag=tag, file=Path(str(candidate["path"])).name,
            rank=candidate.get("rank", "-"), used=int(candidate["use_count"]))
        return str(candidate["path"])

    def pick_by_tag(self, tag: str, exclude_paths: Sequence[str] = ()) -> Optional[str]:
        """只按标签取一张（外部脚本/无语境场景用）。首选首位标签匹配的那批。"""
        candidates = self._candidates_for_tag(tag, exclude_paths)
        if not candidates:
            return None
        return self._take(candidates[0], tag)

    def choose_meme(self, message: str, reply: str, exclude_paths: Sequence[str] = (),
                    chat_key: Optional[str] = None) -> Optional[str]:
        """为一轮回复挑一张表情包；返回 None 表示这轮不该发。

        chat_key（群号 / 私聊用户号）用于会话级避让：该会话最近发过的图这轮不再入选，
        避免「赞同」这种只有一张图的标签每次都发同一张。

        决策只有一轮：Jev 给全库标签各自打分，取排名前三的标签，然后按「图自己的
        标签匹配度」取第一张。发不发只由 Jev 的 none 判定，不再卡分数门槛——
        门槛会让"三个标签都只是中等贴切"这种正常情况整轮沉默，线上表现就是几乎不发。
        """
        cfg = load_config().get("meme", {})
        summary = self.tag_summary()
        if not summary:
            return None
        cooldown = self._cooldown_paths(chat_key) | set(exclude_paths or ())
        # 一次请求最多问 64 个 question（含 none），即一次能给 63 个标签打分。
        # 标签多于这个数就分几批问：noul 是独立概率，跨批的分数可以直接比。
        rounds_cap = max(1, min(8, int(cfg.get("tag_max_rounds", 4))))
        capacity = 63 * rounds_cap
        tags = self._rank_tags((message or "") + " " + (reply or ""), capacity)
        if not tags:
            log("Meme", "skip", reason="no_tags")
            return None

        pick = None
        if cfg.get("use_jev", True):
            try:
                from core.jev import JevClient

                pick = JevClient().rank_meme_tags(
                    message=message, reply=reply, tags=tags, max_batches=rounds_cap)
            except Exception as exc:
                log("Meme", "jev.error", error=str(exc)[:160])
                pick = None
        if pick is not None:
            top_tags, scores = pick
            if not top_tags:
                log("Meme", "skip", reason="none_wins",
                    none=round(float(scores.get("none") or 0.0), 3))
                return None
            return self._pick_by_tags(top_tags, scores, cooldown)

        # 降级（Jev 不可用）：只在标签字面出现在回复里时才发，宁缺毋滥。
        lowered = (reply or "").lower()
        fallback_tag = next(
            (tag for tag in tags if len(tag) >= 2 and tag.lower() in lowered), None)
        if not fallback_tag:
            log("Meme", "skip", reason="no_tag_chosen")
            return None
        path = self.pick_by_tag(fallback_tag, cooldown)
        if not path:
            log("Meme", "skip", reason="tag_empty", tag=fallback_tag)
        return path

    def _pick_by_tags(self, top_tags: Sequence[str], scores: Dict[str, float],
                      exclude_paths: Sequence[str]) -> Optional[str]:
        """用 Jev 排名前三的标签匹配全库，取匹配度最高的一张，必定发出一张。

        匹配度 = Σ 位置权重(top1~3) × 标签分 × 图自身位置权重。
        Jev 已经用 none 回答过"要不要发"，这里再卡一道分数阈值，等于让"三个标签
        都只是中等贴切"这种正常情况整轮沉默——旧实现就是这么几乎不发的。

        图库覆盖不到这 3 个标签时逐级退让，但一定会发：先只看标签重叠数，
        再退到全库最久没发的一张，避免"图库里碰巧没有这个情绪"就整轮哑掉。
        """
        if not top_tags:
            return None
        excluded = set(exclude_paths or ())
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id, file_path, tags, use_count FROM memes WHERE file_path IS NOT NULL"
            ).fetchall()
        items: List[Dict[str, object]] = []
        for row in rows:
            path = _abs(row["file_path"])
            if path in excluded or not Path(path).exists():
                continue
            tags = normalize_tags(row["tags"] or "[]")
            if not tags:
                continue
            score = sum(
                TOP_TAG_WEIGHTS[position] * float(scores.get(tag) or 0.0)
                * self._image_position_weight(tags, tag)
                for position, tag in enumerate(top_tags) if tag in tags
            )
            items.append({
                "id": row["id"], "path": path, "tags": tags, "score": score,
                "overlap": len(set(top_tags) & set(tags)),
                "use_count": int(row["use_count"] or 0),
            })
        if not items:
            log("Meme", "skip", reason="no_image", tags=list(top_tags))
            return None
        best = max(items, key=lambda item: (float(item["score"]), int(item["overlap"]),
                                            -int(item["use_count"]), random.random()))
        if float(best["score"]) <= 0:
            best = max(items, key=lambda item: (int(item["overlap"]),
                                                -int(item["use_count"]), random.random()))
            log("Meme", "matched.by_overlap", tags=list(top_tags),
                overlap=best["overlap"])
        if int(best["overlap"]) == 0:
            best = min(items, key=lambda item: (int(item["use_count"]), random.random()))
            log("Meme", "matched.by_rotation", tags=list(top_tags))
        used_tag = next((tag for tag in top_tags if tag in best["tags"]), top_tags[0])
        log("Meme", "picked", tag=used_tag, top=list(top_tags),
            score=round(float(best["score"]), 3), tags=best["tags"])
        return self._take(best, used_tag)

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


def pick_meme_asset(reply: str, message: str = "", scope: str = "main",
                    chat_key: str = "") -> dict:
    """按最终回复挑一张表情并存进媒体库，返回 ``{"path", "ref"}``；不挑返回 ``{}``。

    这里只做「挑 + 存」，不碰正文：发送顺序必须是先文字、后图片，而挑图要问
    Jev（1~2s）。把它压在正文前面，用户就会盯着空气多等一两秒——之前挂占位符
    那版正是这个毛病。决策依据仍然只有最终回复，工具的中间文本不参与。
    """
    text = reply if isinstance(reply, str) else ""
    if not text.strip():
        return {}
    if text.lstrip().startswith(FAILURE_PREFIXES):
        return {}
    # 正文里已经有媒体（生成好的图/视频）就不再加一张表情：两条媒体连着发是噪音，
    # 而且文件类交付物后面跟个表情很像在开玩笑。
    if "media://" in text:
        return {}
    chat_key = chat_key or chat_key_from_scope(scope)
    try:
        manager = get_meme_manager()
        path = manager.choose_meme(message or "", text, chat_key=chat_key or None)
        if not path:
            return {}
        data = Path(path).read_bytes()
        from core.media_store import MediaStore

        asset = MediaStore().add(data, scope or "main", origin="sent", label=Path(path).name)
        ref = str(asset.get("media_ref") or "")
        if not ref:
            return {}
        if chat_key:
            # 冷却记在挑中时：发送失败最多让一张图下次再被避开，比重复发同一张便宜。
            manager.note_sent(chat_key, path)
        log("Meme", "picked", ref=ref, file=Path(path).name, chat=chat_key or scope,
            bytes=len(data), reply_chars=len(text))
        return {"path": str(path), "ref": ref}
    except Exception as exc:
        log("Meme", "pick.error", error=str(exc)[:160])
        return {}


def record_meme_in_history(session_id: str, ref: str) -> bool:
    """把「这一轮发了张表情」作为一条独立记录写进聊天记录。

    正文那条在发送前就落库了（含工具的轮次结构复杂，事后回填要改库）。分成
    两条更贴近真实：文字一条、图片一条，role 都是 assistant，下一轮模型照样
    知道「我上一条发过这张图」，也就不会把同一张再发一遍。
    """
    if not session_id or not ref:
        return False
    try:
        from core.memory import MiniMemory

        MiniMemory().append_message(session_id, {
            "role": "assistant",
            "content": f"![图片](media://{ref})",
        })
        return True
    except Exception as exc:
        log("Meme", "record.error", error=str(exc)[:160])
        return False


# 单例管理
_instance = None
_instance_lock = threading.Lock()


def get_meme_manager() -> MemeManager:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MemeManager()
        return _instance
