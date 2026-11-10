"""Bounded pipe capture and process-tree timeouts (no unbounded communicate())."""

import os
import subprocess
import threading
import time


def pid_alive(pid):
    """判断进程是否还活着（跨平台）。"""
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def terminate_pid(pid):
    """按 PID 结束整棵进程树，用于手上没有 Popen 句柄的“遗留进程”。"""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        return
    import signal

    try:
        os.killpg(int(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def terminate_tree(proc):
    if proc.poll() is not None:
        return
    terminate_pid(proc.pid)


def clean_environment(base_env=None):
    """
    Scrub sensitive credentials (keys, tokens, secrets, bot auth) from child environment,
    preventing spawned commands and scripts from exfiltrating or abusing credentials.
    """
    env = dict(base_env if base_env is not None else os.environ)
    sensitive_patterns = (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSW",
        "CREDENTIAL",
        "AUTH",
        "COOKIE",
        "SESSION",
    )
    sensitive_prefixes = (
        "OPENAI_",
        "VLLM_",
        "ANTHROPIC_",
        "GEMINI_",
        "NAPCAT_",
        "MINIBOT_",
        "AWS_",
        "AZURE_",
        "GH_",
        "GITHUB_",
    )
    scrubbed = {}
    for k, v in env.items():
        k_upper = k.upper()
        if k_upper == "PROXY" or k_upper.endswith("_PROXY"):
            continue
        if any(pat in k_upper for pat in sensitive_patterns):
            continue
        if any(k_upper.startswith(p) for p in sensitive_prefixes):
            continue
        scrubbed[k] = v
    scrubbed["NO_PROXY"] = "*"
    scrubbed["no_proxy"] = "*"
    return scrubbed


def run_bounded(
    command,
    timeout=15,
    max_bytes=16000,
    shell=False,
    input_text=None,
    cwd=None,
    cancel_event=None,
    clean_env=True,
    env=None,
):
    flags = (
        {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    env_vars = env
    if clean_env:
        env_vars = clean_environment(env)
    proc = subprocess.Popen(
        command,
        shell=shell,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env_vars,
        **flags,
    )
    job = WindowsJob(proc)
    chunks = []
    count = 0
    truncated = False

    def drain():
        nonlocal count, truncated
        while True:
            data = proc.stdout.read(2048)
            if not data:
                break
            room = max_bytes - count
            if room > 0:
                chunks.append(data[:room])
                count += min(room, len(data))
            if len(data) > room:
                truncated = True

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    if input_text is not None:
        try:
            proc.stdin.write(input_text.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
    timed_out = False
    timeout_val = float(timeout) if timeout is not None else 15.0
    deadline = time.monotonic() + max(0.1, timeout_val)
    cancelled = False
    try:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                terminate_tree(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_tree(proc)
                break
            try:
                proc.wait(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        proc.wait(timeout=5)
    finally:
        job.close()
        if proc.poll() is None:
            terminate_tree(proc)
            proc.wait(timeout=5)
        reader.join(2)
        if not reader.is_alive():
            proc.stdout.close()
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if truncated:
        text += "\n[输出已截断]"
    if timed_out and "[任务运行超时" not in text:
        text += f"\n[任务运行超时 ({timeout_val:g}s)]"
    return {
        "ok": not timed_out and not cancelled and proc.returncode == 0,
        "exit_code": proc.returncode,
        "timeout": timed_out,
        "cancelled": cancelled,
        "output": text,
        "truncated": truncated,
    }


class WindowsJob:
    def __init__(self, proc):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes as w

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64),
                ("JobTime", ctypes.c_int64),
                ("Flags", w.DWORD),
                ("MinWS", ctypes.c_size_t),
                ("MaxWS", ctypes.c_size_t),
                ("Active", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", w.DWORD),
                ("Scheduling", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOps",
                    "WriteOps",
                    "OtherOps",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("Basic", BASIC),
                ("Io", IO),
                ("ProcessMem", ctypes.c_size_t),
                ("JobMem", ctypes.c_size_t),
                ("PeakProcess", ctypes.c_size_t),
                ("PeakJob", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXTENDED()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) or not kernel.AssignProcessToJobObject(handle, w.HANDLE(int(proc._handle))):
            error = ctypes.get_last_error()
            kernel.CloseHandle(handle)
            terminate_tree(proc)
            raise OSError(error, "Cannot contain process tree in Windows Job")
        self.handle = handle
        self.kernel = kernel

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
