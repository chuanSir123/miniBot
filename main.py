import argparse
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

# 确保在 Windows 控制台下支持 UTF-8 编码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.brain import VLMBrain
from core.config import is_configured, load_config
from core.harness import AgentHarness
from core.jev import JevClient
from core.memory import MiniMemory
from core.perception import LocalPerception
from core.proactive import ProactiveEngine
from core.skill_manager import SkillManager
from webui.server import run_server


class MiniBotApp:
    def __init__(self, mode: str = "pet"):
        self.mode = mode  # "pet", "napcat", "all"
        self.cfg = load_config()

        # 初始化核心组件
        self.memory = MiniMemory()
        self.perception = LocalPerception()
        self.jev = JevClient()
        self.vlm = VLMBrain()
        self.skill_manager = SkillManager()

        # 初始化智能体执行骨架 (Agent Harness)
        self.harness = AgentHarness(
            brain=self.vlm,
            jev=self.jev,
            memory=self.memory,
            perception=self.perception,
            skill_manager=self.skill_manager,
            max_turns=5
        )

        # 通道实例
        self.pet_widget = None
        self.napcat_mgr = None
        self.napcat_channel = None

        # 初始化主动协同引擎
        self.proactive = ProactiveEngine(
            memory=self.memory,
            perception=self.perception,
            jev=self.jev,
            vlm=self.vlm,
            skill_manager=self.skill_manager,
            notify_callback=self._handle_proactive_notification
        )

    def _handle_user_query(self, user_text: str, channel_source: str = "pet") -> str:
        """统一处理用户交互输入 (桌宠输入框 或 QQ 消息) - 接入 AgentHarness"""
        print(f"[MiniBot] 处理来自 [{channel_source}] 的用户提问: {user_text}")
        self.memory.add_event(f"用户: {user_text}", source=f"user_{channel_source}", importance=1.5)

        # 0. 自动提取核心事实 (如 '记住我叫小明', '记住我喜欢玩黑神话')
        extracted_facts = self.memory.auto_extract_facts_from_text(user_text)
        if extracted_facts:
            print(f"[Memory] 自动提取并持久化核心事实: {extracted_facts}")

        # 1. 结合记忆上下文 (检索高相关度+近期的历史记忆)
        relevant_memories = self.memory.search_events_with_decay(user_text, limit=3)
        context = self.memory.build_context_prompt(max_events=5)
        if relevant_memories:
            rel_strs = [f"- {m['content']}" for m in relevant_memories]
            context += "\n【与提问相关的历史记忆线索】:\n" + "\n".join(rel_strs)

        # 1.5 技能意图路由 (Two-Stage: Jev快速意图路由 + 无参秒级直驱 / 有参VLM提取槽位)
        skill_names = self.skill_manager.list_skill_names()
        if skill_names and self.jev.is_configured():
            candidates = self.skill_manager.get_candidate_options_for_jev()
            chosen_raw = self.jev.route_skill_intent(user_text, candidates)
            chosen_skill_name = chosen_raw.split()[0] if chosen_raw != "chat_only" else "chat_only"

            if chosen_skill_name in self.skill_manager.skills:
                skill_obj = self.skill_manager.get_skill(chosen_skill_name)
                print(f"[MiniBot] Jev 快脑命中技能: {skill_obj.name} (需要参数: {skill_obj.has_parameters})")

                if not skill_obj.has_parameters:
                    # 无参技能：0 延迟秒级直驱
                    res = skill_obj.run({})
                    self.memory.add_event(f"执行技能 [{skill_obj.name}] 结果: {res}", source="skill")
                    if self.pet_widget:
                        self.pet_widget.finish_say_safe(res, action=3)
                    return res
                else:
                    # 有参技能：利用 VLM 从聊天记录和提问中精准提取槽位参数
                    slot_prompt = (
                        f"用户提问: '{user_text}'\n"
                        f"目标技能: {skill_obj.name} ({skill_obj.description})\n"
                        f"所需参数模式: {json.dumps(skill_obj.parameters_schema, ensure_ascii=False)}\n"
                        f"请从用户提问或历史中精准提取对应参数，仅输出合法纯 JSON 字符串，例如 {{\"port\": 8080}}。严禁输出额外文字。"
                    )
                    slot_res = self.vlm.chat([{"role": "user", "content": slot_prompt}])
                    try:
                        clean_json = slot_res.strip("` \n").replace("json\n", "")
                        extracted_args = json.loads(clean_json)
                    except Exception:
                        extracted_args = {"query": user_text}

                    res = skill_obj.run(extracted_args)
                    self.memory.add_event(f"执行有参技能 [{skill_obj.name}] 结果: {res}", source="skill")
                    if self.pet_widget:
                        self.pet_widget.finish_say_safe(res, action=3)
                    return res

        # 2. 定义流式双通道回调 (输出 VLM 中间思考文本与工具状态)
        def on_text_chunk(delta_text, accumulated_text):
            if self.pet_widget:
                self.pet_widget.stream_say_safe(accumulated_text)
            if channel_source != "pet":
                sys.stdout.write(delta_text)
                sys.stdout.flush()

        def on_status_change(status_text):
            print(f"[Harness Status] {status_text}")
            if self.pet_widget:
                self.pet_widget.stream_say_safe(f"【{status_text}】")

        # 3. 驱动 Harness 智能体执行循环 (Jev 快速裁决 + 工具调用 + 流式双通道)
        reply = self.harness.run(
            user_prompt=user_text,
            context_prompt=context,
            on_text_chunk=on_text_chunk,
            on_status_change=on_status_change
        )

        self.memory.add_event(f"助手回复: {reply}", source="bot", importance=1.2)
        if self.pet_widget:
            self.pet_widget.finish_say_safe(reply, action=3)

        return reply

    def _handle_proactive_notification(self, message: str, posture: str = "general", anim_action: int = 3):
        """主动协同触发通知分发"""
        if self.pet_widget:
            self.pet_widget.finish_say_safe(message, action=anim_action)

    def run(self):
        print("=" * 60)
        print(f"       🤖 MiniBot 启动中... (当前运行模式: {self.mode})")
        print("=" * 60)

        # 启动主动协同引擎
        self.proactive.start()

        # 根据模式启动通道
        if self.mode in ("napcat", "all"):
            self._start_napcat()

        if self.mode in ("pet", "all"):
            self._start_pet()
        else:
            # 纯后台模式（如仅 NapCat），保持主线程等待
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stop()

    def _start_pet(self):
        try:
            from PyQt5.QtWidgets import QApplication
            from channels.pet_channel import DesktopPetWidget
        except ImportError:
            print("[MiniBot] 错误：缺少 PyQt5 相关依赖，请使用 launcher 安装。")
            return

        app = QApplication(sys.argv)
        pet_dir_cfg = self.cfg.get("channels", {}).get("pet", {}).get("pet_dir", "assets/pets/ikunchick")
        pet_path = Path(pet_dir_cfg)
        if not pet_path.is_absolute():
            pet_path = Path(__file__).resolve().parent / pet_path

        def on_open_settings():
            run_server(port=18080, open_browser=True, blocking=False)

        def on_input(text):
            # 后台线程执行 Harness，保证桌宠动画和界面流畅不卡顿
            threading.Thread(target=self._handle_user_query, args=(text, "pet"), daemon=True).start()

        self.pet_widget = DesktopPetWidget(
            pet_dir=str(pet_path),
            on_user_input=on_input,
            on_open_settings=on_open_settings
        )
        self.pet_widget.show()
        self.pet_widget.say("哈喽！我是你的专属伴侣鸡哥~", action=3)

        sys.exit(app.exec_())

    def _start_napcat(self):
        from channels.napcat_channel import NapCatChannel
        from channels.napcat_manager import NapCatManager

        nap_cfg = self.cfg.get("channels", {}).get("napcat", {})
        ws_url = nap_cfg.get("ws_url", "ws://127.0.0.1:3001")
        access_token = nap_cfg.get("access_token", "")
        qq_num = nap_cfg.get("qq_number", "")

        # 启动本地内嵌 NapCat 进程
        self.napcat_mgr = NapCatManager()
        self.napcat_mgr.start(qq_number=qq_num)

        # 启动 WebSocket 监听客户端
        def on_qq_msg(raw_text, event):
            return self._handle_user_query(raw_text, channel_source="qq")

        self.napcat_channel = NapCatChannel(
            ws_url=ws_url,
            access_token=access_token,
            on_message_callback=on_qq_msg
        )
        self.napcat_channel.start()

    def stop(self):
        print("\n[MiniBot] 正在优雅退出...")
        self.proactive.stop()
        if self.napcat_mgr:
            self.napcat_mgr.stop()
        if self.napcat_channel:
            self.napcat_channel.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MiniBot 主程序")
    parser.add_argument("--mode", choices=["pet", "napcat", "all"], default="pet", help="运行模式")
    args = parser.parse_args()

    bot = MiniBotApp(mode=args.mode)
    bot.run()
