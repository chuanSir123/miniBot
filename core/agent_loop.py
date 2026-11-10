"""Pi agent harness 的循环骨架。

对应 ``pi/packages/agent/src/agent-loop.ts`` 的 ``runLoop`` 与
``pi/packages/agent/src/agent.ts`` 的 ``PendingMessageQueue`` / ``steer`` / ``followUp``。

这一层只管「什么时候还要再发一次模型请求」，不碰业务：模型流、工具执行、
完成核验、压缩、取消都留在 ``core/harness.py``。``runLoop`` 的内外双层 while
被暴露成 :meth:`AgentLoop.iter_turns`，于是 harness 原有的循环体可以原样保留，
只把驱动它的 ``for turn in itertools.count()`` 换成 pi 的骨架。

pi 的收尾条件原文::

    while (hasMoreToolCalls || pendingMessages.length > 0) { ... }
    const followUpMessages = (await config.getFollowUpMessages?.()) || [];
    if (followUpMessages.length > 0) { pendingMessages = followUpMessages; continue; }
    if (explicitContinuation) { explicitContinuation = false; continue; }
    break;

业务侧每轮用 :meth:`AgentLoop.note_turn` 报告 ``hasMoreToolCalls`` 与
``finishTurn`` 决策；如果本轮直接用 ``continue`` 跳到下一轮（引导打断、连接
重试、核验重跑……），等价于 pi 里 ``finishTurn`` 没有给出 ``end``，骨架会
自动再要一次请求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, List, Optional

QUEUE_MODE_ONE = "one"
QUEUE_MODE_ALL = "all"


class PendingMessageQueue:
    """pi agent.ts 的 ``PendingMessageQueue``。

    ``one`` 一次只交付队首（pi 默认的 steering 行为），``all`` 一次交付全部。
    额外保留 ``append`` 让旧调用方按 list 用法继续工作。
    """

    def __init__(self, mode: str = QUEUE_MODE_ONE) -> None:
        self.messages: List[Any] = []
        self.mode = mode

    def enqueue(self, message: Any) -> None:
        self.messages.append(message)

    def has_items(self) -> bool:
        return len(self.messages) > 0

    def peek(self) -> List[Any]:
        if self.mode == QUEUE_MODE_ALL:
            return list(self.messages)
        return self.messages[:1]

    def drain(self) -> List[Any]:
        drained = self.peek()
        self.messages = self.messages[len(drained):]
        return drained

    def clear(self) -> None:
        self.messages = []

    # 兼容 _steering_inbox 原来的 list 用法
    def append(self, message: Any) -> None:
        self.enqueue(message)

    def __iter__(self):
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __bool__(self) -> bool:
        return self.has_items()


@dataclass
class TurnContext:
    """pi types.ts 的 ``PrepareNextTurnContext``。"""

    message: Any = None
    tool_results: List[Any] = field(default_factory=list)
    context: Any = None
    new_messages: List[Any] = field(default_factory=list)


@dataclass
class FinishTurnDecision:
    """pi types.ts 的 ``FinishTurn`` 返回值：``end`` 或 ``continue``。"""

    action: str = "continue"
    reply: Any = None


class AgentLoop:
    """pi ``runLoop`` 的控制流，不含任何模型 / 工具 / 存储逻辑。"""

    def __init__(
        self,
        *,
        get_steering_messages: Optional[Callable[[], List[Any]]] = None,
        get_follow_up_messages: Optional[Callable[[], List[Any]]] = None,
        emit: Optional[Callable[[str, Any], None]] = None,
        max_turns: int = 0,
    ) -> None:
        self._get_steering_messages = get_steering_messages or (lambda: ())
        self._get_follow_up_messages = get_follow_up_messages or (lambda: ())
        self._emit = emit or (lambda event, payload=None: None)
        self.max_turns = int(max_turns or 0)
        self.turn = 0
        self.pending_messages: List[Any] = []
        self.has_more_tool_calls = True
        self.explicit_continuation = False
        self.last_completed_turn: Optional[TurnContext] = None
        self.exhausted = False
        self._reported = False

    def iter_turns(self) -> Iterator[int]:
        """按 pi 的内外层循环产出 turn 序号；预算耗尽时自然收束。"""
        self.pending_messages = list(self._get_steering_messages() or ())
        while True:
            self.has_more_tool_calls = True
            while self.has_more_tool_calls or self.pending_messages:
                if self.max_turns and self.turn >= self.max_turns:
                    self.exhausted = True
                    return
                self._reported = False
                self._emit("turn_start", {"turn": self.turn})
                yield self.turn
                # 业务侧 continue 时不会调用 note_turn；按 pi 的 finishTurn 缺省
                # 语义，本轮仍然欠一次请求。
                if not self._reported:
                    self.has_more_tool_calls = True
                self._emit(
                    "turn_end",
                    {"turn": self.turn, "context": self.last_completed_turn},
                )
                self.turn += 1
                # pi: pendingMessages = await config.getSteeringMessages?.()
                if not self.pending_messages:
                    self.pending_messages = list(self._get_steering_messages() or ())

            # Agent would stop here. Check for follow-up messages.
            follow_ups = list(self._get_follow_up_messages() or ())
            if follow_ups:
                self.explicit_continuation = False
                self.pending_messages = follow_ups
                continue
            # No natural request was selected, so fulfill the continuation
            # decision with one context-only turn.
            if self.explicit_continuation:
                self.explicit_continuation = False
                continue
            break
        self._emit("agent_end", {"turns": self.turn, "exhausted": self.exhausted})

    def note_turn(
        self,
        *,
        turn_context: Optional[TurnContext] = None,
        has_more_tool_calls: bool = True,
        decision: Optional[FinishTurnDecision] = None,
    ) -> None:
        """业务侧在一轮末尾报告 pi 的 ``lastCompletedTurn`` / ``finishTurn`` 结果。"""
        self._reported = True
        self.last_completed_turn = turn_context or TurnContext()
        if decision is not None and decision.action == "end":
            self.has_more_tool_calls = False
            self.pending_messages = []
        else:
            self.has_more_tool_calls = bool(has_more_tool_calls)


__all__ = [
    "AgentLoop",
    "FinishTurnDecision",
    "PendingMessageQueue",
    "QUEUE_MODE_ALL",
    "QUEUE_MODE_ONE",
    "TurnContext",
]
