import json
import os
import re
import socket
import time
import shutil
import subprocess
import threading
import urllib.request
from core.network import urlopen
import zipfile
from pathlib import Path
from typing import Optional
from core.config import load_config, save_config
from core.process import terminate_tree, WindowsJob

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
NAPCAT_DIR = TOOLS_DIR / "napcat"
NAPCAT_ZIP_URL = (
    "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"
)
NAPCAT_MIRROR_URL = "https://ghproxy.net/https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"


class NapCatManager:
    """
    NapCat QQ 本地进程集成与生命周期管理器
    负责:
      1. 检测并自动下载/解压 NapCat.Shell 绿色包到 tools/napcat/
      2. 预先配置 OneBot 11 (开启本地 WebSocket 端口 3001)
      3. 作为子进程拉起 NapCat，实时捕获标准输出并打印终端扫码二维码
      4. 进程生命周期守护，主程序退出时优雅关闭
    """

    def __init__(self, napcat_dir: Path = NAPCAT_DIR, ws_port: int = 3001):
        self.napcat_dir = napcat_dir
        self.ws_port = ws_port
        self.process: Optional[subprocess.Popen] = None
        self._output_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._job = None
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self.core_loaded = threading.Event()

    def _detect_last_account(self) -> str:
        """从历史配置文件中自动探测上一次登录的 QQ 号，用于快速免扫码登录"""
        cfg_dir = self.napcat_dir / "config"
        if not cfg_dir.exists():
            return ""
        # 1. 读取 webui.json 中的 autoLoginAccount
        webui_cfg = cfg_dir / "webui.json"
        if webui_cfg.is_file():
            try:
                data = json.loads(webui_cfg.read_text(encoding="utf-8"))
                acc = str(data.get("autoLoginAccount") or "").strip()
                if acc and acc.isdigit():
                    return acc
            except Exception:
                pass
        # 2. 扫描历史 onebot11_<uin>.json 或 napcat_<uin>.json
        candidates = []
        for cp in cfg_dir.glob("onebot11_*.json"):
            m = re.search(r"onebot11_(\d+)\.json", cp.name)
            if m:
                candidates.append((cp.stat().st_mtime, m.group(1)))
        for cp in cfg_dir.glob("napcat_*.json"):
            m = re.search(r"napcat_(\d+)\.json", cp.name)
            if m:
                candidates.append((cp.stat().st_mtime, m.group(1)))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        return ""

    def is_installed(self) -> bool:
        return all(
            (self.napcat_dir / name).is_file()
            for name in (
                "NapCatWinBootMain.exe",
                "NapCatWinBootHook.dll",
                "napcat.mjs",
                "qqnt.json",
            )
        )

    def prepare_napcat(self, use_mirror: bool = True) -> bool:
        """确保 NapCat.Shell 已经下载并解压"""
        if self.is_installed():
            return True

        self.napcat_dir.mkdir(parents=True, exist_ok=True)
        zip_path = TOOLS_DIR / "NapCat.Shell.zip"
        download_url = NAPCAT_MIRROR_URL if use_mirror else NAPCAT_ZIP_URL

        print("[NapCat Manager] 正在从镜像源下载 NapCat.Shell ...")
        print(f"下载地址: {download_url}")

        try:
            # 下载 zip
            req = urllib.request.Request(
                download_url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with (
                urlopen(req, timeout=60) as resp,
                open(zip_path, "wb") as out_f,
            ):
                shutil.copyfileobj(resp, out_f)

            print("[NapCat Manager] 下载完成，正在解压...")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(self.napcat_dir)

            if zip_path.exists():
                zip_path.unlink()

            print(f"[NapCat Manager] NapCat 已就绪于: {self.napcat_dir}")
            self._ensure_config()
            return True

        except Exception as e:
            print(f"[NapCat Manager] 自动下载失败: {e}")
            print(f"你可以手动下载 NapCat.Shell.zip 并解压至: {self.napcat_dir}")
            return False

    def _ensure_config(self, qq_number=""):
        """Merge our loopback WS server into NapCat's documented network configuration."""
        from urllib.parse import urlparse
        import secrets

        cfg = load_config()["channels"]["napcat"]
        parsed = urlparse(cfg["ws_url"])
        if parsed.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("内嵌NapCat只允许本机WS地址，远程实例请关闭auto_launch")
        token = cfg.get("access_token")
        if not token:
            token = secrets.token_urlsafe(24)
            save_config({"channels": {"napcat": {"access_token": token}}})
        cfg_dir = self.napcat_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        if not qq_number:
            qq_number = self._detect_last_account()
        if qq_number:
            webui_cfg_path = cfg_dir / "webui.json"
            if webui_cfg_path.is_file():
                try:
                    wdata = json.loads(webui_cfg_path.read_text(encoding="utf-8"))
                    if wdata.get("autoLoginAccount") != qq_number:
                        wdata["autoLoginAccount"] = qq_number
                        webui_cfg_path.write_text(json.dumps(wdata, ensure_ascii=False, indent=4), encoding="utf-8")
                except Exception:
                    pass
        paths = [cfg_dir / "onebot11.json"]
        if qq_number:
            if not qq_number.isdigit():
                raise ValueError("QQ号必须是数字")
            paths.append(cfg_dir / f"onebot11_{qq_number}.json")
        for path in paths:
            obj = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            network = obj.setdefault("network", {})
            servers = network.setdefault("websocketServers", [])
            owned = next(
                (item for item in servers if item.get("name") == "MiniBot"), None
            )
            if owned is None:
                owned = next(
                    (
                        item
                        for item in servers
                        if item.get("port") == (parsed.port or 3001)
                        and item.get("host", "0.0.0.0")
                        in ("127.0.0.1", "localhost", "0.0.0.0", "")
                    ),
                    None,
                )
            if owned is None:
                owned = {}
                servers.append(owned)
            owned.update(
                {
                    "name": "MiniBot",
                    "enable": True,
                    "host": "127.0.0.1",
                    "port": parsed.port or 3001,
                    "token": token,
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "heartInterval": 30000,
                    "debug": False,
                }
            )
            path.write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def start(self, qq_number: str = "") -> bool:
        """启动 NapCat 子进程，并在终端打印日志与二维码"""
        if not self.is_installed():
            if not self.prepare_napcat():
                return False

        self._ensure_config(qq_number)

        # Launch the official native bootstrap directly: no detached UAC wrapper,
        # owned process tree, and its documented single positional quick-login id.
        try:
            cmd, env = self._build_command(qq_number)
        except (OSError, ValueError) as exc:
            print(f"[NapCat Manager] 启动配置错误: {exc}")
            return False

        print("\n" + "=" * 55)
        print(" [NapCat QQ] 正在启动内嵌 NTQQ 运行环境...")
        print(" [NapCat QQ] 请留意下方控制台输出的 登录二维码，使用手机 QQ 扫码即可！")
        print("=" * 55 + "\n")

        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.napcat_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=self._creation_flags(),
            )
            self._job = WindowsJob(self.process)
            self._is_running = True
            self._stop_event.clear()
            self.core_loaded.clear()
            self._output_thread = threading.Thread(
                target=self._stream_output, daemon=True
            )
            self._output_thread.start()
            self._monitor_thread = threading.Thread(
                target=self._monitor_readiness, daemon=True, name="napcat-readiness"
            )
            self._monitor_thread.start()
            return True
        except Exception as e:
            print(f"[NapCat Manager] 启动进程失败: {e}")
            return False

    @staticmethod
    def _creation_flags():
        if os.name != "nt":
            return 0
        import ctypes

        # Keep an existing console for native bootstrap logging, without creating a new one.
        has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
        return subprocess.CREATE_NEW_PROCESS_GROUP | (
            0 if has_console else subprocess.CREATE_NO_WINDOW
        )

    def _resolve_qq_executable(self):
        configured = load_config()["channels"]["napcat"].get("qq_path", "").strip()
        if configured:
            path = Path(configured)
            if not path.is_file():
                raise ValueError("qq_path 指定的 QQ.exe 不存在")
            return path.resolve()
        if os.name == "nt":
            import winreg

            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        value = winreg.QueryValueEx(key, "UninstallString")[0]
                    match = re.match(
                        r'^\s*"([^"]+)"|^\s*(.*?\.exe)(?:\s|$)',
                        os.path.expandvars(value),
                        re.I,
                    )
                    if match:
                        path = Path(match.group(1) or match.group(2)).parent / "QQ.exe"
                        if path.is_file():
                            return path.resolve()
                except OSError:
                    continue
        raise ValueError("未找到QQ安装路径，请在 channels.napcat.qq_path 指定 QQ.exe")

    def _build_command(self, qq_number=""):
        qq_number = qq_number.strip()
        if not qq_number:
            qq_number = self._detect_last_account()
        if qq_number and not qq_number.isdigit():
            raise ValueError("QQ号必须是数字")
        root = self.napcat_dir.resolve()
        boot = root / "NapCatWinBootMain.exe"
        hook = root / "NapCatWinBootHook.dll"
        for path in (boot, hook, root / "napcat.mjs", root / "qqnt.json"):
            if not path.is_file():
                raise ValueError("NapCat安装不完整，缺少 " + path.name)
        loader = root / "loadNapCat.js"
        if not loader.exists():
            loader.write_text(
                "(async()=>{await import("
                + json.dumps((root / "napcat.mjs").as_uri())
                + ");})();\n",
                encoding="utf-8",
            )
        env = os.environ.copy()
        env.update(
            {
                "NAPCAT_PATCH_PACKAGE": str(root / "qqnt.json"),
                "NAPCAT_LOAD_PATH": str(loader),
                "NAPCAT_INJECT_PATH": str(hook),
                "NAPCAT_LAUNCHER_PATH": str(boot),
                "NAPCAT_MAIN_PATH": str(root / "napcat.mjs"),
            }
        )
        if qq_number:
            env["ACCOUNT"] = qq_number
            env["NAPCAT_QUICK_ACCOUNT"] = qq_number
        return [str(boot), str(self._resolve_qq_executable()), str(hook)] + (
            [qq_number] if qq_number else []
        ), env

    @staticmethod
    def _listening(host, port):
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

    def readiness(self):
        from urllib.parse import urlparse

        parsed = urlparse(load_config()["channels"]["napcat"]["ws_url"])
        local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        return {
            "bootstrap_running": bool(self.process and self.process.poll() is None),
            "core_log_seen": self.core_loaded.is_set(),
            "onebot_listening": self._listening(parsed.hostname, parsed.port or 3001),
            "webui_listening": self._listening("127.0.0.1", 6099) if local else False,
        }

    def _monitor_readiness(self):
        started = time.monotonic()
        last_report = 0
        while self._is_running and not self._stop_event.is_set():
            status = self.readiness()
            if status["onebot_listening"]:
                print("[NapCat Manager] OneBot端口已开始监听，等待WebSocket鉴权完成。")
                return
            now = time.monotonic()
            if now - started >= 30 and now - last_report >= 60:
                if not status["bootstrap_running"]:
                    message = "NapCat启动进程已经退出；请检查前面的注入/依赖错误。"
                elif not status["webui_listening"] and not status["core_log_seen"]:
                    message = "QQ已启动，但NapCat核心尚未就绪，6099和OneBot均未监听；普通QQ扫码不代表NapCat已加载。请检查控制台是否出现 NapCat Shell App Loading，及QQ/注入器兼容性。"
                else:
                    message = "NapCat核心已启动，OneBot尚未监听；请完成NapCat登录并检查账户对应的WS配置。"
                print("[NapCat Manager]", message)
                last_report = now
            if self._stop_event.wait(2):
                return

    def _stream_output(self):
        """流式读取并显示控制台输出"""
        if not self.process or not self.process.stdout:
            return

        for line in iter(self.process.stdout.readline, ""):
            if not self._is_running:
                break
            clean_line = line.rstrip()
            if clean_line:
                if "NapCat Shell App Loading" in clean_line or "[WebUi]" in clean_line:
                    self.core_loaded.set()
                # 打印日志到终端（包括字符二维码）
                print(f"[NapCat] {clean_line}")

    def stop(self):
        self._is_running = False
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(3)
        if self._job:
            self._job.close()
            self._job = None
        if self.process:
            try:
                terminate_tree(self.process)
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        if self._output_thread:
            self._output_thread.join(2)
        if self.process:
            if self.process.stdout:
                self.process.stdout.close()
            self.process = None
