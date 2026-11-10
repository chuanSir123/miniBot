---
name: diagram
description: 用 SVG 画架构图、流程图、对比图、概念示意图，并渲染成高清 PNG 用于 QQ 发送或 WebUI 展示。当主人要求"画图/画个架构图/流程图/示意图/图解/可视化说明"时调用本技能。内含完整设计规范（画布、调色板、布局、图型路由），配 validate 脚本做发送前校验、render 脚本出 PNG。
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：用 `read` 读完，按下面的设计规范写好 SVG，再用
`bash` 直接跑随技能附带的脚本。
脚本就是普通命令，不需要任何技能执行工具。

    python skills/diagram/scripts/validate.py --arguments '{"file_path":"assets/diagrams/example.svg"}'
    python skills/diagram/scripts/render.py --arguments '{"file_path":"assets/diagrams/example.svg","scale":2}'

# diagram：SVG 图示设计与渲染

工作流：**写 SVG → `validate.py` 校验 → `render.py` 出 PNG → 把输出路径告诉用户**。
三步缺一不可：不校验就渲染可能白出废图；不渲染 QQ 发不了 SVG。

## 第一步：写 SVG

写 SVG 文件到当前工作空间（如 `assets/diagrams/example.svg`）。规范分三部分：

### 图型路由（先选型再动手）

| 需求 | 选型 | 画法 |
|---|---|---|
| 步骤、因果、决策 | 流程图 | 单向流，箭头连接，最多 4~5 个节点 |
| 包含关系、层级、架构 | 结构图 | 外层大圆角容器（rx 20~24），内层 100~200 号浅色填充，最多嵌套 2~3 层 |
| 对比（A vs B） | 分栏结构图 | 左右两列同构，用不同色系区分 |
| 数量、强度 | 色阶隐喻 | 暖色系=热/活跃，冷色系=冷/静止 |
| 时间线 | 横向刻度条 | 事件点均匀分布，文字在点上方/下方交替 |

### 画布与布局（硬性）

- 根元素：`viewBox="0 0 680 H"`，**宽固定 680**；H = 最底部元素 y + 20，不要猜
- 安全区：内容放在 x 40~640、y 40~H-40
- 节点间距 ≥ 60px；框内边距 ≥ 24px；文字与框边 ≥ 12px
- 同类节点同高：单行文字 44px 高，双行 56px
- 箭头 marker 定义一次放 `<defs>`，连接线一律 `fill="none"`
- 全部文字 `dominant-baseline="central"`，居中排用 `text-anchor="middle"`

### 颜色与文字（硬性）

- 每个图形、每个 `<text>` **必须显式 fill**，不能依赖继承
- 字号最小 11px；标题 14px font-weight 500；正文 12~13px font-weight 400
- 只用两种字重：400 和 500，禁止 600/700
- 调色板（浅色主题：浅号填充 + 600 描边 + 800 文字）：

| 色系 | 50(浅填充) | 100 | 600(描边) | 800(文字) |
|---|---|---|---|---|
| purple | #EEEDFE | #CECBF6 | #534AB7 | #3C3489 |
| teal | #E1F5EE | #9FE1CB | #0F6E56 | #085041 |
| coral | #FAECE7 | #F5C4B3 | #993C1D | #712B13 |
| pink | #FBEAF0 | #F4C0D1 | #993556 | #72243E |
| gray | #F1EFE8 | #D3D1C7 | #5F5E5A | #444441 |
| blue | #E6F1FB | #B5D4F4 | #185FA5 | #0C447C |
| green | #EAF3DE | #C0DD97 | #3B6D11 | #173404 |
| amber | #FAEEDA | #FAC775 | #854F0B | #633806 |
| red | #FCEBEB | #F7C1C1 | #A32D2D | #791F1F |

- 一张图最多 2 个色系 + gray 中性色；红/绿仅用于错误与成功语义
- 禁止：渐变、阴影、模糊、emoji、英文 ALL CAPS 标题

## 第二步：校验

```
python skills/diagram/scripts/validate.py assets/diagrams/example.svg
```

errors 必须清零，warnings 逐条判断。校验通过才进入渲染。

## 第三步：渲染 PNG

```
python skills/diagram/scripts/render.py assets/diagrams/example.svg --scale 2
```

- 用 Playwright 无头 Chromium 截图（自动探测可用解释器），`scale` 默认 2 出高清图
- 返回 `output`（PNG 绝对路径）、`width/height/size_bytes`
- 渲染失败且报缺 playwright 时，如实告知用户环境缺依赖，不要伪造输出

## 参数

- `action` (string, 必填)：`validate` 或 `render`
- `file_path` (string, 必填)：SVG 文件路径（绝对或相对当前工作空间）
- `output` (string, 可选)：渲染输出 PNG 路径，默认与输入同名 .png
- `scale` (number, 可选)：渲染倍率，默认 2，范围 1~4
