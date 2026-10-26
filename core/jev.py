import json
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

from core.config import load_config


class JevClient:
    """
    快脑 (System 1) - 基于 BambooNode Jev 决策模型
    核心原语:
      - Noul: 真假概率判断 (0.0 ~ 1.0)
      - Choice: 多候选项决策 (选择最佳动作/Skill)
      - Score: 关键度与紧急度打分
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, model: Optional[str] = None):
        cfg = load_config().get("jev", {})
        self.base_url = (base_url or cfg.get("base_url") or "https://api.bamboonode.dev/v1").rstrip("/")
        self.api_key = api_key or cfg.get("api_key") or ""
        self.model = model or cfg.get("model") or "startlux-decision-27b-autothink"

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def decide(self, state: str, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        调用 BambooNode /v1/decisions 接口
        :param state: 描述当前用户环境、操作上下文、旁听流信息
        :param questions: 问题原语字典，如:
               {
                 "should_intervene": {"type": "noul", "instructions": "Should we interrupt user?"},
                 "action": {"type": "choice", "options": ["remind", "silent", "code_assist"]}
               }
        :return: {"should_intervene": 0.85, "action": "code_assist", ...}
        """
        if not self.is_configured():
            return {}

        endpoint = f"{self.base_url}/decisions" if not self.base_url.endswith("/decisions") else self.base_url
        payload = {
            "model": self.model,
            "state": state,
            "questions": questions
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MiniBot-Jev/1.0"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
                # BambooNode Jev 接口通常返回 answers 或 direct fields
                if "answers" in resp_json:
                    return resp_json["answers"]
                return resp_json
        except Exception as e:
            print(f"[Jev] Decision query error: {e}")
            return {}

    def should_intervene(self, context_summary: str, threshold: float = 0.65) -> Tuple[bool, float]:
        """
        使用 Noul 原语快速判定是否需要在此刻主动唤醒桌宠并介入用户
        """
        if not self.is_configured():
            return False, 0.0

        questions = {
            "intervene": {
                "type": "noul",
                "instructions": "Based on the user's desktop state, should the assistant proactively speak up or remind the user right now? Answer True only if it's genuinely helpful and not annoying."
            }
        }
        res = self.decide(context_summary, questions)
        score = res.get("intervene", 0.0)

        # 归一化处理（支持 bool 或 float）
        if isinstance(score, bool):
            prob = 1.0 if score else 0.0
        elif isinstance(score, (int, float)):
            prob = float(score)
        else:
            prob = 0.0

        return (prob >= threshold), prob

    def choose_action(self, context_summary: str, candidate_actions: List[str]) -> str:
        """
        使用 Choice 原语快速从动作/Skill 候选项中选择最优项
        """
        if not self.is_configured() or not candidate_actions:
            return candidate_actions[0] if candidate_actions else "none"

        questions = {
            "selected_action": {
                "type": "choice",
                "instructions": "Given the user's context, which action or skill is most appropriate?",
                "options": candidate_actions
            }
        }
        res = self.decide(context_summary, questions)
        chosen = res.get("selected_action")
        if chosen in candidate_actions:
            return chosen
        return candidate_actions[0]
