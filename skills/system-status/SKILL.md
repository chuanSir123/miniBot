---
name: system_status
description: 检查本机系统基本状态与时间
entry: scripts/system_status.py
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/system-status/scripts/system_status.py --arguments '{}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`ok` 表示是否成功。
- 需要按会话解析图片时加 `--scope qq:group:88`；耗时任务给 `bash` 传 `background=true`。
- 需要媒体文件时先用 `get_media` 取 `file_path` 再传路径；不要传 Base64、URL 或编造引用。
- 下面的参数表对应 `--arguments` 里的字段；脚本由 bash 直接执行，输出一行 JSON。

# system_status


## 参数

无参数。
