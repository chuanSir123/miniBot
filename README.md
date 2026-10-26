# 🤖 MiniBot (极简主动交互智能体)

一个占用内存极小、精简、运行快速的主动交互式桌面伴侣与 QQ 机器人。

## 🌟 核心特性

- **轻量独立环境**：绝不污染全局环境，首次运行 `start.bat` 自动创建本地独立 `.venv` 并配置清华大学开源镜像源。
- **模块化按需安装**：启动器提供选择菜单，选择哪种交互模式，就仅下载并安装对应的轻量依赖，不浪费 1MB 磁盘与内存。
- **IKun 鸡哥桌宠原生交互（内置相对路径）**：
  - 默认内置于项目相对路径 `assets/pets/ikunchick/`，基于标准 192x208 帧切片渲染。
  - 透明无边框置顶、鼠标自由拖拽。
  - 鼠标悬停右下角浮现交互按钮，点击弹出输入框。
  - 头顶半透明气泡流式打字机回复与主动提示。
- **自由换装与 Codex Pets 支持**：
  - 支持直接将 [CodexPets.net](https://codexpets.net) 社区下载的任何桌宠包解压放入 `assets/pets/`，无缝换皮！
- **Agent Harness (智能体执行骨架)**：
  - 吸取 Claude Code 与 DeepSeek Harness 精髓，让桌宠不仅能聊天，还能**执行终端命令、按行读写代码、截屏观察屏幕、动态保存新技能**。
  - **双通道流式解耦**：当大模型同时生成中间思考文本与工具调用时，中间文本**毫秒级即时显示在桌宠头顶气泡与控制台**，无需在工具执行期间无聊干等。
- **NapCat QQ 深度内嵌集成**：
  - 无需另外开启独立项目或复杂部署，主程序内嵌管理 NapCat 进程。
  - 启动后终端直接输出字符二维码，掏出手机 QQ 扫码即登。
  - 主程序退出时自动关闭子进程，不留任何残留进程。
- **Jev (快脑) + VLM (慢脑) 主动协同**：
  - **快脑 (Jev / System 1)**：接入 BambooNode 决策模型，毫秒级通过 `Noul`、`Choice` 裁决是否打扰、过滤闲聊、以及工具执行后 100ms 快速终结循环（节省慢脑 3-5 秒延迟）。
  - **慢脑 (VLM / System 2)**：OpenAI 兼容格式视觉语言模型，负责多模态视觉理解与代码深度解决。
  - **三轴主动触发**：本机旁听流（活动窗口与剪贴板）、时序记忆延迟脉冲、心跳脉冲。
- **100% 本地极简嵌入式记忆引擎 (零向量依赖)**：
  - 纯 SQLite3 + FTS5 全文索引 + 艾宾浩斯物理时间衰减算法，内存占用 < 3MB，0 Token 消耗。
- **开箱即用可视化配置中心**：
  - 运行在本地 `http://127.0.0.1:18080`，零额外第三方依赖，自带直达注册链接与实时连接测试。

---

## 🎨 换装与自定义桌宠 (Codex Pets)

本项目全面兼容 **Codex Pets V1/V2** 像素动画标准（8列切片）：

1. 前往 **[Codex Pets 社区图库 (codexpets.net)](https://codexpets.net)** 挑选并下载你喜欢的桌宠压缩包；
2. 将压缩包直接解压到本项目的 `assets/pets/` 目录下（例如 `assets/pets/crystal-maiden/`，确保目录下有 `spritesheet.webp` 与 `pet.json`）；
3. 打开配置中心（浏览器 `http://127.0.0.1:18080`）或在 `config.json` 中将 `pet_dir` 改为对应的相对路径：
   ```json
   "pet_dir": "assets/pets/crystal-maiden"
   ```
4. 重新启动即可完成一键换装！

---

## 🚀 极速上手

### 1. 启动
在项目根目录下双击运行：
```cmd
start.bat
```

### 2. 配置 API Key
首次启动时若未配置，会自动在浏览器打开配置中心：
* **Jev API Key**: 点击页面上的引导链接前往 [https://bamboonode.dev/tokens](https://bamboonode.dev/tokens) 创建并填入。
* **VLM API Key**: 填写你的 OpenAI / Qwen-VL / DeepSeek 等 API Key 与模型名称。
* 页面中点击 **测试连接**，验证无误后点击 **保存配置**。

### 3. 选择运行模式
* `[1]` **仅桌宠交互 (Desktop Pet)**：唤出桌宠，开始本地感知与对话、代码排查。
* `[2]` **仅 NapCat QQ 交互 (Headless QQ Bot)**：终端打印二维码，手机 QQ 扫码登录。
* `[3]` **全功能模式**：桌宠与 QQ 同时在线，共享记忆与主动决策内核。
* `[4]` **偏好设置中心**：随时修改参数并即时生效。

---

## 📂 项目结构

```
d:\github\miniBot\
│  start.bat                 # 一键快速启动脚本
│  launcher.py               # 交互式菜单与按需依赖安装器
│  main.py                   # 统一核心调度入口
│  config.json               # 本地配置文件 (相对路径引用)
│
├─assets\                    # 桌宠与静态资产
│  └─pets\
│      └─ikunchick\          # 默认坤坤桌宠 (spritesheet.webp + pet.json)
│
├─core\                      # 核心微型大脑与智能体执行骨架
│  ├─harness.py              # Agent Harness 执行骨架 (双通道流式、Jev快速门控、系统工具)
│  ├─brain.py                # 慢脑：OpenAI 兼容 VLM 多模态流式客户端
│  ├─jev.py                  # 快脑：BambooNode Jev 决策客户端
│  ├─memory.py               # 极简嵌入式记忆引擎 (SQLite3 + FTS5 + 时间衰减)
│  ├─perception.py           # 本机状态旁听感知 (窗口/空闲/剪贴板/截屏)
│  ├─proactive.py            # 主动协同引擎 (心跳脉冲与延时队列)
│  ├─skill_manager.py        # Skill 动态热加载与自动提炼管理器
│  └─config.py               # 配置读写与热重载
│
├─channels\                  # 交互通道适配器
│  ├─pet_channel.py          # 桌宠界面 (PyQt5，支持流式打字机气泡)
│  ├─napcat_channel.py       # OneBot v11 WebSocket 监听
│  └─napcat_manager.py       # NapCat 本地进程与扫码生命周期
│
├─skills\                    # 自定义与自动提炼的 Skill
│  └─system_status.py
│
├─webui\                     # 零依赖极轻量配置中心 (http://127.0.0.1:18080)
│  ├─index.html
│  └─server.py
│
├─data\                      # 本地记忆库与临时数据
│  └─memory.db
│
└─tools\
   └─napcat\                 # 内嵌 NapCat.Shell 绿色工作目录
```
