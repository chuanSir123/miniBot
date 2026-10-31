---
name: svg_to_mp4
description: 把 HTML/SVG 动画页面转成 MP4 视频。主人要求"SVG/HTML动画转MP4/录制成视频/导出视频"时调用本技能。内部用 Playwright 无头 Chromium + 虚拟时钟逐帧截图（时间精确推进，平均帧间隔恰为1/fps），ffmpeg 编码器自动回退（libx264→libopenh264→mpeg4，兼容禁用libx264的ffmpeg），最后 ffprobe 验证时长/分辨率/编码/帧率并返回结果。截图在独立 Python 子进程中执行（自动探测装了 playwright 的解释器），避免宿主进程缺模块。
entry: scripts/svg_to_mp4.py
---

# svg_to_mp4

通过 `execute_skill` 调用，脚本 `scripts/svg_to_mp4.py` 在隔离子进程中执行（Harness 注入媒体会话上下文）。需要 playwright 与 ffmpeg。

## 参数

- `file_path` (string, 必填)：HTML/SVG动画文件路径（绝对或相对Harness工作空间）
- `duration_sec` (number, 可选)：录制时长秒数，默认12
- `fps` (integer, 可选)：帧率，默认30
- `width` (integer, 可选)：视口宽度，默认1280
- `height` (integer, 可选)：视口高度，默认800
- `output` (string, 可选)：输出MP4路径，默认输入文件名+.mp4
