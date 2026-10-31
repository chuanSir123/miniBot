"""One-shot Python skill host. Process isolation is not an OS security sandbox."""

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path


def _inject_media_helpers(request):
    """给技能进程装好 media_ref 解析器。

    技能参数里流动的是 media_ref（如 img_xxx），不是图片数据；上游接口要的是
    data URL 或原始字节。这里把解析能力直接挂到 context["_minibot"]，让技能不必
    自己构造 MediaStore、自己处理 scope，也不会把 Base64 回灌到模型上下文。
    """
    runtime = request.get("_minibot")
    if not isinstance(runtime, dict) or not runtime.get("media_root"):
        return False
    try:
        from core.media_store import MediaStore, normalize_ref
    except Exception:
        return False
    scope = runtime.get("media_scope") or "local"
    try:
        store = MediaStore(runtime["media_root"])
    except Exception:
        return False

    def data_url(ref, ref_scope=None):
        """media_ref -> data:<mime>;base64,... （上游图像接口直接可用的形态）"""
        return store.data_url(normalize_ref(ref), ref_scope or scope)

    def media_bytes(ref, ref_scope=None):
        """media_ref -> 原始字节。"""
        return store.get(normalize_ref(ref), ref_scope or scope)[1]

    def media_manifest(ref, ref_scope=None):
        """media_ref -> 元数据（mime/width/height/size/origin 等）。"""
        return store.get(normalize_ref(ref), ref_scope or scope)[0]

    def media_path(ref, ref_scope=None):
        """media_ref -> 本地文件路径，适合交给 ffmpeg/子进程。"""
        return str(store.path(normalize_ref(ref), ref_scope or scope))

    def resolve_media_refs(refs, ref_scope=None):
        """[media_ref, ...] -> [data URL, ...]，保持顺序；单个字符串也接受。"""
        if isinstance(refs, str):
            refs = [refs]
        return [data_url(ref, ref_scope) for ref in (refs or [])]

    runtime.update({
        "store": store,
        "data_url": data_url,
        "media_bytes": media_bytes,
        "media_manifest": media_manifest,
        "media_path": media_path,
        "resolve_media_refs": resolve_media_refs,
    })
    return True


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Generated skills commonly use urllib directly. Disable Windows proxy
    # discovery here as well as scrubbing proxy variables in the parent.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    # 解析器注入失败不能影响不需要媒体的技能，技能自己也会再校验一次。
    _inject_media_helpers(request)
    spec = importlib.util.spec_from_file_location("isolated_skill", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.execute(request)
    if isinstance(result, dict) and isinstance(result.get("ok"), bool):
        print("__MINIBOT_RESULT__" + json.dumps(result, ensure_ascii=False, default=str))
    else:
        print(str(result))
