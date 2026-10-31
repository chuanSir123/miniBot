"""Isolated one-shot host for fixed built-in skill implementations."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.default_skills import execute

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        result = execute(request["name"], request["arguments"])
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 100000:
        serialized = json.dumps(
            {
                "ok": result.get("ok", False),
                "untrusted_external_content": True,
                "output": serialized[:24000],
                "truncated": True,
            },
            ensure_ascii=False,
        )
    print(serialized)
    sys.exit(0 if result.get("ok") else 1)
