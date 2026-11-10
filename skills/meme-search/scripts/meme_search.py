# -*- coding: utf-8 -*-
r"""meme_search: 表情包图库检索（转发 core/meme.py 的受信实现）。

action=tags  -> 标签 → 库存数量列表（按最近少用优先）
action=pick  -> 按标签取一张图，返回本地路径（use_count/冷却机制照常生效）
"""
import os


def _get_args(context):
    if isinstance(context, dict):
        if isinstance(context.get("args"), dict):
            return context["args"]
        return {k: v for k, v in context.items() if k != "_minibot"}
    return {}


def execute(context):
    from core.meme import get_meme_manager

    args = _get_args(context)
    action = str(args.get("action") or "tags")
    mgr = get_meme_manager()

    if action == "tags":
        limit = max(1, min(100, int(args.get("limit", 30) or 30)))
        rows = mgr.tag_summary()[:limit]
        return {
            "ok": True,
            "count": len(rows),
            "tags": [
                {"tag": r["tag"], "count": r["count"], "used": r["used"]} for r in rows
            ],
            "output": "以上是图库标签与库存。按语义选一个最贴合的标签，用 action=pick 取图。",
        }

    if action == "pick":
        tag = str(args.get("tag") or "").strip()
        if not tag:
            return {"ok": False, "error": "缺少 tag 参数"}
        path = mgr.pick_by_tag(tag)
        if not path:
            all_tags = [r["tag"] for r in mgr.tag_summary()]
            near = [t for t in all_tags if any(c in t for c in tag)][:8]
            return {
                "ok": False,
                "error": f"标签 {tag!r} 无可用图（不存在或都在冷却）",
                "suggest_tags": near,
            }
        return {
            "ok": True,
            "tag": tag,
            "file_path": path,
            "exists": os.path.isfile(path),
            "output": "已取出图片，用返回的 file_path 发送（走媒体发送，不要读文件内容）。",
        }

    return {"ok": False, "error": f"未知 action: {action}（可用: tags / pick）"}

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from core.skill_cli import main

    raise SystemExit(main(execute))
