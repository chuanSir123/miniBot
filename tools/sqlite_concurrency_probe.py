"""验证：多线程同时对同一个 SQLite 库读写，会不会出现 readonly 报错。

背景：把压缩挪到后台线程后，滚动压缩测试出现约 50% 的偶发
``sqlite3.OperationalError: attempt to write a readonly database``。
这里脱离业务逻辑，只测"每次操作新建连接"这个模式本身。
"""
import sys
import threading
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from core.memory import MiniMemory


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    errors = []
    for attempt in range(rounds):
        root = Path(tempfile.mkdtemp())
        mem = MiniMemory(root / "memory.db")
        sid = "probe"
        for i in range(6):
            mem.append_message(sid, {"role": "user", "content": "seed %d" % i})

        stop = threading.Event()
        hits = []

        def reader():
            while not stop.is_set():
                try:
                    mem.get_history(sid)
                except Exception as exc:
                    hits.append(("read", type(exc).__name__, str(exc)[:60]))
                    return

        def writer():
            for i in range(120):
                try:
                    mem.append_message(sid, {"role": "user", "content": "w%d" % i})
                except Exception as exc:
                    hits.append(("write", type(exc).__name__, str(exc)[:60]))
                    return
                if i % 20 == 0:
                    mem.save_task_state(sid, {"i": i})

        threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=writer)]
        for t in threads:
            t.start()
        threads[1].join()
        stop.set()
        threads[0].join(timeout=2)
        errors.extend(hits)
        print("attempt %d: %s" % (attempt + 1, hits or "clean"), flush=True)

    print("\n总错误:", len(errors))


if __name__ == "__main__":
    main()
