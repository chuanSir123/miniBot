import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.brain import VLMBrain
from core.config import load_config
from core.jev import JevClient
from core.memory import MiniMemory
from core.perception import LocalPerception
from core.skill_manager import SkillManager


class ProactiveEngine:
    """
    主动协同引擎 (类 Grok Bot 主动交互)
    三轴驱动：
      1. 旁听流触发 (窗口切换、异常报错剪贴板)
      2. 延时脉冲与记忆驱动 (意图定时器到期)
      3. 环境与心跳脉冲 (Jev 快脑毫秒级裁决是否打扰)
    """

    def __init__(
        self,
        memory: MiniMemory,
        perception: LocalPerception,
        jev: JevClient,
        vlm: VLMBrain,
        skill_manager: SkillManager,
        notify_callback: Optional[Callable[[str, str], None]] = None
    ):
        self.memory = memory
        self.perception = perception
        self.jev = jev
        self.vlm = vlm
        self.skill_manager = skill_manager
        self.notify_callback = notify_callback  # (text, action_type) -> 输出给桌宠气泡或QQ

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_win_title = ""
        self._last_intervene_time = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print("[Proactive] 主动协同脉冲引擎已启动")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        print("[Proactive] 主动协同脉冲引擎已停止")

    def _run_loop(self):
        while self._running:
            cfg = load_config().get("proactive", {})
            interval = cfg.get("heartbeat_interval_sec", 30)

            try:
                self._tick(cfg)
            except Exception as e:
                print(f"[Proactive] Tick error: {e}")

            # 睡眠等待下一个脉冲
            for _ in range(int(interval)):
                if not self._running:
                    break
                time.sleep(1)

    def _tick(self, cfg: Dict[str, Any]):
        now = time.time()

        # 1. 检查延时意图队列 (定时提醒)
        due_intents = self.memory.pop_due_intents()
        for intent in due_intents:
            prompt = intent.get("action_prompt", "")
            print(f"[Proactive] 延时意图唤醒: {prompt}")
            reply = self.vlm.generate_bubble_reply(f"执行预定的定时提醒任务：{prompt}", context="延时意图到期")
            self._trigger_notification(reply, "intent_reminder")
            return

        # 2. 旁听流感知 - 窗口变动
        if cfg.get("enable_window_listener", True):
            win_info = self.perception.get_active_window()
            cur_title = win_info.get("title", "")
            cur_proc = win_info.get("process", "")

            if cur_title and cur_title != self._last_win_title:
                self._last_win_title = cur_title
                # 记录情景流
                self.memory.add_event(f"切换至窗口: [{cur_proc}] {cur_title}", source="window", importance=1.0)

        # 3. 旁听流感知 - 剪贴板监听
        if cfg.get("enable_clipboard_listener", True):
            clip_text = self.perception.check_clipboard_change()
            if clip_text:
                is_error = any(kw in clip_text for kw in ["Traceback", "Error:", "Exception", "failed", "errno"])
                imp = 2.5 if is_error else 1.0
                self.memory.add_event(f"剪贴板更新: {clip_text[:100]}", source="clipboard", importance=imp)

                # 若是明显代码报错，提炼关键信息给 Jev 评估是否立即主动提供帮助
                if is_error and (now - self._last_intervene_time > 120):
                    self._evaluate_and_intervene("用户刚复制了一段代码报错/异常堆栈，可能正在卡关。")
                    return

        # 4. 心跳脉冲与专注度检测
        idle_sec = self.perception.get_idle_seconds()
        threshold = cfg.get("interrupt_score_threshold", 0.65)

        # 冷却控制：避免频繁打扰（每次主动提醒后至少间隔 3 分钟）
        if now - self._last_intervene_time < 180:
            return

        # 组织精炼状态供 Jev 评估
        recent_context = self.memory.build_context_prompt(max_events=4)
        state_summary = f"用户当前状态: 空闲时长 {int(idle_sec)} 秒；\n{recent_context}"

        if not self.jev.is_configured():
            return

        should_speak, prob = self.jev.should_intervene(state_summary, threshold=threshold)
        if should_speak:
            print(f"[Proactive] Jev 判定需要主动介入 (概率: {prob:.2f})")
            self._evaluate_and_intervene(state_summary)

    def _evaluate_and_intervene(self, state_summary: str):
        self._last_intervene_time = time.time()

        # 让 Jev 决策动作类别
        actions = ["remind_rest", "offer_help", "cheer_up", "none"]
        action = self.jev.choose_action(state_summary, actions)
        if action == "none":
            return

        # 生成桌宠头顶气泡文案
        reply = self.vlm.generate_bubble_reply(
            f"根据你观察到的用户当前状态（{state_summary}），以桌宠口吻，非常自然简短地主动问候或协助一句（50字内）。动作意图：{action}",
            context=state_summary
        )

        self._trigger_notification(reply, action)

    def _trigger_notification(self, message: str, action_type: str = "general"):
        print(f"[Proactive Notification] [{action_type}] {message}")
        if self.notify_callback:
            try:
                self.notify_callback(message, action_type)
            except Exception as e:
                print(f"[Proactive] 回调通知错误: {e}")
