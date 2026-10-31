"""Offline idle baseline; never reads the user's clipboard/window or calls a model.
python tools/benchmark.py --seconds 2
python tools/benchmark.py --pid <running MiniBot PID>  # Windows process-tree working set
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def working_set(pid):
    if os.name != "nt":
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    from ctypes import wintypes as w

    class COUNTERS(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [
            (n, ctypes.c_size_t)
            for n in (
                "peak_ws",
                "ws",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
    handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle:
        return None
    try:
        counter = COUNTERS()
        counter.cb = ctypes.sizeof(counter)
        return (
            int(counter.ws)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counter), counter.cb)
            else None
        )
    finally:
        kernel.CloseHandle(handle)


def process_tree(pid):
    if os.name != "nt":
        return [{"pid": pid, "working_set_bytes": working_set(pid)}]
    from ctypes import wintypes as w

    class ENTRY(ctypes.Structure):
        _fields_ = [
            ("size", w.DWORD),
            ("usage", w.DWORD),
            ("pid", w.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", w.DWORD),
            ("threads", w.DWORD),
            ("parent", w.DWORD),
            ("priority", w.LONG),
            ("flags", w.DWORD),
            ("name", w.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(ENTRY)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ENTRY)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError("process snapshot failed")
    entries = []
    entry = ENTRY()
    entry.size = ctypes.sizeof(entry)
    try:
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            entries.append((entry.pid, entry.parent, entry.name))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    included = {pid}
    changed = True
    while changed:
        changed = False
        for child, parent, name in entries:
            if parent in included and child not in included:
                included.add(child)
                changed = True
    return [
        {"pid": child, "name": name, "working_set_bytes": working_set(child)}
        for child, parent, name in entries
        if child in included
    ]


def child(mode, seconds):
    from core.memory import MiniMemory
    from core.brain import VLMBrain
    from core.jev import JevClient
    from core.harness import AgentHarness
    from core.runtime import Dispatcher
    from core.proactive import ProactiveEngine
    from core.skill_manager import SkillManager
    from core import config

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config.CONFIG_FILE_PATH = root / "config.json"
        config._cache = None
        config._stamp = None
        memory = MiniMemory(root / "memory.db")
        skills = SkillManager(root / "skills")

        class Perception:
            def snapshot(self, **kwargs):
                return {
                    "title": "offline-fixture",
                    "process": "fixture",
                    "idle_sec": 40,
                    "locked": False,
                    "fullscreen": False,
                    "meeting": False,
                }

            def check_clipboard_change(self):
                return None

        perception = Perception()
        dispatcher = Dispatcher(2, 16)
        brain = VLMBrain()
        jev = JevClient()
        _harness = AgentHarness(brain, jev, memory, perception, skills)
        engine = ProactiveEngine(
            memory, perception, jev, brain, skills, dispatcher=dispatcher
        )
        app = None
        widget = None
        if mode == "pet":
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
            from PyQt5.QtWidgets import QApplication
            from channels.pet_channel import DesktopPetWidget

            app = QApplication([])
            widget = DesktopPetWidget(
                str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
                lambda _: None,
                lambda: None,
            )
            widget.show()
        if mode == "qq":
            from channels.napcat_channel import NapCatChannel

            _channel = NapCatChannel()  # loaded, without connecting an account
        engine.start()
        start = time.perf_counter()
        cpu = time.process_time()
        while time.perf_counter() - start < seconds:
            if app:
                app.processEvents()
            time.sleep(0.01)
        duration = time.perf_counter() - start
        consumed = time.process_time() - cpu
        result = {
            "scenario": mode,
            "pid": os.getpid(),
            "working_set_mib": round(working_set(os.getpid()) / 1048576, 2),
            "idle_cpu_one_core_percent": round(consumed / duration * 100, 3),
            "seconds": round(duration, 2),
            "scope": "offline synthetic idle; excludes actual NapCat/QQ and cloud latency",
        }
        if widget:
            result["cached_animation_rows"] = len(widget.frames)
            widget.close()
            widget.bubble.close()
            widget.hud.close()
        engine.stop()
        dispatcher.shutdown()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--child", choices=["core", "pet", "qq"])
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.pid:
        rows = process_tree(args.pid)
        print(
            json.dumps(
                {
                    "processes": rows,
                    "total_working_set_mib": round(
                        sum(r["working_set_bytes"] or 0 for r in rows) / 1048576, 2
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.child:
        print(json.dumps(child(args.child, args.seconds), ensure_ascii=False))
    else:
        results = []
        for mode in ("core", "pet", "qq"):
            run = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--child",
                    mode,
                    "--seconds",
                    str(args.seconds),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if run.returncode:
                raise RuntimeError(run.stderr)
            results.append(json.loads(run.stdout.strip().splitlines()[-1]))
        print(json.dumps(results, ensure_ascii=False, indent=2))
