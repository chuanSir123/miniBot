---
name: image_gen
description: 生成新图片，或编辑聊天里收到/引用/刚生成的图片。改背景、换衣服、保持同一人物、修图、继续改上一张等用mode=edit并传image_refs。优先使用当前上传或引用消息中的media_ref；不明确时先list_media查询、get_media看预览，必要时询问。全新创作用mode=generate。最多9张参考图，顺序保持；不要传Base64或猜测引用。输出assets含media_ref及QQ可发送的media://链接；后续编辑直接复用返回的media_ref。遵循用户指定的风格，编辑时保留未要求改变的内容。默认文件保存在项目Harness工作空间。
entry: scripts/image_gen.py
---

# image_gen

通过 `execute_skill` 调用，脚本 `scripts/image_gen.py` 在隔离子进程中执行（Harness 注入媒体会话上下文）。编辑模式必须传 image_refs。

## 参数

- `prompt` (string, 必填)：具体创作/修改要求。编辑时说明保留与修改的内容；忠实用户风格，不擅自锁定动漫风格。
- `mode` (string, 可选)：
- `image_refs` (array, 可选)：按顺序填写当前会话真实media_ref。编辑必须提供；从当前上传/引用消息或list_media获取。最多9张。
- `width` (integer, 可选)：可选；编辑时省略沿用首张参考图尺寸，新图默认480
- `height` (integer, 可选)：可选；编辑时省略沿用首张参考图尺寸，新图默认768
- `n` (integer, 可选)：
- `model` (string, 可选)：
