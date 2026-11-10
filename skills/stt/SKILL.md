---
name: stt
description: 语音转文字。fileUrl 支持 http(s) 音频地址或 data:audio/...;base64。云端 OpenAI 兼容接口优先，未配置时自动用本地 faster-whisper。QQ 语音消息会自动转写，无需调用本技能；只有其他来源的音频才需要手动跑。
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/stt/scripts/stt.py --arguments '{"fileUrl":"https://example.com/audio.wav"}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`text` 是转写文本，`engine` 标明用了云端还是本地。
- 音频上限约 16MB，超限会明确报错。
- 云端配置在设置页「语音转写 STT」（配置文件的 `stt` 段）：API 地址 / 密钥 / 模型，兼容 OpenAI `/audio/transcriptions`。
- 没配云端时自动用本地 faster-whisper；两者都不可用会明确报错，不要编造转写结果。

## 参数

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `fileUrl` | string，必填 | http(s) 音频 URL，或 `data:audio/...;base64` 数据 |

## 注意

QQ 语音消息由通道层自动转写后进入对话，本技能只用于「用户另外给了音频地址」的场景。
启用开关在配置文件的 `stt.enabled`，停用后本技能不会出现在目录里。
