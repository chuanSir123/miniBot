"""本地语音转写：faster-whisper（CTranslate2）。

- 自动探测 NVIDIA GPU：有可用 CUDA 库就用 float16 跑 cuda，否则回落 CPU int8。
- 模型按需下载到 data/models/stt，纯 CPU 也能推理；默认 GPU 用 large-v3-turbo，
  CPU 用 small（约 480MB，中文短语音够用）。
- 安装/下载在后台线程执行，Web 设置页轮询状态。
"""
import importlib.util
import io
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from core.config import load_config
from core.process import run_bounded

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data" / "models" / "stt"

MODEL_DEFAULT = "small"
MODEL_CHOICES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"

_state_lock = threading.RLock()
_state = {"state": "idle"}
_model_lock = threading.RLock()
_models = {}
_gpu_cache = {"at": 0.0, "value": None}
_cuda_broken = False


def is_available():
    return importlib.util.find_spec("faster_whisper") is not None


def _nvidia_smi():
    exe = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if not exe:
        return None
    result = run_bounded(
        [exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
        timeout=8,
        max_bytes=8000,
    )
    if not result.get("ok"):
        return None
    line = next((x.strip() for x in (result.get("output") or "").splitlines() if x.strip()), "")
    if not line:
        return None
    name, _, memory = line.partition(",")
    vram = re.sub(r"[^0-9]", "", memory)
    return {"name": name.strip()[:80], "vram_mb": int(vram) if vram else 0}


def _ctranslate2():
    try:
        import ctranslate2  # noqa: F401

        return ctranslate2
    except Exception:
        return None


def _supported_compute_types(device):
    lib = _ctranslate2()
    if lib is None:
        return set()
    try:
        return set(lib.get_supported_compute_types(device))
    except Exception:
        return set()


def _cuda_device_count():
    lib = _ctranslate2()
    if lib is None:
        return 0
    try:
        return int(lib.get_cuda_device_count())
    except Exception:
        return 0


def gpu_info(refresh=False):
    """检测 NVIDIA GPU 硬件与 CUDA 运行时是否真正可用（只探测，不加载模型）。"""
    with _state_lock:
        if not refresh and _gpu_cache["value"] and time.monotonic() - _gpu_cache["at"] < 60:
            return dict(_gpu_cache["value"])
    hardware = _nvidia_smi()
    count = _cuda_device_count()
    detected = hardware is not None or count > 0
    types = _supported_compute_types("cuda") if count > 0 else set()
    usable = bool(types) and not _cuda_broken
    name = (hardware or {}).get("name", "")
    if usable:
        reason = (
            f"已检测到 {name}，CUDA 运行时可用"
            if name
            else "CUDA 运行时可用，默认用 GPU 推理（未取到显卡型号）"
        )
    elif detected:
        reason = "检测到 NVIDIA GPU，但 CUDA/cuDNN 运行时不可用，将使用 CPU"
    else:
        reason = "未检测到 NVIDIA GPU，使用 CPU 推理"
    if _cuda_broken:
        usable = False
        reason = "CUDA 加载失败，已自动回落到 CPU 推理"
    value = {
        "detected": detected,
        "name": name,
        "vram_mb": (hardware or {}).get("vram_mb", 0),
        "devices": count,
        "compute_types": sorted(types),
        "usable": usable,
        "reason": reason,
    }
    with _state_lock:
        _gpu_cache.update(at=time.monotonic(), value=dict(value))
        if _cuda_broken:
            value["usable"] = False
    return value


def resolve_device(cfg):
    """返回 (device, compute_type, 说明)。"""
    cfg = cfg or {}
    gpu = gpu_info()
    wanted = str(cfg.get("device") or "auto")
    if wanted == "cuda" and not gpu["usable"]:
        return "cpu", _cpu_compute(cfg), "配置为 CUDA 但运行时不可用，已回落 CPU"
    if wanted == "cpu":
        return "cpu", _cpu_compute(cfg), "按配置使用 CPU"
    if wanted == "cuda":
        return "cuda", _cuda_compute(cfg), "按配置使用 CUDA"
    if gpu["usable"]:
        return "cuda", _cuda_compute(cfg), gpu["reason"]
    return "cpu", _cpu_compute(cfg), gpu["reason"]


def _cuda_compute(cfg):
    explicit = str(cfg.get("compute_type") or "auto")
    if explicit != "auto":
        return explicit
    types = _supported_compute_types("cuda") or {"float16"}
    for candidate in ("float16", "int8_float16", "float32"):
        if candidate in types:
            return candidate
    return "float32"


def _cpu_compute(cfg):
    explicit = str(cfg.get("compute_type") or "auto")
    if explicit != "auto":
        return explicit
    types = _supported_compute_types("cpu")
    if not types or "int8" in types:
        return "int8"
    if "int8_float32" in types:
        return "int8_float32"
    return "float32"


def model_name(cfg):
    cfg = cfg or {}
    configured = str(cfg.get("model") or "").strip()
    return configured or MODEL_DEFAULT


def _normalize(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def model_downloaded(name):
    return bool(model_path(name))


def canonical_dir(name):
    """每个模型一个独立目录，避免不同模型的文件互相覆盖。"""
    return MODEL_DIR / (_normalize(name) or "model")


def model_path(name):
    target = _normalize(name)
    if not target:
        return ""
    own = MODEL_DIR / target
    if (own / "model.bin").is_file():
        return str(own)
    if not MODEL_DIR.exists():
        return ""
    for entry in MODEL_DIR.iterdir():
        if entry.is_dir() and _matches_dir(entry.name, target):
            if (entry / "model.bin").is_file():
                return str(entry)
    return ""


def _matches_dir(dir_name, target):
    normalized = _normalize(dir_name)
    return (
        normalized == target
        or normalized.endswith("-" + target)
        or normalized.endswith("--" + target)
    )


_DOWNLOAD_SNIPPET = (
    "import sys\n"
    "from faster_whisper.utils import download_model\n"
    "print(download_model(sys.argv[1], output_dir=sys.argv[2]))\n"
)


def _download_env(local_cfg=None):
    local_cfg = local_cfg or load_config()["stt"].get("local", {})
    endpoint = str(local_cfg.get("hf_endpoint") or "").strip()
    env = dict(os.environ)
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def ensure_model(name, local_cfg=None, timeout=1800):
    """确保模型权重在本地；缺了就用子进程（带镜像环境变量）下载。"""
    path = model_path(name)
    if path:
        return path
    if not is_available():
        raise ValueError("本地 STT 未安装，请先安装依赖")
    target_dir = canonical_dir(name)
    target_dir.mkdir(parents=True, exist_ok=True)
    result = run_bounded(
        [sys.executable, "-c", _DOWNLOAD_SNIPPET, name, str(target_dir)],
        timeout=timeout,
        max_bytes=8000,
        cwd=str(ROOT),
        env=_download_env(local_cfg),
    )
    path = model_path(name)
    if path:
        return path
    detail = (result.get("output") or "").strip()
    raise ValueError("模型下载失败：" + (detail[-300:] or "请检查网络或下载源设置"))


def status():
    local_cfg = load_config()["stt"].get("local", {})
    device, compute_type, reason = resolve_device(local_cfg)
    name = model_name(local_cfg)
    installed = is_available()
    version = ""
    if installed:
        try:
            import faster_whisper

            version = getattr(faster_whisper, "__version__", "")
        except Exception:
            version = ""
    with _state_lock:
        installation = dict(_state)
    return {
        "installed": installed,
        "version": version,
        "python": sys.executable,
        "gpu": gpu_info(),
        "device": device,
        "compute_type": compute_type,
        "device_reason": reason,
        "model": name,
        "model_downloaded": model_downloaded(name),
        "model_path": model_path(name),
        "model_dir": str(MODEL_DIR),
        "model_choices": MODEL_CHOICES,
        "installation": installation,
    }


def start_install(index=None):
    """后台 pip 安装 faster-whisper（含 ctranslate2 / tokenizers / av）。"""

    def job():
        local_cfg = load_config()["stt"].get("local", {})
        pip_index = str(index or local_cfg.get("pip_index") or "").strip()
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            "--no-input",
            "faster-whisper",
        ]
        if pip_index:
            cmd[4:4] = ["-i", pip_index]
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        result = run_bounded(cmd, timeout=1500, max_bytes=400000, env=env)
        output = (result.get("output") or "").strip()
        tail = output[-1500:]
        if not result.get("ok") or "ERROR" in output.upper():
            return {"state": "install_failed", "error": tail or "pip 安装失败", "log": tail}
        _gpu_cache.update(at=0.0, value=None)
        return {"state": "installed", "log": tail}

    return _run_job("installing", job)


def start_download(model=None):
    """后台下载模型权重（默认当前配置的模型）。"""

    def job():
        if not is_available():
            return {"state": "model_failed", "error": "请先安装本地 STT 依赖"}
        local_cfg = load_config()["stt"].get("local", {})
        name = str(model or "").strip() or model_name(local_cfg)
        try:
            path = ensure_model(name, local_cfg)
            return {"state": "model_ready", "model": name, "path": path}
        except Exception as exc:
            return {"state": "model_failed", "error": str(exc)[:300]}

    return _run_job("downloading", job)


def _run_job(active_state, job):
    with _state_lock:
        if _state.get("state") in ("installing", "downloading"):
            return dict(_state)
        _state.clear()
        _state.update(state=active_state, started_at=time.time())

    def worker():
        try:
            result = job()
        except Exception as exc:
            result = {"state": "failed", "error": str(exc)[:300]}
        result.setdefault("state", "idle")
        with _state_lock:
            _state.clear()
            _state.update(result)
            _state["finished_at"] = time.time()

    threading.Thread(target=worker, daemon=True, name=f"local-stt-{active_state}").start()
    return dict(_state)


def _load_model(name, device, compute_type, local_cfg=None):
    key = (name, device, compute_type)
    with _model_lock:
        cached = _models.get(key)
        if cached is not None:
            return cached
        if not is_available():
            raise ValueError("本地 STT 未安装，请在设置页安装依赖")
        model_dir = ensure_model(name, local_cfg)
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise ValueError(f"本地 STT 依赖异常：{str(exc)[:200]}") from exc
        try:
            model = WhisperModel(
                model_dir,
                device=device,
                compute_type=compute_type,
                cpu_threads=max(1, (os.cpu_count() or 4) - 1),
                num_workers=1,
            )
        except Exception as exc:
            raise ValueError(f"加载本地模型失败：{str(exc)[:300]}") from exc
        _models[key] = model
        return model


def transcribe_bytes(raw, local_cfg=None):
    """把音频字节转写成文字；CUDA 失败时自动回落 CPU。"""
    local_cfg = local_cfg or {}
    if not raw:
        raise ValueError("音频内容为空")
    device, compute_type, _reason = resolve_device(local_cfg)
    name = model_name(local_cfg)
    language = str(local_cfg.get("language") or "").strip() or None
    beam = local_cfg.get("beam_size", 5)
    if isinstance(beam, bool) or not isinstance(beam, int):
        beam = 5

    def run(dev, ctype):
        model = _load_model(name, dev, ctype, local_cfg)
        segments, _info = model.transcribe(
            io.BytesIO(raw),
            language=language,
            beam_size=beam,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        return "".join(segment.text for segment in segments).strip()

    try:
        return run(device, compute_type)
    except Exception as exc:
        global _cuda_broken
        message = str(exc).lower()
        cuda_related = any(
            key in message
            for key in ("cuda", "cudnn", "cublas", "gpu", "no kernel image", "device")
        )
        if device == "cuda" and cuda_related:
            _cuda_broken = True
            _gpu_cache.update(at=0.0, value=None)
            from core.console import log

            log("STT", "local.cuda_fallback", error=str(exc)[:200])
            return run("cpu", _cpu_compute(local_cfg))
        raise


def reset_cache():
    with _model_lock:
        _models.clear()
    with _state_lock:
        _gpu_cache.update(at=0.0, value=None)
