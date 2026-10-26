import copy
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config import load_config


class ContextCompactor:
    """
    上下文前缀缓存优化与静默无感压缩管理器
    参考 Claude Code (Tiered Microcompact) 与 DeepSeek-Harness (ctxopt) 设计：
      1. 【前缀缓存对齐 (Prefix Caching)】: 保证首部 System Prompt 绝对静态不可变，将时钟/窗口等易变状态严格放置于尾部最新 User 消息中，实现 95%+ KV 缓存命中率。
      2. 【微压缩 (Microcompact)】: 历史消息中的旧 Base64 大图载荷与超长工具输出自动修剪为轻量占位符。
      3. 【静默语义压缩 (Silent Compaction)】: 当 Total Tokens 达到设定阈值 (默认 256k 的 60%) 时，后台异步提取摘要并无缝置换，新流入的对话无感追加在尾部。
    """

    def __init__(self, brain=None):
        self.brain = brain
        self.lock = threading.Lock()
        self.is_compacting = False
        self.last_total_tokens = 0
        self.compacted_summary: Optional[str] = None

    def get_config(self) -> Tuple[int, float]:
        cfg = load_config().get("vlm", {})
        context_window = cfg.get("context_window", 262144)  # 256k
        ratio = cfg.get("compaction_threshold_ratio", 0.60)  # 60%
        return context_window, ratio

    def update_usage(self, total_tokens: int, history_messages: List[Dict[str, Any]]):
        """记录并评估最新 Token 消耗"""
        self.last_total_tokens = total_tokens
        context_window, ratio = self.get_config()
        threshold = int(context_window * ratio)

        # 达到阈值且当前未在压缩中时，触发后台静默压缩
        if total_tokens >= threshold and not self.is_compacting:
            print(f"[Compaction] 触发静默压缩：当前 Token {total_tokens} >= 阈值 {threshold} (上限 {context_window} 的 {int(ratio*100)}%)")
            self.trigger_async_compaction(history_messages)

    def trigger_async_compaction(self, history_messages: List[Dict[str, Any]]):
        """后台异步启动压缩线程，完全不阻塞当前对话"""
        if self.is_compacting:
            return
        t = threading.Thread(target=self._run_compaction_worker, args=(copy.deepcopy(history_messages),), daemon=True)
        t.start()

    def _run_compaction_worker(self, messages_snapshot: List[Dict[str, Any]]):
        with self.lock:
            self.is_compacting = True

        try:
            # 至少保留末尾 4 条最新活动消息，仅压缩前面的中间历史
            if len(messages_snapshot) <= 6:
                return

            slice_to_compact = messages_snapshot[1:-4]
            if not slice_to_compact or not self.brain:
                return

            # 构建压缩提炼 Prompt
            compact_prompt = (
                "你是一个上下文压缩专家。请对以下对话历史与工具执行日志进行高保真语义总结，提取出：\n"
                "1. 用户核心诉求与关键背景事实；\n"
                "2. 已经完成的操作步骤、创建/修改的文件以及明确结论；\n"
                "3. 当前正在进行或遗留的待办任务。\n"
                "请保持精炼结构化（Markdown 格式，控制在 500 字以内）：\n\n"
                + json.dumps(slice_to_compact, ensure_ascii=False)
            )

            summary = self.brain.chat([{"role": "user", "content": compact_prompt}])
            if summary and not summary.startswith("【VLM"):
                with self.lock:
                    self.compacted_summary = summary
                print("[Compaction] ✅ 后台静默语义压缩完成，摘要已就绪并将在下次组装时生效！")
        except Exception as e:
            print(f"[Compaction] 后台压缩异常: {e}")
        finally:
            with self.lock:
                self.is_compacting = False

    def align_and_assemble_messages(
        self,
        static_system_prompt: str,
        history_messages: List[Dict[str, Any]],
        volatile_tail_context: str = "",
        image_bytes: Optional[bytes] = None
    ) -> List[Dict[str, Any]]:
        """
        按照顶级前缀缓存 (Prefix Caching) 规范组装全量消息：
          [0] 绝对静态的 System Prompt (包含工具规范、身份、原则，永不注入动态时钟/易变状态)
          [1] 压缩摘要块 (如果已生成静默压缩)
          [2..N-1] 历史轮次 (执行 Tier-1 Microcompact：剔除旧 Base64 大图、修剪超长旧工具输出)
          [N] 最新 User 轮次：将所有易变上下文 (当前时钟、活动窗口、屏幕截屏) 严格附着于此！
        """
        assembled: List[Dict[str, Any]] = []

        # 1. 静态前缀：Index 0 绝对稳定，最大化命中 KV 缓存
        assembled.append({"role": "system", "content": static_system_prompt})

        # 2. 如果存在已完成的压缩摘要，插入摘要锚点
        with self.lock:
            current_summary = self.compacted_summary

        working_history = [copy.deepcopy(m) for m in history_messages if m.get("role") != "system"]

        if current_summary and len(working_history) > 4:
            # 仅保留末尾 4 条，中间替换为压缩摘要
            tail_turns = working_history[-4:]
            assembled.append({
                "role": "user",
                "content": f"【系统前序对话历史与操作摘要】:\n{current_summary}"
            })
            assembled.append({
                "role": "assistant",
                "content": "我已掌握之前的历史进度与操作背景，继续协助您完成当前任务。"
            })
            working_history = tail_turns

        # 3. Tier-1 微压缩 (Microcompact): 过滤历史轮次中的冗余大体积载荷
        for idx, msg in enumerate(working_history[:-1]):
            role = msg.get("role")
            # 清理历史用户消息中的旧图片 Base64 (防止多轮对话重复发送大图爆显存/Token)
            if role == "user" and isinstance(msg.get("content"), list):
                filtered_content = []
                for item in msg["content"]:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        filtered_content.append({"type": "text", "text": "[历史屏幕截图已分析，载荷已修剪]"})
                    else:
                        filtered_content.append(item)
                msg["content"] = filtered_content

            # 清理过早的工具返回超大文本 (历史结果只需保留核心状态)
            if role == "tool":
                raw_c = msg.get("content", "")
                if len(raw_c) > 600 and idx < len(working_history) - 3:
                    msg["content"] = raw_c[:300] + f"\n...[历史工具输出过长，已微修剪，保留前300字符]"

            assembled.append(msg)

        # 4. 尾部：最新一轮消息 + 附带所有动态易变上下文 (时钟、活动窗口、截屏)
        if working_history:
            latest_msg = working_history[-1]
            # 如果提供了易变感知上下文，拼装至最新消息尾部，确保不破坏前面的缓存行
            if volatile_tail_context:
                orig_c = latest_msg.get("content", "")
                tail_addon = f"\n\n【实时动态感知 (附于末尾)】\n{volatile_tail_context}"
                if isinstance(orig_c, str):
                    latest_msg["content"] = orig_c + tail_addon
                elif isinstance(orig_c, list):
                    latest_msg["content"].append({"type": "text", "text": tail_addon})

            assembled.append(latest_msg)

        return assembled
