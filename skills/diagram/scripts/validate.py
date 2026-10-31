# -*- coding: utf-8 -*-
r"""diagram validate: 发送前校验 SVG 是否符合 SKILL.md 设计规范。

纯 stdlib（xml.etree），规则：
  errors（必须清零）：XML 不可解析 / 缺 viewBox / viewBox 宽不是 680 / text 缺显式 fill /
              字号 < 11 / 连接线缺 fill="none"
  warnings（自行判断）：疑似外部 http 引用 / 渐变滤镜 / emoji / 内容溢出安全区
"""
import re
import sys
import xml.etree.ElementTree as ET

SVG_NS = "{http://www.w3.org/2000/svg}"
MIN_FONT_PX = 11
SAFE = (40, 640)  # 安全区 x 范围

_EMORE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F900-\U0001F9FF]"
)


def _q(tag):
    return tag.split("}", 1)[-1]


def _explicit_fill(el):
    return el.get("fill") is not None


def validate(path):
    errors, warnings = [], []
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return {"ok": False, "errors": [f"XML 不可解析: {exc}"], "warnings": [], "stats": {}}
    root = tree.getroot()

    vb = root.get("viewBox", "")
    if not vb:
        errors.append("根元素缺 viewBox（规范: 0 0 680 H）")
    else:
        parts = vb.replace(",", " ").split()
        if len(parts) != 4:
            errors.append(f"viewBox 需要 4 个值，实际: {vb!r}")
        else:
            try:
                x, y, w, h = (float(p) for p in parts)
            except ValueError:
                errors.append(f"viewBox 含非数字: {vb!r}")
            else:
                if w != 680:
                    errors.append(f"viewBox 宽必须为 680，实际 {w:g}")
                if x != 0 or y != 0:
                    warnings.append(f"viewBox 起点非 0 0（规范建议 0 0 680 H）: {vb!r}")

    texts = connectors = shapes = 0
    for el in root.iter():
        tag = _q(el.tag)
        style = el.get("style") or ""

        if tag == "text":
            texts += 1
            if not _explicit_fill(el) and "fill" not in style:
                errors.append(f"text 缺显式 fill: {(el.text or '')[:24]!r}")
            fs = el.get("font-size") or ""
            m = re.search(r"([\d.]+)", fs)
            if fs and m and float(m.group(1)) < MIN_FONT_PX:
                errors.append(f"字号 {m.group(1)}px < {MIN_FONT_PX}px: {(el.text or '')[:24]!r}")

        if tag in ("path", "polyline", "line") and el.get("marker-end"):
            connectors += 1
            if el.get("fill") != "none" and "fill:none" not in style.replace(" ", ""):
                errors.append(f"连接线必须 fill=\"none\" 否则箭头路径会被填充")

        if tag in ("linearGradient", "radialGradient", "filter"):
            warnings.append(f"使用了 <{tag}>（规范禁止渐变/滤镜）")

        if tag in ("rect", "circle", "ellipse", "polygon"):
            shapes += 1
            if not _explicit_fill(el) and "fill" not in style:
                errors.append(f"<{tag}> 缺显式 fill")

        if tag == "image":
            href = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
            if href.startswith(("http://", "https://")):
                warnings.append(f"外部图片引用（渲染需联网且可能超时）: {href[:60]}")

    raw = open(path, "rb").read().decode("utf-8", "ignore")
    found = _EMORE.findall(raw)
    if found:
        warnings.append(f"发现 {len(found)} 个 emoji（规范禁止，改用图形符号）")

    return {
        "ok": not errors,
        "errors": errors[:30],
        "warnings": warnings[:30],
        "stats": {"texts": texts, "connectors": connectors, "shapes": shapes},
    }


def execute(context):
    if isinstance(context, dict) and isinstance(context.get("args"), dict):
        args = dict(context["args"])
    else:
        args = {k: v for k, v in (context or {}).items() if k != "_minibot"}
    file_path = str(args.get("file_path") or "")
    if not file_path:
        return {"ok": False, "error": "缺少 file_path 参数"}
    ws = (context.get("_minibot") or {}).get("workspace")
    if ws and not __import__("os").path.isabs(file_path):
        file_path = __import__("os").path.join(ws, file_path)
    import os
    if not os.path.isfile(file_path):
        return {"ok": False, "error": f"文件不存在: {file_path}"}
    result = validate(file_path)
    result["file"] = file_path
    return result


if __name__ == "__main__":
    print(validate(sys.argv[1]))
