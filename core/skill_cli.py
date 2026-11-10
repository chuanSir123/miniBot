"""技能脚本的命令行引导库（被 ``skills/<name>/scripts/*.py`` import，不是执行入口）。

模型读完 SKILL.md 后，直接用 ``bash`` 跑该技能自己的脚本；脚本末尾写::

    if __name__ == "__main__":
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from core.skill_cli import main
        raise SystemExit(main(execute))

本库只做三件事：解析 ``--arguments`` / ``--scope``、构造技能脚本惯用的 context
（含 media_ref 解析助手）、把 ``execute()`` 结果打印成一行 JSON。
它**不按技能名分派**、不加载其它技能、不替代 harness 的工具级权限校验
（那层在 ``bash`` 之前由 ``tool_access`` 完成）。

context 同时提供三种取参形态，兼容历史脚本：参数展开在顶层、``args``、
``arguments``；``_minibot`` 里是 media / workspace 运行时。

媒体数据走**显式工具链**：模型先用 ``get_media`` 把 media_ref 解析成本机
``file_path``，再把路径作为参数传给脚本，脚本自己读文件生成 Base64。
``_minibot`` 里的解析助手只为兼容旧脚本保留，不是新技能该依赖的入口。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def project_root(script_file: str) -> Path:
    """``skills/<name>/scripts/<file>.py`` -> 项目根（parents[3]）。"""
    return Path(script_file).resolve().parents[3]


def _parse_arguments(raw: str | None) -> dict:
    """--arguments 支持内联 JSON、@文件；都缺省时从 stdin 读一行 JSON。"""
    if raw is None:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        # PowerShell 5.1 吞掉双引号后会收到 {mode:clone,text:喵} 这种形态。
        # harness 的 bash 分支已做引号适配；这里是兜底，给模型一条能自救的提示，
        # 免得它反复换成 ASCII / 改标点去试错。
        if raw.startswith("{") and '"' not in raw:
            raise ValueError(
                f"{exc}；命令行 JSON 的双引号疑似被 shell 吞掉，"
                "请改用 --arguments @<参数文件路径> 传参"
            ) from exc
        raise
    if not isinstance(value, dict):
        raise ValueError("技能参数必须是 JSON 对象")
    return value


def main(execute, argv=None) -> int:
    parser = argparse.ArgumentParser(description="运行本技能脚本，参数见同目录 SKILL.md")
    parser.add_argument("--arguments", dest="arguments", default=None,
                        help="参数 JSON 对象，或 @参数文件路径；缺省读 stdin")
    parser.add_argument("--scope", default="local", help="媒体/工作空间会话，默认 local")
    args = parser.parse_args(argv)

    # 内置小游戏等靠这个环境变量隔离不同群的会话。
    os.environ["MINIBOT_SKILL_SCOPE"] = args.scope

    from core.config import load_config
    from core.media_store import MEDIA_ROOT
    from core.workspace import workspace_for
    from core.skill_worker import _inject_media_helpers

    try:
        arguments = _parse_arguments(args.arguments)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": f"参数解析失败: {exc}"}, ensure_ascii=False))
        return 1

    workspace = str(workspace_for(args.scope))
    runtime = {
        "media_scope": args.scope,
        "media_root": str(MEDIA_ROOT),
        "workspace": workspace,
        "retention_days": load_config().get("memory", {}).get("retention_days", 30),
    }
    context = dict(arguments)
    context.update({"args": arguments, "arguments": arguments, "_minibot": runtime})
    _inject_media_helpers(context)

    # bash 的 cwd 本来就是 workspace；手动跑脚本时也对齐，
    # 让相对路径与 os.getcwd() 的语义一致。
    try:
        os.chdir(workspace)
    except OSError:
        pass

    try:
        result = execute(context)
    except Exception as exc:  # 技能抛错要变成模型能读到的结构化失败，而不是 traceback
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        return 1

    if not isinstance(result, dict):
        result = {"ok": True, "output": str(result)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok", False) else 1
