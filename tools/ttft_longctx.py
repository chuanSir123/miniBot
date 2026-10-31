"""长上下文 + 前缀缓存命中场景下的 TTFT 复测（走真实连接池）。

对比三件事：
  1. 同入参多轮重复 -> 第 1 轮冷、第 2 轮起命中缓存，TTFT 差多少
  2. 上下文长度 8k / 16k / 32k token -> 命中后 TTFT 是否随长度线性上升
  3. 真实 HARNESS_TOOLS 规模下的增量

用法: python tools/ttft_longctx.py [每档轮数]
"""
import json
import statistics
import sys
import time

from core.brain import VLMBrain
from core.harness import HARNESS_TOOLS

FILLER = (
    "第{i}条：这是一段用于构建长上下文的固定填充文本，内容与编号都保持不变，"
    "以便前缀缓存可以逐字命中；它模拟真实会话中稳定的系统提示与历史记录。"
)


def build_messages(chars):
    """构造约 chars 字符的稳定前缀 + 一个固定问句。"""
    line_len = len(FILLER.format(i=99999))
    count = max(1, chars // line_len)
    stable = "\n".join(FILLER.format(i=i) for i in range(1, count + 1))
    system = "你是测试助手，请只回复 OK，不要解释。\n" + stable
    return [{"role": "system", "content": system}, {"role": "user", "content": "只回复 OK"}]


def run_once(brain, messages, tools):
    started = time.monotonic()
    ttft = None
    gen = brain.stream_chat_and_tools(messages, tools=tools)
    while True:
        try:
            kind, _ = next(gen)
        except StopIteration as done:
            _, _, usage = done.value
            break
        if ttft is None and kind in ("text_delta", "reasoning_delta"):
            ttft = round((time.monotonic() - started) * 1000)
    total = round((time.monotonic() - started) * 1000)
    details = (usage or {}).get("prompt_tokens_details") or {}
    prompt = (usage or {}).get("prompt_tokens")
    cached = details.get("cached_tokens", (usage or {}).get("cached_tokens"))
    hit = round(cached * 100 / prompt, 1) if isinstance(cached, int) and prompt else None
    return {"ttft_ms": ttft, "total_ms": total, "prompt_tokens": prompt,
            "cached_tokens": cached, "hit_pct": hit,
            "created_cache": (usage or {}).get("created_cache_tokens")}


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    brain = VLMBrain()
    sizes = [8000, 16000, 32000, 64000]
    print(f"每档 {rounds} 轮相同入参，tools=HARNESS_TOOLS({len(HARNESS_TOOLS)} 个)\n")

    summary = []
    for chars in sizes:
        messages = build_messages(chars)
        rows = []
        print(f"--- 前缀约 {chars:,} 字符（≈{chars // 3:,} token）---")
        for i in range(rounds):
            row = run_once(brain, messages, HARNESS_TOOLS)
            rows.append(row)
            print(json.dumps({"run": i + 1, **row}, ensure_ascii=False), flush=True)
            time.sleep(0.6)
        warm = [r["ttft_ms"] for r in rows[1:] if r["ttft_ms"]]
        summary.append({
            "chars": chars,
            "cold_ttft": rows[0]["ttft_ms"],
            "warm_p50": pct(warm, 0.5),
            "warm_min": min(warm) if warm else None,
            "warm_max": max(warm) if warm else None,
            "hit_pct": rows[-1]["hit_pct"],
            "prompt_tokens": rows[-1]["prompt_tokens"],
        })
        print()

    print("=== 汇总 ===")
    print(f"{'chars':>8} {'prompt_tk':>10} {'命中%':>7} {'冷 TTFT':>9} {'热 p50':>8} {'热 min':>8} {'热 max':>8}")
    for s in summary:
        print(f"{s['chars']:>8,} {str(s['prompt_tokens']):>10} {str(s['hit_pct']):>7} "
              f"{str(s['cold_ttft']):>9} {str(s['warm_p50']):>8} {str(s['warm_min']):>8} {str(s['warm_max']):>8}")


if __name__ == "__main__":
    main()
