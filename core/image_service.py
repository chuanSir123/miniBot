"""Image API adapter: scoped refs -> original data URLs -> cached output refs."""
import base64
import json
import urllib.request
from pathlib import Path

from core.config import load_config
from core.media_store import MediaStore, normalize_ref
from core.network import urlopen

# 测试/自定义场景可直接覆盖模块级 ENDPOINT；为空时读配置 image.base_url。
ENDPOINT = None


def _image_config():
    return (load_config().get("image") or {})


def _endpoint():
    if ENDPOINT:
        return ENDPOINT
    endpoint = str(_image_config().get("base_url") or "").strip()
    if not endpoint:
        raise ValueError("未配置图片服务地址（config.json 的 image.base_url）")
    return endpoint


def execute(context):
    try:
        return _execute(context)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _execute(context):
    runtime = context.get("_minibot") or {}
    image_cfg = _image_config()
    if not runtime.get("media_scope") or not runtime.get("workspace") or not runtime.get("media_root"):
        raise ValueError("必须通过Harness调用image_gen，缺少受信任媒体会话")
    prompt = context.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("prompt必须是1到12000字的文本")
    refs = context.get("image_refs", [])
    if not isinstance(refs, list) or len(refs) > 9 or any(not isinstance(ref, str) for ref in refs):
        raise ValueError("image_refs必须是最多9个media_ref的数组")
    mode = context.get("mode", "auto")
    if mode not in ("auto", "generate", "edit"):
        raise ValueError("mode只能是auto、generate或edit")
    mode = "edit" if mode == "auto" and refs else "generate" if mode == "auto" else mode
    if mode == "edit" and not refs:
        raise ValueError("编辑图片必须提供image_refs；先用list_media查询当前上传、引用消息或上一张生成图片，必要时get_media查看；不会退化为无参考生成")
    if mode == "generate" and refs:
        raise ValueError("generate模式不能带参考图片，请使用edit模式")
    store = MediaStore(runtime["media_root"])
    scope = runtime["media_scope"]
    # 模型有时会把聊天里的 media://img_xxx 原样带过来，统一收敛成裸引用。
    refs = [normalize_ref(ref) for ref in refs]
    images = [store.data_url(ref, scope) for ref in refs]
    original = store.get(refs[0], scope)[0] if refs else {}
    # 无 Pillow 的环境里入库尺寸为 0（未知），这里必须退化为默认画布，不能拿 0 去除。
    default_width = original.get("width") or 480
    default_height = original.get("height") or 768
    scale = min(1, 4096 / max(default_width, default_height))
    default_width, default_height = max(64, round(default_width * scale)), max(64, round(default_height * scale))
    width, height, count = context.get("width", default_width), context.get("height", default_height), context.get("n", 1)
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (width, height, count)):
        raise ValueError("width、height、n必须是整数")
    if not 64 <= width <= 4096 or not 64 <= height <= 4096 or not 1 <= count <= 9:
        raise ValueError("width/height须为64到4096，n须为1到9")
    payload = {"model": context.get("model") or image_cfg.get("model") or "sensenova-u1", "prompt": prompt.strip(),
               "width": width, "height": height, "images": images, "n": count, "response_format": "b64_json"}
    headers = {"Content-Type": "application/json", "User-Agent": "MiniBot-Image/1.0"}
    api_key = str(image_cfg.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    # Respect the user style; editing must not force an unrelated anime prompt.
    request = urllib.request.Request(_endpoint(), data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    with urlopen(request, timeout=110) as response:
        body = response.read(64 * 1024 * 1024 + 1)
    if len(body) > 64 * 1024 * 1024:
        raise ValueError("图片接口响应超过64MB")
    response_data = json.loads(body.decode("utf-8"))
    items = response_data.get("data", []) if isinstance(response_data, dict) else response_data
    if not isinstance(items, list) or not items:
        raise ValueError("图片接口没有返回data图片数组")
    decoded = []
    for item in items[:count]:
        encoded = item.get("b64_json") if isinstance(item, dict) else None
        if not isinstance(encoded, str) or len(encoded) > 28_000_000:
            raise ValueError("图片接口返回的b64_json无效或超过20MB")
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        decoded.append(base64.b64decode(encoded, validate=True))
    workspace = Path(runtime["workspace"]) / "images"
    workspace.mkdir(parents=True, exist_ok=True)
    assets = []
    for raw in decoded:
        asset = store.add(raw, scope, origin="generated", label=prompt,
                          retention_days=runtime.get("retention_days", load_config()["memory"]["retention_days"]))
        source = store.path(asset["media_ref"], scope)
        output = workspace / (asset["media_ref"] + source.suffix)
        output.write_bytes(raw)
        assets.append({**asset, "path": str(output), "url": "media://" + asset["media_ref"]})
    return {"ok": True, "mode": mode, "reference_media": refs, "assets": assets, "count": len(assets),
            "output": "\n".join("![图片](" + asset["url"] + ")" for asset in assets)}
