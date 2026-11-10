---
name: tts
description: TTS 语音合成，支持两种模式：design=通过文字描述设计特定音色（Qwen3 VoiceDesign），clone=提供参考音频克隆音色（IndexTTS-2.0）。当用户要求"说句话/念一段/读一下/用XX的声音/设计一个声音/克隆音色/语音合成/TTS/配音/朗读/用男声女声读/用XX的语气/把文字变成语音/模仿某人的声音"时调用本技能。即使用户没有明确说"TTS"，只要意图是把文字转成语音、用某种特定声音念、或模仿某段参考音频的音色，都应该使用本技能。
entry: scripts/tts.py
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/tts/scripts/tts.py --arguments '{"mode":"design","text":"你好，今天天气不错","instructions":"年轻女性，声音清亮"}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`ok` 表示是否成功。
- 需要按会话解析图片时加 `--scope qq:group:88`；耗时任务给 `bash` 传 `background=true`。
- 需要媒体文件时先用 `get_media` 取 `file_path` 再传路径；不要传 Base64、URL 或编造引用。
- 下面的参数表对应 `--arguments` 里的字段；脚本由 bash 直接执行，输出一行 JSON。

# tts


## 参数

- `mode` (string, 可选)：合成模式：design=设计声音(VoiceDesign)，clone=参考音色克隆
- `text` (string, 可选)：要合成的文本内容
- `instructions` (string, 可选)：design模式专用：声音描述，如'年轻男性，声音清亮，语速自然'
- `language` (string, 可选)：design模式专用：语言，默认Chinese
- `reference_audio` (array, 可选)：clone模式专用：参考音色的 media_ref 数组（旧字段，仅为兼容保留）。
- `reference_files` (array, 可选)：clone模式专用：**本机音频文件路径数组**。先调用 `get_media` 取它返回的 `file_path` 字符串，把该值放进数组；脚本自己读文件生成 base64，不要把音频数据写进命令，也不要编造路径。
