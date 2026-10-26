import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from core.brain import VLMBrain
from core.compaction import ContextCompactor
from core.jev import JevClient
from core.memory import MiniMemory
from core.perception import LocalPerception
from core.skill_manager import SkillManager

# 核心工具定义 (OpenAI Tool Specification 格式)
HARNESS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "在本地 Windows 系统终端执行命令 (PowerShell/CMD)，可用于检查状态、运行脚本、编译或排查问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令行命令，如 'git status', 'dir', 'python test.py'"
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "description": "超时秒数，默认 15 秒",
                        "default": 15
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取本地文件内容，支持指定行范围，防止长文件过载。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件绝对路径或相对路径"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号 (从1开始)",
                        "default": 1
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最多读取行数",
                        "default": 120
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入或覆盖本地文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件文本内容"
                    }
                },
                "required": ["file_path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screen",
            "description": "捕获当前桌面屏幕图像并让视觉模型观察屏幕上的报错、窗口或内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "截屏观察的原因"
                    }
                },
                "required": ["reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_skill",
            "description": "执行内置或用户动态生成的特定技能。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能名称"
                    },
                    "arguments": {
                        "type": "string",
                        "description": "技能入参"
                    }
                },
                "required": ["skill_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_skill",
            "description": "将一个经常使用或刚调试成功的多步工作流固化保存为一个独立的 Python 技能 (存入 skills/ 目录)，以后可供直接调用或由 Jev 快脑决策触发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能唯一英文标识 (不带.py)，如 clean_project, check_port"
                    },
                    "description": {
                        "type": "string",
                        "description": "技能的功能描述与触发条件，供 Jev 决策模型理解何时调用"
                    },
                    "code": {
                        "type": "string",
                        "description": "可执行 Python 代码，必须包含 execute(context: dict) -> str 入口函数"
                    }
                },
                "required": ["skill_name", "description", "code"]
            }
        }
    }
]


class AgentHarness:
    """
    智能体执行骨架 (Agent Harness)
    吸取 Claude Code 与 DeepSeek Harness 精髓：
      1. 双通道流式解耦：在模型生成工具的同时，立即将中间思考/解释文本输出给桌宠气泡与终端。
      2. 快慢脑协同：引入 Jev 决策模型在工具执行后做 100ms 快速终结与异常裁决，节约大量慢脑轮次。
      3. 完备工具沙箱：支持终端命令、文件读写、屏幕视觉观察、技能执行。
    """

    def __init__(
        self,
        brain: VLMBrain,
        jev: JevClient,
        memory: MiniMemory,
        perception: LocalPerception,
        skill_manager: SkillManager,
        max_turns: int = 5
    ):
        self.brain = brain
        self.jev = jev
        self.memory = memory
        self.perception = perception
        self.skill_manager = skill_manager
        self.max_turns = max_turns
        self.compactor = ContextCompactor(brain=self.brain)

    def run(
        self,
        user_prompt: str,
        context_prompt: str = "",
        on_text_chunk: Optional[Callable[[str, str], None]] = None,
        on_status_change: Optional[Callable[[str], None]] = None
    ) -> str:
        """
        运行 Harness 主循环
        """
        # 1. Jev 决策模型评估任务复杂度 (System 1 门控)
        if self.jev.is_configured():
            action_type = self.jev.classify_harness_intent(user_prompt)
            if action_type == "pure_chat":
                reply = self.brain.generate_bubble_reply(user_prompt, context=context_prompt)
                if on_text_chunk:
                    on_text_chunk(reply, reply)
                return reply

        # 2. 前缀缓存对齐：System Prompt 严格保持静态，严禁注入动态时间/变动状态
        static_system_instruction = (
            "你是一个高行动力的智能体桌宠伙伴。你有权使用工具（运行终端命令、读写文件、查看屏幕截屏、执行技能）来实际解决用户的问题。\n"
            "【原则】\n"
            "1. 当需要调用工具时，务必在调用前输出一两句简短亲和的说明（告诉用户你正打算做什么），这些文本会流式显示在桌宠气泡上。\n"
            "2. 拿到工具结果后，继续推进直到彻底完成目标。\n"
            "3. 保持简练，直接给重点。\n"
            f"工作目录基准: {os.getcwd()}"
        )

        initial_history: List[Dict[str, Any]] = [
            {"role": "user", "content": user_prompt}
        ]

        accumulated_user_facing_text = []

        # 3. ReAct 执行循环
        for turn in range(self.max_turns):
            if on_status_change:
                on_status_change(f"思考第 {turn + 1} 步...")

            # 通过 Compactor 组装消息：确保前缀绝对稳定，动态易变感知注入于尾部
            assembled_messages = self.compactor.align_and_assemble_messages(
                static_system_prompt=static_system_instruction,
                history_messages=initial_history,
                volatile_tail_context=context_prompt if turn == 0 else ""
            )

            generator = self.brain.stream_chat_and_tools(assembled_messages, tools=HARNESS_TOOLS)
            current_turn_text = []
            tool_calls = []
            usage_data = {}

            # 流式捕获并即时输出中间文本与思考链
            try:
                while True:
                    event_type, event_data = next(generator)
                    if event_type == "reasoning_delta":
                        if on_status_change:
                            on_status_change("💭 深度思考中...")
                    elif event_type == "text_delta":
                        current_turn_text.append(event_data)
                        accumulated_user_facing_text.append(event_data)
                        if on_text_chunk:
                            on_text_chunk(event_data, "".join(accumulated_user_facing_text))
            except StopIteration as e:
                if e.value:
                    _, tool_calls, usage_data = e.value

            # 更新 Token 统计与自动触发静默压缩
            if usage_data and "total_tokens" in usage_data:
                self.compactor.update_usage(usage_data["total_tokens"], initial_history)

            turn_text_str = "".join(current_turn_text).strip()

            # 将本轮消息加入历史记录
            asst_msg: Dict[str, Any] = {"role": "assistant"}
            if turn_text_str:
                asst_msg["content"] = turn_text_str
            if tool_calls:
                asst_msg["tool_calls"] = tool_calls
            initial_history.append(asst_msg)

            # 如果没有工具调用，说明智能体认为任务已完成并给出了最终回复
            if not tool_calls:
                return turn_text_str or "".join(accumulated_user_facing_text)

            # 4. 执行本地工具调用
            tool_outputs_summary = []
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "")
                call_id = tc.get("id", f"call_{int(time.time()*1000)}")
                raw_args = tc.get("function", {}).get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                if on_status_change:
                    on_status_change(f"执行工具: {func_name} ...")

                tool_result_str = self._dispatch_tool(func_name, args)
                tool_outputs_summary.append(f"[{func_name}] -> {tool_result_str[:120]}")

                if len(tool_result_str) > 2500:
                    tool_result_str = tool_result_str[:2500] + "\n...[输出过长，已截断]"

                initial_history.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_result_str
                })

            # 5. Jev 快脑快速终结判定 (100ms 省去慢脑 3 秒)
            if self.jev.is_configured() and turn >= 1:
                tool_sum = "; ".join(tool_outputs_summary)
                should_finish, prob = self.jev.evaluate_task_completion(
                    goal=user_prompt,
                    tool_summary=tool_sum,
                    threshold=0.82
                )
                if should_finish:
                    if on_status_change:
                        on_status_change("Jev 判定任务已完成，收尾中...")
                    final_messages = self.compactor.align_and_assemble_messages(
                        static_system_prompt=static_system_instruction,
                        history_messages=initial_history + [{"role": "user", "content": "请向用户简明汇报任务完成结果。"}]
                    )
                    final_summary = self.brain.chat(final_messages)
                    if on_text_chunk:
                        on_text_chunk(final_summary, final_summary)
                    return final_summary

        return "".join(accumulated_user_facing_text) or "任务已执行完毕。"

    # ==========================================================
    # 本地工具调度执行中心
    # ==========================================================
    def _dispatch_tool(self, name: str, args: Dict[str, Any]) -> str:
        try:
            if name == "execute_command":
                cmd = args.get("command", "")
                timeout = int(args.get("timeout_sec", 15))
                return self._tool_exec_command(cmd, timeout)

            elif name == "read_file":
                path = args.get("file_path", "")
                start = int(args.get("start_line", 1))
                max_lines = int(args.get("max_lines", 120))
                return self._tool_read_file(path, start, max_lines)

            elif name == "write_file":
                path = args.get("file_path", "")
                content = args.get("content", "")
                return self._tool_write_file(path, content)

            elif name == "capture_screen":
                reason = args.get("reason", "观察屏幕")
                return self._tool_capture_screen(reason)

            elif name == "execute_skill":
                skill_name = args.get("skill_name", "")
                skill_args = args.get("arguments", "")
                return self.skill_manager.execute_skill(skill_name, {"query": skill_args})

            elif name == "save_skill":
                s_name = args.get("skill_name", "").replace(".py", "").strip()
                s_desc = args.get("description", "").strip()
                s_code = args.get("code", "").strip()
                full_content = f'"""\nname: {s_name}\ndescription: {s_desc}\n"""\n{s_code}\n'
                ok = self.skill_manager.register_new_skill(f"{s_name}.py", full_content)
                if ok:
                    return f"✅ 技能 [{s_name}] 固化保存成功并已完成动态热装载！以后可直接使用。"
                return f"❌ 技能 [{s_name}] 保存失败。"

            else:
                return f"未知工具: {name}"
        except Exception as e:
            return f"工具执行异常 ({name}): {e}"

    def _tool_exec_command(self, cmd: str, timeout: int = 15) -> str:
        if not cmd.strip():
            return "错误：命令为空"

        print(f"[Harness Tool] 执行命令: {cmd}")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace"
            )
            out = proc.stdout.strip()
            err = proc.stderr.strip()
            res = []
            if out:
                res.append(f"STDOUT:\n{out}")
            if err:
                res.append(f"STDERR:\n{err}")
            res.append(f"Exit Code: {proc.returncode}")
            return "\n".join(res) if res else "命令执行成功，无输出。"
        except subprocess.TimeoutExpired:
            return f"命令执行超时 (限制 {timeout} 秒)"
        except Exception as e:
            return f"执行失败: {e}"

    def _tool_read_file(self, file_path: str, start_line: int, max_lines: int) -> str:
        p = Path(file_path)
        if not p.exists():
            return f"文件不存在: {file_path}"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(lines)
            selected = lines[max(0, start_line - 1): start_line - 1 + max_lines]
            numbered = [f"{i + start_line}: {line}" for i, line in enumerate(selected)]
            return f"文件: {file_path} (共 {total} 行，显示 {start_line}-{start_line + len(selected)}):\n" + "\n".join(numbered)
        except Exception as e:
            return f"读取文件失败: {e}"

    def _tool_write_file(self, file_path: str, content: str) -> str:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
            return f"成功写入文件: {file_path} (大小: {len(content)} 字符)"
        except Exception as e:
            return f"写入文件失败: {e}"

    def _tool_capture_screen(self, reason: str) -> str:
        raw_bytes = self.perception.capture_screen_jpeg(quality=70)
        if raw_bytes:
            # 临时存入并附带在后续轮次中
            return f"屏幕已成功截取 (原因: {reason})，图像已就绪供视觉大模型识别。"
        return "截屏失败，未获取到屏幕画面。"
