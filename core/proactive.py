"""Event-driven proactive collaboration; heartbeat is a fallback, not a model timer."""

import datetime
import hashlib
import json
import re
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
# 时长归因：出现"看/盯/挂着 N 分钟"这类说法，就是在替主人复述"你刚才在干什么、干了多久"。
# 这是最容易编的一句话——窗口标题只能说明"哪个程序在最前面"，截图只能说明"此刻屏幕上是什么"，
# 两者都推不出"你盯着某个东西看了 N 分钟"。
# 所以这类时长必须落在"整段时间"上（在电脑前 / 这段时间 / 累计），
# 落在某个窗口、某张截图上的，一律拦下。
_DURATION_CLAIM = re.compile(
    r"(?:\d+|[一二三四五六七八九十两半]+)\s*(?:个)?\s*(?:多)?\s*(?:分钟|小时)"
)
_OBSERVE_VERBS = ("盯", "看", "挂", "停留", "泡在", "开着")
# 允许出现的时长口径：只描述"人在这台电脑前待了多久"，不描述"看某个东西看了多久"。
_PERIOD_SCOPES = ("在电脑前", "坐在电脑前", "这段时间", "累计", "总共")


def assertion_grounded(text):
    """主动消息的事实闸门：断言要有证据，时长要落在整段时间上。"""
    if not text:
        return False
    if any(word in text for word in ASSERTION_WORDS) and not any(
        word in text for word in EVIDENCE_WORDS
    ):
        return False
    if _DURATION_CLAIM.search(text) and any(verb in text for verb in _OBSERVE_VERBS):
        return any(scope in text for scope in _PERIOD_SCOPES)
    return True


# 把 (进程, 窗口标题) 说成人话，用来描述"这段时间主要在干什么"。
# 只做粗粒度归类：宁可说"在看浏览器"，也不去猜"你在看哪个页面的哪一行"。
_APP_LABELS = (
    ("idea64", "写代码"),
    ("pycharm", "写代码"),
    ("code.exe", "写代码"),
    ("wxwork", "企业微信"),
    ("weixin", "微信"),
    ("tim.exe", "TIM"),
    ("qq.exe", "QQ"),
    ("navicat", "数据库"),
    ("excel", "表格"),
    ("winword", "文档"),
    ("explorer", "文件管理"),
    ("chatgpt", "ChatGPT"),
    ("gameviewer", "远程桌面"),
    ("workbuddy", "WorkBuddy"),
    ("chrome", "浏览器"),
    ("msedge", "浏览器"),
    ("firefox", "浏览器"),
)
_BROWSER_HINTS = (
    ("redmine", "Redmine 工单"),
    ("gemini", "Gemini"),
    ("chatgpt", "ChatGPT"),
    ("项目管理总表", "项目总表"),
    ("需求", "需求文档"),
)

# 关怀类姿势：说的是"你该歇歇了"这种用户本来就知道的事，价值最低、打扰成本最高。
# 这类不该按条数放开，要合成一个日预算——不然久坐、饭点、深夜三个计时器各说一句，
# 一天下来就是被连着催。
CARE_POSTURES = (
    "sedentary_rest_reminder",
    "meal_time_checkin",
    "late_night_care",
    "afk_return_greeting",
)


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
        rest_callback=None,
    ):
        self.memory, self.perception, self.jev, self.vlm = memory, perception, jev, vlm
        self.skill_manager, self.notify_callback = skill_manager, notify_callback
        self.dispatcher, self.followup_callback = dispatcher, followup_callback
        # 断言型消息发出前的只读核实通道（harness, readonly=True）。
        # 没有它，主动提醒就只能靠模型凭空描述状态。
        self.task_callback = task_callback
        self.verify_callback = verify_callback
        # 久坐提醒的文案写手：只负责把"坐了多久 + 这段时间花在哪"说成一句有人设的话。
        # 它不核实时实屏幕、也拿不到"当前窗口多久"，素材只有整段分布——
        # 模型能发挥的地方就少，编不出"盯着某张截图看 70 分钟"。
        self.rest_callback = rest_callback
        self._stop = threading.Event()
        self._thread = None
        self._last_state = None
        self._last_decision = 0
        self._last_intervene_time = 0
        # 主人最后一次跟桌宠说话的时间。人刚聊完还处在对话态里，
        # 这时候插一句久坐提醒等于打断他自己刚开的话题。
        self._last_user_turn = 0
        # 关怀类消息的日预算：{日期: 已用条数}。用完就静默到明天。
        self._care_sent = {}
        # 最近几条已经发出去的关怀文案。日预算只挡住"说太多次"，挡不住
        # "每次说同一句"：本地小模型拿着几乎不变的素材，很容易复读上次那句。
        # 所以出口再设一道：和最近几条太像的，宁可不发。
        self._care_texts = []
        # 这一轮专注期里的前台切换次数。只看窗口分布说明不了"碎不碎"，
        # 切换次数才是那个能说"你这上午一直在来回跳"的证据。
        self._switch_count = 0
        self._last_meal_key = ""
        self._error_until = 0
        self._error_hash = ""
        self._active_start = time.time()
        # 当前窗口的连续停留起点。active_minutes 是"在电脑前待了多久"的全局计时器，
        # 跟具体窗口无关；模型拿它去描述某个页面就会变成"盯这个页面 90 分钟"。
        # 窗口级时长必须单独计时，才有资格用来描述"你在看什么看了多久"。
        self._window_sig = None
        self._window_start = time.time()
        # 这一轮专注期内各窗口的累计时长：用来回答"这段时间主要在干什么"，
        # 而不是"此刻在干什么"。人离开（idle 超阈值）就清空，新的一轮重新记。
        self._period_windows = {}
        self._last_tick = time.time()
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

    @staticmethod
    def _describe_window(process, title):
        """把前台窗口归到一个粗粒度说法上。

        刻意只到"写代码·question-bank""浏览器"这个层级：窗口标题能证明的只有
        "哪个程序在最前面"，再细就是猜了，而猜错的部分迟早会被写进主动消息里。
        """
        proc = (process or "").lower()
        label = next(
            (name for key, name in _APP_LABELS if key in proc), proc.replace(".exe", "")
        ) or "其他"
        if label == "浏览器":
            low = (title or "").lower()
            return next((name for key, name in _BROWSER_HINTS if key in low), "浏览器")
        if label == "写代码":
            project = re.split(r"\s+[–-]\s+", (title or "").strip())[0].strip()
            if project and project.lower() not in ("untitled", "无标题", "idea"):
                return f"写代码·{project[:20]}"
        return label

    def _record_window_time(self, signature, delta, idle, threshold, locked):
        """把这一段 tick 的时长记到当前窗口名下。

        delta 截断到 120 秒：机器休眠/进程被挂起后恢复，两帧之间的墙钟差会很大，
        直接累加会把一整晚睡眠算成"在写代码"。
        """
        if delta <= 0 or idle >= threshold or locked:
            return
        label = self._describe_window(*signature)
        self._period_windows[label] = (
            self._period_windows.get(label, 0.0) + min(delta, 120.0)
        )
        if len(self._period_windows) > 128:
            keep = sorted(self._period_windows.items(), key=lambda kv: -kv[1])[:64]
            self._period_windows = dict(keep)

    def _period_breakdown(self, limit=3, min_sec=180):
        """这一轮专注期里，时间主要花在哪儿。返回"写代码·x 40 分钟、企业微信 12 分钟"。"""
        ranked = sorted(self._period_windows.items(), key=lambda kv: -kv[1])
        return "、".join(
            f"{name} {int(sec // 60)} 分钟"
            for name, sec in ranked[:limit]
            if sec >= min_sec and int(sec // 60) > 0
        )

    def _period_shape(self):
        """把窗口分布压成一个"形态"标签，专门喂给写文案的那一轮。

        和 _period_breakdown 的区别是这里**一个分钟数都不给**。原因很直接：
        素材里写着"微信 24 分钟"，模型就一定会把它念出来——"主人，微信占了
        二十四分钟，还切了一堆窗口"。那不叫关心，叫播报监控数据，听的人第一
        反应是"你在盯着我"。
        这里只回答一件事：这段时间是专注、是碎，还是工作沟通混着来。
        模型拿不到数字，也就编不出数字。
        """
        total = sum(self._period_windows.values())
        if total < 300:
            return ""
        ranked = sorted(self._period_windows.items(), key=lambda kv: -kv[1])
        top_name, top_sec = ranked[0]
        share = top_sec / total
        if share >= 0.7:
            return f"基本一直停在{top_name}，很少切走"
        if self._switch_count >= 20 and share < 0.5:
            return f"在 {len(ranked)} 个程序之间来回跳，注意力挺碎的"
        others = "、".join(name for name, _ in ranked[1:3])
        return f"主要在{top_name}" + (f"，剩下的零散在{others}" if others else "")

    @staticmethod
    def _recent_talk(memory, limit=6, chars=80, window=5400):
        """最近几轮对话的短摘要，用来让提醒接住"主人正在关心的事"。

        以前这里读的是 decision_context 的 recent_conversation，但它有个坑：
        非 web 会话下只要 chat 快照非空就会被清成空列表，而 Web 端的对话压根
        不在 main:pet 里。结果主人在网页里说"喝了喝了"，久坐提醒那边一个字
        都看不到，继续自说自话。所以这里按会话白名单直读最近的消息。
        """
        try:
            with memory._get_conn() as db:
                rows = db.execute(
                    "SELECT payload FROM messages_v2 WHERE created_at>=? "
                    "AND (session_id=? OR session_id LIKE 'web:%' "
                    "OR session_id LIKE 'qq:private:%') "
                    "ORDER BY id DESC LIMIT ?",
                    (time.time() - window, PET_SESSION_ID, limit),
                ).fetchall()
        except Exception:
            return ""
        lines = []
        for row in reversed(rows):
            try:
                item = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            content = item.get("content")
            if isinstance(content, list):
                # 多模态消息：只取文本片段，图片对"接住话题"没有帮助。
                content = " ".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                )
            text = str(content or "").replace("\n", " ").strip()
            if not text or text.startswith(("【", "[Harness")):
                continue
            who = "主人" if item.get("role") == "user" else "你"
            lines.append(f"{who}：{text[:chars]}")
        return "\n".join(lines)

    def note_user_turn(self):
        """主人刚说过话。用于进入"对话优先期"，这段时间主动提醒让路。"""
        self._last_user_turn = time.time()

    def _care_budget_left(self, cfg):
        """关怀类消息今天还能不能说。"""
        used = self._care_sent.get(datetime.date.today().isoformat(), 0)
        return used < int(cfg.get("daily_care_limit", 4))

    def _care_spend(self):
        day = datetime.date.today().isoformat()
        self._care_sent = {day: self._care_sent.get(day, 0) + 1}

    @staticmethod
    def _text_similar(a, b):
        """两句短文案的相似度（字符二元组 Jaccard）。

        中文短句没什么词可切，2-gram 足够抓住"换了开头、后半句照抄"这种情况
        ——提醒里的固定尾巴（起来走两步、喝口水）会带来一点基础相似度，
        但真正一字不差的复读会直接顶到 0.9 以上。
        """
        na = {a[i:i + 2] for i in range(len(a) - 1)}
        nb = {b[i:i + 2] for i in range(len(b) - 1)}
        if not na or not nb:
            return 0.0
        return len(na & nb) / len(na | nb)

    def _care_repeated(self, message):
        """这句话和最近几条关怀提醒是不是一个意思。"""
        return any(
            self._text_similar(message, old) >= 0.55
            for old in self._care_texts[-4:]
        )

    def _remember_care(self, message):
        self._care_texts = (self._care_texts + [message])[-6:]

    def _compose_rest(self, state):
        """久坐提醒：文案直接取模型写的那句。

        素材只有"在电脑前坐了 N 分钟"、这段时间的形态标签，以及最近几轮对话。
        单窗口的精确分钟数刻意不给——那不是关心，是复述监控数据。
        上一轮说过的话会作为"别说成这句"一起递过去；真写出来还是雷同，
        这一轮就干脆不发——重复第二遍的关心，比沉默更像机器。
        """
        if not self.rest_callback or not (
            state.get("period_shape") or state.get("period_summary")
        ):
            return ""
        state["avoid_text"] = self._care_texts[-1] if self._care_texts else ""
        try:
            text = (self.rest_callback(state, "sedentary_rest_reminder") or "").strip()
        except Exception as exc:
            print("[Proactive] 久坐文案生成失败", exc)
            return ""
        text = text.strip().strip("\"'“”‘’")
        if text and self._care_repeated(text):
            print("[Proactive] 久坐文案与最近几条雷同，这轮不发：" + text[:50])
            return ""
        return text

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
        # 兜底：别让同一种关心的同一句话连发两遍。_compose_rest 里已经查过一次，
        # 这里覆盖其余几个关怀姿势（饭点、深夜、归来）。
        if posture in CARE_POSTURES and self._care_repeated(message):
            print(f"[Proactive] 关怀文案重复，已拦截：{message[:50]}")
            return False
        try:
            ok = self.notify_callback(
                message, posture, 4 if posture == "intent_reminder" else 3, task=task
            )
            if ok:
                if posture in CARE_POSTURES:
                    self._remember_care(message)
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
        signature = (state.get("process", ""), state.get("title", ""))
        delta = max(0.0, now - self._last_tick)
        self._last_tick = now
        if idle >= threshold:
            # 人离开了：全局计时、窗口计时、这一轮的窗口分布都该从头算。
            self._active_start = now
            self._window_sig = signature
            self._window_start = now
            self._period_windows.clear()
            self._switch_count = 0
        elif signature != self._window_sig:
            # 换了前台窗口（含 Chrome 切标签，标题会变）：窗口计时重来，全局计时继续。
            self._window_sig = signature
            self._window_start = now
            self._switch_count += 1
        self._record_window_time(
            signature, delta, idle, threshold, state.get("locked")
        )
        state["active_minutes"] = int((now - self._active_start) / 60)
        state["window_minutes"] = int((now - self._window_start) / 60)
        state["returned"] = returned
        if cfg.get("enable_window_listener", True):
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
        # 主人刚说过话就别插嘴。对话有它自己的连续性，人正等着你接上一句，
        # 这时候推一条"起来走两步"，读起来就是两台机器各说各的。
        # 显式提醒（他自己设的闹钟）在上面 _run_due_tasks 里已经处理过，不受影响。
        after_turn = float(cfg.get("after_user_turn_sec", 1800))
        if after_turn > 0 and now - self._last_user_turn < after_turn:
            return
        if (
            now - self._last_intervene_time
            < cfg.get("cooldown_sec", 180) * preferences["cooldown_multiplier"]
            or self._pending
        ):
            return
        # 单条通知不设上限：本地模型不产生调用成本，限次数只会让该提醒的时候闭嘴。
        # 节奏交给 cooldown_sec 与各 posture 的冷却窗口；关怀类另有一份日预算，
        # 在 _decide 里按 CARE_POSTURES 统一扣。
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
        # 饭点窗口内不谈久坐：中午坐着等外卖，"起来走两步"是句废话，
        # 而且和几分钟前的饭点提醒叠在一起，就成了连催。
        rest_ready = (
            state["active_minutes"] >= 45
            and now - self._last_rest > 2700
            and not state.get("meal_period")
        )
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
            summary = self._period_breakdown()
            state["period_summary"] = summary
            # 给模型写文案的素材：只给形态标签，不给分钟数。
            # 具体到"哪个程序占了多少分钟"只留在 trigger 里供判定用，不进文案素材——
            # 那些数字一旦到模型手里，就会被原样念给主人听。
            state["period_shape"] = self._period_shape()
            state["period_detail"] = self._period_breakdown(limit=6, min_sec=120)
            period = f"，这段时间主要在{summary}" if summary else ""
            reasons.append(
                f"在电脑前累计 {state['active_minutes']} 分钟{period}，"
                "达到 45 分钟久坐提醒阈值"
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
        # 最近几轮对话只塞给"写文案"那一轮，不进决策快照——context 里已经有了，
        # 重复序列化只会让前缀变长，对"要不要说"这个判断没有增量。
        state["recent_talk"] = self._recent_talk(self.memory)
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
                        "另外看 context.recent_conversation：主人刚说过的事如果还没了结"
                        "（在等外卖、等构建、等回复），这时插一条无关的关怀提醒只会显得你没在听——"
                        "要么把这轮的话接在那件事上，要么先别开口。"
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
        # 关怀类共用一份日预算。久坐、饭点、深夜各算各的冷却，加起来仍然会变成
        # 一天被催四五次；合成一个池子，用完就静到明天。
        if posture in CARE_POSTURES and not self._care_budget_left(cfg):
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
        # 久坐提醒不走"核实"通道（没什么可核实的，它说的都是代码算出来的事实），
        # 但文案仍然交给模型写成有人味的一句话；素材被限定在整段分布上，
        # 写砸了或者把时长挂到某个窗口上，就退回模板。
        if posture == "sedentary_rest_reminder":
            message, verified = self._compose_rest(state), True
            if not message:
                # 文案写砸了，或者跟上一句几乎一样。当成"这一轮已经提醒过"处理：
                # 不然接下来几十分钟会每分钟重算一次，反复撞同一堵墙。
                self._last_rest = time.time()
                self._posture_last[posture] = time.time()
                return
        elif not cfg.get("allow_readonly_followup", True):
            # 没有核实通道，断言型主动提醒就只剩"凭状态编话"这一条路，干脆什么都不做。
            # 久坐提醒不在此列：它说的是代码算出来的事实，不需要看屏幕。
            return
        else:
            # 断言型消息：模型判断出"你还在报错"，必须先只读核实真实屏幕/文件，
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
            if posture in CARE_POSTURES:
                self._care_spend()
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
