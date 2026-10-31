"""量 dispatcher 的跨会话排队：多少个会话能同时跑，第 N 个要等多久。

不涉及模型调用，只测调度层：提交 4 个不同会话的任务，每个占住 worker 2 秒，
看每个任务的排队时间。用来回答"web 两个空间 / QQ 两个发送人会不会互相挡"。
"""
import sys
import time

sys.path.insert(0, ".")

from core.runtime import Dispatcher


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    sessions = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    hold_sec = 2.0

    dispatcher = Dispatcher(workers, 16)

    def work(label):
        time.sleep(hold_sec)
        return label

    started = time.monotonic()
    futures = []
    for i in range(sessions):
        futures.append(dispatcher.submit(f"session-{i}", work, f"s{i}"))

    for i, future in enumerate(futures):
        value = future.result()
        print("  %s 完成 排队 %5d ms  总耗时 %5d ms"
              % (value, future.queue_wait_ms, round((time.monotonic() - started) * 1000)),
              flush=True)

    print("\n结论：%d 个 worker，%d 个会话并发提交，第 %d 个会话起开始排队。"
          % (workers, sessions, workers + 1))
    dispatcher.shutdown(timeout=1)


if __name__ == "__main__":
    main()
