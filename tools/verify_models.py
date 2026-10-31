"""Verify configured cloud models with synthetic prompts/images only. Never prints keys."""

import argparse
import json
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import load_config
from core.brain import VLMBrain
from core.jev import JevClient
from core.harness import AgentHarness
from core.memory import MiniMemory
from core.skill_manager import SkillManager


def consume(generator):
    errors = []
    while True:
        try:
            kind, value = next(generator)
            if kind == "error":
                errors.append(str(value))
        except StopIteration as done:
            return done.value, errors


def verify():
    cfg = load_config()
    report = {
        "requested_models": {"vlm": cfg["vlm"]["model"], "jev": cfg["jev"]["model"]}
    }
    brain = VLMBrain()
    jev = JevClient()
    start = time.monotonic()
    result, errors = consume(
        brain.stream_chat_and_tools(
            [
                {"role": "system", "content": "这是连接测试，请简短回答。"},
                {"role": "system", "content": "上下文仅包含合成测试数据。"},
                {"role": "user", "content": "请只回复：连接成功。"},
            ]
        )
    )
    text, calls, usage = result
    report["vlm_stream"] = {
        "ok": not errors and bool(text) and not text.startswith("【提示"),
        "reply": text[:120],
        "elapsed_sec": round(time.monotonic() - start, 2),
        "errors": errors,
    }
    start = time.monotonic()
    try:
        decision = jev.decide_raw(
            "合成连接测试：2+2=4。这是一条普通的问候测试，不紧急。",
            {
                "probe": {"type": "noul", "instructions": "2加2等于4吗？"},
                "route": {
                    "type": "choice",
                    "instructions": "这是问候测试，请选chat。",
                    "criteria": {"chat": "普通问候", "action": "需要执行操作"},
                },
                "urgency": {
                    "type": "score",
                    "instructions": "这条消息紧急吗？",
                    "criteria": ["Low", "Normal", "Urgent"],
                },
            },
        )
        answer = decision["answers"]
        report["jev_decision"] = {
            "ok": answer["probe"]["noul"] > 0.5
            and answer["route"]["choice"] == "chat"
            and 0 <= answer["urgency"]["score"] <= 2,
            "response": decision,
            "elapsed_sec": round(time.monotonic() - start, 2),
        }
    except Exception as exc:
        report["jev_decision"] = {
            "ok": False,
            "error": str(exc),
            "elapsed_sec": round(time.monotonic() - start, 2),
        }
    try:
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (96, 96), (255, 0, 0)).save(buffer, format="JPEG")
        text = brain.chat(
            [{"role": "user", "content": "这张合成图片是什么颜色？只回答颜色名称。"}],
            image_bytes=buffer.getvalue(),
        )
        report["vlm_vision"] = {
            "ok": ("红" in text or "red" in text.lower()) and not text.startswith("【"),
            "reply": text[:120],
        }
    except ImportError:
        report["vlm_vision"] = {"ok": False, "error": "Pillow未安装"}
    with tempfile.TemporaryDirectory(prefix="minibot-model-probe-") as temp:
        # No real memories, desktop screenshots, or local action tools in the probe.
        harness = AgentHarness(
            brain,
            jev,
            MiniMemory(Path(temp) / "memory.db"),
            None,
            SkillManager(Path(temp) / "skills"),
        )
        text = harness.run(
            "你是谁", session_id="probe:identity", scope="probe", allow_tools=False
        )
        report["desktop_chat_pipeline"] = {
            "ok": bool(text)
            and not any(
                marker in text
                for marker in ("【", "未执行本轮工具", "没有返回有效回答")
            ),
            "reply": text[:240],
        }
    report["tool_roundtrip"] = verify_tool_roundtrip(brain)
    return report


def verify_tool_roundtrip(brain):
    tool = {
        "type": "function",
        "function": {
            "name": "connection_echo",
            "description": "Return a synthetic test string; no external action.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }
    messages = [
        {
            "role": "system",
            "content": "这是工具接口测试。只调用connection_echo，参数text为ping。得到结果后仅回复工具连接成功。",
        },
        {"role": "user", "content": "请使用connection_echo做一次回声测试。"},
    ]
    result, errors = consume(brain.stream_chat_and_tools(messages, tools=[tool]))
    text, calls, _ = result
    supported = (
        not errors
        and bool(calls)
        and all(c["function"]["name"] == "connection_echo" for c in calls)
    )
    if supported:
        messages.append(
            {"role": "assistant", "content": text or None, "tool_calls": calls}
        )
        for call in calls:
            arguments = json.loads(call["function"]["arguments"] or "{}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps({"echo": arguments.get("text")}),
                }
            )
        result, errors = consume(brain.stream_chat_and_tools(messages, tools=[tool]))
        text, extra_calls, _ = result
        supported = not errors and bool(text) and not extra_calls
    return {"ok": supported, "reply": text[:120], "errors": errors}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    report = verify()
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output:
        Path(args.output).write_text(serialized + "\n", encoding="utf-8")
    sys.exit(
        0
        if all(
            report[key]["ok"]
            for key in (
                "vlm_stream",
                "jev_decision",
                "vlm_vision",
                "desktop_chat_pipeline",
                "tool_roundtrip",
            )
        )
        else 1
    )
