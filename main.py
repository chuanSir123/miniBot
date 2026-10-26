import argparse
import os
import sys
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
        """统一处理用户交互输入 (桌宠输入框 或 QQ 消息)"""
        print(f"[MiniBot] 处理来自 [{channel_source}] 的用户提问: {user_text}")
        self.memory.add_event(f"用户: {user_text}", source=f"user_{channel_source}", importance=1.5)

        # 0. 自动提取核心事实 (如 '记住我叫小明', '记住我喜欢玩黑神话')
        extracted_facts = self.memory.auto_extract_facts_from_text(user_text)
        if extracted_facts:
            print(f"[Memory] 自动提取并持久化核心事实: {extracted_facts}")

        # 1. 尝试匹配并调用 Skill
        skill_names = self.skill_manager.list_skill_names()
        if skill_names and self.jev.is_configured():
            # 快脑挑选是否需要调用特定技能
            chosen_skill = self.jev.choose_action(
                f"用户请求: {user_text}\n可用技能: {self.skill_manager.get_skill_descriptions()}",
                candidate_actions=skill_names + ["chat_only"]
            )
            if chosen_skill != "chat_only" and chosen_skill in skill_names:
                print(f"[MiniBot] 快脑决策触发技能: {chosen_skill}")
                res = self.skill_manager.execute_skill(chosen_skill, {"query": user_text})
                self.memory.add_event(f"技能执行结果: {res}", source="skill")
                return res

        # 2. 结合记忆上下文调用 VLM 慢脑回复 (检索高相关度+近期的历史记忆)
        relevant_memories = self.memory.search_events_with_decay(user_text, limit=3)
        context = self.memory.build_context_prompt(max_events=5)
        if relevant_memories:
            rel_strs = [f"- {m['content']}" for m in relevant_memories]
            context += "\n【与提问相关的历史记忆线索】:\n" + "\n".join(rel_strs)

        # 按需截屏 (若提到“看屏幕”、“看看代码”、“当前页面”)
        img_bytes = None
        if any(kw in user_text for kw in ["看", "屏幕", "代码", "报错", "界面"]):
            img_bytes = self.perception.capture_screen_jpeg(quality=70)

        reply = self.vlm.generate_bubble_reply(user_text, context=context, image_bytes=img_bytes)
        self.memory.add_event(f"助手回复: {reply}", source="bot", importance=1.2)
        return reply

    def _handle_proactive_notification(self, message: str, action_type: str):
        """主动协同触发通知分发"""
        # 1. 如果桌宠处于运行态，桌宠头顶弹气泡
        if self.pet_widget:
            self.pet_widget.say(message, action=3)

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
        pet_dir = self.cfg.get("channels", {}).get("pet", {}).get("pet_dir", r"C:\Users\admin\.codex\pets\ikunchick")

        def on_open_settings():
            run_server(port=18080, open_browser=True, blocking=False)

        def on_input(text):
            reply = self._handle_user_query(text, channel_source="pet")
            if self.pet_widget:
                self.pet_widget.say(reply, action=3)

        self.pet_widget = DesktopPetWidget(
            pet_dir=pet_dir,
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
