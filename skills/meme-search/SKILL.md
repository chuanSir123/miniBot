---
name: meme-search
description: 检索本地表情包图库（自动收集并带 VLM 标签）。主人要求"找张表情包/来个梗图/发表情/看看图库有什么标签"时调用本技能。可列出全部标签及库存数量，按标签取图；返回本地文件路径可直接用于发送。
entry: scripts/meme_search.py
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/meme-search/scripts/meme_search.py --arguments '{"action":"tags","limit":30}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`ok` 表示是否成功。
- 需要按会话解析图片时加 `--scope qq:group:88`；耗时任务给 `bash` 传 `background=true`。
- 需要媒体文件时先用 `get_media` 取 `file_path` 再传路径；不要传 Base64、URL 或编造引用。
- 下面的参数表对应 `--arguments` 里的字段；脚本由 bash 直接执行，输出一行 JSON。

# meme-search：表情包图库检索

图库由系统自动收集（群聊/私聊里的表情会静默入库，VLM 自动打标签），本技能给模型一个查询入口。

## 两种用法

**1) 列标签**——不知道图库里有什么时先查这个：

```json
python skills/meme-search/scripts/meme_search.py --arguments '{"action": "tags", "limit": 30}'
```

返回标签 → 库存数量列表（`used` 越小说明最近越少发，优先用它避免重复）。

**2) 按标签取图**：

```json
python skills/meme-search/scripts/meme_search.py --arguments '{"action": "pick", "tag": "赞同"}'
```

返回 `file_path`（本地图片绝对路径）与该图标签；标签不存在返回 `ok:false` 并附相近标签建议。

## 注意

- 标签来自 VLM 自动打标 + 文件名提取，常见如：赞同、疑问、开心、无语、猫猫……
- 发送遵循 QQ 权限策略（和普通图片一样，不越权）
- 取图会计入 `use_count`，系统发图避让（冷却）机制同样生效
