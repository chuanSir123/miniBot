---
name: qq_games
description: 群聊小游戏：dice 掷骰子、guess_number 猜 1 到 100 的数字、riddle 谜语。动作 start 开始、guess 猜答案、stop 结束。同一个群共享一轮游戏，不同群隔离。当主人或群友说「来个骰子/掷骰子/猜数字/猜谜语/玩个游戏」时使用。
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/qq_games/scripts/qq_games.py --arguments '{"game":"dice","count":2,"sides":6}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`output` 是可以直接发给群里的结果文本。
- 在 QQ 群里玩时加 `--scope qq:group:88`，让不同群各自保存一局游戏状态。
- 掷骰子 `dice` 不需要 scope（无状态）；`guess_number` / `riddle` 的 start / guess / stop 要连续传同一个 scope。

## 参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `game` | string，必填 | `dice` / `guess_number` / `riddle` |
| `action` | string | `start` / `guess` / `stop`，默认 `start` |
| `count` | integer | dice 的骰子数量，1–6，默认 1 |
| `sides` | integer | dice 的面数，2–100，默认 6 |
| `number` | integer | guess_number 猜的数字，1–100 |
| `answer` | string | riddle 猜的答案 |

## 示例

```
python skills/qq_games/scripts/qq_games.py --arguments '{"game":"guess_number","action":"start"}' --scope qq:group:88
python skills/qq_games/scripts/qq_games.py --arguments '{"game":"guess_number","action":"guess","number":50}' --scope qq:group:88
```

游戏状态存在 SQLite 的 `chat_games` 表里，24 小时自动过期。`stop` 会清掉当前 scope 的这一局。
