"""
Workspace Checkpoint & Diff Tracking Engine for MiniBot Harness.
Provides:
1. Turn-by-turn and message-level file state snapshots.
   - 文本变更：write_file / edit_file 记录 before/after 全文（可出 diff 卡片）。
   - 二进制/媒体变更：技能（ffmpeg、image_gen 等）直接写盘，工具层拿不到内容，
     改由每轮开始扫描工作空间清单、轮末二次扫描对比得出「新建 / 修改 / 删除」。
2. Code rollback: Reverts workspace files and conversation history to any prior message state.
3. Unified diff computation (+added, -removed line counts) for diff-style change cards.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.workspace import PROJECT_ROOT, resolve_file, workspace_for

CHECKPOINT_DIR = PROJECT_ROOT / "data" / "checkpoints"
BLOB_ROOT = CHECKPOINT_DIR / "blobs"

# 扫描与回滚都要跳过的目录：既不该被回滚，也不该拖慢每轮开始的清单扫描。
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", "dist", "build",
}

# 需要「改动前留底」的二进制/媒体类型。
# 文本文件由 write_file / edit_file 的 before_content 负责，不需要额外拷贝；
# 媒体文件常常被技能原地覆盖（同名输出），只能在轮次开始前先存一份前像。
_BACKUP_SUFFIXES = {
    ".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico", ".svg",
    ".mp3", ".wav", ".m4a", ".webm", ".mov", ".mkv", ".pdf", ".zip",
    ".docx", ".xlsx", ".pptx", ".psd", ".ttf", ".otf", ".woff", ".woff2",
}
_MAX_BACKUP_FILE_BYTES = 8 * 1024 * 1024
_MAX_BACKUP_TURN_BYTES = 16 * 1024 * 1024


def _file_digest(path: Path) -> Optional[str]:
    """按内容取摘要，用于前像去重；读不动就当没有前像（宁可少备份也不报错）。"""
    try:
        h = hashlib.sha1()
        with path.open("rb") as fp:
            for chunk in iter(lambda: fp.read(256 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "session")
    digest = hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:48]}_{digest}"


def _scan_tree(root: Path) -> Tuple[Dict[str, Tuple[int, int]], set]:
    """返回 (文件清单 {abs_path: (mtime_ns, size)}, 目录集合)。"""
    files: Dict[str, Tuple[int, int]] = {}
    dirs = set()
    if not root or not root.exists():
        return files, dirs
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for d in dirnames:
            dirs.add(os.path.join(dirpath, d))
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            files[full] = (st.st_mtime_ns, st.st_size)
    return files, dirs


class CheckpointManager:
    def __init__(self, db_path: Optional[Path] = None):
        self.root = CHECKPOINT_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or (self.root / "checkpoints.sqlite3")
        self._turn_lock = threading.Lock()
        self._turns: Dict[str, Dict] = {}
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    abs_path TEXT NOT NULL,
                    before_content TEXT,
                    after_content TEXT,
                    added_lines INTEGER DEFAULT 0,
                    removed_lines INTEGER DEFAULT 0,
                    diff_text TEXT,
                    message_id INTEGER,
                    created_at REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_sess ON file_snapshots(session_id, id DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_msg ON file_snapshots(session_id, message_id);")
            # 轮次级文件台账：补住「技能/命令直接写盘」那条路。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turn_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    message_id INTEGER,
                    rel_path TEXT NOT NULL,
                    abs_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size INTEGER DEFAULT 0,
                    backup_path TEXT,
                    created_at REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turn_sess ON turn_files(session_id, id DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turn_msg ON turn_files(session_id, message_id);")

    # ------------------------------------------------------------------
    # 轮次快照（新建的 mp4 / gif / 图片都在这里被登记）
    # ------------------------------------------------------------------
    def begin_turn(self, session_id: str, scope: str, message_id: Optional[int] = None) -> Dict:
        """在一轮开始前记录工作空间清单，并给易被覆盖的媒体文件留前像。"""
        root = workspace_for(scope).resolve()
        files, dirs = _scan_tree(root)
        state = {
            "session_id": session_id,
            "root": root,
            "scope": scope,
            "message_id": message_id,
            "files": files,
            "dirs": dirs,
            "backed_bytes": 0,
            "backups": {},
            "tracked": set(),
            "started": time.time(),
        }
        with self._turn_lock:
            self._turns[session_id] = state
        try:
            # 先清掉上一轮残留的无人引用前像，再存本轮前像（顺序不能反，否则刚存的会被清掉）。
            self._gc_blobs(session_id)
            self._backup_preimages(state)
        except Exception as exc:
            print(f"[checkpoint] 前像备份失败: {exc}")
        return {"root": str(root), "files": len(files), "dirs": len(dirs)}

    def has_open_turn(self, session_id: str) -> bool:
        with self._turn_lock:
            return session_id in self._turns

    def _backup_preimages(self, state: Dict) -> None:
        """只给媒体/文档类文件留前像，且有单文件与单轮总量上限。

        前像按内容摘要存一份：同一个文件连续几轮没变，不会重复占用磁盘。
        """
        for abs_path, (mtime_ns, size) in state["files"].items():
            if size <= 0 or size > _MAX_BACKUP_FILE_BYTES:
                continue
            src = Path(abs_path)
            if src.suffix.lower() not in _BACKUP_SUFFIXES:
                continue
            digest = _file_digest(src)
            if not digest:
                continue
            dst = self._blob_dir(state["session_id"]) / (digest + src.suffix.lower()[:10])
            if not dst.exists():
                if state["backed_bytes"] + size > _MAX_BACKUP_TURN_BYTES:
                    break
                if not self._store_backup(src, dst):
                    continue
                state["backed_bytes"] += size
            state["backups"][abs_path] = str(dst)

    @staticmethod
    def _store_backup(src: Path, dst: Path) -> bool:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True
        except Exception as exc:
            print(f"[checkpoint] 备份 {src} 失败: {exc}")
            return False

    def _blob_dir(self, session_id: str) -> Path:
        return BLOB_ROOT / _slug(session_id)

    def _gc_blobs(self, session_id: str) -> None:
        """删掉没有被任何台账行引用的前像。"""
        base = self._blob_dir(session_id)
        if not base.exists():
            return
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            rows = conn.execute(
                "SELECT backup_path FROM turn_files WHERE session_id=? AND backup_path IS NOT NULL",
                (session_id,),
            ).fetchall()
        keep = {str(Path(r[0]).resolve()) for r in rows if r and r[0]}
        for item in base.iterdir():
            try:
                if item.is_file() and str(item.resolve()) not in keep:
                    item.unlink()
            except Exception:
                pass

    def end_turn(self, session_id: str) -> Dict:
        """轮末对比清单，把新建/修改/删除的文件登记进 turn_files。"""
        with self._turn_lock:
            state = self._turns.pop(session_id, None)
        if not state:
            return {"ok": False, "reason": "no_open_turn"}
        root: Path = state["root"]
        after_files, after_dirs = _scan_tree(root)
        before_files = state["files"]
        scope = state["scope"]
        message_id = state["message_id"]
        tracked = state["tracked"]

        created = [p for p in after_files if p not in before_files]
        modified = [p for p in after_files if p in before_files and after_files[p] != before_files[p]]
        removed = [p for p in before_files if p not in after_files]
        new_dirs = sorted(p for p in after_dirs if p not in state["dirs"])

        rows = []
        now = time.time()
        skipped = []

        def rel_of(abs_path: str) -> str:
            try:
                return str(Path(abs_path).relative_to(root)).replace("\\", "/")
            except ValueError:
                return Path(abs_path).name

        for p in created:
            if p in tracked:
                continue  # write_file 已经用 before_content=NULL 记过，回滚会删
            rows.append((session_id, scope, message_id, rel_of(p), p, "created", after_files[p][1], None, now))
        for p in modified:
            if p in tracked:
                continue
            backup = state["backups"].get(p)
            rows.append((session_id, scope, message_id, rel_of(p), p, "modified", after_files[p][1], backup, now))
            if not backup:
                skipped.append(rel_of(p))
        for p in removed:
            backup = state["backups"].get(p)
            rows.append((session_id, scope, message_id, rel_of(p), p, "deleted", before_files[p][1], backup, now))
            if not backup:
                skipped.append(rel_of(p))
        for d in new_dirs:
            rows.append((session_id, scope, message_id, rel_of(d), d, "dir", 0, None, now))

        if rows:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.executemany(
                    """
                    INSERT INTO turn_files (
                        session_id, scope, message_id, rel_path, abs_path, kind, size, backup_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return {
            "ok": True,
            "created": len([r for r in rows if r[5] == "created"]),
            "modified": len([r for r in rows if r[5] == "modified"]),
            "deleted": len([r for r in rows if r[5] == "deleted"]),
            "dirs": len([r for r in rows if r[5] == "dir"]),
            "unrestorable": skipped,
        }

    # ------------------------------------------------------------------
    # 文本变更
    # ------------------------------------------------------------------
    def record_file_change(
        self,
        session_id: str,
        scope: str,
        abs_path: str,
        before_content: Optional[str],
        after_content: str,
        message_id: Optional[int] = None,
    ) -> Dict:
        """
        Record a file creation or modification before & after state.
        Computes line diff and +/- stats.
        """
        try:
            p = Path(abs_path).resolve()
            ws = workspace_for(scope).resolve()
            try:
                rel_path = str(p.relative_to(ws)).replace("\\", "/")
            except ValueError:
                try:
                    rel_path = str(p.relative_to(PROJECT_ROOT.resolve())).replace("\\", "/")
                except ValueError:
                    rel_path = p.name
        except Exception:
            rel_path = Path(abs_path).name

        before_lines = before_content.splitlines(keepends=True) if before_content is not None else []
        after_lines = after_content.splitlines(keepends=True) if after_content is not None else []

        diff = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )
        diff_text = "\n".join(diff)

        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        if before_content is None and not added:
            added = len(after_lines)

        # 归属到当前轮次：轮询扫描发现同一文件时不再重复登记。
        with self._turn_lock:
            state = self._turns.get(session_id)
            if state is not None:
                if message_id is None:
                    message_id = state.get("message_id")
                try:
                    state["tracked"].add(str(Path(abs_path)))
                    resolved = str(Path(abs_path).resolve())
                    state["tracked"].add(resolved)
                except Exception:
                    pass

        now = time.time()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO file_snapshots (
                    session_id, scope, rel_path, abs_path, before_content,
                    after_content, added_lines, removed_lines, diff_text, message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    scope,
                    rel_path,
                    str(abs_path),
                    before_content,
                    after_content,
                    added,
                    removed,
                    diff_text,
                    message_id,
                    now,
                ),
            )
            snap_id = cur.lastrowid

        return {
            "id": snap_id,
            "session_id": session_id,
            "rel_path": rel_path,
            "abs_path": str(abs_path),
            "added": added,
            "removed": removed,
            "diff": diff_text,
            "is_new": before_content is None,
        }

    def get_session_changes(self, session_id: str, limit: int = 30) -> List[Dict]:
        """Get list of recent file modifications for a session."""
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, rel_path, abs_path, added_lines, removed_lines, diff_text,
                       (before_content IS NULL) as is_new, created_at
                FROM file_snapshots
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 回滚
    # ------------------------------------------------------------------
    @staticmethod
    def _filter_clause(cutoff: Optional[float], message_id: Optional[int]) -> Tuple[str, tuple]:
        """归属筛选：按时间截断为主，兼容只带 message_id 的旧调用。"""
        clauses = []
        params: List = []
        if message_id is not None:
            clauses.append("message_id >= ?")
            params.append(message_id)
        if cutoff is not None:
            clauses.append("created_at >= ?")
            params.append(cutoff - 0.001)
        return ("(" + " OR ".join(clauses) + ")" if clauses else "1=1"), tuple(params)

    def _revert_records(
        self, session_id: str, cutoff: Optional[float], message_id: Optional[int]
    ) -> List[Dict]:
        """把某个时间点之后的文件变更全部还原，返回动作清单。"""
        reverted: List[Dict] = []
        clause, params = self._filter_clause(cutoff, message_id)

        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            snaps = conn.execute(
                f"SELECT * FROM file_snapshots WHERE session_id = ? AND {clause} ORDER BY id ASC",
                (session_id,) + params,
            ).fetchall()
            turns = conn.execute(
                f"SELECT * FROM turn_files WHERE session_id = ? AND {clause} ORDER BY id ASC",
                (session_id,) + params,
            ).fetchall()

            # 同一路径可能既有文本快照又有轮次台账：以**最早**的文本快照 before 为准
            # （同一个文件可能被连续改多次，只有第一次的 before 才是回滚目标状态）。
            plan: Dict[str, Dict] = {}
            for row in snaps:
                abs_path = row["abs_path"] or ""
                if not abs_path or abs_path in plan:
                    continue
                before = row["before_content"]
                plan[abs_path] = {
                    "rel": row["rel_path"],
                    "before": before,
                    "blob": None,
                    # before 为 None 表示这一轮之前文件不存在 -> 回滚时应当删除
                    "kind": "restore" if before is not None else "delete",
                    "size": 0,
                }
            dirs_to_prune: List[str] = []
            for row in turns:
                abs_path = row["abs_path"] or ""
                kind = row["kind"]
                if kind == "dir":
                    dirs_to_prune.append(abs_path)
                    continue
                if not abs_path or abs_path in plan:
                    continue
                plan[abs_path] = {
                    "rel": row["rel_path"],
                    "before": None,
                    "blob": row["backup_path"],
                    "kind": kind,
                    "size": row["size"] or 0,
                }
                if kind == "created":
                    plan[abs_path]["before"] = None
                    plan[abs_path]["kind"] = "delete"

            for abs_path, info in plan.items():
                target = Path(abs_path)
                rel = info["rel"]
                try:
                    if info["kind"] == "delete":
                        if target.exists() and target.is_file():
                            target.unlink()
                            reverted.append({"path": rel, "action": "deleted"})
                        dirs_to_prune.append(str(target.parent))
                        continue
                    if info["blob"]:
                        blob = Path(info["blob"])
                        if blob.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(blob, target)
                            reverted.append({"path": rel, "action": "restored"})
                            continue
                    if info["before"] is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(info["before"], encoding="utf-8")
                        reverted.append({"path": rel, "action": "restored"})
                        continue
                    if info["kind"] == "deleted":
                        reverted.append({"path": rel, "action": "missing"})
                    else:
                        reverted.append({"path": rel, "action": "unrestorable"})
                except Exception as exc:
                    print(f"Error reverting {target}: {exc}")
                    reverted.append({"path": rel, "action": "failed"})

            conn.execute(
                f"DELETE FROM file_snapshots WHERE session_id = ? AND {clause}",
                (session_id,) + params,
            )
            conn.execute(
                f"DELETE FROM turn_files WHERE session_id = ? AND {clause}",
                (session_id,) + params,
            )

        self._gc_blobs(session_id)
        for d in sorted(set(dirs_to_prune), key=len, reverse=True):
            self._prune_empty_dir(Path(d))

        # 去重：同一路径只报一次。
        seen = set()
        unique = []
        for item in reverted:
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            unique.append(item)
        return unique

    @staticmethod
    def _prune_empty_dir(path: Path) -> None:
        """删掉本轮新建、回滚后变空的目录；绝不越过工作空间根/项目根。"""
        from core.workspace import WORKSPACE_ROOT

        ws_root = WORKSPACE_ROOT.resolve()
        proj = PROJECT_ROOT.resolve()
        cur = path
        for _ in range(12):
            try:
                resolved = cur.resolve()
                if resolved in (ws_root, proj):
                    return
                # cur 是某个工作空间的根目录、或项目下的一级目录时停手
                if resolved.parent in (ws_root, proj):
                    return
                if not cur.exists() or not cur.is_dir() or any(cur.iterdir()):
                    return
                cur.rmdir()
            except OSError:
                return
            except Exception:
                return
            cur = cur.parent

    def _cleanup_generated_media(self, session_id: str, scope: Optional[str], cutoff: Optional[float]) -> int:
        """删除该时间点之后生成的媒体库条目（图片/视频），避免聊天画廊还留着影子。"""
        if cutoff is None:
            return 0
        try:
            from core.media_store import MediaStore, media_scope
        except Exception:
            return 0
        derived = scope
        if not derived:
            parts = session_id.split(":")
            derived = parts[1] if len(parts) > 1 else session_id
        try:
            m_scope = media_scope(derived)
        except Exception:
            m_scope = derived
        store = MediaStore()
        db_file = store.root / "index.sqlite3"
        if not db_file.exists():
            return 0
        removed = 0
        try:
            with sqlite3.connect(str(db_file), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT media_ref, digest FROM media WHERE scope=? AND created_at>=? AND origin='generated'",
                    (m_scope, cutoff - 0.001),
                ).fetchall()
                for row in rows:
                    conn.execute("DELETE FROM media WHERE media_ref=?", (row["media_ref"],))
                    conn.execute("DELETE FROM media_messages WHERE media_ref=?", (row["media_ref"],))
                    still = conn.execute("SELECT 1 FROM media WHERE digest=? LIMIT 1", (row["digest"],)).fetchone()
                    if still:
                        continue
                    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mp4", ".webm", ".mp3", ".wav", ".m4a"):
                        blob = store.root / "blobs" / (row["digest"] + ext)
                        if blob.exists():
                            try:
                                blob.unlink()
                            except Exception:
                                pass
                    removed += 1
        except Exception:
            return removed
        return removed

    def rollback_to_message(
        self, session_id: str, target_message_id: int, memory, scope: Optional[str] = None
    ) -> Dict:
        """
        Roll back all file modifications made in session_id at or after target_message_id,
        and truncate conversation history in memory.
        Returns the prompt of target_message_id so it can be refilled in the input box.
        """
        target_prompt = ""
        cutoff: Optional[float] = None

        # 1. Fetch message prompt + 时间戳（时间戳用于归属判断，比 message_id 可靠）
        with memory._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            target_row = conn.execute(
                "SELECT id, payload, created_at FROM messages_v2 WHERE session_id=? AND id=?",
                (session_id, target_message_id),
            ).fetchone()
            if target_row:
                try:
                    cutoff = target_row["created_at"]
                except Exception:
                    cutoff = None
                try:
                    payload = json.loads(target_row["payload"])
                    target_prompt = payload.get("content", "")
                    if isinstance(target_prompt, list):
                        # extract text parts
                        parts = [p.get("text", "") for p in target_prompt if isinstance(p, dict) and p.get("type") == "text"]
                        target_prompt = "\n".join(parts)
                except Exception:
                    pass

        reverted_files = self._revert_records(session_id, cutoff, int(target_message_id))
        media_removed = self._cleanup_generated_media(session_id, scope, cutoff)

        # 3. Truncate messages in memory: delete target_message_id and all subsequent messages
        with memory._get_conn() as conn:
            conn.execute(
                "DELETE FROM messages_v2 WHERE session_id=? AND id >= ?",
                (session_id, target_message_id),
            )

        return {
            "success": True,
            "session_id": session_id,
            "target_message_id": target_message_id,
            "prompt": target_prompt,
            "reverted_files": reverted_files,
            "media_removed": media_removed,
        }

    def revert_latest_turn(self, session_id: str) -> Dict:
        """Revert all file modifications of the most recent turn of session_id."""
        candidates = []
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            for table in ("turn_files", "file_snapshots"):
                row = conn.execute(
                    f"SELECT message_id, created_at FROM {table} WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if row:
                    candidates.append((row["created_at"] or 0.0, row["message_id"]))
            if not candidates:
                return {"success": False, "error": "没有可撤销的变更"}
            latest_created, latest_msg = max(candidates, key=lambda item: item[0])
            turn_start = latest_created
            if latest_msg is not None:
                # 同一轮的登记时间不同，取该轮最早的一条作为起点。
                for table in ("turn_files", "file_snapshots"):
                    row = conn.execute(
                        f"SELECT MIN(created_at) AS t FROM {table} WHERE session_id=? AND message_id=?",
                        (session_id, int(latest_msg)),
                    ).fetchone()
                    if row and row["t"]:
                        turn_start = min(turn_start, row["t"])

        if latest_msg is not None:
            reverted = self._revert_records(session_id, None, int(latest_msg))
        else:
            # 旧数据没有 message_id：退化成「只回滚最近这批」（同一轮内的登记很密集）。
            reverted = self._revert_records(session_id, max(0.0, turn_start - 180), None)
        media_removed = self._cleanup_generated_media(session_id, None, turn_start if turn_start else None)
        return {"success": True, "reverted_files": reverted, "media_removed": media_removed}

    def clear_session(self, session_id: str) -> None:
        """会话清空：连同轮次台账与前像一起丢弃。"""
        with self._turn_lock:
            self._turns.pop(session_id, None)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("DELETE FROM file_snapshots WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM turn_files WHERE session_id=?", (session_id,))
        shutil.rmtree(BLOB_ROOT / _slug(session_id), ignore_errors=True)


_GLOBAL_CHECKPOINT_MGR: Optional[CheckpointManager] = None


def get_checkpoint_manager() -> CheckpointManager:
    global _GLOBAL_CHECKPOINT_MGR
    if _GLOBAL_CHECKPOINT_MGR is None:
        _GLOBAL_CHECKPOINT_MGR = CheckpointManager()
    return _GLOBAL_CHECKPOINT_MGR
