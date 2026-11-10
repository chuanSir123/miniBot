"""
Background Task Manager for long-running commands and skills.
Provides non-blocking background execution, streaming log persistence,
live tail capturing, sync waiting, async status inspection, and tree cancellation.
"""

import collections
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.process import clean_environment, terminate_tree


class BackgroundTask:
    def __init__(
        self,
        task_id: str,
        name: str,
        command,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        input_text: Optional[str] = None,
        shell: bool = False,
        clean_env: bool = True,
        metadata: Optional[dict] = None,
        max_tail_bytes: int = 65536,
    ):
        self.task_id = task_id
        self.name = name
        self.command = command
        self.cwd = cwd
        self.shell = shell
        self.metadata = metadata or {}
        self.max_tail_bytes = max_tail_bytes

        self.created_at = time.time()
        self.started_at = self.created_at
        self.ended_at: Optional[float] = None
        self.status = "running"  # running, completed, failed, cancelled, timeout
        self.exit_code: Optional[int] = None
        self.result_payload: Optional[dict] = None

        self.lock = threading.RLock()
        self.done_event = threading.Event()
        self.cancel_event = threading.Event()

        # Output logging & tail buffer
        self.log_dir = Path("data/tasks")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"{self.task_id}.log"
        self._log_handle = None
        self._tail_chunks = collections.deque()
        self._tail_bytes = 0

        # Subprocess spawn
        env_vars = dict(env if env is not None else os.environ)
        if clean_env:
            env_vars = clean_environment(env_vars)
        # 中文 Windows 下 python 子进程 stdout 默认按 GBK(cp936) 输出，
        # _drain_output 却按 UTF-8 读，中文会整段变成替换字符。统一让子进程
        # 输出 UTF-8；PowerShell / cmd 自身的 GBK 报错由解码回退兜住。
        env_vars.setdefault("PYTHONIOENCODING", "utf-8")

        flags = (
            {
                "creationflags": subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )

        try:
            self._log_handle = open(self.log_file, "w", encoding="utf-8", errors="replace")
        except OSError:
            self._log_handle = None

        self.proc = subprocess.Popen(
            command,
            shell=shell,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env_vars,
            **flags,
        )

        if input_text is not None:
            try:
                self.proc.stdin.write(input_text.encode("utf-8"))
                self.proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        # Dedicated streaming reader thread
        self.reader_thread = threading.Thread(target=self._drain_output, daemon=True)
        self.reader_thread.start()

    def _append_chunk(self, text: str):
        with self.lock:
            if self._log_handle:
                try:
                    self._log_handle.write(text)
                    self._log_handle.flush()
                except OSError:
                    pass
            self._tail_chunks.append(text)
            self._tail_bytes += len(text.encode("utf-8", errors="replace"))
            while self._tail_bytes > self.max_tail_bytes and len(self._tail_chunks) > 1:
                removed = self._tail_chunks.popleft()
                self._tail_bytes -= len(removed.encode("utf-8", errors="replace"))

    def _drain_output(self):
        try:
            for line in iter(self.proc.stdout.readline, b""):
                try:
                    text = line.decode("utf-8")
                except UnicodeDecodeError:
                    # PowerShell / cmd 自己写的报错常是 GBK 字节，按 UTF-8 会全变问号。
                    text = line.decode("gbk", errors="replace")
                self._append_chunk(text)
        except Exception:
            pass
        finally:
            try:
                self.proc.wait()
            except Exception:
                pass
            with self.lock:
                self.exit_code = self.proc.returncode
                self.ended_at = time.time()
                if self.status == "running":
                    self.status = "completed" if self.exit_code == 0 else "failed"

                # Check for structured __MINIBOT_RESULT__ payload
                full_text = "".join(self._tail_chunks)
                if "__MINIBOT_RESULT__" in full_text:
                    try:
                        idx = full_text.rfind("__MINIBOT_RESULT__")
                        payload_str = full_text[idx + len("__MINIBOT_RESULT__"):].strip()
                        # Might have trailing newlines or extra text; find first valid JSON block or entire line
                        lines = payload_str.splitlines()
                        if lines:
                            payload = json.loads(lines[0])
                            if isinstance(payload, dict):
                                self.result_payload = payload
                    except Exception:
                        pass

                if self._log_handle:
                    try:
                        self._log_handle.close()
                    except OSError:
                        pass
                    self._log_handle = None

            self.done_event.set()
            TaskManager.get_instance()._notify_task_finished(self)

    def tail_output(self, max_chars: int = 4000) -> str:
        with self.lock:
            text = "".join(self._tail_chunks)
            if len(text) > max_chars:
                return text[-max_chars:]
            return text

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for task completion up to timeout seconds.
        Returns True if task finished, False if still running.
        """
        return self.done_event.wait(timeout=timeout)

    def cancel(self):
        with self.lock:
            if self.status != "running":
                return
            self.status = "cancelled"
            self.cancel_event.set()
            terminate_tree(self.proc)
            self._append_chunk("\n[任务已被用户主动取消终止]")
        self.done_event.set()


class TaskManager:
    _instance = None
    _lock = threading.RLock()

    def __init__(self):
        self.tasks: Dict[str, BackgroundTask] = {}
        self.tasks_lock = threading.RLock()
        self.completion_callbacks: List[Callable[[BackgroundTask], None]] = []

    @classmethod
    def get_instance(cls) -> "TaskManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = TaskManager()
        return cls._instance

    def register_completion_callback(self, callback: Callable[[BackgroundTask], None]):
        with self.tasks_lock:
            self.completion_callbacks.append(callback)

    def _notify_task_finished(self, task: BackgroundTask):
        callbacks = []
        with self.tasks_lock:
            callbacks = list(self.completion_callbacks)
        for cb in callbacks:
            try:
                cb(task)
            except Exception:
                pass

    def start_task(
        self,
        command,
        name: str = "task",
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        input_text: Optional[str] = None,
        shell: bool = False,
        clean_env: bool = True,
        metadata: Optional[dict] = None,
    ) -> BackgroundTask:
        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        task = BackgroundTask(
            task_id=task_id,
            name=name,
            command=command,
            cwd=cwd,
            env=env,
            input_text=input_text,
            shell=shell,
            clean_env=clean_env,
            metadata=metadata,
        )
        with self.tasks_lock:
            self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        with self.tasks_lock:
            return self.tasks.get(task_id)

    def list_tasks(self, status: Optional[str] = None, limit: int = 10) -> List[dict]:
        with self.tasks_lock:
            items = list(self.tasks.values())
        items.sort(key=lambda t: t.created_at, reverse=True)
        if status:
            items = [t for t in items if t.status == status]
        items = items[:limit]
        return [
            {
                "task_id": t.task_id,
                "name": t.name,
                "status": t.status,
                "started_at": t.started_at,
                "duration_sec": round((t.ended_at or time.time()) - t.started_at, 1),
                "exit_code": t.exit_code,
            }
            for t in items
        ]

    def check_task(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if not task:
            return {"ok": False, "error": f"未找到任务: {task_id}"}
        duration = round((task.ended_at or time.time()) - task.started_at, 1)
        output = task.tail_output(max_chars=4000)
        res = {
            "ok": True,
            "task_id": task.task_id,
            "name": task.name,
            "status": task.status,
            "is_alive": task.is_alive(),
            "exit_code": task.exit_code,
            "duration_sec": duration,
            "tail_output": output,
            "log_file": str(task.log_file),
        }
        if task.result_payload:
            res["payload"] = task.result_payload
        return res

    def wait_task(self, task_id: str, timeout_sec: float = 15.0) -> dict:
        task = self.get_task(task_id)
        if not task:
            return {"ok": False, "error": f"未找到任务: {task_id}"}

        finished = task.wait(timeout=max(0.1, float(timeout_sec)))
        duration = round((task.ended_at or time.time()) - task.started_at, 1)
        tail = task.tail_output(max_chars=4000)

        if finished:
            is_ok = task.status == "completed" and (task.exit_code == 0)
            result = {
                "ok": is_ok,
                "task_id": task.task_id,
                "name": task.name,
                "status": task.status,
                "exit_code": task.exit_code,
                "duration_sec": duration,
                "output": tail or "(无输出)",
            }
            if task.result_payload:
                result = {**result, **task.result_payload}
            return result

        # Not finished within timeout - do NOT kill! Return progressive status
        return {
            "ok": True,
            "task_id": task.task_id,
            "name": task.name,
            "status": "running",
            "still_running": True,
            "duration_sec": duration,
            "output": (
                f"任务仍在运行中（已运行 {duration:.1f} 秒）。\n"
                f"最新捕获日志:\n{tail or '(暂无输出)'}\n\n"
                f"[操作提示]: 任务未被终止，继续在后台执行。\n"
                f"- 若需同步继续等待其完成，请调用 wait_task(task_id='{task.task_id}', timeout_sec=15)；\n"
                f"- 若需先进行其他操作，可稍后调用 check_task(task_id='{task.task_id}') 查看进度与结果。"
            ),
        }

    def cancel_task(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        if not task:
            return {"ok": False, "error": f"未找到任务: {task_id}"}
        task.cancel()
        return {
            "ok": True,
            "task_id": task.task_id,
            "name": task.name,
            "status": "cancelled",
            "output": f"任务 {task_id} 已成功发送终止信号并取消。",
        }
