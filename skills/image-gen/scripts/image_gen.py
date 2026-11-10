"""Harness image generation/editing; reference data stays outside model context."""

DESCRIPTION = (
    "生成新图片，或编辑聊天里收到/引用/刚生成的图片。"
    "改背景、换衣服、保持同一人物、修图、继续改上一张等用mode=edit并传image_refs。"
    "优先使用当前上传或引用消息中的media_ref；不明确时先list_media查询、get_media看预览，必要时询问。"
    "全新创作用mode=generate。最多9张参考图，顺序保持；不要传Base64或猜测引用。"
    "输出assets含media_ref及QQ可发送的media://链接；后续编辑直接复用返回的media_ref。"
    "遵循用户指定的风格，编辑时保留未要求改变的内容。默认文件保存在项目Harness工作空间。"
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "具体创作/修改要求。编辑时说明保留与修改的内容；忠实用户风格，不擅自锁定动漫风格。"},
        "mode": {"type": "string", "enum": ["auto", "generate", "edit"], "default": "auto"},
        "image_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 9,
                       "description": "按顺序填写当前会话真实media_ref。编辑必须提供；从当前上传/引用消息或list_media获取。最多9张。"},
        "width": {"type": "integer", "description": "可选；编辑时省略沿用首张参考图尺寸，新图默认480"},
        "height": {"type": "integer", "description": "可选；编辑时省略沿用首张参考图尺寸，新图默认768"},
        "n": {"type": "integer", "default": 1},
        "model": {"type": "string", "default": "sensenova-u1"},
    },
    "required": ["prompt"],
    "additionalProperties": False,
}


def execute(context):
    from core.image_service import execute as run
    return run(context)

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from core.skill_cli import main

    raise SystemExit(main(execute))
