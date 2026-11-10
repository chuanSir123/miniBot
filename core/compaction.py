"""会话压缩：把较早的历史折叠成一份结构化摘要。

设计要点：

- **按 token 找切点**，不是按消息条数。从最新往回累加，攒够 ``keep_recent_tokens``
  就切，且只切在轮次起点上——工具结果必须紧跟着自己的 tool_call。
- **摘要是有格式的**（目标 / 约束 / 进展 / 决定 / 下一步 / 关键上下文），并且
  第二次压缩是在已有摘要上**增量更新**，不是重新总结一遍——否则每次压缩都在丢信息。
- **原样保留文件清单**。<read-files> / <modified-files> 由工具调用抽取，不进摘要正文，
  模型续接时才知道自己动过哪些文件。
- **摘要以 user 身份出现在历史最前面**（见 core/transcript.py），不再是第二条 system。

与本项目的取舍（有意保留）：
- 这里的 ``save_summary`` 会把旧行归档并物理删除，
  所以切点额外设了"至少保留最近 20 条原始记录"的下限，避免切得太狠之后拿不回来。
- 本项目的 task_state 是跨进程持久化的续接契约，摘要里再写一遍会走形，
  因此把 ``<task_checkpoint>`` 作为独立数据块喂给摘要模型，要求它不得改写。
"""

from __future__ import annotations

import copy
import json
import threading
import time

from core.config import load_config
from core.console import log
from core.text_budget import estimate_text_tokens
from core.transcript import (
    Transcript,
    estimate_entry_tokens,
    find_cut_point,
    is_tool_group_intact,
)

# 压缩后保留的原始上下文上限（token）。超过窗口预算时会被按窗口比例压下来。
KEEP_RECENT_TOKENS = 20000
# 至少保留的原始消息条数：压缩会物理删除旧行，切太狠就回不来了。
KEEP_RECENT_MESSAGES = 20
# 喂给摘要模型的单条工具结果上限（字符）。全文对摘要没有价值，只会撑爆摘要请求。
TOOL_RESULT_MAX_CHARS = 2000

SUMMARIZATION_SYSTEM_PROMPT = """你是一个上下文压缩助手。你的任务是读一段用户与助手的对话，然后按指定格式输出结构化摘要。

不要继续对话，不要回答对话中的任何问题，只输出结构化摘要。"""

SUMMARIZATION_PROMPT = """上面的对话需要被压缩成一份结构化检查点摘要，供另一个模型接着干活。

严格按以下格式输出：

## 目标
[用户想完成什么？会话覆盖多个任务时逐条列出]

## 约束与偏好
- [用户提出的限制、偏好、要求]
- [没有就写“（无）”]

## 进展
### 已完成
- [x] [已完成的任务/改动]

### 进行中
- [ ] [当前在做的事]

### 阻塞
- [阻碍推进的问题，没有就写“（无）”]

## 关键决定
- **[决定]**：[简短理由]

## 下一步
1. [按顺序列出接下来该做什么]

## 关键上下文
- [续接所需的数据、示例、引用；没有就写“（无）”]

每节都要简短。文件路径、函数名、错误信息必须原样保留。"""

UPDATE_SUMMARIZATION_PROMPT = """上面的对话是**新增**的消息，需要并入 <previous-summary> 里已有的摘要。

规则：
- 保留已有摘要中的所有信息
- 加入新消息里的进展、决定和上下文
- 更新「进展」：已完成的从「进行中」移到「已完成」
- 根据当前状态更新「下一步」
- 文件路径、函数名、错误信息必须原样保留
- 确实不再相关的内容可以删除

严格按与之前相同的格式输出（目标 / 约束与偏好 / 进展 / 关键决定 / 下一步 / 关键上下文）。"""


def serialize_conversation(messages):
    """把消息序列化成纯文本，防止摘要模型把它当成"该继续的对话"。

    思考内容不写入：它既不在真实上下文里（各家会丢弃对不上的 thinking 块），
    体积还常常比被压缩的对话本身更大。可见文本、工具调用、工具结果记录了发生的事。
    """
    parts = []
    for message in messages:
        text = _message_text(message)
        role = message.get("role")
        if role == "user":
            if text:
                parts.append(f"[用户]: {text}")
        elif role == "assistant":
            calls = [
                "%s(%s)" % (
                    call.get("function", {}).get("name", ""),
                    _compact_args(call.get("function", {}).get("arguments")),
                )
                for call in (message.get("tool_calls") or [])
            ]
            if text:
                parts.append(f"[助手]: {text}")
            if calls:
                parts.append("[助手工具调用]: " + "; ".join(calls))
        elif role == "tool":
            if text:
                parts.append("[工具结果]: " + _truncate(text, TOOL_RESULT_MAX_CHARS))
    return "\n\n".join(parts)


def _message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _compact_args(raw, limit=400):
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _truncate(text, max_chars):
    if len(text) <= max_chars:
        return text
    return "%s\n\n[... 另有 %d 字符已截断]" % (text[:max_chars], len(text) - max_chars)


# ---------------------------------------------------------------------------
# 文件操作清单
# ---------------------------------------------------------------------------

_FILE_TOOL_KINDS = {
    "read": "read",
    "list_files": "read",
    "write": "written",
    "edit": "edited",
}


def extract_file_ops(messages):
    """从工具调用里抽取读过/写过/改过的文件；上一轮摘要里的清单也要继承。"""
    ops = {"read": [], "written": [], "edited": []}
    seen = {key: set() for key in ops}

    def add(kind, path):
        if path and path not in seen[kind]:
            seen[kind].add(path)
            ops[kind].append(path)

    for message in messages:
        for call in (message.get("tool_calls") or []):
            function = call.get("function") or {}
            kind = _FILE_TOOL_KINDS.get(function.get("name"))
            if not kind:
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict):
                add(kind, args.get("file_path") or args.get("path"))
    return ops


def format_file_operations(ops):
    modified = ops["edited"] + [p for p in ops["written"] if p not in ops["edited"]]
    read_only = sorted(p for p in ops["read"] if p not in modified)
    sections = []
    if read_only:
        sections.append("<read-files>\n%s\n</read-files>" % "\n".join(read_only))
    if modified:
        sections.append("<modified-files>\n%s\n</modified-files>" % "\n".join(sorted(set(modified))))
    return ("\n\n" + "\n\n".join(sections)) if sections else ""


def merge_file_ops(previous_summary, ops):
    """把上次摘要里的文件清单并进来，避免增量更新时丢掉更早的文件。"""
    for tag, key in (("read-files", "read"), ("modified-files", "written")):
        marker = "<%s>\n" % tag
        if previous_summary and marker in previous_summary:
            body = previous_summary.split(marker, 1)[1].split("</%s>" % tag, 1)[0]
            for line in body.splitlines():
                path = line.strip()
                if path and path not in ops[key]:
                    ops[key].append(path)
    return ops


# 压缩失败后的退避：60s 起，翻倍，最长 15 分钟。
# 没有退避的话，撞阈值的那一轮会在 turn loop 里每一步都重试一次摘要调用。
COMPACTION_RETRY_BASE_SEC = 60
COMPACTION_RETRY_MAX_SEC = 900

_UNSET = object()


class ContextCompactor:
    """会话级压缩器：只追加、按 token 切、增量更新摘要。

    触发分两档（"到阈值就压，但不挡住当前这一轮"）：

    - **软阈值**（窗口 × ratio）：交给后台线程压，本轮照常用未压缩历史发请求。
      压完的摘要要到**下一次** ``begin_turn()`` 才生效。
    - **硬上限**（窗口 - max_tokens）：没有选择，只能在发请求前同步压一次。

    为什么摘要要冻结到轮边界：一轮里可能连发十几次模型请求，前缀缓存靠逐字
    匹配。后台压完立刻写进 ``self.summary`` 的话，下一次 ``assemble()`` 会突然
    丢掉刚命中的那段历史，等于主动把自己的缓存打穿。
    """

    def __init__(self, brain=None, memory=None, session_id="local:pet", cancelled=None):
        self.brain, self.memory, self.session_id = brain, memory, session_id
        self.summary = None
        self.last_total_tokens = 0
        self.cancelled = cancelled or (lambda: False)
        self._last_attempt = None
        self._state_lock = threading.Lock()
        self._compacting = False
        self._failures = 0
        self._retry_after = 0.0
        self._turn_summary = _UNSET

    # -- 轮边界与后台触发 ---------------------------------------------------

    def begin_turn(self):
        """一轮开始：把摘要快照固定下来，轮内不再变化。"""
        self._turn_summary = self._load_summary()
        return self._turn_summary

    def end_turn(self):
        """一轮结束：解除冻结，下一轮重新读（这时后台压缩的结果才会生效）。"""
        self._turn_summary = _UNSET

    def _load_summary(self):
        summary = self.summary
        if self.memory:
            _, persisted = self.memory.get_history(self.session_id)
            if persisted and (not summary or persisted["covered_until_id"] > summary["covered_until_id"]):
                summary = persisted
                self.summary = persisted
        return summary

    def request_background(self, history_messages, reason="soft"):
        """软阈值：起一条后台线程压缩，绝不阻塞当前轮。

        用独立 daemon 线程而不是 dispatcher：dispatcher 只有几个 worker，那是给
        对话用的；压缩挤进去会直接抢走一次对话的执行位。
        """
        now = time.monotonic()
        with self._state_lock:
            if self._compacting or now < self._retry_after:
                return False
            self._compacting = True
        # 只拷贝容器：worker 只读消息、不写回，浅拷贝足够且省内存。
        snapshot = list(history_messages)
        threading.Thread(
            target=self._background_worker,
            args=(snapshot, reason),
            daemon=True,
            name=f"bot-compact-{self.session_id}",
        ).start()
        return True

    def _background_worker(self, snapshot, reason):
        ok = False
        try:
            # 独立实例：它的 summary / _last_attempt 都是新的，不会改到正在跑的那一份。
            worker = ContextCompactor(self.brain, self.memory, self.session_id,
                                      cancelled=self.cancelled)
            ok = worker._run_compaction_worker(snapshot)
        except Exception as exc:  # 后台线程里的异常不能无声消失
            log("Compaction", "background_error", session=self.session_id, error=str(exc)[:200])
        finally:
            with self._state_lock:
                self._compacting = False
                if ok:
                    self._failures = 0
                    self._retry_after = 0.0
                else:
                    self._failures += 1
                    delay = min(COMPACTION_RETRY_MAX_SEC,
                                COMPACTION_RETRY_BASE_SEC * (2 ** (self._failures - 1)))
                    self._retry_after = time.monotonic() + delay
                    log("Compaction", "backoff", session=self.session_id,
                        reason=reason, failures=self._failures, retry_in_sec=delay)

    # -- 兼容既有调用点 -----------------------------------------------------

    @staticmethod
    def safe_prefix(history, keep=KEEP_RECENT_MESSAGES):
        """按条数取切点，且不切断工具组。保留给旧调用点与测试。"""
        pending = set()
        cuts = []
        for i, message in enumerate(history):
            if message.get("tool_calls"):
                pending.update(c["id"] for c in message["tool_calls"])
            if message.get("role") == "tool":
                pending.discard(message.get("tool_call_id"))
            if not pending and i + 1 <= len(history) - keep:
                cuts.append(i + 1)
        return history[: cuts[-1]] if cuts else []

    @staticmethod
    def estimate_tokens(messages, tools=None):
        return estimate_text_tokens({"messages": messages, "tools": tools or []})

    # -- 触发 ---------------------------------------------------------------

    @staticmethod
    def should_compact(context_tokens, context_window, ratio):
        return context_tokens >= context_window * ratio

    def keep_recent_tokens(self):
        """压缩后保留多少 token 的原始历史。

        窗口很小时按窗口比例折算，否则会出现"触发压缩了，但按 20k 保留下来一条都
        没被摘要"的死循环。
        """
        cfg = load_config()["vlm"]
        budget = int(cfg["context_window"] * cfg["compaction_threshold_ratio"])
        return max(1000, min(KEEP_RECENT_TOKENS, budget // 2))

    def estimate_pending(self, history_messages):
        """估算"当前未压缩部分"的 token，压缩阈值看这个数。"""
        summary = self._current_summary()
        cutoff = summary["covered_until_id"] if summary else 0
        pending = [m for m in history_messages if m.get("_id", 0) > cutoff]
        if summary:
            pending = [{"role": "user", "content": summary["summary"]}] + pending
        return self.estimate_tokens(pending)

    def update_usage(self, total_tokens, history_messages):
        """记录用量并在软阈值处**发起后台**压缩，返回当前估算值。

        这里绝不能同步压缩：它每步都可能被调用，同步一次就是一次摘要 LLM 调用，
        直接算进这一轮的 TTFT。
        """
        self.last_total_tokens = total_tokens
        estimate = self.estimate_pending(history_messages)
        cfg = load_config()["vlm"]
        if self.should_compact(estimate, cfg["context_window"], cfg["compaction_threshold_ratio"]):
            self.request_background(history_messages, reason="soft")
        return estimate

    def trigger_async_compaction(self, history_messages):
        """兼容旧调用点：语义就是"后台压"，不再同步执行。"""
        return self.request_background(history_messages, reason="manual")

    # -- 主流程 -------------------------------------------------------------

    def _run_compaction_worker(self, history, keep=KEEP_RECENT_MESSAGES,
                               keep_tokens=None, budget_override=None):
        if self.memory and not self.summary:
            _, persisted = self.memory.get_history(self.session_id)
            if persisted:
                self.summary = persisted
        cutoff = self.summary["covered_until_id"] if self.summary else 0
        entries = [m for m in history if m.get("_id", 0) > cutoff]
        if not entries or not self.brain or self.cancelled():
            return False

        cut, split = find_cut_point(entries, keep_tokens or self.keep_recent_tokens(), keep)
        if cut == 0 and keep > 4 and len(entries) >= 6:
            # 上下文压力优先于"至少留 20 条"的舒适下限：几轮工具就把窗口撑爆时，
            # 只留 4 条也必须压，否则只能等溢出报错。
            cut, split = find_cut_point(entries, keep_tokens or self.keep_recent_tokens(), 4)
        prefix = entries[:cut]
        if not prefix:
            log("Compaction", "skipped", session=self.session_id, reason="没有可压缩的前缀",
                entries=len(entries), cut=cut)
            return False
        # 切点只落在轮次起点，这里再验一次：宁可少压，也不产出非法的工具序列。
        while prefix and not is_tool_group_intact(prefix):
            prefix = prefix[:-1]
        if not prefix or not prefix[-1].get("_id"):
            return False

        attempt = (prefix[-1]["_id"], cutoff)
        if attempt == self._last_attempt:
            return False
        self._last_attempt = attempt

        summary = self._summarize(prefix, split=split, budget_override=budget_override)
        if not summary:
            log("Compaction", "skipped", session=self.session_id, reason="摘要生成失败", prefix=len(prefix))
            return False
        if self.cancelled():
            return False
        # 摘要没有比原文更省，就不值得删掉原始证据。
        if estimate_text_tokens(summary) >= sum(estimate_entry_tokens(m) for m in prefix):
            log("Compaction", "skipped", session=self.session_id, reason="摘要未减少文本预算，原始历史保留")
            return False

        new_cutoff = prefix[-1]["_id"]
        if self.memory:
            try:
                self.memory.save_summary(self.session_id, new_cutoff, summary)
            except Exception as exc:
                log("Compaction", "failed", session=self.session_id, error=str(exc)[:200])
                return False
        self.summary = {"covered_until_id": new_cutoff, "summary": summary}
        log("Compaction", "complete", session=self.session_id,
            covered_until_id=new_cutoff, compressed=len(prefix), kept=len(entries) - len(prefix))
        return True

    def compact_for_overflow(self, history, failed_tokens):
        """服务端已经明确报超长之后的强制压缩。

        与常规压缩最大的区别是"压到多大"的参照物。常规路径看配置里的
        context_window，可这个值完全可能比服务端真实窗口大（换了模型没同步改配置、
        中间还隔了一层代理），照它压等于没压。这里以"刚刚失败的那次请求"为基准，
        目标取它的一半，摘要请求自己也被压到同一量级以下（见 _summarize 的
        budget_override）——否则压缩这一步会先撞同一堵墙。

        第一次压不动（保留区自身就超预算）就退到只留 1 条；再压不动，说明问题
        不在"历史太多"，而在"某一条太大"，交给 shrink_oversized 处理。
        """
        failed = int(failed_tokens or 0)
        if failed <= 0 or not history:
            return False
        target = max(2000, failed // 2)
        for keep in (KEEP_RECENT_MESSAGES, 1):
            # 上一轮可能刚因"没有可压的前缀"记过标记，这里要允许重试。
            self._last_attempt = None
            if self._run_compaction_worker(history, keep=keep,
                                           keep_tokens=max(1000, target // 4),
                                           budget_override=target):
                log("Compaction", "overflow_compacted", session=self.session_id,
                    failed_tokens=failed, target=target, keep=keep)
                return True
        log("Compaction", "overflow_skipped", session=self.session_id,
            failed_tokens=failed, target=target, reason="没有可压缩的前缀")
        return False

    @staticmethod
    def shrink_oversized(messages, target_tokens, min_chars=400):
        """最后一道兜底：把单条超大消息就地截短。

        压缩是按"切点"工作的——切点之前进摘要、之后原样保留。所以"一条 20 万字符
        的工具结果"只要落在保留区里，压多少次都还在，请求照样发不出去。这里按体积
        从大到小逐次减半，直到估算降到目标以下。

        只改返回的副本，不动历史行：原文还在库里，模型真需要可以 read_history 取回。
        """
        if target_tokens <= 0 or not messages:
            return messages
        result = copy.deepcopy(messages)
        for _ in range(40):
            if estimate_text_tokens({"messages": result}) <= target_tokens:
                break
            index, size = -1, 0
            for position, message in enumerate(result):
                content = message.get("content")
                if not isinstance(content, str) or message.get("role") == "system":
                    continue
                # 末条是这一轮的提问，砍它等于把用户的话截一半，宁可继续砍别的。
                if position == len(result) - 1 and message.get("role") == "user":
                    continue
                if len(content) > size and len(content) > min_chars:
                    index, size = position, len(content)
            if index < 0:
                break
            content = result[index]["content"]
            keep = max(200, len(content) // 2)
            head, tail = content[: keep // 2], content[-keep // 2:]
            result[index]["content"] = (
                head + "\n…[内容过长，已截断；原始结果仍在历史里，必要时用 read_history 查询]…\n" + tail
            )
        return result

    def _summarize(self, prefix, split=False, budget_override=None):
        """一次结构化摘要调用。已有摘要时走增量更新，不重新总结。"""
        previous = (self.summary or {}).get("summary", "")
        task_checkpoint = None
        if self.memory and hasattr(self.memory, "get_task_state"):
            task_checkpoint = self.memory.get_task_state(self.session_id)

        history_for_model = self.history_for_model(prefix)
        ops = merge_file_ops(previous, extract_file_ops(history_for_model))

        cfg = load_config()["vlm"]
        budget = max(2000, cfg["context_window"] - cfg["max_tokens"] - 1000)
        if budget_override:
            # 服务端已经报过超长时，配置里的窗口不能信（它可能比服务端真实窗口大）。
            # 摘要请求本身也是发给同一个服务端的，必须压到失败请求那个量级以下，
            # 否则"压缩"这一步会先撞同一堵墙，压来压去全是白跑。
            budget = min(budget, max(2000, int(budget_override)))
        conversation = serialize_conversation(history_for_model)
        while estimate_text_tokens(conversation) > budget and len(history_for_model) > 1:
            history_for_model = history_for_model[len(history_for_model) // 2:]
            conversation = serialize_conversation(history_for_model)

        blocks = ["<conversation>\n%s\n</conversation>" % conversation]
        if task_checkpoint:
            # 原始目标与真实执行状态由 task_checkpoint 独立保存，摘要不得改写它。
            blocks.append(
                "<task_checkpoint>\n%s\n</task_checkpoint>"
                % json.dumps(
                    {k: task_checkpoint.get(k) for k in ("original_goal", "status", "latest_request") if task_checkpoint.get(k)},
                    ensure_ascii=False,
                )
            )
        if previous:
            blocks.append("<previous-summary>\n%s\n</previous-summary>" % previous)
        if split:
            # 切点落在一轮中间：后面的消息仍属于同一任务，摘要不能写成"已完成"。
            blocks.append(
                "注意：这段对话在一个**进行中的任务**中间被截断，后面的消息仍在同一个任务里。"
                "「进行中」一节必须写清当前正在做什么、已经执行到哪一步。"
            )
        blocks.append(UPDATE_SUMMARIZATION_PROMPT if previous else SUMMARIZATION_PROMPT)

        try:
            result = self.brain.chat([
                {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(blocks)},
            ])
        except Exception as exc:
            log("Compaction", "failed", session=self.session_id, error=str(exc)[:200])
            return None
        if not result or result.startswith(("【VLM", "【提示", "【模型接口错误")):
            return None
        summary = result[:8000] + format_file_operations(ops)
        goal = (task_checkpoint or {}).get("original_goal")
        if not goal:
            # 没有持久化检查点时，退回到"这段历史里的第一条用户原话"：
            # 目标必须逐字保住，不能只存在于摘要模型的复述里。
            goal = next((_message_text(m) for m in history_for_model if m.get("role") == "user"), "")
        if goal:
            # 原始目标由任务检查点独立保存，**不经摘要改写**地放在最前面：
            # 摘要模型再稳，也有把目标写成"已完成的旧任务"的时候。
            summary = "当前目标（由任务检查点保存，未经摘要改写）：\n%s\n\n%s" % (goal[:6000], summary)
        return summary

    # -- 装配 ---------------------------------------------------------------

    def history_for_model(self, history):
        """历史图片变成持久 ref；只有本轮附件才是视觉输入。"""
        import base64

        from core.media_store import MediaStore

        scope = self.session_id if self.session_id.startswith("qq:") else "local"
        result = copy.deepcopy(history)
        for message_index, message in enumerate(result):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for index, part in enumerate(content):
                if part.get("type") != "image_url":
                    continue
                url = part.get("image_url", {}).get("url", "")
                ref = part.get("media_ref") or (url[8:] if url.startswith("media://") else "")
                if not ref and url.startswith("data:image/"):
                    try:
                        raw = base64.b64decode(url.partition(",")[2], validate=True)
                        asset = MediaStore().add(raw, scope, origin="history",
                                                 message_id=f"history:{self.session_id}:{message.get('_id', message_index)}:{index}")
                        ref = asset["media_ref"]
                    except (ValueError, OSError):
                        pass
                content[index] = {"type": "text", "text": "[历史图片 media_ref=" + ref + "]" if ref else "[历史图片未登记，请引用原消息或重新上传]"}
        return result

    def align_and_assemble_messages(
        self,
        static_system_prompt,
        history_messages,
        volatile_tail_context="",
        image_bytes=None,
        input_image_urls=None,
    ):
        """装配一条请求的消息序列：system → 摘要 → 历史 → 本轮图片 → 尾部数据。

        顺序即前缀：任何位置的改动都会让它之后的 token 全部失效。所以这里是
        **纯投影**——不改写历史、不重排、不追加额外 system。
        """
        history = self.history_for_model(history_messages)
        summary = self._current_summary()
        if summary:
            history = [m for m in history if m.get("_id", 0) > summary["covered_until_id"]]

        transcript = Transcript(history, summary=summary["summary"] if summary else "")
        messages = transcript.project(static_system_prompt)

        if input_image_urls:
            messages.append({
                "role": "user",
                "content": [{"type": "text", "text": "上面是本轮消息及其引用中的图片，历史图片请使用 media_ref。"}]
                + [{"type": "image_url", "image_url": {"url": url, "detail": "auto"}} for url in input_image_urls[:9]],
            })
        if image_bytes:
            messages.append(self._build_capture_message(image_bytes))
        if volatile_tail_context:
            messages.append({"role": "user", "_volatile_context": True, "content": volatile_tail_context})
        return messages

    def _current_summary(self):
        # 轮内冻结：后台压完的摘要只在下一轮 begin_turn() 生效，否则当前轮的
        # 消息前缀会在两次 assemble() 之间跳变，把已经命中的缓存全部打穿。
        if self._turn_summary is not _UNSET:
            return self._turn_summary
        return self._load_summary()

    @staticmethod
    def _build_capture_message(image_bytes):
        """把本轮浏览器/桌面截图包成一条 user 消息。"""
        import base64

        frames = image_bytes if isinstance(image_bytes, list) else [image_bytes]
        frames = [frame for frame in frames if frame]
        if not frames:
            return {"role": "user", "content": "（本轮截图采集失败，没有可用帧）"}
        if len(frames) > 1:
            hint = (f"本轮浏览器真实渲染的连续采样共 {len(frames)} 帧，按时间顺序推进。"
                    "请对比帧间差异确认运动是否连续，再给出一句简短结论。")
        else:
            hint = "本轮浏览器真实渲染的页面截图。请确认排版与视觉效果，再给出一句简短结论。"
        return {
            "role": "user",
            "content": [{"type": "text", "text": hint}]
            + [{"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(frame).decode(), "detail": "auto"}}
               for frame in frames],
        }

    @staticmethod
    def trim_old_outputs(messages):
        """压缩之后仍然超预算时的兜底：把最旧的工具输出压成摘要骨架。

        注意这**不是**常规路径。它就地改写历史，等于把改写点之后的缓存全部作废，
        所以只在"压缩救不回来"的极端情况下调用一次。
        """
        result = copy.deepcopy(messages)
        original_indices = [i for i, m in enumerate(result) if m.get("role") != "system" and not (
            isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]))]
        # 不足 20 条时不能把"保护线"算成 0——那会让所有下标都落在保护区内，
        # 整条兜底路径变成空操作（历史实现就踩过这个坑）。
        protected_from = (original_indices[-KEEP_RECENT_MESSAGES]
                          if len(original_indices) >= KEEP_RECENT_MESSAGES else -1)
        for index, message in enumerate(result):
            if message.get("role") != "tool" or index >= protected_from:
                continue
            raw = message.get("content", "")
            if len(raw) > 1000:
                try:
                    data = json.loads(raw)
                    preserved = {key: data[key] for key in ("ok", "error", "exit_code", "timeout", "path") if key in data}
                except (ValueError, TypeError):
                    preserved = {}
                preserved.update({
                    "output_excerpt": raw[:400] + " ... " + raw[-400:],
                    "truncated": True,
                    "detail": "原始结果已保存，必要时用read_history查询当前会话。",
                })
                message["content"] = json.dumps(preserved, ensure_ascii=False)
        return result
