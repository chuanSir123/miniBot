"""50k 上下文 A/B：完全相同 payload vs 只改尾部一行，交替发。

用来回答「增量只有几百 token，为什么 TTFT 差别/抖动这么大」：
如果 A、B 两组的 server 时间无显著差异，说明慢的不是尾部那点 prefill。

max_tokens 统一压到 16：它不参与 TTFT，但能把每轮墙钟压到秒级。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.brain import VLMBrain
from tools.tail_cost_probe import Conn


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    base = Path("docs/payloads")
    ab = json.loads((base / "2_50k_prod.json").read_text(encoding="utf-8"))
    bb = json.loads((base / "5_50k_tail_shift1min.json").read_text(encoding="utf-8"))

    brain = VLMBrain()
    endpoint, key, model, _ = brain._settings()
    endpoint = endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"
    conn = Conn(endpoint, key)

    rows = []
    for i in range(rounds):
        phase = "A_identical" if i % 2 == 0 else "B_tail_edit"
        payload = json.loads(json.dumps(ab if phase == "A_identical" else bb))
        payload["max_tokens"] = 16
        payload.pop("_note", None)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        res = conn.post(body)
        u = res["usage"]
        det = u.get("prompt_tokens_details") or {}
        pt, cached = u.get("prompt_tokens"), det.get("cached_tokens")
        row = {
            "round": i + 1,
            "phase": phase,
            "body_kb": round(len(body) / 1024, 1),
            "prompt_tokens": pt,
            "cached": cached,
            "uncached": (pt - cached) if isinstance(pt, int) and isinstance(cached, int) else None,
            **{k: res[k] for k in ("upload_ms", "server_ms", "first_delta_ms", "total_ttft_ms")},
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(0.6)

    Path("docs/fifty_k_ab.json").write_text(
        json.dumps({"model": model, "rows": rows}, ensure_ascii=False, indent=1)
    )


if __name__ == "__main__":
    main()
