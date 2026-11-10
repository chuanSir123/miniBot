# MiniBot 示例索引

本目录是系统提示词 `<docs>` 段里 `Examples` 指向的位置，供模型在需要参考本项目写法时读取。

## 可参考的现成示例

| 想找什么 | 去哪读 |
| --- | --- |
| 自定义技能怎么写（SKILL.md frontmatter + 正文指令 + 可选脚本） | `skills/<name>/SKILL.md`，完整规范见 `skills/skill-creator/SKILL.md` |
| 技能脚本如何接收参数、返回结构化结果、使用 media_ref | `skills/*/scripts/*.py` |
| 工具调用与 harness 行为的断言写法 | `tests/test_*.py` |
| 诊断与维护脚本 | `tools/` |
| 配置模板 | `config.example.json` |
| 架构与实现说明 | `README.md`、`docs/` |

## 约定

- 路径相对项目根目录解析；`<docs>` 段里给出的 `Additional docs` / `Examples` 是绝对路径，不要拿当前工作目录去拼。
- 这里的示例是**只读参考**：不要在示例目录里写运行时产物，运行时文件应落在 `data/workspace/` 下。
- 新增示例时，优先补到对应的 `skills/`、`tests/` 或 `tools/`，再在本表登记一行。
