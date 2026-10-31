"""Workspace management for MiniBot Harness.
Supports multi-workspace isolation, including:
- Default 'main' workspace (used by Desktop Pet and default Web sessions)
- QQ private chat workspaces (isolated by user ID: qq_private_<uid>)
- QQ group chat workspaces (isolated by group ID: qq_group_<group_id>)
- Custom named workspaces created via the Web interface
"""
import hashlib
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "workspace"


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", name.strip())
    return cleaned[:64] or "main"


def workspace_for(scope: str = "main") -> Path:
    """Return the filesystem directory for a given scope / workspace."""
    scope = (scope or "main").strip()
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    # 1. Main / Local / Pet workspace
    if scope in ("main", "local", "pet", ""):
        path = WORKSPACE_ROOT / "main"
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    # 2. QQ Private scope (e.g. qq:private:123456 or qq_private_123456)
    m_priv = re.search(r"qq[:_]private[:_](\d+)", scope)
    if m_priv:
        uid = m_priv.group(1)
        path = WORKSPACE_ROOT / f"qq_private_{uid}"
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    # 3. QQ Group scope (e.g. qq:group:987654 or qq_group_987654)
    m_grp = re.search(r"qq[:_]group[:_](\d+)", scope)
    if m_grp:
        gid = m_grp.group(1)
        path = WORKSPACE_ROOT / f"qq_group_{gid}"
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    # 4. Custom named workspace (e.g. "my_project", "web:project1")
    if scope.startswith("web:"):
        parts = scope.split(":")
        clean_name = sanitize_name(parts[1]) if len(parts) > 1 else "main"
    else:
        clean_name = sanitize_name(scope)

    path = WORKSPACE_ROOT / clean_name
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_file(path, scope: str = "main") -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else workspace_for(scope) / p


def list_workspaces() -> List[Dict[str, str]]:
    """List all available workspaces. Ensure 'main' is always present at the top."""
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    main_dir = WORKSPACE_ROOT / "main"
    main_dir.mkdir(parents=True, exist_ok=True)

    items = []
    seen = set()

    # 'main' is default
    items.append({
        "name": "main",
        "label": "默认工作空间 (main)",
        "path": str(main_dir.resolve()),
        "is_default": True,
        "type": "default"
    })
    seen.add("main")

    for entry in sorted(WORKSPACE_ROOT.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and entry.name not in seen:
            name = entry.name
            wtype = "custom"
            label = name
            if name.startswith("qq_private_"):
                wtype = "qq_private"
                label = f"QQ私聊空间 ({name.replace('qq_private_', '')})"
            elif name.startswith("qq_group_"):
                wtype = "qq_group"
                label = f"QQ群空间 ({name.replace('qq_group_', '')})"
            items.append({
                "name": name,
                "label": label,
                "path": str(entry.resolve()),
                "is_default": False,
                "type": wtype
            })
            seen.add(name)

    return items


def _import_folder(src: Path, dest_root: Path) -> Dict[str, object]:
    """Copy a local folder's contents into dest_root/<basename> (merge on conflict)."""
    import shutil
    dest = dest_root / src.name
    copied = 0
    try:
        for item in src.rglob("*"):
            if item.is_dir():
                continue
            rel = item.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
    except Exception as exc:  # noqa: BLE001
        return {"folder": str(src), "copied": copied, "error": str(exc)}
    return {"folder": str(src), "copied": copied, "error": None}


def create_workspace(name: str, folders: Optional[List[str]] = None) -> Dict[str, object]:
    """Create a new named workspace, optionally importing local folders into it."""
    clean_name = sanitize_name(name)
    if not clean_name:
        clean_name = "workspace_" + hashlib.sha256(name.encode()).hexdigest()[:6]
    path = WORKSPACE_ROOT / clean_name
    path.mkdir(parents=True, exist_ok=True)

    imported: List[Dict[str, object]] = []
    for raw in (folders or []):
        try:
            src = Path(str(raw)).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        if not src.is_dir() or str(src).startswith(str(WORKSPACE_ROOT.resolve())):
            imported.append({"folder": str(src), "copied": 0, "error": "不是有效的文件夹"})
            continue
        imported.append(_import_folder(src, path))

    return {
        "name": clean_name,
        "label": clean_name,
        "path": str(path.resolve()),
        "is_default": (clean_name == "main"),
        "type": "custom",
        "imported": imported,
    }


def list_workspace_files(scope: str = "main", subpath: str = "") -> List[Dict]:
    """Recursively list files inside the workspace (limited depth and size)."""
    base = workspace_for(scope)
    target = (base / subpath.lstrip("/\\")).resolve()
    if not str(target).startswith(str(base)):
        target = base

    results = []
    if not target.exists():
        return results

    try:
        for root, dirs, files in os.walk(target):
            # Limit search depth
            rel_root = os.path.relpath(root, base)
            if rel_root.count(os.sep) > 5:
                dirs.clear()
                continue
            # Ignore hidden / venv dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".venv")]
            for f in sorted(files):
                if f.startswith("."):
                    continue
                full_path = Path(root) / f
                try:
                    rel_p = str(full_path.relative_to(base)).replace("\\", "/")
                    results.append({
                        "name": f,
                        "path": rel_p,
                        "size": full_path.stat().st_size,
                        "modified": full_path.stat().st_mtime,
                        "is_dir": False
                    })
                except Exception:
                    pass
    except Exception:
        pass
    return sorted(results, key=lambda x: x["path"])


def read_workspace_file(scope: str, rel_path: str, max_chars: int = 100000) -> Dict:
    """Read file content safely within a workspace."""
    base = workspace_for(scope)
    target = (base / rel_path.lstrip("/\\")).resolve()
    if not str(target).startswith(str(base)):
        raise PermissionError("Access denied: path outside workspace")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"File not found: {rel_path}")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > max_chars
        return {
            "path": rel_path,
            "content": content[:max_chars],
            "size": target.stat().st_size,
            "truncated": truncated
        }
    except Exception as exc:
        raise IOError(f"Failed to read file: {exc}")
