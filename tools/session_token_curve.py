"""画出某个会话每一轮请求的 message 数与 token 曲线，并标出被删掉的区间。

同一会话里 input token 突然变少，只有三种可能：消息被删（id 跳号）、被
summary 覆盖、或这一轮没带图片。前两种能从库里直接看出来，第三种看 images。

用法:
    python tools/session_token_curve.py                    # 最近活跃的会话
    python tools/session_token_curve.py web:main:default
    python tools/session_token_curve.py web:main:default -n 40
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.text_budget import estimate_text_tokens  # noqa: E402


def load(session_id, limit):
    db = sqlite3.connect(f"file:{ROOT / 'data/memory.db'}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT id,payload,created_at FROM messages_v2 WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        summary = db.execute(
            "SELECT covered_until_id,summary FROM summaries_v2 WHERE session_id=?", (session_id,)
        ).fetchone()
        archived = db.execute(
            "SELECT count(*),MIN(id),MAX(id) FROM archived_messages_v2 WHERE session_id=?",
            (session_id,),
        ).fetchone()
    finally:
        db.close()
    return list(reversed(rows)), summary, archived


def active_session():
    db = sqlite3.connect(f"file:{ROOT / 'data/memory.db'}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT session_id FROM messages_v2 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    return row[0] if row else "web:main:default"


def main():
    argv = sys.argv[1:]
    limit = 60
    if "-n" in argv:
        idx = argv.index("-n")
        limit = int(argv[idx + 1])
        argv = argv[:idx] + argv[idx + 2:]
    session_id = argv[0] if argv else active_session()

    rows, summary, archived = load(session_id, limit)
    if not rows:
        raise SystemExit(f"会话 {session_id} 没有消息")

    print(json.dumps({
        "session": session_id,
        "messages": len(rows),
        "id_range": [rows[0][0], rows[-1][0]],
        "archived": {"count": archived[0], "min": archived[1], "max": archived[2]},
        "summary_covered_until": summary[0] if summary else None,
        "summary_chars": len(summary[1]) if summary else 0,
    }, ensure_ascii=False))
    print()

    fixed = estimate_text_tokens({"messages": [{"role": "system", "content": "x" * 6000}],
                                  "tools": [{"type": "function", "function": {
                                      "name": "x", "description": "x" * 10000,
                                      "parameters": {"type": "object", "properties": {}}}}]})
    cum = 0
    prev_id = None
    print(f"{'id':>6} {'role':<9} {'chars':>6} {'Δid':>4} {'cum_tok':>8}  flags")
    for rid, payload, _ in rows:
        msg = json.loads(payload)
        content = msg.get("content")
        raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        cost = estimate_text_tokens({"messages": [{"role": "user", "content": raw or ""}]})
        cum += cost
        flags = []
        if prev_id is not None and rid != prev_id + 1:
            flags.append(f"跳号 {prev_id}→{rid}（中间 {rid - prev_id - 1} 条已删/归档）")
        if msg.get("reasoning_content"):
            flags.append(f"reasoning={len(msg['reasoning_content'])}")
        if msg.get("images"):
            flags.append("images")
        if msg.get("tool_calls"):
            flags.append(f"tool_calls={len(msg['tool_calls'])}")
        print(f"{rid:>6} {msg.get('role', '?'):<9} {len(raw or ''):>6} "
              f"{(rid - prev_id) if prev_id else 0:>4} {cum + fixed:>8}  {' '.join(flags)}")
        prev_id = rid

    print()
    print(f"固定开销（system + tools，每轮都要付）≈ {fixed} tok")
    print(f"历史正文累计 ≈ {cum} tok，最新一轮请求估算 ≈ {cum + fixed} tok")


if __name__ == "__main__":
    main()
