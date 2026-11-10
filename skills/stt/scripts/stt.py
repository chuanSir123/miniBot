"""语音转文字 技能的命令行入口。

模型读 SKILL.md 后用 bash 直接跑本脚本；实现复用宿主内置的
core.default_skills，脚本本身只做参数转发与输出。
"""


def execute(context):
    from core.default_skills import execute as _execute_builtin

    args = context.get("args") or context.get("arguments") or {}
    return _execute_builtin("stt", args)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from core.skill_cli import main

    raise SystemExit(main(execute))
