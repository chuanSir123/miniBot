"""记录 Jev 各调用点的实际 input 与分数，用于改动前后对照。

不改线上代码，只读。
    PYTHONPATH=. python tools/jev_input_baseline.py
"""
import json
import sys

sys.path.insert(0, ".")

import core.jev as J
from core.jev import JevClient
from core.qq_policy import QQPolicy


class FakeMem:
    def model_chat_snapshot(self, scope):
        return ([
            {"role": "user", "message_id": 900, "speaker": {"user_id": "399705062", "nickname": "Xrala", "group_role_label": "成员"},
             "text": "[@2149968416] 好的，我将每天在凌晨三到四点准时为你拨打 QQ 电话骚扰你"},
            {"role": "user", "message_id": 901, "speaker": {"user_id": "1243437968", "nickname": "绵羊", "group_role_label": "成员"},
             "text": "哈哈凌晨三四点电话服务 阴间客服是吧"},
            {"role": "user", "message_id": 902, "speaker": {"user_id": "416448943", "nickname": "BoaT", "group_role_label": "成员"},
             "text": "每天早上 9 点 给我点赞 50 次"},
        ], None, 0)

    def qq_strike(self, uid):
        return 0

    def qq_block(self, *a, **k):
        pass

    def get_task_state(self, *a, **k):
        return {}


def show(label, state, questions):
    """打印 state 的键顺序 + 实际分数。"""
    keys = list(state) if isinstance(state, dict) else ["<str>"]
    payload = json.dumps(state, ensure_ascii=False)
    print(f"\n--- {label} ---")
    print(f"state 键顺序: {keys}   长度 {len(payload)}")


def main():
    jev = JevClient()
    policy = QQPolicy(FakeMem(), jev)
    base_ev = {"message_type": "group", "group_id": 473923892, "user_id": "416448943",
               "self_id": "178108758", "sender": {"nickname": "BoaT", "card": ""}}

    cases = [
        ("@别人原句", "[@1243437968] 每天早上 9 点 给我点赞 50 次", [{"qq": "1243437968", "name": "绵羊"}], "416448943"),
        ("主人指令不@人", "每天早上 9 点 给我点赞 50 次", [], "416448943"),
        ("群友闲聊", "今天这破群真热闹", [], "399705062"),
    ]
    # 抓请求体
    captured = {}

    def spy(state, questions, images=None):
        captured["state"] = json.loads(state) if isinstance(state, str) else state
        captured["questions"] = questions
        return orig(state, questions, images)

    for label, direct, at, uid in cases:
        orig = jev.decide_raw
        jev.decide_raw = spy
        ev = dict(base_ev, message_id=1, user_id=uid, _parsed={"direct_text": direct, "at_targets": at})
        verdict = policy.decide_reply(direct, ev)
        jev.decide_raw = orig
        print(f"\n=== reply.gate / {label} ===")
        print("state 键顺序:", list(captured["state"]))
        print("questions 顺序:", list(captured["questions"]))
        print("verdict:", verdict, " last_confidence:", jev.last_confidence)

    # select_fixed_action
    for prompt in ("发最后一张图", "把文件改一下"):
        orig = jev.decide_raw
        jev.decide_raw = spy
        action = jev.select_fixed_action(prompt, context={"recent_media": [{"media_ref": "m1", "mime": "image/jpeg"}]})
        jev.decide_raw = orig
        print(f"\n=== select_fixed_action / {prompt} ===")
        print("state 键顺序:", list(captured["state"]))
        print("state:", json.dumps(captured["state"], ensure_ascii=False)[:300])
        print("action:", action)

    # classify_steering_intent
    for prompt in ("继续", "另外查一下天气"):
        orig = jev.decide_raw
        jev.decide_raw = spy
        intent = jev.classify_steering_intent(prompt, task_state={"original_goal": "改一个登录页", "status": "active"})
        jev.decide_raw = orig
        print(f"\n=== classify_steering_intent / {prompt} ===")
        print("state 键顺序:", list(captured["state"]))
        print("state:", json.dumps(captured["state"], ensure_ascii=False)[:300])
        print("intent:", intent)


if __name__ == "__main__":
    main()
