"""Event-driven proactive collaboration; heartbeat is a fallback, not a model timer."""

import datetime
import hashlib
import json
import threading
import time
from core.config import load_config

# 桌宠对话真实所在的会话（main 空间），主动决策要读这一份上下文。
PET_SESSION_ID = "main:pet"


class ProactiveEngine:
    def __init__(
        self,
        memory,
        perception,
        jev,
        vlm,
        skill_manager,
        notify_callback=None,
        dispatcher=None,
        followup_callback=None,
    ):
        self.memory, self.perception, self.jev, self.vlm = memory, perception, jev, vlm
        self.skill_manager, self.notify_callback = skill_manager, notify_callback
        self.dispatcher, self.followup_callback = dispatcher, followup_callback
        self._stop = threading.Event()
        self._thread = None
        self._last_state = None
        self._last_decision = 0
        self._last_intervene_time = 0
        self._last_meal_key = ""
        self._error_until = 0
        self._error_hash = ""
        self._active_start = time.time()
        self._activity = None
        self._pending = False
        self._last_maintenance = 0
        self._posture_last = {}
        self._last_rest = 0
        self._watch_state = {}
        self._signals_until = {}

    def _environment_signals(self, state, previous, cfg, now):
        from pathlib import Path

        events = []
        paths = cfg.get("watch_paths", [])[:32]
        self._watch_state = {
            key: value for key, value in self._watch_state.items() if key in paths
        }
        for name in paths:
            try:
                stat = Path(name).stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            if name in self._watch_state and self._watch_state[name] != signature:
                events.append({"path": name, "exists": signature is not None})
            self._watch_state[name] = signature
        if events:
            self._signals_until["environment"] = (now + 120, events)
        env = self._signals_until.get("environment", (0, []))
        state["environment_events"] = env[1] if env[0] > now else []
        state["distraction_switch"] = False
        if cfg.get("enable_focus_nudge") and previous:
            work = any(
                word in previous["process"].lower()
                for word in ("code", "idea", "pycharm", "winword")
            )
            leisure = any(
                word in (state["process"] + state["title"]).lower()
                for word in ("steam", "bilibili", "哔哩", "抖音")
            )
            if work and leisure:
                facts = self.memory.get_all_facts()
                goal = facts.get("focus_task") or facts.get("当前任务")
                if goal:
                    self._signals_until["distraction"] = (now + 120, goal)
        distraction = self._signals_until.get("distraction", (0, ""))
        if distraction[0] > now:
            state["distraction_switch"] = True
            state["focus_goal"] = distraction[1]
        local = datetime.datetime.now()
        state["local_time"] = local.isoformat(timespec="minutes")
        minute = local.hour * 60 + local.minute
        state["meal_period"] = (
            (
                "lunch"
                if 705 <= minute <= 750
                else "dinner"
                if 1080 <= minute <= 1140
                else ""
            )
            if cfg.get("enable_time_reminders")
            else ""
        )
        state["late_night"] = bool(
            cfg.get("enable_time_reminders") and (minute >= 1410 or minute < 300)
        )

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="bot-perception"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
        self._flush_activity(time.time())

    def _run_loop(self):
        while not self._stop.is_set():
            cfg = load_config()["proactive"]
            try:
                self._tick(cfg)
            except Exception as exc:
                print("[Proactive]", exc)
            self._stop.wait(max(0.2, float(cfg.get("poll_interval_sec", 2))))

    def _flush_activity(self, now):
        if self._activity and now - self._activity["start"] >= 5:
            item = self._activity
            self.memory.add_event(
                f"活动片段 {datetime.datetime.fromtimestamp(item['start']).isoformat(timespec='minutes')} 至 {datetime.datetime.fromtimestamp(now).isoformat(timespec='minutes')}：[{item['process']}] {item['title']}",
                source="activity",
            )
        self._activity = None

    @staticmethod
    def condition_met(condition, state):
        if not condition:
            return True
        if condition == "idle":
            return state["idle_sec"] >= 60
        if condition == "active":
            return state["idle_sec"] < 60 and not state.get("locked")
        if condition == "error":
            return state.get("has_error", False)
        if condition == "error_cleared":
            return not state.get("has_error", False)
        if condition.startswith("window:"):
            return condition[7:].lower() in state.get("title", "").lower()
        return False

    def _deliver(self, message, posture, task=None):
        if not self.notify_callback:
            return False
        try:
            ok = self.notify_callback(
                message, posture, 4 if posture == "intent_reminder" else 3, task=task
            )
            if ok:
                self.memory.add_event(
                    f"主动通知 [{posture}] {message}",
                    source="notification",
                    scope=task["scope"] if task else "local",
                )
            return bool(ok)
        except Exception as exc:
            print("[Notification]", exc)
            return False

    @staticmethod
    def _quiet(cfg):
        hour = datetime.datetime.now().hour
        begin, end = cfg.get("quiet_hours", [0, 7])
        return begin != end and (
            begin <= hour < end if begin < end else hour >= begin or hour < end
        )

    def _snapshot(self, cfg):
        return self.perception.snapshot(
            include_window=cfg.get("enable_window_listener", True)
        )

    def _tick(self, cfg):
        now = time.time()
        state = self._snapshot(cfg)
        state["has_error"] = now < self._error_until
        idle = state["idle_sec"]
        threshold = cfg.get("idle_threshold_sec", 600)
        previous = self._last_state
        returned = bool(previous and previous["idle_sec"] >= threshold and idle < 15)
        if idle >= threshold:
            self._active_start = now
        state["active_minutes"] = int((now - self._active_start) / 60)
        state["returned"] = returned
        if cfg.get("enable_window_listener", True):
            signature = (state["process"], state["title"])
            if not self._activity or signature != (
                self._activity["process"],
                self._activity["title"],
            ):
                self._flush_activity(now)
                self._activity = dict(state, start=now)
            elif now - self._activity["start"] >= 300:
                self._flush_activity(now)
                self._activity = dict(state, start=now)
        else:
            self._flush_activity(now)
        if cfg.get("enable_clipboard_listener", True) and not state.get("locked"):
            clip = self.perception.check_clipboard_change()
            if clip:
                error = any(
                    word in clip.lower()
                    for word in ("traceback", "error:", "exception", "failed", "errno")
                )
                if error:
                    self._error_until = now + cfg.get("error_ttl_sec", 300)
                    self._error_hash = hashlib.sha256(clip.encode()).hexdigest()[:16]
                    state["has_error"] = True
                # Full clipboard text is opt-in; redact likely credentials even when enabled.
                if cfg.get("clipboard_store_text"):
                    import re

                    clean = re.sub(
                        r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*\S+",
                        r"\1=[redacted]",
                        clip,
                    )
                    clean = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", clean)
                    self.memory.add_event(
                        "剪贴板: " + clean[:500],
                        source="clipboard",
                        importance=3 if error else 1,
                    )
                elif error:
                    self.memory.add_event(
                        "检测到剪贴板报错（内容未保存）",
                        source="clipboard",
                        importance=3,
                    )
        self._last_state = state
        self._environment_signals(state, previous, cfg, now)
        preferences = self.memory.notification_preferences("local")
        # Explicit reminders are independent of model availability and environmental quiet hours.
        for _ in range(20):
            claimed = self.memory.claim_due_intents(limit=1)
            if not claimed:
                break
            task = claimed[0]
            if not self.condition_met(task["condition_desc"], state):
                self.memory.finish_intent(task["id"], False, defer_sec=15)
                continue
            message = task["action_prompt"]
            if task["kind"] == "followup":
                if (
                    cfg.get("muted")
                    or preferences["muted"]
                    or self._quiet(cfg)
                    or state.get("locked")
                    or state.get("meeting")
                    or state.get("fullscreen")
                ):
                    self.memory.finish_intent(task["id"], False, defer_sec=60)
                    continue
                if cfg.get("allow_readonly_followup") and self.followup_callback:
                    try:
                        message = self.followup_callback(task, state)
                    except Exception as exc:
                        self.memory.finish_intent(task["id"], False, str(exc))
                        continue
                else:
                    message = (
                        "跟进："
                        + message
                        + "；当前"
                        + (
                            "仍检测到异常迹象。"
                            if state["has_error"]
                            else "此前异常信号已不再出现，需要我继续检查吗？"
                        )
                    )
            ok = self._deliver(message, "intent_reminder", task)
            self.memory.finish_intent(
                task["id"], ok, "" if ok else "通道未确认投递成功"
            )
        if now - self._last_maintenance > 3600:
            memcfg = load_config()["memory"]
            self.memory.maintenance(memcfg["retention_days"], memcfg["max_events"])
            self._last_maintenance = now
        if (
            cfg.get("muted")
            or preferences["muted"]
            or state.get("locked")
            or state.get("meeting")
            or state.get("fullscreen")
        ):
            return
        if self._quiet(cfg):
            return
        if (
            now - self._last_intervene_time
            < cfg.get("cooldown_sec", 180) * preferences["cooldown_multiplier"]
            or self._pending
        ):
            return
        if self.memory.notification_count_today() >= cfg.get("daily_limit", 12):
            return
        # Check active goals cadence
        goal_ready = False
        if hasattr(self.memory, "goals") and now - getattr(self, "_last_goal_check", 0) > 300:
            from core.goals import parse_cadence
            active_goals = self.memory.goals.list_goals(status="active", scope="local")
            if active_goals:
                local_now = datetime.datetime.now()
                for g in active_goals:
                    cadence = g.get("cadence", "")
                    parsed = parse_cadence(cadence) if cadence else None
                    should_nudge = False
                    if parsed:
                        if parsed["type"] == "on_active" and returned:
                            should_nudge = True
                        elif parsed["type"] in ("daily", "weekdays"):
                            if parsed["type"] == "weekdays" and local_now.weekday() >= 5:
                                continue
                            if local_now.hour == parsed["hour"] and abs(local_now.minute - parsed["minute"]) <= 5:
                                should_nudge = True
                    if should_nudge:
                        steps = g.get("steps", [])
                        curr = next((s for s in steps if s["status"] in ("in_progress", "pending")), None)
                        if curr:
                            goal_ready = True
                            state["goal_nudge"] = f"目标《{g['title']}》推进时刻：当前步骤 #{curr['id']} [{curr['title']}]"
                            self._last_goal_check = now
                            break
        # Model gate receives only actionable state changes or actual pending context.
        # 报错检测放宽到空闲 10 秒：很多人复制报错后立刻继续敲键盘，
        # 原来的 30 秒门槛会把"正在读报错"的这一刻整段滤掉。
        error_ready = state["has_error"] and idle >= 10
        rest_ready = state["active_minutes"] >= 45 and now - self._last_rest > 2700
        # 饭点是一个 45 分钟的窗口：同一个窗口只提醒一次。
        # 否则窗口内每隔一分钟就会问一次模型，既烧预算又没意义。
        meal_key = (
            f"{state['local_time'][:10]}:{state['meal_period']}"
            if state.get("meal_period")
            else ""
        )
        meal_ready = bool(meal_key) and meal_key != self._last_meal_key
        candidate = (
            error_ready
            or rest_ready
            or returned
            or state["environment_events"]
            or state["distraction_switch"]
            or goal_ready
            or meal_ready
            or state["late_night"]
        )
        if not candidate:
            return
        # 久坐提醒和报错协助本来就发生在"人正坐在电脑前"的时候，
        # 用 idle>=15 当门槛等于把这两类永久挡在门外（人越专注越不可能有 15 秒空白）。
        # 只对"环境变化 / 饭点 / 深夜"这类需要人不在打字的场景保留空闲门槛。
        if idle < 15 and not returned and not (rest_ready or error_ready):
            return
        if meal_ready:
            self._last_meal_key = meal_key
        # 把"这次为什么会来打扰"写进状态里。不写这一句，模型只能从一堆原始字段
        # 里自己猜，往往判定"没有明确收益"而保持静默——主动提醒就永远发不出来。
        reasons = []
        if error_ready:
            reasons.append("检测到报错迹象（剪贴板里出现了报错内容）")
        if rest_ready:
            reasons.append(
                f"已连续专注 {state['active_minutes']} 分钟，达到 45 分钟久坐提醒阈值"
            )
        if returned:
            reasons.append("你刚回到电脑前")
        if state["environment_events"]:
            reasons.append("你指定的监控文件有变化")
        if state["distraction_switch"]:
            reasons.append("你从工作切到了娱乐程序")
        if goal_ready:
            reasons.append(state.get("goal_nudge", "长程目标到了推进时刻"))
        if meal_ready:
            reasons.append("到了饭点")
        if state["late_night"]:
            reasons.append("已经是深夜时段")
        state["trigger"] = "；".join(reasons)
        fingerprint = (
            self._error_hash if error_ready else "",
            state["active_minutes"] // 45 if rest_ready else 0,
            returned,
            json.dumps(state["environment_events"], sort_keys=True),
            state["distraction_switch"],
            state.get("goal_nudge", ""),
            meal_key if meal_ready else "",
            state["late_night"],
        )
        if fingerprint == getattr(
            self, "_fingerprint", None
        ) and now - self._last_decision < max(
            60, cfg.get("heartbeat_interval_sec", 30)
        ):
            return
        self._fingerprint = fingerprint
        self._last_decision = now
        if not self.jev.is_configured():
            return
        self._pending = True
        if self.dispatcher:
            future = self.dispatcher.submit(
                "local:proactive", self._decide, state, cfg, background=True
            )

            def finished(done):
                self._pending = False
                if not done.cancelled() and done.exception():
                    print("[Proactive]", done.exception())

            future.add_done_callback(finished)
        else:
            try:
                self._decide(state, cfg)
            finally:
                self._pending = False

    def _decide(self, state, cfg):
        from core.decision_context import build_decision_context

        # 桌宠的对话真实落在 main:pet（main 空间）。以前这里读的是 local:pet，
        # 那条会话基本是空的，于是主动提醒永远只看得到桌面活动、看不到刚聊过什么。
        context = build_decision_context(self.memory, PET_SESSION_ID, "main")
        # 但"刚才是哪个程序、用了多久"这类桌面活动记在 local 空间，两边都带上，
        # 主动提醒才有连续感。
        context["local_activity"] = self.memory.build_context_prompt(
            scope="local", max_events=4, max_chars=1200
        )
        context["notification_preferences"] = self.memory.notification_preferences("local")
        context["last_notification_at"] = self._last_intervene_time
        snapshot = json.dumps({"desktop_state": state, "context": context}, ensure_ascii=False)
        # One Jev request returns both gate and posture.
        result = self.jev.decide(
            snapshot,
            {
                "intervene": {
                    "type": "noul",
                    "instructions": (
                        "仅在异常停顿、45分钟疲劳、离开后归来有待办、用户指定监控文件有变化、"
                        "长程目标推进时刻、明确专注目标下切到娱乐、或已启用饭点/深夜关怀时考虑提醒。"
                        "状态里的 trigger 字段说明了这次为什么会来打扰你：trigger 非空即表示"
                        "已经出现具体信号（例如连续专注满 45 分钟、检测到报错、刚回到电脑前），"
                        "这时 intervene 应判为真；没有具体信号、也没有明确收益时才保持静默。"
                    ),
                },
                "posture": {
                    "type": "choice",
                    "instructions": "选择最符合状态的协同动作。",
                    "options": [
                        "debug_assist",
                        "sedentary_rest_reminder",
                        "afk_return_greeting",
                        "task_progress_update",
                        "goal_advancement",
                        "distraction_gentle_nudge",
                        "meal_time_checkin",
                        "late_night_care",
                        "ambient_silent_watch",
                    ],
                },
            },
        )
        try:
            probability = float(result.get("intervene", 0))
        except (TypeError, ValueError):
            return
        posture = result.get("posture", "ambient_silent_watch")
        if (
            probability < cfg.get("interrupt_score_threshold", 0.65)
            or posture == "ambient_silent_watch"
        ):
            return
        if time.time() - self._posture_last.get(posture, 0) < (
            7200
            if posture in ("meal_time_checkin", "late_night_care")
            else 2700
            if posture in ("sedentary_rest_reminder", "goal_advancement")
            else 600
        ):
            return
        # Recheck volatile state after network latency.
        current = self._snapshot(cfg)
        if (
            self._stop.is_set()
            or load_config()["proactive"].get("muted")
            or current.get("locked")
            or current.get("fullscreen")
            or current.get("meeting")
        ):
            return
        if (
            state.get("idle_sec", 0) >= 15
            and current["idle_sec"] < 15
            and posture != "afk_return_greeting"
        ):
            # 决策期间人回到了电脑前（决策时是空闲的，现在在敲键盘）——提醒已经过时，丢掉。
            # 久坐/报错这类本来就是冲着"正在用电脑的人"去的，不能只因为 idle 低就丢。
            return
        message = self.vlm.generate_bubble_reply(
            "根据以下状态给出一句具体、克制的协助建议，不要声称已操作：" + snapshot,
            context=posture,
        )
        if not message or message.startswith(("【VLM", "【提示")):
            return
        live = load_config()["proactive"]
        latest = self._snapshot(live)
        if (
            self._stop.is_set()
            or live.get("muted")
            or self._quiet(live)
            or self.memory.notification_preferences("local")["muted"]
            or latest.get("locked")
            or latest.get("meeting")
            or latest.get("fullscreen")
            or (
                state.get("idle_sec", 0) >= 15
                and latest["idle_sec"] < 15
                and posture != "afk_return_greeting"
            )
        ):
            return
        channel = cfg.get("notification_channel", "pet")
        target = cfg.get("notification_target", "")
        task = {
            "scope": "local",
            "channel": channel,
            "target": target,
            "session_id": PET_SESSION_ID,
        }
        if self._deliver(message, posture, task):
            self._last_intervene_time = time.time()
            self._posture_last[posture] = time.time()
            if posture == "sedentary_rest_reminder":
                self._last_rest = time.time()
            if posture == "debug_assist" and cfg.get("auto_followup", True):
                self.memory.add_delayed_intent(
                    time.time() + 120,
                    "此前报错是否已经解决",
                    scope="local",
                    channel=channel,
                    target=target,
                    dedupe_key="error:" + self._error_hash,
                    kind="followup",
                )
