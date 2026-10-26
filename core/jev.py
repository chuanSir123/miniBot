import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from core.config import load_config


class JevClient:
    """
    快脑 (System 1) - 基于 BambooNode Jev 决策模型
    核心原语:
      - Noul: 真假概率判断 (0.0 ~ 1.0)
      - Choice: 多候选项决策 (选择最佳动作/Skill/姿态)
      - Score: 关键度与紧急度打分
      
    针对提示词敏感性进行了深度 Prompt Engineering，包含详尽的正反例边界规则。
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None):
        cfg = load_config().get("jev", {})
        self.base_url = (base_url or cfg.get("base_url") or "https://api.bamboonode.dev/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "startlux-decision-27b-autothink"

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def decide(self, state: str, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """调用 BambooNode /v1/decisions 接口"""
        if not self.is_configured():
            return {}

        endpoint = f"{self.base_url}/decisions" if not self.base_url.endswith("/decisions") else self.base_url
        payload = {
            "model": self.model,
            "state": state,
            "questions": questions
        }

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-Jev/2.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                if "answers" in resp_json:
                    return resp_json["answers"]
                return resp_json
        except Exception as e:
            print(f"[Jev] Decision query error: {e}")
            return {}

    # ==========================================================
    # 场景 1: 主动打扰门控 (Noul) - 严格边界提示词
    # ==========================================================
    def evaluate_proactive_gate(self, state_summary: str, threshold: float = 0.70) -> Tuple[bool, float]:
        """
        裁决当前状态下，桌宠是否应当主动插话/提醒用户。
        内置详尽的 Positive (必须打扰) 与 Negative (严禁打扰) 判定规则。
        """
        if not self.is_configured():
            return False, 0.0

        instructions = (
            "【角色】你是一个极度克制、不讨人嫌的桌宠主动交互门控器。\n"
            "【任务】评估当前是否必须立即主动发声提醒或协助用户。\n\n"
            "【判定为 TRUE 的触发条件（满足其一即可考虑打扰）】:\n"
            "1. 异常报错停滞：用户剪贴板包含明显代码报错/异常堆栈 (Traceback, Error, Exception)，且当前已停顿超过 30 秒无操作。\n"
            "2. 疲劳久坐：用户无中断连续工作/按键时长累计超过 45 分钟，需要健康提醒。\n"
            "3. 挂机归来：用户空闲(AFK)超过 10 分钟后刚回到电脑前，有待办事项需同步。\n"
            "4. 深夜关怀：当前时间处于深夜 (23:30 ~ 05:00) 且用户仍在进行密集操作。\n"
            "5. 定时提醒脉冲：用户之前预设的定时提醒时间已到达。\n\n"
            "【判定为 FALSE 的一票否决规则（只要符合任何一条，必须为 FALSE）】:\n"
            "1. 用户正在全神贯注高速打字或高频操作 (空闲时间 < 15 秒)，除非遇到致命报错，否则严禁打断思路！\n"
            "2. 用户当前正在全屏游戏、全屏观看视频或音视频会议中。\n"
            "3. 距上次主动提醒的时间间隔过短 (处于冷却期内)。\n"
            "4. 当前只是正常的日常操作，没有任何异常、疲劳或待办事项。"
        )

        questions = {
            "should_intervene": {
                "type": "noul",
                "instructions": instructions
            }
        }
        res = self.decide(state_summary, questions)
        raw_val = res.get("should_intervene", 0.0)

        prob = 1.0 if raw_val is True else (0.0 if raw_val is False else float(raw_val or 0.0))
        return (prob >= threshold), prob

    # ==========================================================
    # 场景 2: 主动提醒姿态分类 (Choice) - 丰富的情境动作
    # ==========================================================
    def choose_proactive_posture(self, state_summary: str) -> str:
        """
        根据采集到的丰富多模态信息 (窗口进程、键盘空闲、剪贴板报错、本地时间)，
        精准裁定当前提醒的姿态类型。
        """
        if not self.is_configured():
            return "ambient_silent_watch"

        options = [
            "debug_assist",              # 发现代码/软件报错，主动提供排查线索
            "sedentary_rest_reminder",   # 连续久坐办公超时，提醒伸懒腰喝水
            "afk_return_greeting",       # 挂机离开超10分钟刚回来，简要欢迎并总结状态
            "late_night_care",           # 深夜还在熬夜敲代码，温和关怀提醒休息
            "distraction_gentle_nudge",  # 在工作时间突然频繁切到摸鱼/娱乐/视频软件，温和拉回焦点
            "meal_time_checkin",         # 处于正餐时间点 (12:00 或 18:00 前后)，提醒按时干饭
            "ambient_silent_watch"       # 不适宜主动打扰，保持静默，只做记忆记录
        ]

        instructions = (
            "【任务】根据当前桌面状态上下文，从候选姿态中选出最契合当前环境的主动提醒分类：\n"
            "- debug_assist: 剪贴板或窗口标题出现明确的代码异常/报错/Traceback/崩溃。\n"
            "- sedentary_rest_reminder: 连续活跃时间 > 45分钟，键鼠未见明显离开。\n"
            "- afk_return_greeting: 用户空闲时长 > 10分钟后刚出现动作。\n"
            "- late_night_care: 当前时钟在 23:30 ~ 05:00 之间。\n"
            "- distraction_gentle_nudge: 从工作 IDE 频繁跳去 Bilibili/微博/Steam/游戏。\n"
            "- meal_time_checkin: 处于 11:45~12:30 或 18:00~19:00 正餐时段。\n"
            "- ambient_silent_watch: 场景无特殊异常或无足够把握，选择静默。"
        )

        questions = {
            "posture": {
                "type": "choice",
                "instructions": instructions,
                "options": options
            }
        }
        res = self.decide(state_summary, questions)
        chosen = res.get("posture")
        return chosen if chosen in options else "ambient_silent_watch"

    # ==========================================================
    # 场景 3: Harness 任务意图初筛 (Choice) - 70ms 区分闲聊 vs 任务
    # ==========================================================
    def classify_harness_intent(self, user_prompt: str) -> str:
        """
        70ms 判定用户输入是纯粹日常闲聊 (pure_chat) 还是需要工具系统操作 (need_action_or_tools)
        """
        if not self.is_configured():
            return "need_action_or_tools"

        options = ["pure_chat", "need_action_or_tools"]
        instructions = (
            "【任务】分析用户输入的目标意图类型：\n"
            "1. 'pure_chat': 纯日常打招呼 ('你好', '早安')、闲聊发牢骚、情绪倾诉、哲学常识问答、简单文本润色，完全无需动用终端、无需读写本地文件或观察桌面。\n"
            "2. 'need_action_or_tools': 涉及本地环境操作的任务，包括：查看/杀死进程、执行命令行、读写本地代码/文档、分析桌面截屏、排查报错、创建/执行技能。"
        )

        questions = {
            "intent_type": {
                "type": "choice",
                "instructions": instructions,
                "options": options
            }
        }
        res = self.decide(f"用户输入文本: {user_prompt}", questions)
        chosen = res.get("intent_type")
        return chosen if chosen in options else "need_action_or_tools"

    # ==========================================================
    # 场景 4: Harness 工具执行结果快速终结 (Noul) - 100ms 省去慢脑 3-5 秒
    # ==========================================================
    def evaluate_task_completion(self, goal: str, tool_summary: str, threshold: float = 0.82) -> Tuple[bool, float]:
        """
        工具执行后，以 100ms 快速研判用户目标是否已彻底解决完毕，避免盲目空转下一轮慢脑。
        """
        if not self.is_configured():
            return False, 0.0

        instructions = (
            "【任务】严格评估最新工具的执行输出是否已经彻底解决了用户的最初目标。\n\n"
            "【判定为 TRUE 的准则】:\n"
            "1. 用户的查询目标已经在工具输出中直接得到了明确答案 (如查看版本、查看目录、端口查询返回了明确结果)。\n"
            "2. 执行修改/创建/运行命令已成功完成 (退出码为 0，无任何错误堆栈)。\n\n"
            "【判定为 FALSE 的准则】:\n"
            "1. 工具执行报错、超时或返回非 0 退出码 (如 Command not found, Permission denied, SyntaxError)。\n"
            "2. 工具输出仅解决了复合任务的第一部分，仍有后续步骤未执行。\n"
            "3. 结果存在歧义或需要进一步视觉比对/验证。"
        )

        state = f"用户最初目标: {goal}\n最新工具执行反馈: {tool_summary}"
        questions = {
            "is_complete": {
                "type": "noul",
                "instructions": instructions
            }
        }
        res = self.decide(state, questions)
        raw_val = res.get("is_complete", 0.0)

        prob = 1.0 if raw_val is True else (0.0 if raw_val is False else float(raw_val or 0.0))
        return (prob >= threshold), prob

    # ==========================================================
    # 场景 5: 技能意图路由 (Choice) - 支持无参直驱与有参槽位提取
    # ==========================================================
    def route_skill_intent(self, user_prompt: str, candidate_options: List[str]) -> str:
        """
        从候选技能列表及兜底项中选出最优路由项
        """
        if not self.is_configured() or not candidate_options:
            return "chat_only"

        instructions = (
            "【任务】根据用户输入的提问或指令，判断是否应当直接调用某项专业技能。\n"
            "如果用户的需求完全符合某项技能的描述，选择对应的 skill 名称；\n"
            "如果都不符合或者属于自由问答，必须选择 'chat_only'。"
        )

        questions = {
            "routed_skill": {
                "type": "choice",
                "instructions": instructions,
                "options": candidate_options
            }
        }
        res = self.decide(f"用户需求: {user_prompt}", questions)
        chosen = res.get("routed_skill")
        return chosen if chosen in candidate_options else "chat_only"
