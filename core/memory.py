import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "memory.db"


class MiniMemory:
    """
    极简嵌入式记忆系统 (100% 本地离线，无需任何云端向量或 Embedding API，内存 < 3MB)
    
    架构设计参考 Stanford Generative Agents 与 Letta/Mem0 思想，采用三层混合模型：
      1. 【核心事实层 (Core Facts)】: 用户设定、工作偏好、常用路径（SQLite 键值持久化，常驻上下文注入）
      2. 【情境流与时序衰减层 (Episodic Stream + Time Decay)】:
         - 记录旁听流（窗口、剪贴板、操作轨迹）与聊天记录
         - 检索评分算法：Score = 0.5 * 关键词相关度 + 0.3 * 重要度 + 0.2 * 艾宾浩斯时间衰减因子
         - 基于 SQLite 内置 FTS5 全文索引 + 中文子串模糊索引，无需任何向量模型
      3. 【未来延时意图队列 (Intent Queue)】: 定时主动提醒、条件脉冲触发
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
            # 1. 核心事实表 (长期记忆)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS core_facts (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # 2. 情境日志流 (短期与中期记忆)
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

            # 3. 意图与定时脉冲表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS intent_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_at REAL NOT NULL,
                    condition_desc TEXT,
                    action_prompt TEXT NOT NULL,
                    status TEXT DEFAULT 'pending'
                )
            """)

            # 4. FTS5 全文检索虚拟表
            try:
                cur.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5(
                        content,
                        content='episodic_stream',
                        content_rowid='id'
                    )
                """)
            except sqlite3.OperationalError:
                pass

            conn.commit()

    # ==========================================================
    # 维度 1: 核心事实管理 (Core Facts - 长期属性)
    # ==========================================================
    def set_fact(self, key: str, value: str):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO core_facts (key, value, updated_at) VALUES (?, ?, ?)",
                (key.strip(), value.strip(), time.time())
            )
            conn.commit()

    def delete_fact(self, key: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM core_facts WHERE key = ?", (key.strip(),))
            conn.commit()

    def get_all_facts(self) -> Dict[str, str]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT key, value FROM core_facts").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def auto_extract_facts_from_text(self, text: str) -> List[Tuple[str, str]]:
        """
        自动捕获显式记忆指令，例如：
        用户说：'记住我喜欢用VSCode写Python' 或 '[FACT: language = Python]'
        """
        extracted = []
        # 1. 结构化标记解析 [FACT: key = value]
        tag_matches = re.findall(r"\[FACT:\s*([^=\]]+)=\s*([^\]]+)\]", text, re.IGNORECASE)
        for k, v in tag_matches:
            self.set_fact(k.strip(), v.strip())
            extracted.append((k.strip(), v.strip()))

        # 2. 自然语言显式指令捕获
        pattern = r"(?:请?记住|我的)\s*([^，。！？\n]{2,10}?)(?:是|为|叫|喜欢|习惯使用)\s*([^，。！？\n]+)"
        matches = re.findall(pattern, text)
        for k, v in matches:
            clean_k = k.strip()
            clean_v = v.strip()
            if len(clean_k) <= 12 and len(clean_v) <= 30:
                self.set_fact(clean_k, clean_v)
                extracted.append((clean_k, clean_v))

        return extracted

    # ==========================================================
    # 维度 2: 时序情境流与无向量衰减检索 (Episodic Stream + Decay)
    # ==========================================================
    def add_event(self, content: str, source: str = "desktop", importance: float = 1.0) -> int:
        now = time.time()
        with self._get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO episodic_stream (source, content, importance, created_at, last_accessed) VALUES (?, ?, ?, ?, ?)",
                (source, content.strip(), float(importance), now, now)
            )
            row_id = cur.lastrowid
            try:
                conn.execute("INSERT INTO episodic_fts(rowid, content) VALUES (?, ?)", (row_id, content.strip()))
            except Exception:
                pass
            conn.commit()
            return row_id

    def get_recent_events(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, source, content, importance, created_at FROM episodic_stream ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def search_events_with_decay(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        无向量相关度检索算法 (BM25 / 倒排子串匹配 + 艾宾浩斯时间衰减)
        公式:
          Score = 0.5 * Text_Match + 0.3 * Importance + 0.2 * exp(-Δt / Half_Life)
        """
        now = time.time()
        half_life_sec = 7200  # 2小时半衰期（刚才发生的事权重高，越久远越淡出）

        candidates: Dict[int, Dict[str, Any]] = {}

        # 1. 获取近期候选集 (保证新事件的高召回)
        recent_events = self.get_recent_events(limit=15)
        for ev in recent_events:
            candidates[ev["id"]] = ev

        # 2. 获取文本相关候选集 (中文双字/关键词或整体匹配)
        keywords = [k for k in re.split(r"[\s,，。！？]+", query) if len(k) >= 2]
        with self._get_conn() as conn:
            # FTS5 匹配
            try:
                fts_rows = conn.execute(
                    "SELECT rowid as id FROM episodic_fts WHERE episodic_fts MATCH ? LIMIT 10",
                    (query,)
                ).fetchall()
                for r in fts_rows:
                    ev_row = conn.execute("SELECT * FROM episodic_stream WHERE id = ?", (r["id"],)).fetchone()
                    if ev_row:
                        candidates[ev_row["id"]] = dict(ev_row)
            except Exception:
                pass

            # 模糊子串补充匹配
            for kw in keywords[:3]:
                like_rows = conn.execute(
                    "SELECT * FROM episodic_stream WHERE content LIKE ? ORDER BY created_at DESC LIMIT 5",
                    (f"%{kw}%",)
                ).fetchall()
                for r in like_rows:
                    candidates[r["id"]] = dict(r)

        if not candidates:
            return []

        # 3. 综合打分
        scored_list = []
        for ev in candidates.values():
            delta_t = max(0.0, now - ev["created_at"])
            # 艾宾浩斯衰减因子 [0.0 ~ 1.0]
            recency_factor = math.exp(-delta_t / half_life_sec)

            # 文本重合度打分 [0.0 ~ 1.0]
            matched_kw_count = sum(1 for kw in keywords if kw in ev["content"]) if keywords else (1 if query in ev["content"] else 0)
            text_match_factor = min(1.0, (matched_kw_count / max(1, len(keywords))))

            # 重要度因子 [0.2 ~ 1.0]
            importance_factor = min(1.0, max(0.2, ev.get("importance", 1.0) / 3.0))

            # 最终混合得分
            final_score = (0.5 * text_match_factor) + (0.3 * importance_factor) + (0.2 * recency_factor)
            scored_list.append((final_score, ev))

        # 按总得分降序排列
        scored_list.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_list[:limit]]

    # ==========================================================
    # 维度 3: 延时意图脉冲队列 (Intent Queue)
    # ==========================================================
    def add_delayed_intent(self, trigger_at: float, action_prompt: str, condition_desc: str = ""):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO intent_queue (trigger_at, condition_desc, action_prompt, status) VALUES (?, ?, ?, 'pending')",
                (trigger_at, condition_desc.strip(), action_prompt.strip())
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

    # ==========================================================
    # 聚合 Prompt 上下文 (轻量紧凑，控制在 300 Token 内)
    # ==========================================================
    def build_context_prompt(self, max_events: int = 6) -> str:
        facts = self.get_all_facts()
        recent = self.get_recent_events(limit=max_events)

        parts = []
        if facts:
            fact_strs = [f"- {k}: {v}" for k, v in facts.items()]
            parts.append("【用户核心记忆档案】\n" + "\n".join(fact_strs))

        if recent:
            event_strs = []
            for ev in recent:
                t_str = time.strftime("%H:%M:%S", time.localtime(ev["created_at"]))
                event_strs.append(f"[{t_str}] ({ev['source']}) {ev['content']}")
            parts.append("【近期桌面操作与旁听流】\n" + "\n".join(event_strs))

        return "\n\n".join(parts) if parts else "暂无近期历史。"
