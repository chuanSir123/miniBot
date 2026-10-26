import ctypes
import ctypes.wintypes
import io
import time
from typing import Dict, Optional, Tuple


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


class LocalPerception:
    """
    本机状态感知 (旁听流)
    通过 Windows 原生 API 采集:
      1. 活动窗口标题与所属进程
      2. 键盘鼠标空闲时长 (用于判断专注或挂机)
      3. 剪贴板最新文本 (用于识别代码报错或链接)
      4. 按需屏幕截屏 (压缩送入 VLM)
    """

    def __init__(self):
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._last_clipboard = ""

    def get_idle_seconds(self) -> float:
        """获取用户无键鼠操作的空闲秒数"""
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if self._user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = self._kernel32.GetTickCount() - lii.dwTime
            return max(0.0, millis / 1000.0)
        return 0.0

    def get_active_window(self) -> Dict[str, str]:
        """获取当前焦点窗口标题及进程信息"""
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "Desktop / None", "process": "explorer.exe"}

        # 获取窗口标题
        length = self._user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()

        # 获取进程 PID
        pid = ctypes.wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        # 尝试通过 psutil 或 win32 获取进程名（兜底纯 ctypes）
        proc_name = "unknown"
        h_proc = self._kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h_proc:
            try:
                name_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                # QueryFullProcessImageNameW
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(h_proc, 0, name_buf, ctypes.byref(size)):
                    full_path = name_buf.value
                    proc_name = full_path.split("\\")[-1]
            finally:
                self._kernel32.CloseHandle(h_proc)

        return {"title": title or "Untitled", "process": proc_name}

    def check_clipboard_change(self) -> Optional[str]:
        """检查剪贴板是否有更新，若有则返回最新文本（过滤过长文本）"""
        try:
            import win32clipboard
            import win32con

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    if data and data != self._last_clipboard:
                        self._last_clipboard = data
                        # 控制在 500 字内用于语义旁听
                        return data.strip()[:500]
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
        return None

    def capture_screen_jpeg(self, quality: int = 70) -> Optional[bytes]:
        """按需截屏，转为 JPEG bytes 供 VLM 分析"""
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            # 缩放至最大 1280 宽度以减小传输开销与 Token 延迟
            if img.width > 1280:
                h = int(img.height * (1280 / img.width))
                img = img.resize((1280, h))

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
        except Exception as e:
            print(f"[Perception] Screen capture error: {e}")
            return None
