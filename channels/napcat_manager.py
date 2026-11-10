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
from urllib.parse import urlparse
from core.config import load_config, save_config
from core.process import terminate_pid, pid_alive

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
NAPCAT_DIR = TOOLS_DIR / "napcat"
NAPCAT_ZIP_URL = (
    "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"
)
NAPCAT_MIRROR_URL = "https://ghproxy.net/https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"

# 记录“谁拉起了 NapCat”，让重启后的新进程能接管同一个 QQ，而不是再开一个。
OWNER_STATE_NAME = ".minibot_owner.json"


def port_open(host, port, timeout=0.25):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def onebot_listening(ws_url=""):
    """OneBot WS 端口是否已在监听（上一次启动留下的、或外部实例都算）。"""
    parsed = urlparse(ws_url or load_config()["channels"]["napcat"]["ws_url"])
    return port_open(parsed.hostname or "127.0.0.1", parsed.port or 3001)


class NapCatManager:
    """
    NapCat QQ 本地进程集成与生命周期管理器
    负责:
      1. 检测并自动下载/解压 NapCat.Shell 绿色包到 tools/napcat/
      2. 预先配置 OneBot 11 (开启本地 WebSocket 端口 3001)
      3. 作为子进程拉起 NapCat，输出写入日志文件并转发到终端（含扫码二维码）
      4. 进程生命周期守护：正常退出时清理，重启时交接给下一个实例

    注意：这里**不**把 NapCat 放进 Windows Job。Job 的 KILL_ON_JOB_CLOSE 会让
    MiniBot 进程一退出就把 QQ 一起带走；而 /restart 只是重启本体，QQ 与 OneBot
    必须活下来，否则每重启一次都要重新扫码。
    """

    def __init__(self, napcat_dir: Path = NAPCAT_DIR, ws_port: int = 3001):
        self.napcat_dir = napcat_dir
        self.ws_port = ws_port
        self.process: Optional[subprocess.Popen] = None
        self._output_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._owned_pid = 0
        self._stop_event = threading.Event()
        self._monitor_thread = None
        self._log_file = None
        self.core_loaded = threading.Event()

    # ------------------------------------------------------------------
    # 进程归属（跨重启接管）
    # ------------------------------------------------------------------
    @property
    def _owner_state(self) -> Path:
        return self.napcat_dir / OWNER_STATE_NAME

    @property
    def log_path(self) -> Path:
        return self.napcat_dir / "logs" / "napcat-stdout.log"

    def _remember_owner(self, pid):
        self._owned_pid = int(pid or 0)
        try:
            self._owner_state.write_text(
                json.dumps({"pid": self._owned_pid, "at": time.time()}),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _forget_owner(self):
        self._owned_pid = 0
        try:
            self._owner_state.unlink()
        except OSError:
            pass

    def _previous_owner(self) -> int:
        try:
            data = json.loads(self._owner_state.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return 0
        return pid if pid_alive(pid) else 0

    def adopt(self) -> bool:
        """接管上一次启动、仍在运行的 NapCat（/restart 走这条路，绝不重启 QQ）。"""
        pid = self._previous_owner()
        if not pid:
            self._forget_owner()
            return False
        self._owned_pid = pid
        print(
            f"[NapCat Manager] 复用已在运行的 NapCat（PID {pid}），本次重启不影响 QQ / OneBot。"
        )
        return True

    def detach(self):
        """重启时放手：不动进程，把所有权留给下一个实例。"""
        self._is_running = False
        self._stop_event.set()
        self._close_log()
        self.process = None

    def _kill_stale(self):
        """上一次的 NapCat 还在但不监听端口（半登录卡死），先清掉再重开。

        典型现场：QQ 进程活着、WebUI 6099 在监听、3001 一直不监听、终端也不出二维码。
        这种半死状态只能整棵清掉，否则新 QQ 会被单实例逻辑顶回去，永远起不来。
        """
        self._forget_owner()
        pids = self._boot_pids()
        if not pids:
            return
        print(f"[NapCat Manager] 清理卡住的 NapCat 进程 {pids} 后重新启动。")
        for pid in pids:
            terminate_pid(pid)
        time.sleep(2)

    @staticmethod
    def _boot_pids():
        """列出本机残留的 NapCat 启动器 PID（Windows 用 tasklist，其它系统跳过）。"""
        if os.name != "nt":
            return []
        try:
            # tasklist 在中文 Windows 上按 GBK 输出（无匹配时还会打印中文提示），
            # 直接 text=True 会按 UTF-8 解码失败并让 stdout 变成 None，进而把整个
            # 主进程带崩。这里显式指定 errors="replace"，解不出来的字节变成占位符，
            # 真正的进程行（名称/PID 都是 ASCII）依然能正常解析。
            out = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq NapCatWinBootMain.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        pids = []
        for line in (out or "").splitlines():
            cells = [cell.strip('"') for cell in line.split('","')]
            if (
                len(cells) >= 2
                and cells[0].lower().startswith("napcatwinbootmain")
                and cells[1].isdigit()
            ):
                pids.append(int(cells[1]))
        return pids

    # ------------------------------------------------------------------
    # 安装 / 配置
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def start(self, qq_number: str = "") -> bool:
        """启动 NapCat 子进程，把日志与二维码写到终端"""
        if not self.is_installed():
            if not self.prepare_napcat():
                return False

        self._ensure_config(qq_number)
        self._kill_stale()

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
            # 输出写文件而不是管道：进程退出（重启）后 QQ 继续写自己的文件句柄，
            # 不会因为管道断开而 EPIPE 崩溃；日志同时保留下来便于排查。
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "w", encoding="utf-8", errors="replace")
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.napcat_dir),
                env=env,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
            self._remember_owner(self.process.pid)
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
            self._close_log()
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
        """构造 NapCat 启动命令。

        刻意**不**把 QQ 号作为位置参数传给 NapCatWinBootMain（它会拼成 QQ 的 `-q`）。
        实测（NapCat 4.18.41 + QQ 9.9.36）：带 `-q` 时 QQ 会卡在快速登录中间态——
        既不弹登录窗口，也不发二维码事件，OneBot 永远不监听，终端一片空白；
        不带 `-q` 时 NapCat 自己会走「检测可用账号 → 自动快速登录 → 失败则扫码」，
        两条路都能出结果（日志：`没有 -q 指令指定快速登录，将使用二维码登录方式`）。
        """
        qq_number = (qq_number or "").strip()
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
                # 强制单进程模式。NapCat 默认会把核心逻辑丢给 worker 子进程，
                # 在这台机器（NapCat 4.18.41 + QQ 9.9.36）上 worker 起不来，
                # 表现就是：日志停在 WebUi 那两行、onLoginConnected 永不触发、
                # 不出二维码、OneBot 3001 永不监听，而 QQ 主进程还活着。
                # 关掉多进程后 QQ 登录与 OneBot 都正常。
                "NAPCAT_DISABLE_MULTI_PROCESS": "1",
            }
        )
        return [str(boot), str(self._resolve_qq_executable()), str(hook)], env

    @staticmethod
    def _listening(host, port):
        return port_open(host, port)

    def readiness(self):
        parsed = urlparse(load_config()["channels"]["napcat"]["ws_url"])
        local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        webui_ok = self._listening("127.0.0.1", 6099) if local else False
        return {
            "bootstrap_running": bool(self.process and self.process.poll() is None),
            "core_log_seen": self.core_loaded.is_set() or webui_ok,
            "onebot_listening": self._listening(
                parsed.hostname or "127.0.0.1", parsed.port or 3001
            ),
            "webui_listening": webui_ok,
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
                    message = (
                        "QQ已启动，但NapCat核心尚未就绪，6099和OneBot均未监听；"
                        "普通QQ扫码不代表NapCat已加载。请检查控制台是否出现 NapCat Shell App Loading。"
                    )
                else:
                    message = (
                        f"NapCat核心已启动，OneBot尚未监听；多半是QQ还没登录。"
                        f"请扫下方二维码（也可看 {self.log_path}），或打开 "
                        f"http://127.0.0.1:6099/webui 完成登录。"
                    )
                print("[NapCat Manager]", message)
                last_report = now
            if self._stop_event.wait(2):
                return

    def _emit_line(self, text):
        clean = text.rstrip()
        if not clean:
            return
        if "NapCat Shell App Loading" in clean or "[WebUi]" in clean:
            self.core_loaded.set()
        # 打印日志到终端（包括字符二维码）
        print(f"[NapCat] {clean}", flush=True)

    def _stream_output(self):
        """跟随 NapCat 日志文件，把控制台输出（含二维码）转发到终端。"""
        try:
            stream = open(self.log_path, "rb")
        except OSError:
            return
        pending = b""
        last_growth = time.monotonic()
        with stream:
            while self._is_running:
                chunk = stream.read()
                if chunk:
                    pending += chunk
                    last_growth = time.monotonic()
                lines = pending.split(b"\n")
                pending = lines.pop()
                for raw in lines:
                    self._emit_line(raw.decode("utf-8", errors="replace"))
                # 二维码可能是不带换行地一次性写出：等不到换行就一直不显示，
                # 所以静默一小会儿就把残留内容当成完整一块输出。
                if pending and time.monotonic() - last_growth > 0.4:
                    self._emit_line(pending.decode("utf-8", errors="replace"))
                    pending = b""
                    last_growth = time.monotonic()
                if not chunk:
                    if self.process is not None and self.process.poll() is not None:
                        if pending:
                            self._emit_line(pending.decode("utf-8", errors="replace"))
                        return
                    time.sleep(0.15)

    def _close_log(self):
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def stop(self):
        """正常退出：连 QQ 一起收掉，不留后台驻留进程。"""
        self._is_running = False
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(3)
        if self.process:
            try:
                terminate_pid(self.process.pid)
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        elif self._owned_pid:
            # 接管来的实例（重启后）没有 Popen 句柄，直接按 PID 清理进程树。
            terminate_pid(self._owned_pid)
        if self._output_thread:
            self._output_thread.join(2)
        self.process = None
        self._close_log()
        self._forget_owner()
