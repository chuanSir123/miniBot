"""Jev (System One) 请求体预算控制。

实测：决策服务对 state 字段有硬上限 131,072 字符，超出直接返回
  400 {"error":{"code":"state_too_long", ...}}
而真实一轮 review 的 planning_state 曾达到 163,984 字符（web 长任务），
于是 Jev 不是"慢"，是根本没返回。

体积几乎全部来自对判断无用的字段：
  - context.task_state.file_changes[*].diff   完整 diff，10 条合计 ~58k
  - state.current_sources[*].content          源码全文，8 个文件 ~41k
  - state.current_trace[*].result             全量工具输出 ~33k

这里的策略是"分级裁剪"：按信息价值从低到高逐级压缩，每一级之后重新
计量，一旦进入预算就停手。绝不静默丢字段而不留痕。
"""
import json

# 服务端拒绝阈值（实测报错原文：at most 131072）
MAX_STATE_CHARS = 131072
# 主动裁剪目标：不只是"不被拒"，还要够快。同一 endpoint 实测：
#   263B -> 1.4s   11KB -> 1.3s   16KB -> 1.8s   47KB -> 3.6s   131KB -> 8.7s
# 体积几乎线性决定 prefill；问题数量几乎没有影响（39 个 vs 10 个只差 0.1s）
# 16KB 是"还认得出这轮干了什么"与"够快"的分界点。
SAFE_STATE_CHARS = 16 * 1024


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def _each_trace(wire):
    for item in wire.get("current", {}).get("current_trace", []) or []:
        if isinstance(item, dict):
            yield item


def _trim_strings(node, limit, skip_keys=()):
    """递归把超长字符串截断，返回是否改动。"""
    changed = False
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in skip_keys:
                continue
            if isinstance(value, str) and len(value) > limit:
                node[key] = value[:limit] + f"…[已截断 {len(value) - limit} 字符]"
                changed = True
            elif isinstance(value, (dict, list)):
                changed = _trim_strings(value, limit, skip_keys) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _trim_strings(item, limit, skip_keys) or changed
    return changed


def trim_long_strings(node, limit, skip_keys=()):
    """公开版：递归截断结构里的超长字符串，返回是否改动。"""
    return _trim_strings(node, limit, skip_keys)


def _drop_file_diffs(wire):
    """file_changes.diff 是整份改动，Jev 只需要知道"哪个文件改了多少行"。"""
    state = wire.get("context", {}).get("task_state") or {}
    changes = state.get("file_changes")
    if not isinstance(changes, list):
        return False
    changed = False
    for item in changes:
        if isinstance(item, dict) and "diff" in item:
            diff = item.pop("diff") or ""
            item["diff_chars"] = len(diff)
            changed = True
    if changed:
        state["file_changes"] = changes[-8:]
    return changed


def _trim_trace(limit):
    def apply(wire):
        changed = False
        for item in _each_trace(wire):
            result = item.get("result")
            if isinstance(result, dict):
                changed = _trim_strings(result, limit) or changed
            elif isinstance(result, str) and len(result) > limit:
                item["result"] = result[:limit]
                changed = True
            args = item.get("arguments")
            if isinstance(args, dict):
                changed = _trim_strings(args, limit) or changed
        return changed
    return apply


def _keep_recent_traces(count):
    def apply(wire):
        trace = wire.get("current", {}).get("current_trace")
        if isinstance(trace, list) and len(trace) > count:
            # 保留最新的尾部：核验看重的是最后一次真实操作。
            wire["current"]["current_trace"] = trace[-count:]
            return True
        return False
    return apply


def _trim_sources(limit, max_files=None):
    def apply(wire):
        sources = wire.get("current", {}).get("current_sources")
        if not isinstance(sources, list):
            return False
        changed = False
        if max_files and len(sources) > max_files:
            wire["current"]["current_sources"] = sources[:max_files]
            sources = wire["current"]["current_sources"]
            changed = True
        for item in sources:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str) and len(content) > limit:
                item["content"] = content[:limit]
                item["truncated"] = True
                changed = True
        return changed
    return apply


def _trim_chat(limit, drop_quotes=False):
    def apply(wire):
        changed = False
        for branch in ("context",):
            chat = wire.get(branch, {}).get("recent_chat")
            if not isinstance(chat, list):
                continue
            for item in chat:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and len(text) > limit:
                    item["text"] = text[:limit]
                    changed = True
                if drop_quotes and item.get("quoted_messages"):
                    item["quoted_messages"] = []
                    changed = True
                if drop_quotes and item.get("attachments"):
                    item["attachments"] = []
                    changed = True
        return changed
    return apply


def _trim_last_tools(limit):
    def apply(wire):
        tools = wire.get("context", {}).get("task_state", {}).get("last_tools")
        if not isinstance(tools, list):
            return False
        changed = False
        for item in tools:
            if isinstance(item, dict):
                preview = item.get("result_preview")
                if isinstance(preview, str) and len(preview) > limit:
                    item["result_preview"] = preview[:limit]
                    changed = True
        return changed
    return apply


def _drop_key(branch, key):
    def apply(wire):
        node = wire.get(branch)
        if isinstance(node, dict) and key in node:
            node[key] = None
            return True
        return False
    return apply


# 从"最不心疼"到"最舍不得"，逐级加压。
_LEVELS = [
    ("drop_file_change_diffs", _drop_file_diffs),
    ("trim_last_tools_600", _trim_last_tools(600)),
    ("trim_trace_1200", _trim_trace(1200)),
    ("trim_sources_2500", _trim_sources(2500)),
    ("trim_chat_600", _trim_chat(600)),
    ("trim_trace_600", _trim_trace(600)),
    ("trim_sources_1200", _trim_sources(1200)),
    ("drop_recent_media", _drop_key("context", "recent_media")),
    ("trim_chat_200", _trim_chat(200, drop_quotes=True)),
    ("trim_trace_300", _trim_trace(300)),
    ("trim_sources_600", _trim_sources(600, max_files=4)),
    ("keep_recent_8_traces", _keep_recent_traces(8)),
    ("drop_conversation_summary", _drop_key("context", "conversation_summary")),
    ("drop_task_state_artifacts", _drop_key("context", "task_state")),
    ("drop_recent_chat", _drop_key("context", "recent_chat")),
]


def fit_wire(wire, budget: int = SAFE_STATE_CHARS):
    """把 wire 裁进预算，返回 (wire, 已施加的步骤名列表)。"""
    applied = []
    if _size(wire) <= budget:
        return wire, applied
    for name, apply in _LEVELS:
        try:
            changed = apply(wire)
        except Exception:
            continue  # 某一级对结构不适用时跳过，不影响后续
        if changed:
            applied.append(name)
        if _size(wire) <= budget:
            break
    return wire, applied


def fit_state(state: str, budget: int = SAFE_STATE_CHARS):
    """对外入口：把序列化后的 state 裁进预算。

    非 JSON 结构一律原样返回——那种情况只能由调用方自己负责，
    在这里硬截断反而会把 JSON 截坏，服务端会报结构错误。
    """
    if not isinstance(state, str) or len(state) <= budget:
        return state, []
    try:
        wire = json.loads(state)
    except ValueError:
        return state, []
    if not isinstance(wire, dict):
        return state, []
    wire, applied = fit_wire(wire, budget)
    return json.dumps(wire, ensure_ascii=False), applied
