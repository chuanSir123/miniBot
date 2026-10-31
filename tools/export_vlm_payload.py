"""导出 VLM 请求入参，供 Postman 手工验证。

用法:
    python tools/export_vlm_payload.py                       # 用最近一条 user 消息重建
    python tools/export_vlm_payload.py "关键词"               # 指定 request_id / 消息片段
    python tools/export_vlm_payload.py "关键词" --image path.jpg

产物 (docs/):
    vlm_request_full.json     完整真实请求（system + 历史 + tools + 动态上下文）
    vlm_request_minimal.json  精简版（system 摘要 + 文本 + 图片），Postman 直接粘贴
    vlm_curl.sh               等价 curl 命令
"""
import base64
import io
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.brain import VLMBrain
from core.harness import BASE_RUNTIME_RULES, HARNESS_TOOLS
from core.persona import persona_prompt
from core.skill_manager import SkillManager

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def latest_request_id() -> str:
    db = sqlite3.connect(f"file:{ROOT / 'data/memory.db'}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT payload FROM messages_v2 WHERE payload LIKE '%\"role\": \"user\"%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    text = json.loads(row[0]).get("content") or ""
    if isinstance(text, list):  # 纯图片消息
        raise SystemExit("最近一条 user 消息不含文本，请手动传入关键词")
    return text[:12]


def latest_user_row():
    """按 JSON 实解析找最后一条 user 消息，避免 LIKE 受转义/压缩影响。"""
    db = sqlite3.connect(f"file:{ROOT / 'data/memory.db'}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT id, session_id, payload FROM messages_v2 ORDER BY id DESC").fetchall()
    finally:
        db.close()
    for rid, session, payload in rows:
        try:
            msg = json.loads(payload)
        except ValueError:
            continue
        if msg.get("role") == "user":
            return rid, session
    raise SystemExit("历史里没有可用的 user 消息")


def build_full(target: str):
    """按 core/harness.py 的真实装配顺序重建 messages 与 payload。

    target 可以是消息关键词，也可以是 "session|id" 形式的精确定位。
    """
    from core.compaction import ContextCompactor
    from core.context_builder import (
        SYSTEM_PROMPT_CACHE,
        render_runtime_context,
        render_tail_context,
    )

    db = sqlite3.connect(f"file:{ROOT / 'data/memory.db'}?mode=ro", uri=True)
    with db:
        if "|" in target and target.rsplit("|", 1)[1].isdigit():
            session, cutoff = target.rsplit("|", 1)
            cutoff = int(cutoff)
        else:
            match = db.execute(
                "SELECT id, session_id FROM messages_v2 WHERE payload LIKE ? ORDER BY id DESC",
                ("%" + target + "%",),
            ).fetchall()
            rows = []
            for rid, sess in match:
                payload = db.execute("SELECT payload FROM messages_v2 WHERE id=?", (rid,)).fetchone()[0]
                if target in payload:
                    rows = [(rid, sess)]
                    break
            if not rows:
                raise SystemExit("历史里找不到该 request_id")
            cutoff, session = rows[0]
        summary = db.execute(
            "SELECT covered_until_id, summary FROM summaries_v2 WHERE session_id=?", (session,)
        ).fetchone()
        rows = db.execute(
            "SELECT id, payload FROM messages_v2 WHERE session_id=? AND id<=? ORDER BY id",
            (session, cutoff),
        ).fetchall()
    db.close()

    history = [dict(json.loads(p), _id=mid) for mid, p in rows]
    scope = ":".join(session.split(":")[:2]) or "main"
    channel = "qq_private" if ":private:" in session else (
        "qq_group" if ":group:" in session else ("web" if session.startswith("web:") else "local")
    )
    skills = SkillManager().describe("", stable=True)
    system, fingerprint = SYSTEM_PROMPT_CACHE.render(
        scope=scope,
        channel=channel,
        selected_tools=[tool["function"]["name"] for tool in HARNESS_TOOLS],
        skills=skills,
    )
    compactor = ContextCompactor(VLMBrain(), None, session)
    compactor.summary = (
        {"covered_until_id": summary[0], "summary": summary[1]} if summary else None
    )
    tail = render_tail_context(
        render_runtime_context(scope=scope),
        {"status": "running"},
    )
    messages = compactor.align_and_assemble_messages(system, history, tail)
    print(json.dumps({"system_fp": fingerprint, "scope": scope, "channel": channel}, ensure_ascii=False), flush=True)

    brain = VLMBrain()
    settings = brain._settings()
    payload = brain._build_payload(
        brain._prepare_messages(messages), stream=True, tools=HARNESS_TOOLS, settings=settings
    )
    base, key, model, _ = settings
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    return endpoint, key, model, payload


def jpeg_b64(path: Path) -> str:
    """与 LocalPerception.capture_screen_jpeg 同参：宽 <=1280，JPEG quality 70。"""
    try:
        from PIL import Image
    except ModuleNotFoundError:
        # 无 PIL 时直接用原文件（要求本身就是 JPEG），保证入参结构可验证。
        return base64.b64encode(path.read_bytes()).decode()

    img = Image.open(path).convert("RGB")
    if img.width > 1280:
        img = img.resize((1280, int(img.height * (1280 / img.width))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    argv = sys.argv[1:]
    image_path = None
    positional = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--image":
            image_path = Path(argv[i + 1]) if i + 1 < len(argv) else None
            i += 2
            continue
        if not token.startswith("--"):
            positional.append(token)
        i += 1

    if positional:
        request_id = positional[0]
    else:
        rid, sess = latest_user_row()
        request_id = f"{sess}|{rid}"
    endpoint, key, model, full = build_full(request_id)

    DOCS.mkdir(exist_ok=True)
    (DOCS / "vlm_request_full.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    system_text = full["messages"][0]["content"]
    last_user_text = ""
    for m in reversed(full["messages"]):
        text = m.get("content")
        if m.get("role") != "user" or not isinstance(text, str):
            continue
        # 跳过尾部运行数据块（它是数据不是用户请求）
        if text.startswith("[运行时数据"):
            continue
        last_user_text = text
        break
    if not last_user_text:
        last_user_text = "看图描述一下内容"
    content = [{"type": "text", "text": last_user_text[:2000]}]
    if image_path and image_path.exists():
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + jpeg_b64(image_path), "detail": "low"},
            }
        )
    minimal = {
        "model": full["model"],
        "messages": [
            {"role": "system", "content": system_text[:800]},
            {"role": "user", "content": content},
        ],
        "temperature": full["temperature"],
        "max_tokens": 512,
        "stream": False,
    }
    (DOCS / "vlm_request_minimal.json").write_text(
        json.dumps(minimal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DOCS / "vlm_curl.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# 由 tools/export_vlm_payload.py 生成，仅用于本机验证\n"
        f"curl -sS '{endpoint}' \\\n"
        f"  -H 'Authorization: Bearer {key}' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -H 'User-Agent: MiniBot-VLM/2.0' \\\n"
        "  --data-binary @vlm_request_minimal.json\n",
        encoding="utf-8",
    )
    parts = urllib.parse.urlsplit(endpoint)
    collection = {
        "info": {
            "name": "miniBot VLM 入参验证",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [{"key": "vlm_api_key", "value": "PUT_YOUR_KEY_HERE"}],
        "item": [
            {
                "name": "1. minimal（system+文本+图片，可直接跑）",
                "request": {
                    "method": "POST",
                    "header": [
                        {"key": "Content-Type", "value": "application/json"},
                        {"key": "Authorization", "value": "Bearer {{vlm_api_key}}"},
                        {"key": "User-Agent", "value": "MiniBot-VLM/2.0"},
                    ],
                    "url": {
                        "raw": endpoint,
                        "protocol": parts.scheme,
                        "host": parts.hostname.split("."),
                        "path": [p for p in parts.path.split("/") if p],
                    },
                    "body": {"mode": "raw", "raw": json.dumps(minimal, ensure_ascii=False)},
                },
            },
            {
                "name": "2. full（完整历史+tools，body 用 binary 选 vlm_request_full.json）",
                "request": {
                    "method": "POST",
                    "header": [
                        {"key": "Content-Type", "value": "application/json"},
                        {"key": "Authorization", "value": "Bearer {{vlm_api_key}}"},
                        {"key": "User-Agent", "value": "MiniBot-Harness/2.0"},
                        {"key": "Accept", "value": "text/event-stream"},
                    ],
                    "url": {
                        "raw": endpoint,
                        "protocol": parts.scheme,
                        "host": parts.hostname.split("."),
                        "path": [p for p in parts.path.split("/") if p],
                    },
                    "body": {"mode": "file", "file": {"src": "vlm_request_full.json"}},
                },
            },
        ],
    }
    (DOCS / "minibot_vlm.postman_collection.json").write_text(
        json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "endpoint": endpoint,
                "model": model,
                "request_id": request_id,
                "messages": len(full["messages"]),
                "tools": len(full.get("tools", [])),
                "full_bytes": (DOCS / "vlm_request_full.json").stat().st_size,
                "minimal_bytes": (DOCS / "vlm_request_minimal.json").stat().st_size,
                "image_included": bool(image_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
