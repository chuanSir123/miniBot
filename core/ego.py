"""On-demand, host-aware ego-lite bootstrap and bounded CLI execution."""

import json
import os
import platform
import shutil
import time
import threading
import urllib.request
from core.network import urlopen
import urllib.error
from pathlib import Path
from core.config import load_config
from core.process import run_bounded

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_INSTALLER = "https://cdn.ego.app/setup/windows/x64/egoliteSetup.exe"
INSTALL_GUIDE = "https://github.com/citrolabs/ego-lite/blob/main/skills/ego-browser/references/install.md"
_install_lock = threading.Lock()
_install_state = {"state": "idle"}


def start_install():
    with _install_lock:
        if _install_state.get("state") == "installing":
            return dict(_install_state)
        _install_state.clear()
        _install_state.update(state="installing")

    def worker():
        try:
            result = install()
        except Exception as exc:
            result = {"ok": False, "state": "install_failed", "error": str(exc)}
        with _install_lock:
            _install_state.clear()
            _install_state.update(result)

    threading.Thread(target=worker, daemon=True, name="ego-install").start()
    return {"state": "installing"}


def environment():
    system = platform.system()
    release = platform.release().lower()
    container = Path("/.dockerenv").exists() or bool(os.environ.get("CONTAINER"))
    remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    wsl = "microsoft" in release or bool(os.environ.get("WSL_DISTRO_NAME"))
    machine = platform.machine().lower()
    supported = system in ("Windows", "Darwin") and not (container or remote or wsl)
    if system == "Windows" and machine not in ("amd64", "x86_64", "x64"):
        supported = False
    if system == "Darwin" and machine not in ("arm64", "aarch64", "x86_64", "amd64"):
        supported = False
    return {
        "system": system,
        "architecture": machine,
        "container": container,
        "remote": remote,
        "wsl": wsl,
        "supported": supported,
        "reason": ""
        if supported
        else "需要支持的本机 Windows x64 / macOS 环境；Linux、WSL、容器或SSH不能直接控制另一台电脑的浏览器。请在桌面主机运行 MiniBot。",
    }


def find_executable():
    configured = load_config()["ego_lite"].get("executable", "")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise ValueError("配置的 ego-browser 路径不存在")
        return str(path.resolve())
    found = shutil.which("ego-browser")
    if found:
        return found
    for suffix in (".cmd", ".exe", ".bat", "") if os.name == "nt" else ("",):
        path = Path.home() / ".local" / "bin" / ("ego-browser" + suffix)
        if path.is_file():
            return str(path)
    if os.name == "nt":
        import winreg

        for hive, name in (
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ):
            try:
                with winreg.OpenKey(hive, name) as entry:
                    updated_path = winreg.QueryValueEx(entry, "Path")[0]
                found = shutil.which(
                    "ego-browser", path=os.path.expandvars(updated_path)
                )
                if found:
                    return found
            except OSError:
                pass
    return ""


def status():
    env = environment()
    executable = find_executable() if env["supported"] else ""
    return {
        "ok": True,
        "environment": env,
        "installed_cli": bool(executable),
        "executable": executable,
        "runtime_verified": False,
        "next_step": "运行 guide 验证连接"
        if executable
        else "安装后在 ego-lite 完成首次引导，再检查状态",
        "install_guide": INSTALL_GUIDE,
        "installation": dict(_install_state),
    }


def _command(executable, arguments):
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        if any(
            c in executable
            for c in ('"', "%", "!", "&", "|", "<", ">", "^", "\n", "\r")
        ):
            raise ValueError("ego-browser 启动脚本路径包含不支持的 shell 字符")
        # Only fixed commands go into cmd; model-generated JavaScript uses stdin.
        if arguments not in (["skill"], ["nodejs"]):
            raise ValueError("不支持的 ego-browser 子命令")
        # A list would apply CRT quote escaping, which cmd.exe does not accept.
        return (
            '"'
            + os.environ.get("COMSPEC", "cmd.exe")
            + '" /d /s /c ""'
            + executable
            + '" '
            + arguments[0]
            + '"'
        )
    return [executable, *arguments]


def install(cancel_event=None):
    env = environment()
    if not env["supported"]:
        return {
            "ok": False,
            "environment": env,
            "error": env["reason"],
            "install_guide": INSTALL_GUIDE,
        }
    if find_executable():
        return {**status(), "state": "already_installed"}
    folder = ROOT / "tools" / "ego-lite"
    folder.mkdir(parents=True, exist_ok=True)
    if env["system"] == "Windows":
        target = folder / "egoliteSetup.exe"
        partial = folder / "egoliteSetup.exe.part"
        if not target.exists():
            try:
                started = time.monotonic()
                request = urllib.request.Request(
                    WINDOWS_INSTALLER, headers={"User-Agent": "MiniBot/1.0"}
                )
                with (
                    urlopen(request, timeout=30) as response,
                    partial.open("wb") as output,
                ):
                    total = 0
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise RuntimeError("安装下载已取消")
                        if time.monotonic() - started > 300:
                            raise TimeoutError("安装器下载超过5分钟，可重试安装")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > 600 * 1024 * 1024:
                            raise ValueError("安装器超过大小限制")
                        output.write(chunk)
            except (OSError, urllib.error.URLError):
                curl = shutil.which("curl.exe")
                if not curl:
                    raise
                downloaded = run_bounded(
                    [
                        curl,
                        "--noproxy",
                        "*",
                        "--fail",
                        "--location",
                        "--silent",
                        "--show-error",
                        "--retry",
                        "2",
                        "--connect-timeout",
                        "20",
                        "--max-time",
                        "100",
                        "--max-filesize",
                        "629145600",
                        "--output",
                        str(partial),
                        WINDOWS_INSTALLER,
                    ],
                    timeout=110,
                    max_bytes=2000,
                    cancel_event=cancel_event,
                )
                if not downloaded.get("ok"):
                    raise ValueError(
                        "官方安装器下载失败：" + downloaded.get("output", "请检查网络")
                    )
            partial.replace(target)
        quoted = str(target).replace("'", "''")
        check = run_bounded(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$s=Get-AuthenticodeSignature -LiteralPath '" + quoted + "'; "
                "$o=@{status=$s.Status.ToString();subject=$s.SignerCertificate.Subject};$o|ConvertTo-Json -Compress",
            ],
            timeout=15,
        )
        signature = json.loads(check["output"]) if check.get("ok") else {}
        if signature.get("status") != "Valid" or not any(
            x in signature.get("subject", "").lower() for x in ("citro", "ego")
        ):
            raise ValueError(
                "安装器签名未通过发布者校验；未运行，请从官网核验下载：https://lite.ego.app/"
            )
        os.startfile(str(target))
        return {
            "ok": True,
            "state": "needs_onboarding",
            "installer": str(target),
            "message": "已下载并打开 Windows 官方安装器。请完成安装并打开 ego-lite，自行选择是否导入浏览器数据，完成首次引导后检查环境。此状态不代表浏览器自动化已可用。",
        }
    # Official script is macOS-only. No macOS script is run on Windows or Linux.
    script = folder / "install-macos.sh"
    url = "https://raw.githubusercontent.com/citrolabs/ego-lite/main/skills/ego-browser/scripts/install.sh"
    with urlopen(url, timeout=20) as response:
        content = response.read(100001)
    if len(content) > 100000:
        raise ValueError("安装脚本过大")
    script.write_bytes(content)
    result = run_bounded(
        ["sh", str(script)], timeout=120, max_bytes=16000, cancel_event=cancel_event
    )
    return {
        **result,
        "state": "needs_onboarding" if result.get("ok") else "install_failed",
        "message": "请在 ego-lite 完成首次引导；随后重新检查环境。",
    }


def execute(args, cancel_event=None):
    if not load_config()["ego_lite"]["enabled"]:
        raise ValueError("ego-lite 已停用")
    action = args["action"]
    if action == "status":
        return status()
    if action == "install":
        return {"ok": True, **start_install()}
    env = environment()
    if not env["supported"]:
        return {"ok": False, "environment": env, "error": env["reason"]}
    executable = find_executable()
    if not executable:
        if load_config()["ego_lite"]["auto_install"]:
            return {"ok": True, **start_install()}
        return {
            "ok": False,
            "error": "ego-browser 未安装，请在设置中安装 ego-lite 并完成首次引导",
        }
    if action == "guide":
        result = run_bounded(
            _command(executable, ["skill"]),
            timeout=35,
            max_bytes=900000,
            cancel_event=cancel_event,
        )
        if not result.get("ok"):
            return result
        guide = result["output"]
        complete = "[ego-browser:skill] end of guide" in guide and not result.get(
            "truncated"
        )
        if not complete:
            return {
                "ok": False,
                "error": "ego-browser 指南不完整或版本过旧；请更新应用。未执行浏览器脚本。",
                "output": guide[:8000],
            }
        lines = guide.splitlines()
        start = max(1, int(args.get("start_line", 1)))
        count = max(1, min(220, int(args.get("line_count", 200))))
        end = min(len(lines), start - 1 + count)
        while end > start and len("\n".join(lines[start - 1 : end])) > 8500:
            end -= 1
        return {
            "ok": True,
            "guide": "\n".join(lines[start - 1 : end]),
            "next_line": end + 1 if end < len(lines) else None,
            "total_lines": len(lines),
            "complete_guide_verified": True,
            "instruction": "按 next_line 继续读取到末尾再执行 run。只使用本机版本指南中的API；不要套用网站旧示例。",
        }
    if action != "run":
        raise ValueError("未知 ego-lite action")
    script = args.get("script", "")
    if not script.strip() or len(script) > 50000:
        raise ValueError("script 不能为空且不能超过50000字符")
    result = run_bounded(
        _command(executable, ["nodejs"]),
        input_text=script,
        timeout=40,
        max_bytes=30000,
        cancel_event=cancel_event,
    )
    return {**result, "untrusted_external_content": True}
