import datetime
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
      1. 旁听流感知 (窗口切换、异常报错剪贴板、空闲/专注检测)
      2. 延时脉冲与记忆驱动 (意图定时器到期)
      3. Jev 快脑决策门控与细粒度姿态分类 (基于详尽规则 Prompt)
    """

    def __init__(
        self,
        memory: MiniMemory,
        perception: LocalPerception,
        jev: JevClient,
        vlm: VLMBrain,
        skill_manager: SkillManager,
        notify_callback: Optional[Callable[[str, str, int], None]] = None
    ):
        self.memory = memory
        self.perception = perception
        self.jev = jev
        self.vlm = vlm
        self.skill_manager = skill_manager
        self.notify_callback = notify_callback  # (message, posture, anim_action)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_win_title = ""
        self._last_intervene_time = 0.0
        self._active_start_time = time.time()

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

            for _ in range(int(interval)):
                if not self._running:
                    break
                time.sleep(1)

    def _tick(self, cfg: Dict[str, Any]):
        now = time.time()
        now_dt = datetime.datetime.now()
        time_str = now_dt.strftime("%H:%M")

        # 1. 检查延时意图队列 (定时提醒，最高优先级)
        due_intents = self.memory.pop_due_intents()
        for intent in due_intents:
            prompt = intent.get("action_prompt", "")
            print(f"[Proactive] 延时意图唤醒: {prompt}")
            reply = self.vlm.generate_bubble_reply(f"执行预定的定时提醒任务：{prompt}", context="延时意图到期")
            self._trigger_notification(reply, "intent_reminder", anim_action=4)
            return

        # 2. 旁听流感知 - 窗口变动
        win_info = self.perception.get_active_window()
        cur_title = win_info.get("title", "")
        cur_proc = win_info.get("process", "")

        if cfg.get("enable_window_listener", True):
            if cur_title and cur_title != self._last_win_title:
                self._last_win_title = cur_title
                self.memory.add_event(f"切换至窗口: [{cur_proc}] {cur_title}", source="window", importance=1.0)

        # 3. 旁听流感知 - 剪贴板监听
        clip_error_detected = False
        if cfg.get("enable_clipboard_listener", True):
            clip_text = self.perception.check_clipboard_change()
            if clip_text:
                clip_error_detected = any(kw in clip_text for kw in ["Traceback", "Error:", "Exception", "failed", "errno"])
                imp = 3.0 if clip_error_detected else 1.0
                self.memory.add_event(f"剪贴板更新: {clip_text[:120]}", source="clipboard", importance=imp)

        # 4. 连续活跃时长与空闲时长统计
        idle_sec = self.perception.get_idle_seconds()
        if idle_sec > 600:
            # 挂机重置活跃起点
            self._active_start_time = now
        active_duration_min = max(0, int((now - self._active_start_time) / 60))

        # 冷却控制：避免打扰频率过高 (至少间隔 180 秒)
        if (now - self._last_intervene_time) < 180:
            return

        # 5. 聚合当前全量多维环境数据
        recent_context = self.memory.build_context_prompt(max_events=4)
        state_summary = (
            f"【当前系统环境与用户状态】\n"
            f"- 当前本地时钟: {time_str}\n"
            f"- 活动进程与窗口: [{cur_proc}] {cur_title}\n"
            f"- 键鼠实时空闲时长: {int(idle_sec)} 秒\n"
            f"- 连续未休息活跃时长: {active_duration_min} 分钟\n"
            f"- 剪贴板异常标记: {'发现报错堆栈' if clip_error_detected else '无报错'}\n"
            f"{recent_context}"
        )

        if not self.jev.is_configured():
            return

        # 6. Jev 快脑门控评估 (Noul 原语判定是否应该打扰)
        threshold = cfg.get("interrupt_score_threshold", 0.70)
        should_intervene, prob = self.jev.evaluate_proactive_gate(state_summary, threshold=threshold)

        if should_intervene:
            print(f"[Proactive] Jev 裁决触发主动打扰 (置信度: {prob:.2f})")
            self._evaluate_and_intervene(state_summary, cur_proc, clip_error_detected)

    def _evaluate_and_intervene(self, state_summary: str, cur_proc: str, has_error: bool):
        self._last_intervene_time = time.time()

        # 7. Jev 细粒度姿态分类 (Choice 原语)
        posture = self.jev.choose_proactive_posture(state_summary)
        if posture == "ambient_silent_watch":
            print("[Proactive] Jev 姿态判定为保持静默观察，本轮不发声。")
            return

        print(f"[Proactive] Jev 确定提醒姿态: {posture}")

        # 映射对应桌宠动作: 0=idle, 3=wave(打招呼), 4=jump(高兴), 5=failed(发现报错), 6=waiting(思考)
        posture_anim_map = {
            "debug_assist": 5,
            "sedentary_rest_reminder": 3,
            "afk_return_greeting": 4,
            "late_night_care": 3,
            "distraction_gentle_nudge": 3,
            "meal_time_checkin": 4
        }
        anim_action = posture_anim_map.get(posture, 3)

        # 姿态专属指导提示词
        posture_prompt_map = {
            "debug_assist": "检测到用户刚遇到代码或环境报错，以贴心小鸡助手的口吻，主动询问是否需要帮你查日志或看下报错原因（35字内）。",
            "sedentary_rest_reminder": "用户已经连续高强度工作坐了很久了，提醒他站起来伸个懒腰、喝口水放松一下眼睛（30字内）。",
            "afk_return_greeting": "用户刚刚离开电脑一段时间刚回到桌前，向他打个欢快的招呼，问候一句（25字内）。",
            "late_night_care": "夜深了用户还在工作，送上一句温馨的深夜关怀，提醒别熬太晚注意休息（30字内）。",
            "distraction_gentle_nudge": "用户刚才切到了娱乐摸鱼软件，幽默风趣地轻轻敲打一下，提醒别忘了手头的正事哦（30字内）。",
            "meal_time_checkin": "正餐饭点到了，提醒用户身体是革命本钱，该准备吃饭干饭啦（25字内）。"
        }
        instruction = posture_prompt_map.get(posture, "根据当前状态简短问候一句。")

        # 8. 慢脑 VLM 组织最终轻量气泡文案
        reply = self.vlm.generate_bubble_reply(
            f"情境: {instruction}\n环境状态: {state_summary}",
            context=f"主动姿态: {posture}"
        )

        self._trigger_notification(reply, posture, anim_action=anim_action)

    def _trigger_notification(self, message: str, posture: str = "general", anim_action: int = 3):
        print(f"[Proactive Notification] [{posture}] {message}")
        if self.notify_callback:
            try:
                self.notify_callback(message, posture, anim_action)
            except Exception as e:
                print(f"[Proactive] 回调通知错误: {e}")
