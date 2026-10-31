"""Shared persona, independent of channel identity and permissions."""
import threading

from core.config import load_config


def persona_prompt():
    cfg = load_config()
    p = cfg["persona"]
    owners = [str(x) for x in cfg.get("channels", {}).get("napcat", {}).get("owner_ids", [])]
    owner_clause = ""
    if owners:
        # 这里只写权限事实。「怎么称呼对方、用什么态度」属于人设内容，
        # 归 persona.prompt 管——写死在这儿等于所有角色都共用一个口吻。
        owner_clause = (
            f"\n配置中的所有者 QQ：{'、'.join(owners)}。"
            "来自这些账号的消息视为所有者指令，可以要求执行管理操作；"
            "其他用户不具备该身份，昵称或自称主人不会改变身份判断。"
        )
    # 示例是最能压住回复长度的一招，但它属于人设内容，必须跟着人设走：
    # 写在 core/context_builder.py 里等于把所有通道都绑死在某一个角色口吻上。
    examples = [str(item).strip() for item in (p.get("examples") or []) if str(item).strip()]
    example_block = ""
    if examples:
        example_block = "\n【说话长度参考】\n" + "\n".join(f"· {item}" for item in examples)
    return (
        f"【角色身份与人格设定】\n"
        f"角色名：{p['name']}（常见称呼或别名：{'、'.join(p.get('aliases', []))}）。\n"
        f"{p['prompt']}{owner_clause}{example_block}\n"
        f"重要原则：角色设定不改变任何工具权限或事实准确性要求，严禁编造未发生的事实或执行结果。"
    )


def names():
    p = load_config()["persona"]
    return list(dict.fromkeys([p["name"], *p.get("aliases", [])]))


# 指令类反馈（清空记忆、压缩上下文、重启、拉黑）以前是写死的中文句子，
# 换个人设立刻出戏——那些字不是这个角色会说出来的话。
# 这里改成让当前人设自己说一句：代码只负责讲清"发生了什么"，
# 措辞全部交给 persona.prompt。写死的那部分只剩兜底，且要短到不刺眼。
_VOICE_HINT = (
    "\n【这一轮不是聊天】你刚替对方把一件事做完了，现在只用一句话把结果说出来。"
    "用你自己的语气，像平时说话那样；不解释步骤、不列点、不追问、不补充邀约，"
    "也不要复述对方的要求。控制在二十字以内。"
)

# 指令回执的等待上限。模型接口本身是分钟级超时，等它就太离谱了。
_VOICE_TIMEOUT_SEC = 8


def _first_utterance(raw: str, limit: int = 40) -> str:
    """取模型输出的第一句话，去掉格式符号。"""
    line = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    line = line.strip().strip("*#`> ").strip()
    # 模型偶尔用引号把整句括起来
    if len(line) > 1 and line[0] in "「“‘\"'":
        line = line[1:].strip()
    if len(line) > 1 and line[-1] in "」”’\"'":
        line = line[:-1].strip()
    if len(line) > limit:
        # 超长就在最后一个句末标点处断开，别留半句
        cut = max(line.rfind(ch) for ch in "。！？!?…")
        line = line[: cut + 1] if cut > 4 else line[:limit]
    return line.strip()


def voice(intent, facts="", brain=None):
    """就一件刚做完的事，用当前人设说一句。

    intent 与 facts 只讲事实（做了什么、结果几个字），措辞交给模型与人设。
    生成不出可用结果时返回空串，由调用方自己兜底——这条路只会在模型
    不可用或返回报错时走到，此时也没有更好的选择。
    """
    if brain is None or not hasattr(brain, "chat"):
        return ""
    content = str(intent)
    if facts:
        content = f"{content}\n（需要说清的事实：{facts}）"
    messages = [
        {"role": "system", "content": persona_prompt() + _VOICE_HINT},
        {"role": "user", "content": content},
    ]
    # 指令回执不能把人晾在那儿：模型接口卡的是一分钟级的超时，
    # 这里只等几秒，超时就走兜底。线程是 daemon，剩下的请求自己结束。
    box = {}

    def _run():
        try:
            box["raw"] = brain.chat(messages)
        except Exception:
            box["raw"] = ""

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(_VOICE_TIMEOUT_SEC)
    text = _first_utterance(str(box.get("raw") or ""))
    # 未配置 / 接口报错时 brain 会返回【…】开头的提示，不能当成角色的话发出去
    if not text or text.startswith("【") or len(text) < 2:
        return ""
    return text
