"""Local workflow inspection, activation, deactivation and rollback."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.skill_manager import SkillManager

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["list", "show", "activate", "deactivate", "rollback"]
    )
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    manager = SkillManager()
    if args.action == "list":
        for path in manager.skills_dir.glob("*.json"):
            if path.name == "usage.json":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            print(path.stem, data.get("status"), data.get("description"))
    else:
        name = manager._name(args.name or "")
        if args.action == "show":
            print((manager.skills_dir / (name + ".json")).read_text(encoding="utf-8"))
        elif args.action == "rollback":
            manager.rollback(name)
        else:
            manager.set_workflow_status(name, args.action == "activate")
