"""统计最小请求的耗时分布：热态 vs 空闲后冷态，直连 vs 本地代理。"""
import json
import statistics
import sys
import time

from tools.latency_probe import run_direct, run_proxy


def pct(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def report(rows, label):
    totals = [r["total"] for r in rows]
    ttfbs = [r.get("ttfb", 0) for r in rows]
    slow = [t for t in totals if t > 3000]
    print(f"\n=== {label} ({len(rows)} 次) ===")
    print(f"total: avg {int(statistics.mean(totals))}  p50 {pct(totals,50)}  p90 {pct(totals,90)}  min {min(totals)}  max {max(totals)}")
    print(f"ttfb : avg {int(statistics.mean(ttfbs))}  p50 {pct(ttfbs,50)}  p90 {pct(ttfbs,90)}  min {min(ttfbs)}  max {max(ttfbs)}")
    print(f"超过 3s 的次数: {len(slow)}/{len(rows)}")
    colos = {}
    for r in rows:
        colos[r.get("colo", "?")] = colos.get(r.get("colo", "?"), 0) + 1
    print(f"落点分布: {colos}")


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 7890

    direct = [run_direct()[0] for _ in range(rounds)]
    proxied = [run_proxy("127.0.0.1", port)[0] for _ in range(rounds)]
    report(direct, "直连（热态）")
    report(proxied, f"本地代理 {port}（热态）")

    print("\n空闲 60 秒，测冷态首包……", flush=True)
    time.sleep(60)
    cold = run_direct()[0]
    print(f"冷态首包 total {cold['total']}ms  ttfb {cold['ttfb']}ms  dns {cold['dns']}ms  tls {cold['tls']}ms  colo {cold['colo']}")


if __name__ == "__main__":
    main()
