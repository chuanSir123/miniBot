---
name: tts
description: TTS 语音合成，支持两种模式：design=通过文字描述设计特定音色（Qwen3 VoiceDesign），clone=提供参考音频克隆音色（IndexTTS-2.0）。当用户要求"说句话/念一段/读一下/用XX的声音/设计一个声音/克隆音色/语音合成/TTS/配音/朗读/用男声女声读/用XX的语气/把文字变成语音/模仿某人的声音"时调用本技能。即使用户没有明确说"TTS"，只要意图是把文字转成语音、用某种特定声音念、或模仿某段参考音频的音色，都应该使用本技能。
entry: scripts/tts.py
---

# tts

通过 `execute_skill` 调用，脚本为 `scripts/tts.py`，在隔离子进程中执行。

## 参数

- `mode` (string, 可选)：合成模式：design=设计声音(VoiceDesign)，clone=参考音色克隆
- `text` (string, 可选)：要合成的文本内容
- `instructions` (string, 可选)：design模式专用：声音描述，如'年轻男性，声音清亮，语速自然'
- `language` (string, 可选)：design模式专用：语言，默认Chinese
- `reference_audio` (array, 可选)：clone模式专用：参考音色的media_ref数组，按顺序传当前会话的media_ref
