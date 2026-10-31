"""Check default skills; optionally install the host browser or test search."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ego import install, status
from core.default_skills import anysearch

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-ego", action="store_true")
    parser.add_argument("--test-search", action="store_true")
    args = parser.parse_args()
    try:
        result = {"ego_lite": install() if args.install_ego else status()}
        if args.test_search:
            result["anysearch"] = anysearch(
                {
                    "action": "search",
                    "query": "AnySearch official search API",
                    "max_results": 2,
                }
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
