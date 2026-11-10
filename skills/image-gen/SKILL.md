---
name: image_gen
description: 生成新图片，或编辑聊天里收到/引用/刚生成的图片。改背景、换衣服、保持同一人物、修图、继续改上一张等用mode=edit并传image_refs。优先使用当前上传或引用消息中的media_ref；不明确时先list_media查询、get_media看预览，必要时询问。全新创作用mode=generate。最多9张参考图，顺序保持；不要传Base64或猜测引用。输出assets含media_ref及QQ可发送的media://链接；后续编辑直接复用返回的media_ref。遵循用户指定的风格，编辑时保留未要求改变的内容。默认文件保存在项目Harness工作空间。
entry: scripts/image_gen.py
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/image-gen/scripts/image_gen.py --arguments '{"prompt":"一只橘猫在窗台上晒太阳","mode":"generate"}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`ok` 表示是否成功。
- 需要按会话解析图片时加 `--scope qq:group:88`；耗时任务给 `bash` 传 `background=true`。
- 需要媒体文件时先用 `get_media` 取 `file_path` 再传路径；不要传 Base64、URL 或编造引用。
- 下面的参数表对应 `--arguments` 里的字段；脚本由 bash 直接执行，输出一行 JSON。

# image_gen


## 参数

- `prompt` (string, 必填)：具体创作/修改要求。编辑时说明保留与修改的内容；忠实用户风格，不擅自锁定动漫风格。
- `mode` (string, 可选)：
- `image_refs` (array, 可选)：按顺序填写当前会话真实media_ref。编辑必须提供；从当前上传/引用消息或list_media获取。最多9张。
- `image_files` (array, 可选)：**本机图片路径数组**。先用 `get_media` 取 `file_path` 再填路径；脚本自己读文件生成 data URL，不要把 Base64 写进命令。与 image_refs 合计最多 9 张。
- `width` (integer, 可选)：可选；编辑时省略沿用首张参考图尺寸，新图默认480
- `height` (integer, 可选)：可选；编辑时省略沿用首张参考图尺寸，新图默认768
- `n` (integer, 可选)：
- `model` (string, 可选)：
