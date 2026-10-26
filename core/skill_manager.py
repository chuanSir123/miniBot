import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)


class Skill:
    def __init__(self, name: str, description: str, func: Callable[[Dict[str, Any]], str], filename: str):
        self.name = name
        self.description = description
        self.func = func
        self.filename = filename

    def run(self, context: Dict[str, Any]) -> str:
        try:
            return self.func(context)
        except Exception as e:
            return f"执行技能 [{self.name}] 发生错误: {e}"


class SkillManager:
    """
    Skill 自动化管理与动态热加载
    负责：
      1. 动态扫描 skills/ 目录加载自定义工具
      2. 导出所有可用 Skill 的名称与描述供 Jev 的 Choice 原语进行决策
      3. 接收 LLM/VLM 提炼总结的代码，生成并热装载新 Skill
    """

    def __init__(self, skills_dir: Path = SKILLS_DIR):
        self.skills_dir = skills_dir
        self.skills: Dict[str, Skill] = {}
        self._ensure_default_skills()
        self.reload_skills()

    def _ensure_default_skills(self):
        """写入一个内置示例 Skill"""
        demo_file = self.skills_dir / "system_status.py"
        if not demo_file.exists():
            code = '''"""
name: system_status
description: 检查本机系统基本状态与时间
"""
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
                        # 读取 docstring 或模块属性
                        name = getattr(mod, "NAME", py_file.stem)
                        desc = getattr(mod, "DESCRIPTION", (mod.__doc__ or "自定义技能").strip())
                        self.skills[name] = Skill(name=name, description=desc, func=func, filename=py_file.name)
            except Exception as e:
                print(f"[SkillManager] 加载技能 {py_file.name} 失败: {e}")

    def list_skill_names(self) -> List[str]:
        return list(self.skills.keys())

    def get_skill_descriptions(self) -> Dict[str, str]:
        return {name: skill.description for name, skill in self.skills.items()}

    def execute_skill(self, name: str, context: Optional[Dict[str, Any]] = None) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"未找到名为 [{name}] 的技能"
        return skill.run(context or {})

    def register_new_skill(self, filename: str, code_content: str) -> bool:
        """接收 Agent 总结生成的 Python 代码并热装载"""
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
