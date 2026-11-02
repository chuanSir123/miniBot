"""探测 Jev (System One) 是否有前缀缓存，以及 state / instructions 谁在前。

只用耗时和返回字段判断，不改线上代码。用法：
    PYTHONPATH=. python tools/jev_cache_probe.py
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, ".")

from core.config import load_config
from core.netpool import open_request

CFG = load_config()["jev"]
BASE = CFG["base_url"].rstrip("/")
KEY = CFG["api_key"].strip()
MODEL = CFG.get("model") or "vllm-sr/d3-turbo"

INS = ("结合发言人QQ号、机器人身份与近期群聊，判断是否需要回复当前消息。"
       "recent_chat 是按时间先后排列的聊天历史；current_message 是当前这条消息。"
       "bot_self_qq 是本机号码；current_message.at_targets 是这条消息 @ 的对象，"
       "is_this_bot 表示是不是机器人自己。")


def filler(lines, tag=""):
    """造一段可复现的长文本，用来把 prompt 撑到能被缓存观察的体积。"""
    out = []
    for i in range(lines):
        out.append({"role": "user", "from": f"用户{i}", "user_id": str(100000 + i),
                    "group_role": "成员", "text": f"{tag}第{i}句：今天这破群真热闹，"
                    f"有人在讨论模型上下文长度和调度策略，说得很长很长。"})
    return out


def build(head_tag="", tail_tag="", n=220):
    """head_tag 改的是 state 开头，tail_tag 改的是 state 结尾。"""
    hist = filler(n, "固定")
    if head_tag:
        hist[0]["text"] = head_tag + hist[0]["text"]
    cur = {"user_id": "416448943", "owner": True, "at_targets": [],
           "text": (tail_tag or "") + "每天早上 9 点 给我点赞 50 次"}
    return {"recent_chat": hist, "current_message": cur,
            "bot_self_qq": "178108758",
            "persona_names": ["喵喵", "猫娘", "小猫", "喵儿"],
            "called_persona_name": False}


def call(state, instructions=INS, label=""):
    payload = {"model": MODEL,
               "state": json.dumps(state, ensure_ascii=False),
               "questions": {"reply": {"type": "noul", "instructions": instructions}}}
    req = urllib.request.Request(
        BASE if BASE.endswith("/systemone") else BASE + "/v1/systemone",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        method="POST")
    t0 = time.monotonic()
    with open_request(req, timeout=60) as resp:
        body = json.load(resp)
    ms = round((time.monotonic() - t0) * 1000)
    usage = body.get("usage", {})
    print(f"{label:36s} {ms:5d}ms  usage={json.dumps(usage, ensure_ascii=False)}")
    return ms, usage


def main():
    base = build()
    size = len(json.dumps(base, ensure_ascii=False))
    print(f"state={size} chars, model={MODEL}\n")

    print("== 1. 完全相同的请求连发 4 次（看有没有缓存） ==")
    warm = [call(base, label=f"same #{i+1}")[0] for i in range(4)]

    print("\n== 2. 只改 state 结尾（模拟新消息） ==")
    for i in range(3):
        call(build(tail_tag=f"变体{i}"), label=f"tail-changed #{i+1}")

    print("\n== 3. 只改 state 开头 ==")
    for i in range(3):
        call(build(head_tag=f"变体{i}"), label=f"head-changed #{i+1}")

    print("\n== 4. state 不变，只改 instructions 开头 ==")
    for i in range(3):
        call(base, instructions=f"（变体{i}）" + INS, label=f"ins-changed #{i+1}")

    print("\n== 5. 把 instructions 挪到 state 之后… 无法控制，仅作基线 ==")
    for i in range(3):
        call(build(tail_tag=f"再变{i}"), label=f"tail-again #{i+1}")


if __name__ == "__main__":
    main()
