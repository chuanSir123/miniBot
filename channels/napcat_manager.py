import atexit
import hashlib
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

# 标记“本地快速登录票据已判定失效”。清票之后要是还没扫码登回来，重启时
# 就不能把 autoLoginAccount 写回去 —— 否则快速登录再撞一次、又崩一次，
# 在“清票/恢复”之间来回抖。扫码登成功后这个标记会被删掉。
TICKET_INVALID_MARKER = ".minibot_ticket_invalid"

# 启动后多久还等不到 OneBot 监听，就判定成“QQ 卡在初始化”，清掉整棵树自愈重拉一次。
HEAL_AFTER_SEC = 75

# 启动阶段自动重拉的次数上限（含“快速登录失效→改扫码”那一次）。
MAX_AUTO_RESTARTS = 3

# NapCat 打出这行就说明它走的是“缓存票据快速登录”，而不是扫码。
QUICK_LOGIN_MARKER = "自动快速登录成功"

# NapCat 启动时会打印它拉起的 QQ 主进程 PID，抓下来用于“启动器先死”的收尸场景。
MAIN_PID_PATTERN = re.compile(r"Main Process ID:\s*(\d+)")

# 登录态守护。QQ 被踢下线（或票据过期、被别处顶号）之后，NapCat 自己**不会**再
# 把二维码打到终端：实测 4.18.41 + QQ 9.9.36 只留一行
# `[KickedOffLine] ... 登录服务重启失败：重启功能未初始化` —— 多进程被禁掉后它
# 没有 worker 可重启，登录服务就停在 offline，OneBot 端口照旧监听、消息照旧发不出去。
# 这时唯一能自救的动作是向 WebUI 要一张新二维码（NapCat 收到请求会重新打印方块码
# 并刷 cache/qrcode.png），所以这里常驻一个线程盯 isLogin。
LOGIN_POLL_SEC = 20
# 启动阶段的自愈归 _monitor_readiness，这边最多等这么久就接管。
LOGIN_WATCH_GRACE_SEC = 90
# 两次“要二维码”之间的最小间隔，避免登录服务没恢复时被刷屏式请求打爆。
QR_MIN_INTERVAL_SEC = 20
# 同一张二维码的播报间隔下限（换码频繁时别把控制台刷掉）。
QR_ANNOUNCE_MIN_GAP_SEC = 45
# 要不到二维码时整棵树重拉的次数上限，超过就只提醒、不再重启。
MAX_LOGIN_RELAUNCH = 3
# 要到二维码之后给它多久渲染，超过还没出码才认定必须重启进程树。
QR_SETTLE_SEC = 30
# “快速登录成功、QQ 随即退出”的判定窗口。超过这个时间再退出属于运行期崩溃，
# 不能算到“票据失效”头上 —— 否则挂几小时后崩一次就被永久改成扫码。
QUICK_LOGIN_CRASH_WINDOW_SEC = 120
# 接管/启动后连着多少轮读不到登录状态（WebUI 未监听或鉴权失败）就认为这是
# 一个“活着但没起来”的僵尸实例，清理重拉一次。轮数 × LOGIN_POLL_SEC。
BLIND_RELAUNCH_AFTER = 9
# 重拉之后先给它这么久去启动 + 快速登录，再判断有没有救回来。
RELAUNCH_SETTLE_SEC = 15

# NapCat 日志里这些字样等价于“登录态没了”，用它提前叫醒守护线程。
OFFLINE_MARKERS = (
    "账号状态变更为离线",
    "[KickedOffLine]",
    "你的账号当前登录已失效",
    "身份验证失败，请你重新登录",
    "账号已离线",
)


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
        # QQ 主进程 PID（从 NapCat 日志的 "Main Process ID" 里抓），
        # 启动器先死、QQ 变成孤儿时只能靠它收尸。
        self._owned_qq_pid = 0
        self._qq_number = ""
        self._stop_event = threading.Event()
        # 每次拉起的日志跟随线程各拿一个开关：自愈重拉时要让上一轮的线程退场。
        self._stream_stop = threading.Event()
        self._monitor_thread = None
        self._log_file = None
        self.core_loaded = threading.Event()
        # 退出时是否放过 NapCat/QQ：/restart 交接给下一个实例时必须为 True。
        self._keep_on_exit = False
        self._exit_hook = None
        self._atexit_registered = False
        # 启动阶段已经自动重拉过几次（快速登录失效、QQ 卡死都算）。
        self._restarts = 0
        # 本次运行放弃快速登录、强制扫码：快速登录票据一旦失效，QQ 会在登录后
        # 立刻退出，只能靠扫码拿一张新票据。
        self._force_qr = False
        # 是否由我们自己把 webui.json 的 autoLoginAccount 清空过：扫码登录成功后
        # 要写回去，否则一次票据失效会变成“永久扫码”。
        self._autologin_cleared = False
        # 最近一次“自动快速登录成功”的时间点，用来判断“登录后马上退出”是不是它导致的。
        self._quick_login_at = 0.0
        # 连续几次“快速登录成功但 QQ 随即退出”。第一次先原样重试，第二次才改扫码：
        # 实测这种崩溃是偶发的（同一份配置有时 16 秒就登录成功），别一次失败就赶去扫码。
        self._quick_crashes = 0
        # 登录态守护线程（掉线后把二维码重新请出来），见 _watch_login。
        self._login_thread: Optional[threading.Thread] = None
        self._login_stop = threading.Event()
        # 日志里读到掉线字样时置位，让守护线程立刻醒过来，不用等下一次轮询。
        self._offline_event = threading.Event()
        # 上一次主动向 WebUI 要二维码的时间（monotonic），用于限流。
        self._last_qr_at = 0.0

    # ------------------------------------------------------------------
    # 进程归属（跨重启接管）
    # ------------------------------------------------------------------
    @property
    def _owner_state(self) -> Path:
        return self.napcat_dir / OWNER_STATE_NAME

    @property
    def log_path(self) -> Path:
        return self.napcat_dir / "logs" / "napcat-stdout.log"

    def _remember_owner(self, pid, qq_pid=0):
        self._owned_pid = int(pid or 0)
        if qq_pid:
            self._owned_qq_pid = int(qq_pid)
        self._write_owner_state()

    def _write_owner_state(self):
        try:
            self._owner_state.write_text(
                json.dumps(
                    {
                        "pid": self._owned_pid,
                        "qq_pid": self._owned_qq_pid,
                        "at": time.time(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _read_owner_state(self):
        try:
            data = json.loads(self._owner_state.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _as_pid(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def _ticket_marker(self) -> Path:
        return self.napcat_dir / TICKET_INVALID_MARKER

    def _ticket_invalid(self) -> bool:
        """上次清票之后是否还没扫码登回来。"""
        return self._ticket_marker.exists()

    def _mark_ticket_invalid(self, invalid: bool):
        try:
            if invalid:
                self._ticket_marker.write_text("1", encoding="utf-8")
            elif self._ticket_marker.exists():
                self._ticket_marker.unlink()
        except OSError:
            pass

    def _forget_owner(self):
        self._owned_pid = 0
        self._owned_qq_pid = 0
        try:
            self._owner_state.unlink()
        except OSError:
            pass

    def _previous_owner(self) -> int:
        pid = self._as_pid(self._read_owner_state().get("pid"))
        return pid if pid_alive(pid) else 0

    def _previous_qq_pid(self) -> int:
        """上一次留下的 QQ 主进程：启动器先死时它会变成孤儿，只能靠这条记录收掉。"""
        pid = self._as_pid(self._read_owner_state().get("qq_pid"))
        return pid if pid_alive(pid) else 0

    def adopt(self) -> bool:
        """接管上一次启动、仍在运行的 NapCat（/restart 走这条路，绝不重启 QQ）。"""
        pid = self._previous_owner()
        if not pid:
            # 启动器没了但 QQ 还在（孤儿）：别把记录一起删掉，留着 PID 交给
            # _kill_stale 收尸，否则新拉起的 QQ 会被单实例逻辑顶回去。
            if not self._previous_qq_pid():
                self._forget_owner()
            return False
        self._owned_pid = pid
        # 一并接管 QQ 主进程的 PID，否则退出时只认得启动器、收不掉 QQ。
        self._owned_qq_pid = self._previous_qq_pid()
        # 接管来的实例同样算“在运行”，后面任何一处看 _is_running 的守卫都该放行。
        self._is_running = True
        self._keep_on_exit = False
        # 接管来的实例同样需要退出兜底：关控制台时也不能把 QQ 留成孤儿。
        self._install_exit_hooks()
        # 也修一次配置：上一个进程可能是“清票扫码”之后被关掉的，不修的话
        # autoLoginAccount 一直空着，重启后还得扫码 —— 而关掉重开却不用，
        # 这正是“重启又要扫码、关掉重开就免扫码”这种不一致的来源。
        try:
            self._ensure_config()
        except (OSError, ValueError) as exc:
            print(f"[NapCat Manager] 接管时修正配置失败（不影响本次运行）：{exc}")
        # 接管的同时把日志跟随与登录守护接上：/restart 之前 NapCat 是上一个实例拉起的，
        # 不跟日志的话它后面打出的二维码、掉线提示全都进不了当前控制台，等于瞎了。
        self._follow_log(from_end=True)
        self._start_login_watch()
        print(
            f"[NapCat Manager] 复用已在运行的 NapCat（PID {pid}），本次重启不影响 QQ / OneBot。"
        )
        return True

    def detach(self):
        """重启时放手：不动进程，把所有权留给下一个实例。"""
        self._is_running = False
        self._stop_event.set()
        self._stream_stop.set()
        self._stop_login_watch()
        self._keep_on_exit = True
        self._close_log()
        self.process = None

    def _live_pids(self):
        """所有“我们知道还活着”的 NapCat 相关 PID。

        来源三处：状态文件里记录的启动器与 QQ 主进程（跨进程重启有效）、
        本实例记住的 PID、以及扫描出来的残留启动器。
        """
        state = self._read_owner_state()
        candidates = [
            self._as_pid(state.get("pid")),
            self._as_pid(state.get("qq_pid")),
            self._owned_pid,
            self._owned_qq_pid,
        ] + self._boot_pids()
        pids = []
        for pid in candidates:
            if pid and pid not in pids and pid_alive(pid):
                pids.append(pid)
        return pids

    @staticmethod
    def _wait_gone(pids, timeout=6.0):
        """等进程真正退出：taskkill 是异步的，立刻重拉 QQ 会撞上还没释放的文件与管道。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(pid_alive(pid) for pid in pids):
                return True
            time.sleep(0.2)
        return False

    def _kill_stale(self):
        """上一次的 NapCat 还在但不监听端口（半登录卡死），先清掉再重开。

        典型现场：QQ 进程活着、WebUI 6099 在监听、3001 一直不监听、终端也不出二维码。
        这种半死状态只能整棵清掉才能重来。除了启动器，还要收掉它拉起的 QQ 主进程：
        启动器先死时 QQ 会变成孤儿，留在后台就是一个“活着但登录服务没起来”的僵尸。
        """
        pids = self._live_pids()
        self._forget_owner()
        if not pids:
            return
        print(f"[NapCat Manager] 清理卡住的 NapCat 进程 {pids} 后重新启动。")
        for pid in pids:
            terminate_pid(pid)
        if not self._wait_gone(pids):
            print(f"[NapCat Manager] 进程 {pids} 未在限期内退出，继续尝试启动。")

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
        if qq_number and not self._force_qr and not self._ticket_invalid():
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
        self._install_exit_hooks()
        self._qq_number = (qq_number or "").strip()
        self._restarts = 0
        self._force_qr = False
        # start() 前面的 _ensure_config 已经把 autoLoginAccount 补回来了。
        self._autologin_cleared = False
        self._quick_login_at = 0.0
        self._quick_crashes = 0
        return self._launch()

    def _launch(self) -> bool:
        """真正拉起子进程：首次启动与自愈重拉共用同一套逻辑。"""
        try:
            cmd, env = self._build_command(self._qq_number)
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
            self._archive_log()
            self._log_file = open(self.log_path, "w", encoding="utf-8", errors="replace")
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.napcat_dir),
                env=env,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
            self._owned_qq_pid = 0
            self._quick_login_at = 0.0
            self._remember_owner(self.process.pid)
            self._is_running = True
            self._stop_event.clear()
            self.core_loaded.clear()
            self._follow_log()
            self._monitor_thread = threading.Thread(
                target=self._monitor_readiness, daemon=True, name="napcat-readiness"
            )
            self._monitor_thread.start()
            self._start_login_watch()
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

    # ------------------------------------------------------------------
    # WebUI 探针：区分“正在等扫码”和“QQ 卡死”
    # ------------------------------------------------------------------
    @staticmethod
    def _webui_call(port, path, payload, credential=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if credential:
            request.add_header("Authorization", "Bearer " + credential)
        # 回环地址必须绕开系统代理，否则请求会跑到代理进程上去。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def _webui_credential(self):
        """登录一次 WebUI，返回 (端口, Credential)。凭据会过期，所以每次都重新登录。"""
        cfg = json.loads(
            (self.napcat_dir / "config" / "webui.json").read_text(encoding="utf-8")
        )
        port = int(cfg.get("port") or 6099)
        token = str(cfg.get("token") or "")
        if not token:
            return port, ""
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        auth = self._webui_call(port, "/api/auth/login", {"hash": digest})
        return port, str((auth.get("data") or {}).get("Credential") or "")

    def webui_call(self, path, payload=None):
        """带凭据调一次 WebUI 接口，返回完整响应体（含 code / data）。"""
        port, credential = self._webui_credential()
        if not credential:
            raise ConnectionError("未能登录 NapCat WebUI")
        return self._webui_call(port, path, payload or {}, credential)

    def login_state(self):
        """读 QQ 登录状态（isLogin / qrcodeurl / coreReady ...），读不到返回 None。"""
        try:
            state = self.webui_call("/api/QQLogin/CheckLoginStatus")
            data = state.get("data")
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def refresh_qrcode(self) -> str:
        """向 WebUI 要一张新二维码，返回扫码链接；失败返回空串。

        这是掉线后的**唯一**自救动作：NapCat 自己不会重开登录服务（日志里那句
        「重启功能未初始化」），但收到这个请求后会重新生成二维码、把方块码打到
        控制台并刷新 cache/qrcode.png。请求本身不会重启 QQ 进程。
        """
        try:
            reply = self.webui_call("/api/QQLogin/RefreshQRcode")
        except Exception as exc:
            print(
                f"[NapCat 登录] 重新获取二维码失败：{type(exc).__name__} {str(exc)[:120]}"
            )
            return ""
        data = reply.get("data") if isinstance(reply, dict) else None
        url = str((data or {}).get("qrcodeurl") or "").strip()
        if not url:
            message = str((reply or {}).get("message") or (reply or {}).get("code") or "")
            print(f"[NapCat 登录] 二维码请求没有得到链接：{message[:120]}")
        return url

    def _looks_hung(self, status):
        """QQ 进程活着、登录服务却一直不就绪且没有二维码 → 判定卡死，值得重来一次。

        读不到 WebUI 状态时返回 False：宁可让用户去扫码，也不要杀一个正在登录的 QQ。
        强制扫码模式下永远返回 False：这时用户要做的就是扫码，杀了只会换个新二维码。
        """
        if self._force_qr or not status["webui_listening"]:
            return False
        state = self.login_state()
        if state is None:
            return False
        if state.get("loginPhase") == "offline":
            # 掉线是登录态问题，不是初始化卡死：登录守护会去要新二维码，
            # 这里再杀一次进程只会把刚生成的二维码白扔一张。
            return False
        if state.get("isLogin") or state.get("qrcodeurl"):
            return False
        return not state.get("coreReady")

    @property
    def qrcode_path(self) -> Path:
        return self.napcat_dir / "cache" / "qrcode.png"

    def _status_message(self, status):
        if not status["bootstrap_running"]:
            return "NapCat启动进程已经退出；请检查前面的注入/依赖错误。"
        if not status["webui_listening"] and not status["core_log_seen"]:
            return (
                "QQ已启动，但NapCat核心尚未就绪，6099和OneBot均未监听；"
                "普通QQ扫码不代表NapCat已加载。请检查控制台是否出现 NapCat Shell App Loading。"
            )
        state = self.login_state() or {}
        if state.get("qrcodeurl"):
            return (
                f"二维码已生成：扫控制台里的二维码（同一张图也存到了 {self.qrcode_path}），"
                "或打开 http://127.0.0.1:6099/webui 登录。"
            )
        if self._force_qr:
            return (
                "正在等二维码（已按扫码模式启动，不再走快速登录）；"
                "若一分钟内没出现，请查看 " + str(self.log_path)
            )
        if state.get("loginPhase") == "offline":
            return (
                "QQ 已经掉线（登录态失效），NapCat 自己重开不了登录服务；"
                "登录守护会向 WebUI 重新申请二维码，请留意接下来打印的方块码。"
            )
        if not state.get("coreReady"):
            return (
                "NapCat核心已加载，但QQ登录服务还没起来（QQ多半卡在一次异常退出的初始化里）；"
                f"不会再干等，{HEAL_AFTER_SEC}秒后会先向 NapCat 重新申请二维码，"
                f"仍要不到才清理重拉一次。日志：{self.log_path}"
            )
        return (
            "NapCat核心已启动，OneBot尚未监听；多半是QQ还没登录。"
            f"请扫下方二维码（也可看 {self.log_path}），或打开 "
            "http://127.0.0.1:6099/webui 完成登录。"
        )

    def _monitor_readiness(self):
        started = time.monotonic()
        last_report = 0
        asked_qr = False
        qr_requested = 0.0
        while self._is_running and not self._stop_event.is_set():
            status = self.readiness()
            if status["onebot_listening"]:
                print("[NapCat Manager] OneBot端口已开始监听，等待WebSocket鉴权完成。")
                return
            now = time.monotonic()
            elapsed = now - started
            if not status["bootstrap_running"]:
                # 进程退出了：要么改扫码重拉，要么用完了重试次数。
                if not self._recover_after_exit():
                    print("[NapCat Manager]", self._status_message(status))
                return
            if (
                not self._force_qr
                and elapsed >= HEAL_AFTER_SEC
                and self._restarts < MAX_AUTO_RESTARTS
                and self._looks_hung(status)
            ):
                # 先试“向 WebUI 要一张二维码”：登录服务只是没恢复、没出码的情况
                # 很常见（票据失效后停在 waiting_qrcode），要一张码比重启整棵
                # 进程树快得多，也不会白丢一次登录态。要不到再动刀。
                fresh = ""
                if not asked_qr:
                    asked_qr = True
                    fresh = self.refresh_qrcode()
                if fresh:
                    qr_requested = time.monotonic()
                    self._announce_qrcode(self.login_state() or {}, fresh)
                    print("[NapCat Manager] 登录服务没出码，已直接重新申请一张，请扫码登录。")
                elif qr_requested and now - qr_requested < QR_SETTLE_SEC:
                    # 刚要到码，给 QQ 一点时间把它渲染出来，别急着砍。
                    pass
                else:
                    self._heal()
                    return
            if elapsed >= 30 and now - last_report >= 60:
                print("[NapCat Manager]", self._status_message(status))
                last_report = now
            if self._stop_event.wait(2):
                return

    # ------------------------------------------------------------------
    # 登录态守护：掉线后自动把二维码重新请出来
    # ------------------------------------------------------------------
    def _start_login_watch(self):
        """起常驻线程盯登录态；同一个实例只会有一个。"""
        if not load_config()["channels"]["napcat"].get("auto_relogin", True):
            return
        if self._login_thread is not None and self._login_thread.is_alive():
            return
        self._login_stop = threading.Event()
        self._login_thread = threading.Thread(
            target=self._watch_login, daemon=True, name="napcat-login-watch"
        )
        self._login_thread.start()

    def _stop_login_watch(self):
        """让守护线程退场（/restart 交接、正常退出都要叫停它）。"""
        self._login_stop.set()
        # 顺手叫醒它：否则要等满一个轮询间隔才退，进程关不干净。
        self._offline_event.set()

    @staticmethod
    def _offline_reason(state):
        """把 NapCat 的 loginError 压成一行，用于播报。"""
        lines = [line.strip() for line in str((state or {}).get("loginError") or "").splitlines()]
        first = next((line for line in lines if line), "")
        if first:
            return first
        phase = str((state or {}).get("loginPhase") or "")
        return {
            "offline": "账号已离线，登录态失效",
            "waiting_qrcode": "等待扫码登录",
            "reconnecting": "登录服务正在重连",
        }.get(phase) or (phase or "登录态失效")

    def _announce_qrcode(self, state, url):
        """把二维码链接、图片路径、WebUI 入口一次性讲清楚，让人知道去哪扫。"""
        bar = "=" * 64
        print("\n" + bar)
        print(f"[NapCat 登录] QQ 登录状态异常：{self._offline_reason(state)}")
        print("[NapCat 登录] 已生成登录二维码，用手机 QQ 扫控制台里的方块码即可；")
        print(f"[NapCat 登录] 扫码链接：{url}")
        print(f"[NapCat 登录] 二维码图片：{self.qrcode_path}")
        print("[NapCat 登录] 也可以在浏览器打开 http://127.0.0.1:6099/webui 扫页面上的码。")
        print(bar + "\n", flush=True)

    def _relaunch_for_login(self, force_qr=False):
        """要不到二维码时整棵树重拉，让登录服务重新初始化。

        NapCat 在多进程被禁掉的机器上没法自愈（日志里那句「重启功能未初始化」就是
        它自己），只能替它做一次干净重启。

        **第一次重拉必须保留 autoLoginAccount**：实测掉线后关掉再开就能免扫码快速
        登录，说明票据通常还活着，掉线只是网络/被顶号的瞬时状态；一上来就清票据
        等于把一次网络抖动升级成必须扫码，而且会把 autoLoginAccount 写空，之后每次
        重启都扫码。只有重拉过一次仍然登不上（真票据失效）才清票据改扫码。
        """
        if self._keep_on_exit:
            return
        if force_qr:
            print("[NapCat 守护] 干净重拉后仍登不上，判断缓存票据失效，清掉后改走扫码登录。")
            self._clear_autologin()
        else:
            print("[NapCat 守护] WebUI 也要不到新二维码，先干净重拉一次（保留快速登录）。")
        self._restarts = 0
        self._recycle()
        self._launch()

    def _watch_login(self):
        """盯登录态：掉线就重新要二维码，进程没了就重拉。

        启动阶段的自愈归 _monitor_readiness，这边先等 OneBot 起来（或等够
        LOGIN_WATCH_GRACE_SEC）再接管，避免两个线程同时重拉。
        """
        stop = self._login_stop
        parsed = urlparse(load_config()["channels"]["napcat"]["ws_url"])
        host, port = parsed.hostname or "127.0.0.1", parsed.port or 3001
        deadline = time.monotonic() + LOGIN_WATCH_GRACE_SEC
        while not stop.is_set() and time.monotonic() < deadline:
            if port_open(host, port):
                break
            if stop.wait(2):
                return
        # 启动流程已经把当前这张二维码打到屏幕上了，别播报第二遍。
        state = self.login_state() or {}
        announced = str(state.get("qrcodeurl") or "")
        announced_at = time.monotonic()
        attempts = 0
        relaunches = 0
        runtime_crashes = 0
        blind = 0
        offline = False
        # 本次掉线是否已经做过“干净重拉”。只做一次，避免被顶号时无限重拉。
        offline_relaunched = False
        while not stop.is_set():
            wait = LOGIN_POLL_SEC
            # 1. 进程没了：OneBot 不会自己回来，先重拉一次（运行期的收尸）。
            if self.process is not None and self.process.poll() is not None:
                code = self.process.poll()
                if runtime_crashes >= 5:
                    # 连续崩 5 次就别再原地打转了，QQ 自己起不来，重启也没用。
                    print(
                        f"[NapCat 守护] NapCat 运行期连续崩溃 {runtime_crashes} 次"
                        f"（最后一次 exit code {code}），停止自动重拉，请手动检查 QQ 安装。"
                    )
                    wait = 600
                else:
                    runtime_crashes += 1
                    print(f"[NapCat 守护] NapCat 进程已退出（exit code {code}），正在重拉。")
                    offline = True
                    announced = ""
                    self._restarts = 0
                    self._recover_after_exit()
                    if stop.wait(wait):
                        return
                    continue

            state = self.login_state()
            if state is None:
                # WebUI 还没起来 / 鉴权失败：等下一轮，别急着杀进程。
                # 但一直读不到就得说一声，否则用户只会看到“掉线了却什么都没发生”。
                blind += 1
                if blind == 3 or blind % 30 == 0:
                    print(
                        "[NapCat 守护] 读不到 NapCat WebUI 登录状态（6099 未监听或鉴权失败），"
                        f"暂时无法自动申请二维码；日志：{self.log_path}"
                    )
                # 接管来的实例 process 是 None，光靠进程退出判断不到“僵尸”；
                # 一直读不到状态说明它活着但核心没起来，这种只能清理重拉一次。
                if blind >= BLIND_RELAUNCH_AFTER and relaunches < MAX_LOGIN_RELAUNCH:
                    blind = 0
                    relaunches += 1
                    announced = ""
                    print(
                        "[NapCat 守护] 已经 "
                        f"{BLIND_RELAUNCH_AFTER * LOGIN_POLL_SEC} 秒读不到登录状态，"
                        "按僵尸实例处理，清理进程后重拉。"
                    )
                    self._relaunch_for_login(force_qr=relaunches >= 2)
                    wait = 10
                self._offline_event.wait(wait)
                self._offline_event.clear()
                continue
            blind = 0

            if state.get("isLogin"):
                if offline:
                    print("[NapCat 守护] QQ 已重新登录成功，OneBot 连接正在恢复。")
                    offline = False
                announced, attempts, relaunches = "", 0, 0
                runtime_crashes = 0
                offline_relaunched = False
                # 登录成功 = 票据是好的。之前攒下的“快速登录崩溃”计数必须清零，
                # 否则几次无关的崩溃会累加到最后把 autoLoginAccount 清掉。
                self._quick_crashes = 0
                # 扫码救回来之后把自动登录账号补回 webui.json（幂等）。
                self._restore_autologin()
                # 已经登进来了，“强制扫码”这个一次性标记就撤掉：下次再掉线
                # 仍先从快速登录试起，而不是继续当扫码模式跑。
                self._force_qr = False
                self._offline_event.wait(wait)
                self._offline_event.clear()
                continue

            offline = True
            url = str(state.get("qrcodeurl") or "").strip()
            phase = str(state.get("loginPhase") or "")
            if (
                not url
                and phase == "offline"
                and not offline_relaunched
                and relaunches < MAX_LOGIN_RELAUNCH
            ):
                # 掉线时先干净重拉一次，而不是立刻把二维码丢给用户。
                # 理由：这台机器上 NapCat 掉线后登录服务是死的（「重启功能未初始化」），
                # 向它要二维码多半要不到；而 QQ 关掉重开能靠本地票据快速登录 ——
                # 重拉一次十几秒就无感恢复，扫码却要人守着手机。
                offline_relaunched = True
                relaunches += 1
                announced = ""
                print("[NapCat 守护] QQ 掉线，先干净重拉一次（保留快速登录票据，先不打扰你扫码）。")
                self._relaunch_for_login()
                if stop.wait(RELAUNCH_SETTLE_SEC):
                    return
                continue
            if url and url != announced:
                # 二维码换了（刚生成的、或上一张过期后 NapCat 自己刷的）就播报一次。
                if time.monotonic() - announced_at >= QR_ANNOUNCE_MIN_GAP_SEC:
                    announced, announced_at = url, time.monotonic()
                    attempts = 0
                    self._announce_qrcode(state, url)
            elif not url and time.monotonic() - self._last_qr_at >= QR_MIN_INTERVAL_SEC:
                # 没有二维码就主动要一张 —— 这就是掉线后唯一能自救的动作。
                self._last_qr_at = time.monotonic()
                fresh = self.refresh_qrcode()
                if fresh:
                    announced, announced_at = fresh, time.monotonic()
                    attempts = 0
                    self._announce_qrcode(self.login_state() or state, fresh)
                else:
                    attempts += 1
                    if attempts >= 2 and relaunches < MAX_LOGIN_RELAUNCH:
                        relaunches += 1
                        attempts = 0
                        announced = ""
                        # 第一次只做干净重启（快速登录再试一次），第二次才清票据。
                        self._relaunch_for_login(force_qr=relaunches >= 2)
                        wait = 10
                    elif relaunches >= MAX_LOGIN_RELAUNCH:
                        print(
                            "[NapCat 守护] 已经重拉 "
                            f"{relaunches} 次仍拿不到二维码；请手动打开 "
                            "http://127.0.0.1:6099/webui 扫码，或重启 MiniBot。"
                        )
                        wait = 300
            self._offline_event.wait(wait)
            self._offline_event.clear()

    def _clear_autologin(self) -> bool:
        """清掉 WebUI 里的自动快速登录账号，逼 NapCat 走扫码登录。

        快速登录用的是本地缓存的登录票据；票据一旦失效（被强制结束后、或在别的
        设备登录过），QQ 会在“登录成功”后 1~2 秒直接退出，永远等不到 OneBot。
        这时只能扫码登一次，把票据换新。
        """
        self._force_qr = True
        path = self.napcat_dir / "config" / "webui.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not data.get("autoLoginAccount"):
            return True
        data["autoLoginAccount"] = ""
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        except OSError:
            return False
        self._autologin_cleared = True
        # 跨进程记住“票据可能已失效”：下次启动/接管时别再急着把账号写回去。
        self._mark_ticket_invalid(True)
        return True

    def _restore_autologin(self) -> bool:
        """扫码登录成功后把自动快速登录账号写回去。

        不写回去的后果很直观：一次票据失效 → 永久清空 autoLoginAccount →
        之后每次启动都当新机器扫码；而用户手动关掉再打开之所以能免扫码，正是因为
        `_ensure_config` 在启动时又把账号补了回来。这里主动补，省掉一轮“为什么又要扫码”。
        """
        if not (self._autologin_cleared or self._ticket_invalid()):
            return False
        account = self._detect_last_account()
        if not account:
            return False
        path = self.napcat_dir / "config" / "webui.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("autoLoginAccount") == account:
            self._autologin_cleared = False
            self._force_qr = False
            self._mark_ticket_invalid(False)
            return True
        data["autoLoginAccount"] = account
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8"
            )
        except OSError:
            return False
        self._autologin_cleared = False
        self._force_qr = False
        self._mark_ticket_invalid(False)
        print(
            f"[NapCat 守护] 已恢复自动快速登录账号 {account}，下次重启无需扫码。"
        )
        return True

    def _recycle(self):
        """清掉当前这棵进程树（不碰退出钩子），为下一次 _launch 做准备。"""
        self._is_running = False
        self._stop_event.set()
        self._stream_stop.set()
        pids = self._live_pids()
        for pid in pids:
            terminate_pid(pid)
        if pids:
            self._wait_gone(pids)
        self._close_log()
        self.process = None
        self._forget_owner()

    def _recover_after_exit(self) -> bool:
        """启动阶段进程退出了：先按“快速登录失效→改扫码”处理，再按次数重拉。

        返回 True 表示已经重新拉起（新的监听线程会接手），False 表示不再重试。
        """
        code = self.process.poll() if self.process else None
        if self._restarts >= MAX_AUTO_RESTARTS:
            print(
                f"[NapCat Manager] NapCat 连续退出 {self._restarts} 次"
                f"（最后一次 exit code {code}），已停止自动重试。"
            )
            print(
                "  说明：QQ 在“登录成功”之后立刻退出时，事件查看器「应用程序」日志里"
                "通常有一条 QQ.exe 的 1000 事件（故障模块 wrapper.node / 异常 0xc00000fd）。"
            )
            print(
                "  处理：把 webui.json 的 autoLoginAccount 置空后重试扫码登录；"
                "仍不行就重装 QQ 或升级 NapCat。"
            )
            print(f"  日志：{self.log_path}")
            return False
        # “快速登录后立刻退出”才说明票据有问题；跑了一阵子再崩只是运行期崩溃，
        # 不能因此去扫一次码。所以这里卡一个时间窗，并且只在窗口内累计次数。
        quick_crashed = bool(
            self._quick_login_at
            and not self._force_qr
            and time.monotonic() - self._quick_login_at <= QUICK_LOGIN_CRASH_WINDOW_SEC
        )
        if quick_crashed:
            self._quick_crashes += 1
            if self._quick_crashes >= 2 and self._clear_autologin():
                print(
                    "[NapCat Manager] 连续两次“快速登录成功但 QQ 立刻退出”，"
                    "判断本地登录票据已失效，改为扫码登录重试，请扫下面新出的二维码。"
                )
            else:
                print(
                    "[NapCat Manager] 快速登录后 QQ 随即退出（exit code "
                    f"{code}，属于偶发的登录期崩溃），先原样重试一次。"
                )
        else:
            print(f"[NapCat Manager] NapCat 进程退出（exit code {code}），自动重拉。")
        self._restarts += 1
        self._recycle()
        if self._keep_on_exit or not self._launch():
            return False
        return True

    def _heal(self):
        """启动阶段专用：把整棵进程树清干净再拉一次（/restart 绝不走这里）。"""
        self._restarts += 1
        print(
            "[NapCat Manager] QQ卡在初始化（登录服务未就绪、也没有二维码），"
            "清理整棵进程树后自动重拉一次。"
        )
        self._recycle()
        if self._keep_on_exit or not self._launch():
            return
        print("[NapCat Manager] 已重新拉起 NapCat，请留意下面的日志。")

    def _emit_line(self, text):
        clean = text.rstrip()
        if not clean:
            return
        if "NapCat Shell App Loading" in clean or "[WebUi]" in clean:
            self.core_loaded.set()
        if QUICK_LOGIN_MARKER in clean:
            # 记下“走的是缓存票据快速登录”，进程随后退出就能判定票据失效。
            self._quick_login_at = time.monotonic()
        match = MAIN_PID_PATTERN.search(clean)
        if match and not self._owned_qq_pid:
            # 启动器日志里的 QQ 主进程 PID：启动器先退出时靠它把 QQ 一起收掉。
            self._owned_qq_pid = int(match.group(1))
            self._write_owner_state()
        if any(marker in clean for marker in OFFLINE_MARKERS):
            # 掉线了：立刻叫醒登录守护，别等下一次 20 秒轮询。
            self._offline_event.set()
        # 方块二维码是给手机扫的：前缀会挤掉静默区、让镜头对不齐，原样输出。
        if sum(clean.count(mark) for mark in "█▀▄▌▐") >= 8:
            print(clean, flush=True)
            return
        # 打印日志到终端（包括字符二维码）
        print(f"[NapCat] {clean}", flush=True)

    def _follow_log(self, from_end=False):
        """起一个线程跟随 NapCat 日志文件，把终端输出（含二维码）转发到控制台。

        from_end=True 用于接管已有实例：日志里是上一个进程留下的历史，从头读会
        把几十 KB 旧内容一次性刷到屏幕上，还会盖掉当前该看的二维码。
        """
        self._stream_stop = threading.Event()
        self._output_thread = threading.Thread(
            target=self._stream_output,
            args=(self._stream_stop, from_end),
            daemon=True,
            name="napcat-log-follow",
        )
        self._output_thread.start()

    def _stream_output(self, stop_event=None, from_end=False):
        """跟随 NapCat 日志文件，把控制台输出（含二维码）转发到终端。"""
        stop_event = stop_event or self._stream_stop
        try:
            stream = open(self.log_path, "rb")
        except OSError:
            return
        if from_end:
            try:
                stream.seek(0, os.SEEK_END)
            except OSError:
                pass
        pending = b""
        last_growth = time.monotonic()
        with stream:
            while not stop_event.is_set():
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

    def _archive_log(self):
        """把上一条日志留一份 prev：自动重拉会截断当前日志，崩溃前那段就没了。"""
        try:
            if self.log_path.is_file() and self.log_path.stat().st_size:
                shutil.copyfile(
                    self.log_path, self.log_path.with_name("napcat-stdout.prev.log")
                )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 退出兜底：关控制台 / 解释器退出时，连 QQ 一起收掉
    # ------------------------------------------------------------------
    def _install_exit_hooks(self):
        """关控制台窗口时自己把整棵树收掉，别让系统把 python 和 QQ 一起砍死。

        默认行为是 Windows 直接终止控制台进程组：python 和 QQ 同时被 TerminateProcess，
        下次启动很容易撞上一个“活着但登录服务起不来”的 QQ。这里在事件到来时先按记录
        结束整棵树，退出顺序才是干净的；/restart 交接时 _keep_on_exit 会拦住收尾。
        """
        if self._atexit_registered:
            return
        self._atexit_registered = True
        atexit.register(self._cleanup_on_exit)
        if os.name != "nt" or self._exit_hook is not None:
            return
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _on_ctrl(event):
            # 0=CTRL_C 1=CTRL_BREAK 2=CTRL_CLOSE 5=LOGOFF 6=SHUTDOWN
            if event in (0, 1, 2, 5, 6):
                self._cleanup_on_exit()
            # Ctrl+C 仍交回 Python 处理，保留原本的 KeyboardInterrupt 行为。
            return event not in (0, 1)

        # 回调必须留强引用，否则被 GC 后事件一到就直接崩。
        self._exit_hook = handler_type(_on_ctrl)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(self._exit_hook, True)

    def _cleanup_on_exit(self):
        """幂等收尾；/restart 已经把所有权交给下一个实例时直接放过。"""
        if self._keep_on_exit:
            return
        self._stop_login_watch()
        self._is_running = False
        self._stop_event.set()
        self._stream_stop.set()
        pids = self._live_pids()
        for pid in pids:
            terminate_pid(pid)
        if pids:
            self._wait_gone(pids)
        self._close_log()
        self.process = None
        self._forget_owner()

    def stop(self):
        """正常退出：连 QQ 一起收掉，不留后台驻留进程。"""
        self._is_running = False
        self._stop_event.set()
        self._stream_stop.set()
        self._stop_login_watch()
        if self._monitor_thread:
            self._monitor_thread.join(3)
        if self._login_thread and self._login_thread is not threading.current_thread():
            self._login_thread.join(3)
        pids = self._live_pids()
        for pid in pids:
            terminate_pid(pid)
        if self.process:
            try:
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        if pids:
            self._wait_gone(pids)
        if self._output_thread:
            self._output_thread.join(2)
        self.process = None
        self._close_log()
        self._forget_owner()
