# MiniBot

面向 Windows 的轻量主动协同 Bot：Web 智能体工作台、本地终端、桌宠、NapCat QQ 多通道接入，云端大模型驱动（OpenAI 兼容 VLM + vLLM-SR 决策），SQLite 保存记忆与会话。

## 项目结构

```
MiniBot/
├── start.bat                  # 👈 双击启动（自动建 .venv、装依赖、选模式）
├── main.py                    # 程序入口
├── launcher.py                # 守护启动器（支持聊天内热重启）
├── config.example.json        # 配置模板（复制为 config.json 使用）
│
├── core/                      # 核心引擎
│   ├── harness.py             #   AgentHarness：工具循环与任务编排
│   ├── brain.py / jev.py      #   慢脑 VLM + 快脑 Jev 双模型决策
│   ├── persona.py             #   角色人格设定（名字/别名/提示词）
│   ├── memory.py              #   记忆系统（SQLite + FTS5 检索）
│   ├── proactive.py           #   主动协同：窗口/剪贴板/空闲/定时
│   ├── skill_manager.py       #   技能管理与自动学习
│   ├── workspace.py           #   多工作空间隔离
│   ├── stt.py                 #   语音转文字（云端 API / 本地 faster-whisper）
│   ├── local_stt.py           #   本地转写引擎（自动判断 GPU，CPU int8 可跑）
│   └── ...
│
├── channels/                  # 交互通道
│   ├── pet_channel.py         #   桌宠（气泡聊天 + 工具栏）
│   ├── napcat_channel.py      #   NapCat QQ（私聊/群聊）
│   └── qq_media.py            #   QQ 图片/语音/视频处理
│
├── webui/                     # Web 智能体工作台
│   ├── server.py              #   本地 HTTP/SSE 服务（18080 端口）
│   └── index.html             #   工作台 + 设置中心界面
│
├── skills/                    # 技能目录（Agent Skills 规范：每个技能 = 文件夹 + SKILL.md + 可选 scripts/）
│   └── skill-creator/         #   技能创建助手（默认安装）
├── tools/                     # 诊断与维护脚本
├── tests/                     # 测试
│
├── requirements-core.txt      # 基础依赖
├── requirements-pet.txt       # 桌宠模式附加依赖
└── requirements-qq.txt        # QQ 模式附加依赖
```

## 使用方法

双击 **`start.bat`**，按菜单选择启动模式：

| 模式 | 说明 |
|---|---|
| 桌宠 | 屏幕宠物 + 气泡聊天 |
| QQ | NapCat QQ 私聊 / 群聊 |
| 组合 | 桌宠 + QQ 同时启用 |
| 本地终端 | 命令行直接对话 |

首次启动会自动创建 `.venv` 并安装依赖（清华 → 阿里云 → 官方源自动切换）。

启动后打开 **Web 工作台**：<http://127.0.0.1:18080>

- 💬 智能体任务执行（工作空间隔离、工具轨迹、实时引导）
- ⚙️ 智能体设置：模型 Key、拟人角色（名字/别名/性格 Prompt）、语音转写 STT、QQ 通道、桌宠参数

## 语音转写（STT）

设置页「🎙️ 4. 语音转写 STT」支持两种方式，全部为明文配置：

- **云端**：填 Base URL / API Key / 模型。兼容 OpenAI `/audio/transcriptions`（OpenAI、Groq、硅基流动等），
  也兼容旧版 `{"fileUrl": ...}` JSON 接口（`format=file_url`）。
- **本地**：点「安装依赖」装 faster-whisper，「下载模型」拉权重；自动检测 CUDA，
  有可用 GPU 用 float16，否则 CPU int8。默认 GPU 用 `large-v3-turbo`、CPU 用 `small`。
- `mode=auto`（默认）：填了 Base URL 走云端，失败或没填自动转到本地模型。
- 示例配置的 `base_url` 留空，需要云端就把自己的服务地址填进去；留空时直接用本地模型。
- STT 是**内置受信技能**（实现 `core/stt.py`，注册在 `core/default_skills.py`），
  不再放在 `skills/` 目录；QQ 语音消息会自动转写。

## 配置

1. 复制 `config.example.json` 为 `config.json`；
2. 在 Web 工作台「⚙️ 智能体设置」中填写 VLM 与 Jev 的 API Key（也可直接编辑配置文件）；
3. API Key 与 OneBot Token 自动保存在 `config.local.json`（已被 Git 忽略，不会进仓库）。
