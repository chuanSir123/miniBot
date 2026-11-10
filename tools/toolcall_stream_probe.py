"""工具调用流式间隔探针。

目的：验证"上游在工具调用阶段是否还流式吐分片"。
日志里出现的
    连续 3 秒未收到任何新数据切片 / stage=读取流数据 / received_bytes 只有 8KB
如果是因为上游把 tool_calls 攒成一整块再发（或文本说完后要憋几秒才发工具），
那 3 秒死流阈值就会把一次完全正常的工具调用误杀。

用法：
    python tools/toolcall_stream_probe.py            # 默认 3 轮
    python tools/toolcall_stream_probe.py 5 gap      # 5 轮，从文本说到工具的间隔视角打印
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import load_config  # noqa: E402
from core.netpool import open_request  # noqa: E402

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "draw_image",
            "description": "根据文字描述绘制并发送一张图片给用户。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "画面描述，英文"},
                    "size": {"type": "string", "description": "图片尺寸，如 832x480"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询城市天气。",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]

# 三种场景：A 先说话再画图（与线上日志一致）；B 开口就画图；C 工具参数很长。
SCENARIOS = [
    ("A 先文本后工具", "先随口说一句你现在在群里打盹的心情（一句话，别超过 30 字），"
                  "然后用 draw_image 画一只在群里打盹的猫，prompt 用英文写。必须真的调用工具。"),
    ("B 直接工具", "用 draw_image 画一只打盹的猫，prompt 用英文写。不要输出任何解释，直接调用工具。"),
    ("C 长参数", "用 draw_image 画一张细节非常丰富的图：一只猫窝在 QQ 群聊窗口里打盹，"
              "背景有点赞气泡、飘着猫毛、暖黄台灯光。把 prompt 参数写成 180~250 个英文单词的"
              "详细画面描述。直接调用工具。"),
]


def _fake_tools(n: int):
    """凑够线上同级的工具数量（线上 26 个），schema 大小接近真实工具。"""
    extra = []
    for i in range(n - len(TOOLS)):
        extra.append({
            "type": "function",
            "function": {
                "name": f"utility_{i}",
                "description": f"第 {i} 个辅助工具，用于查询/操作某个业务能力，参数与返回值见 schema。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "自然语言查询内容"},
                        "limit": {"type": "integer", "description": "返回条数上限"},
                    },
                    "required": ["query"],
                },
            },
        })
    return TOOLS + extra


def _fake_history(rounds: int):
    """造一段多轮历史，模拟线上 192 条消息的上下文体量。"""
    msgs = []
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"第{i}轮：群里有人问今天广州天气怎么样，顺便吐槽了一下加班。"})
        msgs.append({"role": "assistant", "content": f"第{i}轮回复：广州今天多云，28 度，别加班太晚，早点休息喵～"})
    return msgs


def run_once(label: str, prompt: str, idle_limit: float = 60.0,
             heavy: int = 0, tool_count: int = 2, step2: bool = False):
    cfg = load_config()
    vlm = cfg["vlm"]
    endpoint = vlm["base_url"].rstrip("/") + "/chat/completions"
    tools = _fake_tools(tool_count)
    messages = [{"role": "system", "content": "你是一个会使用工具的助手，需要画图时必须调用 draw_image。"}]
    messages.extend(_fake_history(heavy))
    if step2:
        # 复刻线上 step=2 的形态：上一轮模型已经发过 tool_call，工具也回了结果。
        messages.append({"role": "user", "content": "帮我画一张现在的状态吧"})
        messages.append({
            "role": "assistant",
            "content": "本喵这就画～",
            "tool_calls": [{
                "id": "call_prev_1",
                "type": "function",
                "function": {"name": "draw_image",
                             "arguments": json.dumps({"prompt": "a cat napping in a chat window", "size": "832x480"})},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": "call_prev_1",
            "content": json.dumps({"ok": False, "error": "图片生成超时（57s），未返回图片"}, ensure_ascii=False),
        })
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": vlm["model"],
        "messages": messages,
        "temperature": vlm.get("temperature", 0.7),
        "max_tokens": 2048,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": tools,
        "tool_choice": "auto",
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {vlm['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": "MiniBot-Harness/2.0",
        },
        method="POST",
    )

    started = time.monotonic()
    last = started
    events = []
    tool_arg_chunks = 0
    tool_first_at = None
    last_content_at = None
    content_chars = 0
    finish_reason = None
    done = False
    error = None
    # 字节账：用来和线上 received_bytes 对账，判断"工具分片到底到了几片"。
    total_bytes = 0
    bytes_at_last_content = 0
    bytes_at_first_tool = None
    frame_sizes = []
    content_frames = 0

    try:
        resp = open_request(req, timeout=idle_limit)
        while True:
            raw = resp.readline(1048576)
            now = time.monotonic()
            if not raw:
                break
            total_bytes += len(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                done = True
                events.append((now - started, now - last, "DONE", ""))
                break
            try:
                chunk = json.loads(body)
            except Exception:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                events.append((now - started, now - last, "usage", ""))
                last = now
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            kind = None
            note = ""
            if delta.get("content"):
                kind = "content"
                content_chars += len(delta["content"])
                content_frames += 1
                last_content_at = now - started
                bytes_at_last_content = total_bytes
                frame_sizes.append(len(raw))
                note = delta["content"].replace("\n", "\\n")[:28]
            elif delta.get("reasoning_content") or delta.get("reasoning"):
                kind = "reason"
                note = "…"
            if delta.get("tool_calls"):
                args_bytes = 0
                for tc in delta["tool_calls"]:
                    args = (tc.get("function") or {}).get("arguments") or ""
                    if args:
                        tool_arg_chunks += 1
                        args_bytes += len(args)
                    if tool_first_at is None:
                        tool_first_at = now - started
                        bytes_at_first_tool = total_bytes
                kind = "content+tool" if kind == "content" else "tool"
                note = f"args+{args_bytes}B"
            if finish_reason and kind is None:
                kind = "finish"
                note = str(finish_reason)
            if kind:
                events.append((now - started, now - last, kind, note))
            last = now
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    total = time.monotonic() - started
    gaps = [(t, dt, k, n) for (t, dt, k, n) in events]
    big = [e for e in gaps if e[1] >= 3.0]
    max_gap = max((e[1] for e in gaps), default=0.0)
    tool_gaps = [dt for (t, dt, k, n) in gaps if "tool" in k]
    max_tool_gap = max(tool_gaps[1:], default=0.0)  # 第一片是从上一阶段过来的
    tool_gap = (tool_first_at - last_content_at) if (tool_first_at is not None and last_content_at is not None) else None

    print(f"\n=== {label} ===")
    print(f"  总耗时 {total:.2f}s  content {content_chars} 字 / {tool_arg_chunks} 个工具参片  finish={finish_reason} done={done} err={error}")
    print(f"  TTFT(首个内容) {next((t for t, _, k, _ in events if k in ('content', 'tool', 'content+tool')), None)}")
    if tool_first_at is not None:
        print(f"  首个 tool_calls 分片 @ {tool_first_at:.2f}s"
              + (f"   ← 距最后一段文本 {tool_gap:.2f}s" if tool_gap is not None else ""))
    print(f"  全程最大空档 {max_gap:.2f}s / 工具参数内部最大空档 {max_tool_gap:.2f}s")
    print(f"  单帧间隔 ≥3s 的次数: {len(big)}")
    avg_frame = (sum(frame_sizes) / len(frame_sizes)) if frame_sizes else 0
    print(f"  字节账: 末个 content 片累计 {bytes_at_last_content}B / 首个 tool 片累计 "
          f"{bytes_at_first_tool}B / 全程 {total_bytes}B；content 帧均 {avg_frame:.0f}B（{content_frames} 帧）")
    for t, dt, k, n in big:
        print(f"    ⚠ @{t:.2f}s 前停 {dt:.2f}s  [{k}] {n}")
    tail = events[-6:]
    print("  尾部时间线:")
    for t, dt, k, n in tail:
        print(f"    @{t:7.2f}s (+{dt:5.2f}s) {k:12s} {n}")
    return {
        "label": label,
        "tool_gap": tool_gap,
        "max_gap": max_gap,
        "max_tool_gap": max_tool_gap,
        "tool_arg_chunks": tool_arg_chunks,
        "big_gaps": [(round(t, 2), round(dt, 2)) for t, dt, _, _ in big],
        "error": error,
        "done": done,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    rounds = int(args[0]) if args else 3
    heavy = int(next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--heavy=")), 0))
    tools_n = int(next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--tools=")), 0)) or len(TOOLS)
    step2 = "--step2" in sys.argv
    results = []
    for i in range(rounds):
        for label, prompt in SCENARIOS:
            tag = f"{label} #{i + 1}"
            if heavy:
                tag += f" [历史{heavy * 2 + 2}条/工具{tools_n}]"
            if step2:
                tag += " [step2]"
            results.append(run_once(tag, prompt, heavy=heavy, tool_count=tools_n, step2=step2))
    print("\n===== 汇总 =====")
    for r in results:
        gap = f"{r['tool_gap']:.2f}s" if r["tool_gap"] is not None else "n/a"
        print(f"{r['label']:16s} 文本→工具 {gap:>7s}  参片 {r['tool_arg_chunks']:3d}  "
              f"最大空档 {r['max_gap']:5.2f}s (工具内 {r['max_tool_gap']:5.2f}s)  "
              f"{r['error'] or ''}")


if __name__ == "__main__":
    main()
