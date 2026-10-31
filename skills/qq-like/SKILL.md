---
name: qq_like
description: 给 QQ 好友资料页点赞（OneBot send_like）。当主人或群友说「赞我/点赞/给我点赞/帮我点个赞/给xxx点赞」时使用。参数 user_id 为要点赞的好友 QQ 号（字符串或数字）；不传则默认给当前消息发送者点赞。通过 NapCat 的 OneBot WebSocket 通道（带 access_token 的 Authorization 头）发送 send_like 请求。
entry: scripts/qq_like.py
---

# qq_like

通过 `execute_skill` 调用，脚本 `scripts/qq_like.py` 在隔离子进程中执行（Harness 注入媒体会话上下文）。

## 参数

- `user_id` (string, 可选)：要点赞的好友 QQ 号；不传则给当前消息发送者点赞
- `count` (integer, 可选)：点赞次数，默认 1
