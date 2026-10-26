import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable[[Dict[str, Any]], str],
        filename: str,
        parameters_schema: Optional[Dict[str, Any]] = None
    ):
        self.name = name
        self.description = description
        self.func = func
        self.filename = filename
        self.parameters_schema = parameters_schema or {}
        self.has_parameters = bool(self.parameters_schema)

    def run(self, context: Dict[str, Any]) -> str:
        try:
            return self.func(context)
        except Exception as e:
            return f"执行技能 [{self.name}] 发生错误: {e}"


class SkillManager:
    """
    Skill 自动化管理与动态热加载
    支持：
      1. 无参技能 (Zero-shot Direct Run - Jev快脑毫秒级触发)
      2. 有参技能 (Two-stage Slot Filling - Jev选意图 + VLM从聊天历史提取槽位参数)
    """

    def __init__(self, skills_dir: Path = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.skills: Dict[str, Skill] = {}
        self._ensure_default_skills()
        self.reload_skills()

    def _ensure_default_skills(self):
        """写入内置示例技能"""
        demo_file = self.skills_dir / "system_status.py"
        if not demo_file.exists():
            code = '''"""
name: system_status
description: 检查本机系统基本状态、本地时间与当前工作目录
"""
# 声明参数模式 (空表示无参技能，Jev命中后直接秒级执行)
PARAMETERS = {}

import time
import os

def execute(context: dict) -> str:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"【系统状态】当前本地时间: {now_str}，工作目录: {os.getcwd()}"
'''
            demo_file.write_text(code, encoding="utf-8")

    def reload_skills(self):
        self.skills.clear()
        for py_file in self.skills_dir.glob("*.py"):
            if py_file.name.startswith("__"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    func = getattr(mod, "execute", None)
                    if callable(func):
                        name = getattr(mod, "NAME", py_file.stem)
                        doc = (mod.__doc__ or "").strip()
                        desc = getattr(mod, "DESCRIPTION", None)
                        if not desc:
                            for line in doc.splitlines():
                                if line.lower().startswith("description:"):
                                    desc = line.split(":", 1)[1].strip()
                                    break
                        if not desc:
                            desc = doc.splitlines()[0] if doc else "自定义技能"

                        schema = getattr(mod, "PARAMETERS", getattr(mod, "SCHEMA", {}))
                        self.skills[name] = Skill(
                            name=name,
                            description=desc,
                            func=func,
                            filename=py_file.name,
                            parameters_schema=schema
                        )
            except Exception as e:
                print(f"[SkillManager] 加载技能 {py_file.name} 失败: {e}")

    def list_skill_names(self) -> List[str]:
        return list(self.skills.keys())

    def get_skill(self, name: str) -> Optional[Skill]:
        return self.skills.get(name)

    def get_candidate_options_for_jev(self) -> List[str]:
        """生成供 Jev Choice 原语做决策的候选选项列表"""
        options = []
        for name, s in self.skills.items():
            param_tag = "有参提取" if s.has_parameters else "无参直驱"
            options.append(f"{name} [{param_tag}: {s.description[:40]}]")
        options.append("chat_only")
        return options

    def execute_skill(self, name: str, context: Optional[Dict[str, Any]] = None) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"未找到名为 [{name}] 的技能"
        return skill.run(context or {})

    def register_new_skill(self, filename: str, code_content: str) -> bool:
        """接收并热装载新技能"""
        if not filename.endswith(".py"):
            filename += ".py"
        target_path = self.skills_dir / filename
        try:
            target_path.write_text(code_content, encoding="utf-8")
            self.reload_skills()
            return True
        except Exception as e:
            print(f"[SkillManager] 注册新技能失败: {e}")
            return False
