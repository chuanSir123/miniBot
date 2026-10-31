# -*- coding: utf-8 -*-
r"""ego-lite 技能入口：转发受信核心 core/ego.py（浏览器引导、进程边界、超时控制都在那里）。"""


def execute(context):
    from core.ego import execute as ego_execute

    if isinstance(context, dict) and isinstance(context.get("args"), dict):
        args = context["args"]
    else:
        args = {k: v for k, v in (context or {}).items() if k != "_minibot"}
    return ego_execute(args)
