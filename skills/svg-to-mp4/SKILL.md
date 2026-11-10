---
name: svg_to_mp4
description: 把 HTML/SVG 动画页面转成 MP4 视频。主人要求"SVG/HTML动画转MP4/录制成视频/导出视频"时调用本技能。内部用 Playwright 无头 Chromium + 虚拟时钟逐帧截图（时间精确推进，平均帧间隔恰为1/fps），ffmpeg 编码器自动回退（libx264→libopenh264→mpeg4，兼容禁用libx264的ffmpeg），最后 ffprobe 验证时长/分辨率/编码/帧率并返回结果。截图在独立 Python 子进程中执行（自动探测装了 playwright 的解释器），避免宿主进程缺模块。
entry: scripts/svg_to_mp4.py
---

## 运行方式（MiniBot）

脚本命令相对项目根目录执行：用 `bash` 运行，传 `cwd=project`，路径写 `skills/<name>/scripts/<file>.py`；不要在命令里写绝对路径。

本技能是**纯 Markdown 指令**：先用 `read` 读完本文件，再用 `bash` 运行下面的命令。

    python skills/svg-to-mp4/scripts/svg_to_mp4.py --arguments '{"file_path":"assets/anim/demo.html","duration_sec":4}'

- 参数是单行 JSON 对象，用单引号包住即可；输出是一行 JSON，`ok` 表示是否成功。
- 需要按会话解析图片时加 `--scope qq:group:88`；耗时任务给 `bash` 传 `background=true`。
- 需要媒体文件时先用 `get_media` 取 `file_path` 再传路径；不要传 Base64、URL 或编造引用。
- 下面的参数表对应 `--arguments` 里的字段；脚本由 bash 直接执行，输出一行 JSON。

# svg_to_mp4


## 参数

- `file_path` (string, 必填)：HTML/SVG动画文件路径（绝对或相对Harness工作空间）
- `duration_sec` (number, 可选)：录制时长秒数，默认12
- `fps` (integer, 可选)：帧率，默认30
- `width` (integer, 可选)：视口宽度，默认1280
- `height` (integer, 可选)：视口高度，默认800
- `output` (string, 可选)：输出MP4路径，默认输入文件名+.mp4
