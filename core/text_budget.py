"""Local text-only token estimate: ceil(character count * 0.6)."""
import json
import math
import re

_IMAGE = object()


def text_only(value):
    if isinstance(value, dict):
        if value.get("type") in ("image_url", "input_image"):
            return _IMAGE
        return {key: result for key, item in value.items() if not key.startswith("_")
                and (result := text_only(item)) is not _IMAGE}
    if isinstance(value, list):
        return [result for item in value if (result := text_only(item)) is not _IMAGE]
    if isinstance(value, str):
        return re.sub(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+", "", value)
    return value


def estimate_text_tokens(value):
    value = text_only(value)
    if value is _IMAGE:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(text) * 0.6)
