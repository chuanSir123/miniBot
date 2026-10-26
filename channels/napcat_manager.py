import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
NAPCAT_DIR = TOOLS_DIR / "napcat"
NAPCAT_ZIP_URL = "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip"
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

    def is_installed(self) -> bool:
        launcher = self.napcat_dir / "launcher.bat"
        launcher_w10 = self.napcat_dir / "launcher-win10.bat"
        return launcher.exists() or launcher_w10.exists()

    def prepare_napcat(self, use_mirror: bool = True) -> bool:
        """确保 NapCat.Shell 已经下载并解压"""
        if self.is_installed():
            return True

        self.napcat_dir.mkdir(parents=True, exist_ok=True)
        zip_path = TOOLS_DIR / "NapCat.Shell.zip"
        download_url = NAPCAT_MIRROR_URL if use_mirror else NAPCAT_ZIP_URL

        print(f"[NapCat Manager] 正在从镜像源下载 NapCat.Shell ...")
        print(f"下载地址: {download_url}")

        try:
            # 下载 zip
            req = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as out_f:
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

    def _ensure_config(self):
        """预写入 OneBot 11 配置，开启正向 WebSocket 服务端"""
        cfg_dir = self.napcat_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # 兼容 NapCat 默认 onebot11 配置
        onebot_cfg = {
            "http": {"enable": False, "port": 3000},
            "ws": {"enable": True, "port": self.ws_port},
            "reverse_ws": {"enable": False, "urls": []},
            "token": "",
            "debug": False
        }
        cfg_path = cfg_dir / "onebot11.json"
        if not cfg_path.exists():
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(onebot_cfg, f, indent=2)

    def start(self, qq_number: str = "") -> bool:
        """启动 NapCat 子进程，并在终端打印日志与二维码"""
        if not self.is_installed():
            if not self.prepare_napcat():
                return False

        self._ensure_config()

        # 选择批处理入口
        launcher = self.napcat_dir / "launcher.bat"
        if not launcher.exists():
            launcher = self.napcat_dir / "launcher-win10.bat"

        cmd = [str(launcher)]
        if qq_number.strip():
            cmd.extend(["-q", qq_number.strip()])

        print("\n" + "=" * 55)
        print(" [NapCat QQ] 正在启动内嵌 NTQQ 运行环境...")
        print(" [NapCat QQ] 请留意下方控制台输出的 登录二维码，使用手机 QQ 扫码即可！")
        print("=" * 55 + "\n")

        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.napcat_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )
            self._is_running = True
            self._output_thread = threading.Thread(target=self._stream_output, daemon=True)
            self._output_thread.start()
            return True
        except Exception as e:
            print(f"[NapCat Manager] 启动进程失败: {e}")
            return False

    def _stream_output(self):
        """流式读取并显示控制台输出"""
        if not self.process or not self.process.stdout:
            return

        for line in iter(self.process.stdout.readline, ""):
            if not self._is_running:
                break
            clean_line = line.rstrip()
            if clean_line:
                # 打印日志到终端（包括字符二维码）
                print(f"[NapCat] {clean_line}")

    def stop(self):
        self._is_running = False
        if self.process:
            try:
                print("[NapCat Manager] 正在关闭 NapCat 进程...")
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
