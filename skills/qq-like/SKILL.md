---
name: qq_like
description: 给 QQ 好友资料页点赞（OneBot send_like）。当主人或群友说「赞我/点赞/给我点赞/帮我点个赞/给xxx点赞」时使用。参数 user_id 为要点赞的好友 QQ 号（字符串或数字）；不传则默认给当前消息发送者点赞。通过 NapCat 的 OneBot WebSocket 通道（带 access_token 的 Authorization 头）发送 send_like 请求。
entry: scripts/qq_like.py
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/qq-like/scripts/qq_like.py --arguments '{"count":1}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`ok` 表示是否成功。
- 需要按会话解析图片时加 `--scope qq:group:88`；耗时任务给 `bash` 传 `background=true`。
- 需要媒体文件时先用 `get_media` 取 `file_path` 再传路径；不要传 Base64、URL 或编造引用。
- 下面的参数表对应 `--arguments` 里的字段；脚本由 bash 直接执行，输出一行 JSON。

# qq_like


## 参数

- `user_id` (string, 可选)：要点赞的好友 QQ 号；不传则给当前消息发送者点赞
- `count` (integer, 可选)：点赞次数，默认 1
