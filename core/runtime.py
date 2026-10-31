"""Bounded dispatcher: FIFO within a session, foreground ahead of background work."""

import concurrent.futures
import heapq
import itertools
import threading
import time


def _log_queue_wait(session, wait_ms, background=False, workers=0):
    try:
        from core.console import log

        log("Dispatcher", "queue.wait", session=session, wait_ms=wait_ms,
            background=background, workers=workers)
    except Exception:
        pass


class Dispatcher:
    def __init__(self, workers=2, capacity=16):
        self.capacity = capacity
        self.condition = threading.Condition()
        self.pending = []
        self.active = set()
        self.sequence = itertools.count()
        self.closed = False
        self.threads = []
        self.slots = {}
        self.desired = 0
        self.reconfigure(workers, capacity)

    def submit(self, session, func, *args, background=False, **kwargs):
        future = concurrent.futures.Future()
        future.queued_at = time.monotonic()
        with self.condition:
            if self.closed or len(self.pending) >= self.capacity:
                future.set_exception(RuntimeError("任务队列已满或正在退出，请稍后重试"))
                return future
            heapq.heappush(
                self.pending,
                (
                    int(background),
                    next(self.sequence),
                    session,
                    future,
                    func,
                    args,
                    kwargs,
                ),
            )
            self.condition.notify_all()
        return future

    def reconfigure(self, workers, capacity):
        with self.condition:
            self.desired = int(workers)
            self.capacity = int(capacity)
            for slot in range(self.desired):
                if slot not in self.slots or not self.slots[slot].is_alive():
                    thread = threading.Thread(
                        target=self._worker,
                        args=(slot,),
                        daemon=True,
                        name=f"bot-worker-{slot}",
                    )
                    self.slots[slot] = thread
                    thread.start()
            self.threads = [
                thread for thread in self.slots.values() if thread.is_alive()
            ]
            self.condition.notify_all()

    def cancel_pending(self, session):
        with self.condition:
            removed = [item for item in self.pending if item[2] == session]
            for item in removed:
                item[3].cancel()
                self.pending.remove(item)
            heapq.heapify(self.pending)
            return len(removed)

    def _worker(self, slot):
        while True:
            with self.condition:
                while True:
                    if self.closed or slot >= self.desired:
                        return
                    eligible = [
                        item
                        for item in self.pending
                        if item[2] not in self.active
                        and not any(
                            other[2] == item[2] and other[1] < item[1]
                            for other in self.pending
                        )
                    ]
                    if eligible:
                        item = min(eligible)
                        self.pending.remove(item)
                        heapq.heapify(self.pending)
                        self.active.add(item[2])
                        break
                    self.condition.wait()
            _, _, session, future, func, args, kwargs = item
            try:
                if future.set_running_or_notify_cancel():
                    future.queue_wait_ms = round((time.monotonic() - future.queued_at) * 1000)
                    if future.queue_wait_ms >= 1000:
                        # 排队时间就是"被别的会话挡住"的直接证据。只在超过 1s 时记一条，
                        # 平时不刷屏；排查"为什么这条消息半天没回"时看这个数。
                        _log_queue_wait(session, future.queue_wait_ms,
                                        background=bool(item[0]), workers=self.desired)
                    try:
                        future.set_result(func(*args, **kwargs))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                with self.condition:
                    self.active.remove(session)
                    self.condition.notify_all()

    def shutdown(self, timeout=5):
        with self.condition:
            self.closed = True
            for item in self.pending:
                item[3].cancel()
            self.pending.clear()
            self.condition.notify_all()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
