import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "memory.db"


class MiniMemory:
    """
    极简嵌入式记忆引擎 (内存占用 < 5MB)
    融合:
      1. 核心事实档案 (Core Memory / Facts)
      2. 时序情景流 (Episodic Stream + FTS5 全文搜索 + 时间衰减)
      3. 延时触发与意图队列 (Intent & Trigger Queue)
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = str(db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            cur = conn.cursor()
            # 1. 核心事实表 (用户习惯、偏好、固定设定)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS core_facts (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # 2. 情景记忆流 (时序日志)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS episodic_stream (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance REAL DEFAULT 1.0,
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL
                )
            """)

            # 3. 意图与延迟触发队列 (延时脉冲)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS intent_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_at REAL NOT NULL,
                    condition_desc TEXT,
                    action_prompt TEXT NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)

            # 4. FTS5 全文搜索虚拟表 (用于秒级检索)
            try:
                cur.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5(
                        content,
                        content='episodic_stream',
                        content_rowid='id'
                    )
                """)
            except sqlite3.OperationalError:
                pass  # 部分环境未编译 fts5 时忽略

            conn.commit()

    # --- 核心事实 ---
    def set_fact(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO core_facts (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, time.time())
            )
            conn.commit()

    def get_all_facts(self) -> Dict[str, str]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT key, value FROM core_facts").fetchall()
            return {r["key"]: r["value"] for r in rows}

    # --- 时序情景流 ---
    def add_event(self, content: str, source: str = "desktop", importance: float = 1.0) -> int:
        now = time.time()
        with self._get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO episodic_stream (source, content, importance, created_at, last_accessed) VALUES (?, ?, ?, ?, ?)",
                (source, content, importance, now, now)
            )
            row_id = cur.lastrowid
            try:
                conn.execute("INSERT INTO episodic_fts(rowid, content) VALUES (?, ?)", (row_id, content))
            except Exception:
                pass
            conn.commit()
            return row_id

    def get_recent_events(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, source, content, created_at FROM episodic_stream ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def search_events(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            try:
                # FTS5 优先
                rows = conn.execute("""
                    SELECT e.id, e.source, e.content, e.created_at
                    FROM episodic_fts f
                    JOIN episodic_stream e ON f.rowid = e.id
                    WHERE episodic_fts MATCH ?
                    ORDER BY rank LIMIT ?
                """, (query, limit)).fetchall()
                if rows:
                    return [dict(r) for r in rows]
            except Exception:
                pass

            # LIKE 降级兜底
            rows = conn.execute("""
                SELECT id, source, content, created_at
                FROM episodic_stream
                WHERE content LIKE ?
                ORDER BY created_at DESC LIMIT ?
            """, (f"%{query}%", limit)).fetchall()
            return [dict(r) for r in rows]

    # --- 延时意图队列 ---
    def add_delayed_intent(self, trigger_at: float, action_prompt: str, condition_desc: str = ""):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO intent_queue (trigger_at, condition_desc, action_prompt, status) VALUES (?, ?, ?, 'pending')",
                (trigger_at, condition_desc, action_prompt)
            )
            conn.commit()

    def pop_due_intents(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, condition_desc, action_prompt FROM intent_queue WHERE trigger_at <= ? AND status = 'pending'",
                (now,)
            ).fetchall()
            intents = [dict(r) for r in rows]
            if intents:
                ids = [i["id"] for i in intents]
                conn.execute(
                    f"UPDATE intent_queue SET status = 'fired' WHERE id IN ({','.join('?' for _ in ids)})",
                    ids
                )
                conn.commit()
            return intents

    # --- 聚合感知上下文供 Prompt 使用 ---
    def build_context_prompt(self, max_events: int = 6) -> str:
        facts = self.get_all_facts()
        recent = self.get_recent_events(limit=max_events)

        parts = []
        if facts:
            fact_strs = [f"- {k}: {v}" for k, v in facts.items()]
            parts.append("【用户长期设定与偏好】\n" + "\n".join(fact_strs))

        if recent:
            event_strs = []
            for ev in recent:
                t_str = time.strftime("%H:%M:%S", time.localtime(ev["created_at"]))
                event_strs.append(f"[{t_str}] ({ev['source']}) {ev['content']}")
            parts.append("【近期桌面与交互轨迹】\n" + "\n".join(event_strs))

        return "\n\n".join(parts) if parts else "暂无近期历史。"
