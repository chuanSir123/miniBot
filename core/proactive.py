"""Event-driven proactive collaboration; heartbeat is a fallback, not a model timer."""

import datetime
import hashlib
import json
import threading
import time
from core.config import load_config

# 桌宠对话真实所在的会话（main 空间），主动决策要读这一份上下文。
PET_SESSION_ID = "main:pet"

# 断言型主动消息的"证据闸门"。
# 这些词是在对世界下结论（问题解决了/问题还在），必须同时给出实际看到的东西。
# 没有依据的结论一律拦下——宁可这次不说话，也不要把"错误计时器还没到期"
# 说成"仍然检测到异常"。
ASSERTION_WORDS = (
    "已解决",
    "已修复",
    "已恢复正常",
    "仍然异常",
    "仍检测到异常",
    "已经好了",
    "恢复正常",
    "不再出现",
    "没再出现",
    "已帮你",
    "已替你",
    "已经帮你",
    "已经替你",
)
# 证据必须指向"实际看到了什么"，而不是被断言的对象本身。
# 注意这里刻意不收"报错"：写死文案"仍检测到异常迹象"里就带着报错语境，
# 把它当证据等于自证，闸门就白设了。
EVIDENCE_WORDS = (
    "截图",
    "屏幕",
    "窗口",
    "标题",
    "界面",
    "画面",
    "显示",
    "看到",
    "读到",
    "文件里",
    "文件内容",
    "日志里",
    "剪贴板里",
)


def assertion_grounded(text):
    """断言型文案是否附带了可见证据；无断言词的文案直接放行。"""
    if not text:
        return False
    if not any(word in text for word in ASSERTION_WORDS):
        return True
    return any(word in text for word in EVIDENCE_WORDS)


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
        verify_callback=None,
        task_callback=None,
    ):
        self.memory, self.perception, self.jev, self.vlm = memory, perception, jev, vlm
        self.skill_manager, self.notify_callback = skill_manager, notify_callback
        self.dispatcher, self.followup_callback = dispatcher, followup_callback
        # 断言型消息发出前的只读核实通道（harness, readonly=True）。
        # 没有它，主动提醒就只能靠模型凭空描述状态。
        self.task_callback = task_callback
        self.verify_callback = verify_callback
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
        # 正在核实的跟进任务：租约到期后会被重复领取，用它避免同一个判断核实两遍、
        # 最后发两条几乎一样的消息。
        self._followup_inflight = set()
        # 报错协助：发现即触发，所以需要自己防并发、防同一条报错处理两遍。
        self._error_pending = False
        self._error_assisted = ""

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

    def _deliver(self, message, posture, task=None, verified=True):
        """主动消息的唯一出口。

        verified=False 表示"这是模型自己编的、没有核实过"——直接拦下不投递。
        转述用户原话的提醒（reminder）不需要核实，核实过的断言型消息才允许放行。
        任何新增的主动发言路径都会经过这里，所以闸门放在出口最省事。
        """
        message = (message or "").strip()
        if not message:
            return False
        if not verified:
            print("[Proactive] 未核实内容已拦截：" + message[:80])
            return False
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

    def _submit_followup(self, task, state):
        """把"跟进核实"挪出感知线程。

        核实要跑模型、可能还要截图和读文件，一次能拖几十秒。以前这里是同步调用，
        等于每次跟进都把久坐/报错/文件监控整条感知链冻住——这也是核实通道
        一直被默认关掉的实际原因。交给 dispatcher 后，投递在回调里完成。
        """

        def run():
            try:
                try:
                    message = self.followup_callback(task, state)
                except Exception as exc:
                    self.memory.finish_intent(task["id"], False, str(exc))
                    return
                message = (message or "").strip()
                if not message or not assertion_grounded(message):
                    # 拿不到证据就不发言。以前这里会拼一句"仍检测到异常迹象"，
                    # 而那句话只反映 error_ttl_sec 计时器还没走完，与真实状态无关。
                    # terminal：核实过一次就够了，别每 15 秒重试一遍同一个判断。
                    self.memory.finish_intent(
                        task["id"], False, "核实未取得证据，已静默放弃", terminal=True
                    )
                    return
                ok = self._deliver(message, "intent_reminder", task, verified=True)
                self.memory.finish_intent(
                    task["id"], ok, "" if ok else "通道未确认投递成功"
                )
            finally:
                self._followup_inflight.discard(task["id"])

        self._followup_inflight.add(task["id"])
        if not self.dispatcher:
            run()
            return
        future = self.dispatcher.submit(
            "local:proactive:followup", run, background=True
        )
        if future.done():
            # 队列满/正在退出时 submit 不抛异常，而是直接返回一个已失败的 future，
            # run() 根本不会执行。不清掉标记的话，这条跟进会被自己永久 defer。
            self._followup_inflight.discard(task["id"])
            self.memory.finish_intent(task["id"], False, defer_sec=60)

    def _spawn_error_assist(self, state, cfg, now):
        """剪贴板里一出现报错就立刻核实，不等 tick 攒候选、等空闲、再过 jev。

        原来这条路要 trigger 先成型、再等 idle>=10 秒、再由 jev 判一次，
        报错最该被回应的十几秒全耗在等条件上。报错本身已经是明确信号，
        这里直接把"核实 → 建议"跑起来；节奏仍由 cooldown 和 debug_assist
        的冷却窗口控制，同一条报错也只处理一次。
        """
        if self._error_pending or not self._error_hash or not self.verify_callback:
            return
        if self._error_hash == self._error_assisted:
            return
        if (
            cfg.get("muted")
            or self.memory.notification_preferences("local")["muted"]
            or self._quiet(cfg)
            or state.get("locked")
            or state.get("meeting")
            or state.get("fullscreen")
        ):
            return
        if now - self._last_intervene_time < cfg.get("cooldown_sec", 180):
            return
        if now - self._posture_last.get("debug_assist", 0) < 600:
            return
        self._error_pending = True
        error_hash = self._error_hash
        snapshot = dict(state, trigger="检测到报错迹象（剪贴板里出现了报错内容）")

        def run():
            try:
                message, verified = self._verify(snapshot, "debug_assist")
                if not message or not verified:
                    return
                task = {
                    "scope": "local",
                    "channel": cfg.get("notification_channel", "pet"),
                    "target": cfg.get("notification_target", ""),
                    "session_id": PET_SESSION_ID,
                }
                if not self._deliver(message, "debug_assist", task, verified=True):
                    return
                self._error_assisted = error_hash
                self._last_intervene_time = time.time()
                self._posture_last["debug_assist"] = time.time()
                # 两分钟后再看一次：那时才需要判断"到底解决了没有"。
                if cfg.get("auto_followup", True):
                    self.memory.add_delayed_intent(
                        time.time() + 120,
                        "此前报错是否已经解决",
                        scope="local",
                        channel=task["channel"],
                        target=task["target"],
                        dedupe_key="error:" + error_hash,
                        kind="followup",
                    )
            except Exception as exc:
                print("[Proactive] 报错核实失败", exc)
            finally:
                self._error_pending = False

        if not self.dispatcher:
            run()
            return
        future = self.dispatcher.submit("local:proactive:error", run, background=True)
        if future.done():
            # 队列满时 submit 返回已失败的 future，run() 不会执行，标记得自己清。
            self._error_pending = False

    def _verify(self, state, posture):
        """主动开口之前先核实：有证据的文案才允许出门。

        返回 (message, verified)。模型判断出来的每一条结论（你还在报错、
        你已经连续盯了 45 分钟）都必须先看一眼真实屏幕或文件。
        没有核实通道就拿不到证据，此时返回空——宁可这次不说话，
        也不要退回"让模型凭空描述桌面"的老路。
        """
        if not self.verify_callback:
            return "", False
        claim = state.get("trigger") or posture
        try:
            message = self.verify_callback(state, posture, claim)
        except Exception as exc:
            print("[Proactive] 核实失败", exc)
            return "", False
        message = (message or "").strip()
        if not message or message.startswith(("【VLM", "【提示")):
            return "", False
        if not assertion_grounded(message):
            print("[Proactive] 断言缺少证据，已丢弃：" + message[:80])
            return "", False
        return message, True

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

    def _submit_agent_task(self, task):
        if task["id"] in self._followup_inflight:
            self.memory.finish_intent(task["id"], False, defer_sec=60)
            return
        def run():
            lease_stop = threading.Event()
            def renew():
                while not lease_stop.wait(30):
                    with self.memory._get_conn() as db:
                        db.execute("UPDATE tasks_v2 SET lease_until=? WHERE id=? AND status='running'", (time.time() + 120, task["id"]))
            threading.Thread(target=renew, daemon=True).start()
            try:
                message = task.get("result", "")
                if not message:
                    if not self.task_callback:
                        raise RuntimeError("未配置定时任务执行器")
                    message = (self.task_callback(task) or "").strip()
                    if not message:
                        raise RuntimeError("定时任务未返回结果")
                    with self.memory._get_conn() as db:
                        db.execute("UPDATE tasks_v2 SET result=? WHERE id=? AND status='running'", (message, task["id"]))
                ok = self._deliver(message, "scheduled_task", task)
                self.memory.finish_intent(task["id"], ok, "" if ok else "结果投递失败")
                if ok:
                    with self.memory._get_conn() as db:
                        db.execute("UPDATE tasks_v2 SET result='' WHERE id=?", (task["id"],))
            except Exception as exc:
                self.memory.finish_intent(task["id"], False, str(exc))
            finally:
                lease_stop.set()
                self._followup_inflight.discard(task["id"])
        self._followup_inflight.add(task["id"])
        if self.dispatcher:
            future = self.dispatcher.submit(task["session_id"], run, background=True)
            if future.done() and future.exception():
                self._followup_inflight.discard(task["id"])
                self.memory.finish_intent(task["id"], False, str(future.exception()))
        else:
            run()

    def _run_due_tasks(self, cfg, state):
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
            if task["kind"] == "agent":
                self._submit_agent_task(task)
                continue
            message = task["action_prompt"]
            if task["kind"] == "followup":
                if task["id"] in self._followup_inflight:
                    # 租约到期被重新领取，但上一轮核实还在跑：推后，别核实两遍。
                    self.memory.finish_intent(task["id"], False, defer_sec=30)
                    continue
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
                if not self.followup_callback or not cfg.get(
                    "allow_readonly_followup", True
                ):
                    # 没有核实通道就不发。以前这里的兜底是拼一句固定文案，
                    # 既不核实也不经模型，等于替用户编造结论。
                    self.memory.finish_intent(
                        task["id"], False, "未启用核实通道，已静默放弃", terminal=True
                    )
                    continue
                self._submit_followup(task, dict(state))
                continue
            # 走到这里的都是转述用户原话的提醒（"10 点开会"），内容真实性无需核实。
            ok = self._deliver(message, "intent_reminder", task, verified=True)
            self.memory.finish_intent(
                task["id"], ok, "" if ok else "通道未确认投递成功"
            )

    def _tick(self, cfg):
        now = time.time()
        try:
            state = self._snapshot(cfg)
        except Exception as exc:
            print("[Perception]", exc)
            state = {"idle_sec": 0, "process": "", "title": "", "locked": False}
        self._run_due_tasks(cfg, state)
        # has_error 的语义是"最近 error_ttl_sec 内见过报错信号"，它是个计时器，
        # 不是复检结果。只允许用来解释"为什么现在来打扰"（trigger），
        # 任何面向用户的文案都不能拿它当"问题还在"的依据。
        state["has_error"] = now < self._error_until
        state["error_signal_recent"] = state["has_error"]
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
                    state["error_signal_recent"] = True
                    # 报错是最需要当场回应的信号：发现就核实，不等下一轮候选判定。
                    self._spawn_error_assist(state, cfg, now)
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
        # 单日通知上限已移除：本地模型不产生调用成本，限制次数只会让该提醒的时候闭嘴。
        # 真正的节奏控制交给 cooldown_sec 与各 posture 的冷却窗口。
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
        # 报错不再走这条候选路径：剪贴板一出现报错，_spawn_error_assist 就当场核实，
        # 这里再判一次只会导致同一件事被处理两遍。
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
            rest_ready
            or returned
            or state["environment_events"]
            or state["distraction_switch"]
            or goal_ready
            or meal_ready
            or state["late_night"]
        )
        if not candidate:
            return
        # 久坐提醒本来就发生在"人正坐在电脑前"的时候，
        # 用 idle>=15 当门槛等于把它永久挡在门外（人越专注越不可能有 15 秒空白）。
        # 只对"环境变化 / 饭点 / 深夜"这类需要人不在打字的场景保留空闲门槛。
        if idle < 15 and not returned and not rest_ready:
            return
        if meal_ready:
            self._last_meal_key = meal_key
        # 把"这次为什么会来打扰"写进状态里。不写这一句，模型只能从一堆原始字段
        # 里自己猜，往往判定"没有明确收益"而保持静默——主动提醒就永远发不出来。
        reasons = []
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

        # 没有核实通道，断言型主动提醒就只剩"凭状态编话"这一条路，干脆什么都不做。
        if not cfg.get("allow_readonly_followup", True):
            return

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
        # context 在前、desktop_state 在后：state 是共享前缀，而 desktop_state 里
        # 的计时/计数每轮都在变，放前面等于每轮都让前缀失效。
        snapshot = json.dumps({"context": context, "desktop_state": state}, ensure_ascii=False)
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
                        "已经出现具体信号（例如连续专注满 45 分钟、刚回到电脑前），"
                        "这时 intervene 应判为真；没有具体信号、也没有明确收益时才保持静默。"
                        "注意 trigger 只是线索，不是已核实的结论：报错是否仍在、人是否真的"
                        "专注了 45 分钟，都由后续只读核实确认，你不要在判断里把它当成事实。"
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
        # 断言型消息：模型判断出"你还在报错""你该休息了"，必须先只读核实真实屏幕/文件，
        # 拿不出证据就不开口。这里也顺带吸收掉原来的 VLM 气泡生成。
        message, verified = self._verify(state, posture)
        if not message:
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
        if self._deliver(message, posture, task, verified=verified):
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
