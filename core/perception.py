import ctypes
import ctypes.wintypes
import io
from typing import Dict, Optional


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
        self._clipboard_sequence = None
        u, k = self._user32, self._kernel32
        u.GetForegroundWindow.restype = ctypes.wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
        u.GetWindowTextW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPWSTR,
            ctypes.c_int,
        ]
        u.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        k.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        k.OpenProcess.restype = ctypes.wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        k.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        k.GetTickCount.restype = ctypes.wintypes.DWORD
        u.GetWindowRect.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.RECT),
        ]
        k.GlobalSize.argtypes = [ctypes.wintypes.HANDLE]
        k.GlobalSize.restype = ctypes.c_size_t
        u.GetClipboardData.argtypes = [ctypes.c_uint]
        u.GetClipboardData.restype = ctypes.wintypes.HANDLE
        u.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
        u.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        k.GlobalLock.argtypes = [ctypes.wintypes.HANDLE]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.wintypes.HANDLE]
        u.OpenInputDesktop.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        u.OpenInputDesktop.restype = ctypes.wintypes.HANDLE
        u.CloseDesktop.argtypes = [ctypes.wintypes.HANDLE]
        u.SwitchDesktop.argtypes = [ctypes.wintypes.HANDLE]
        u.MonitorFromWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
        u.MonitorFromWindow.restype = ctypes.wintypes.HANDLE
        u.GetMonitorInfoW.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p]

    def get_idle_seconds(self) -> float:
        """获取用户无键鼠操作的空闲秒数"""
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if self._user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = (self._kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
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
        h_proc = self._kernel32.OpenProcess(
            0x1000, False, pid.value
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if h_proc:
            try:
                name_buf = ctypes.create_unicode_buffer(512)
                size = ctypes.wintypes.DWORD(512)
                # QueryFullProcessImageNameW
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    h_proc, 0, name_buf, ctypes.byref(size)
                ):
                    full_path = name_buf.value
                    proc_name = full_path.split("\\")[-1]
            finally:
                self._kernel32.CloseHandle(h_proc)

        return {"title": title or "Untitled", "process": proc_name}

    def check_clipboard_change(self) -> Optional[str]:
        """Bounded native clipboard read; no pywin32 dependency or full-payload retention."""
        sequence = self._user32.GetClipboardSequenceNumber()
        if sequence == self._clipboard_sequence:
            return None
        if not self._user32.OpenClipboard(None):
            return None
        try:
            if not self._user32.IsClipboardFormatAvailable(13):
                return None
            handle = self._user32.GetClipboardData(13)
            if not handle:
                return None
            size = self._kernel32.GlobalSize(handle)
            self._clipboard_sequence = sequence
            if not 0 < size <= 65536:
                return None
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                data = (
                    ctypes.wstring_at(
                        pointer, min(500, size // ctypes.sizeof(ctypes.c_wchar))
                    )
                    .split("\0", 1)[0]
                    .strip()
                )
            finally:
                self._kernel32.GlobalUnlock(handle)
            if data and data != self._last_clipboard:
                self._last_clipboard = data
                return data
        finally:
            self._user32.CloseClipboard()

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

    def is_locked(self):
        desktop = self._user32.OpenInputDesktop(0, False, 0x0100)
        if not desktop:
            return True
        try:
            return not bool(self._user32.SwitchDesktop(desktop))
        finally:
            self._user32.CloseDesktop(desktop)

    def snapshot(self, include_window=True):
        window = (
            self.get_active_window()
            if include_window
            else {"title": "", "process": "unknown"}
        )
        rect = ctypes.wintypes.RECT()
        hwnd = self._user32.GetForegroundWindow()
        fullscreen = False
        if hwnd and self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("size", ctypes.wintypes.DWORD),
                    ("monitor", ctypes.wintypes.RECT),
                    ("work", ctypes.wintypes.RECT),
                    ("flags", ctypes.wintypes.DWORD),
                ]

            info = MONITORINFO()
            info.size = ctypes.sizeof(info)
            monitor = self._user32.MonitorFromWindow(hwnd, 2)
            if self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                fullscreen = (
                    rect.left <= info.monitor.left
                    and rect.top <= info.monitor.top
                    and rect.right >= info.monitor.right
                    and rect.bottom >= info.monitor.bottom
                    and window["process"].lower() not in ("explorer.exe", "unknown")
                )
        meeting = window["process"].lower() in (
            "teams.exe",
            "ms-teams.exe",
            "zoom.exe",
            "wemeetapp.exe",
        ) or any(
            word in window["title"].lower()
            for word in ("google meet", "zoom meeting", "腾讯会议", "microsoft teams")
        )
        return dict(
            window,
            idle_sec=self.get_idle_seconds(),
            fullscreen=fullscreen,
            locked=self.is_locked(),
            meeting=meeting,
        )
