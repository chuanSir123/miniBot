"""
Long-horizon Goals & Progress Contracts for miniBot.
Design principles:
- Multi-step objectives that persist across sessions
- Step status machine (pending -> in_progress -> done / blocked / skipped)
- Evaluation contracts and verification evidence
- Cadence-based check-in ('daily 09:00', 'weekdays 08:30', 'weekly mon 10:00', 'on_active')
- Strict guardrail: Proposals require user review; plans are never rewritten behind the user's back.
"""

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.memory import DB_PATH, keywords

GOAL_STATUSES = ("active", "paused", "done", "cancelled")
STEP_STATUSES = ("pending", "in_progress", "done", "blocked", "skipped")
CATEGORIES = (
    "coding",
    "learning",
    "project",
    "health",
    "work",
    "life",
    "other",
)

_CADENCE_RE = re.compile(
    r"^(daily|weekdays|weekly(?: (mon|tue|wed|thu|fri|sat|sun))?|monthly(?: ([1-9]|[12]\d|3[01]))?|on_active)"
    r"(?: ([01]?\d|2[0-3]):([0-5]\d))?$",
    re.I,
)


def parse_cadence(spec: str) -> Optional[Dict[str, Any]]:
    """Parse cadence string, e.g. 'daily 09:00', 'weekdays 08:30', 'on_active'."""
    spec = (spec or "").strip().lower()
    if not spec:
        return None
    m = _CADENCE_RE.match(spec)
    if not m:
        return None
    cadence = m.group(1).split()[0]
    hour = int(m.group(4)) if m.group(4) is not None else 9
    minute = int(m.group(5)) if m.group(5) is not None else 0
    return {"type": cadence, "hour": hour, "minute": minute, "raw": spec}


@dataclass
class GoalStep:
    id: int
    title: str
    status: str = "pending"  # pending, in_progress, done, blocked, skipped
    evidence: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GoalManager:
    def __init__(self, memory):
        self.memory = memory
        self._ensure_table()

    def _ensure_table(self):
        with self.memory._get_conn() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS goals_v2 (
                    id TEXT PRIMARY KEY,
                    scope TEXT,
                    title TEXT,
                    category TEXT,
                    cadence TEXT,
                    status TEXT DEFAULT 'active',
                    steps_json TEXT,
                    target_date TEXT,
                    contract_json TEXT,
                    created_at REAL,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS goals_scope_status ON goals_v2(scope,status);
            """)

    def create_goal(
        self,
        title: str,
        category: str = "project",
        steps: Optional[List[Dict[str, Any]]] = None,
        cadence: str = "daily 09:00",
        target_date: str = "",
        contract: Optional[Dict[str, Any]] = None,
        scope: str = "local",
    ) -> Dict[str, Any]:
        goal_id = "g-" + uuid.uuid4().hex[:8]
        now = time.time()
        category = category if category in CATEGORIES else "project"

        formatted_steps = []
        for i, s in enumerate(steps or []):
            if isinstance(s, str):
                formatted_steps.append(GoalStep(id=i + 1, title=s.strip()).to_dict())
            elif isinstance(s, dict):
                formatted_steps.append(
                    GoalStep(
                        id=s.get("id", i + 1),
                        title=s.get("title", s.get("desc", f"步骤 {i+1}")),
                        status=s.get("status", "pending"),
                        evidence=s.get("evidence", ""),
                        notes=s.get("notes", ""),
                    ).to_dict()
                )

        if not formatted_steps:
            formatted_steps = [GoalStep(id=1, title="初始任务规划").to_dict()]

        steps_json = json.dumps(formatted_steps, ensure_ascii=False)
        contract_json = json.dumps(contract or {}, ensure_ascii=False)

        with self.memory._get_conn() as db:
            db.execute(
                """INSERT INTO goals_v2
                   (id, scope, title, category, cadence, status, steps_json, target_date, contract_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal_id,
                    scope,
                    title.strip()[:200],
                    category,
                    cadence.strip()[:50],
                    "active",
                    steps_json,
                    target_date.strip()[:50],
                    contract_json,
                    now,
                    now,
                ),
            )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        with self.memory._get_conn() as db:
            row = db.execute("SELECT * FROM goals_v2 WHERE id=?", (goal_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["steps"] = json.loads(res.get("steps_json", "[]"))
            except Exception:
                res["steps"] = []
            try:
                res["contract"] = json.loads(res.get("contract_json", "{}"))
            except Exception:
                res["contract"] = {}
            return res

    def list_goals(
        self, status: str = "active", scope: str = "local"
    ) -> List[Dict[str, Any]]:
        with self.memory._get_conn() as db:
            if status == "all":
                rows = db.execute(
                    "SELECT * FROM goals_v2 WHERE scope=? ORDER BY updated_at DESC",
                    (scope,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM goals_v2 WHERE scope=? AND status=? ORDER BY updated_at DESC",
                    (scope, status),
                ).fetchall()

            result = []
            for r in rows:
                item = dict(r)
                try:
                    item["steps"] = json.loads(item.get("steps_json", "[]"))
                except Exception:
                    item["steps"] = []
                result.append(item)
            return result

    def update_step(
        self,
        goal_id: str,
        step_id: int,
        status: str,
        evidence: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        if status not in STEP_STATUSES:
            return {"ok": False, "error": f"无效的步骤状态: {status}，允许: {STEP_STATUSES}"}

        goal = self.get_goal(goal_id)
        if not goal:
            return {"ok": False, "error": f"目标不存在: {goal_id}"}

        steps = goal["steps"]
        found = False
        for s in steps:
            if s["id"] == step_id:
                s["status"] = status
                if evidence:
                    s["evidence"] = evidence
                if notes:
                    s["notes"] = notes
                found = True
                break

        if not found:
            return {"ok": False, "error": f"未找到步骤 ID: {step_id}"}

        # Check if all steps are done
        all_done = all(s["status"] in ("done", "skipped") for s in steps)
        now = time.time()
        new_goal_status = "done" if all_done else goal["status"]

        with self.memory._get_conn() as db:
            db.execute(
                "UPDATE goals_v2 SET steps_json=?, status=?, updated_at=? WHERE id=?",
                (
                    json.dumps(steps, ensure_ascii=False),
                    new_goal_status,
                    now,
                    goal_id,
                ),
            )

        return {
            "ok": True,
            "goal_id": goal_id,
            "step_id": step_id,
            "step_status": status,
            "goal_status": new_goal_status,
            "all_done": all_done,
        }

    def set_status(self, goal_id: str, status: str) -> Dict[str, Any]:
        if status not in GOAL_STATUSES:
            return {"ok": False, "error": f"无效的目标状态: {status}，允许: {GOAL_STATUSES}"}
        now = time.time()
        with self.memory._get_conn() as db:
            cur = db.execute(
                "UPDATE goals_v2 SET status=?, updated_at=? WHERE id=?",
                (status, now, goal_id),
            )
            if cur.rowcount == 0:
                return {"ok": False, "error": f"目标不存在: {goal_id}"}
        return {"ok": True, "goal_id": goal_id, "status": status}

    def propose_revision(
        self, goal_id: str, new_steps: List[Dict[str, Any]], reason: str = ""
    ) -> Dict[str, Any]:
        """
        Propose a revision to the steps when an obstacle occurs.
        Guardrail: Plans are never rewritten silently; returns proposal payload for user approval.
        """
        goal = self.get_goal(goal_id)
        if not goal:
            return {"ok": False, "error": f"目标不存在: {goal_id}"}

        return {
            "ok": True,
            "proposal": True,
            "goal_id": goal_id,
            "title": goal["title"],
            "current_steps": goal["steps"],
            "proposed_steps": new_steps,
            "reason": reason,
            "instruction": "请确认是否采纳该计划调整。确认后调用 update_goal_plan 生效。",
        }

    def format_goals_summary(self, scope: str = "local") -> str:
        """Render active goals for system prompt or proactive nudge."""
        active = self.list_goals(status="active", scope=scope)
        if not active:
            return ""

        lines = ["【当前跟进的长程目标】"]
        for g in active[:3]:
            steps = g.get("steps", [])
            curr_step = next(
                (s for s in steps if s["status"] in ("in_progress", "pending")),
                None,
            )
            step_desc = (
                f"当前步骤: #{curr_step['id']} {curr_step['title']} [{curr_step['status']}]"
                if curr_step
                else "所有步骤已完成待确认"
            )
            cadence_note = f" (节奏: {g['cadence']})" if g.get("cadence") else ""
            lines.append(f"- 《{g['title']}》[{g['category']}]{cadence_note}: {step_desc}")
        return "\n".join(lines)
