"""Opt-in, process-isolated Python skills.

技能目录支持 Agent Skills 规范形态：skills/<name>/SKILL.md（必须）+ scripts/（可选）。
渐进式披露：目录技能只把 name/description/SKILL.md 路径注入 <skills> 段，模型用
read 读全文，再用 bash 直接跑该技能 scripts/ 下的 CLI 脚本。
execute_skill 只作为内部兼容入口保留，不再是模型可见的执行链路。
"""

import ast
import json
import re
import sys
import threading
from pathlib import Path
from core.config import load_config, save_config
from core.process import run_bounded
from core import agent_skills

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
INSTRUCTIONS_ONLY = ":instructions:"


def validate_args(schema, args):
    if not isinstance(schema, dict) or not isinstance(
        schema.get("properties", {}), dict
    ):
        raise ValueError("技能参数模式必须是JSON Schema对象")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(key, str) for key in required
    ):
        raise ValueError("required必须是参数名列表")
    if not isinstance(args, dict):
        raise ValueError("技能参数必须是对象")
    for key in schema.get("required", []):
        if key not in args:
            raise ValueError(f"缺少参数 {key}")
    types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in args.items():
        prop = schema.get("properties", {}).get(key)
        if prop is None:
            if schema.get("additionalProperties") is False:
                raise ValueError(f"未知参数 {key}")
            continue
        if not isinstance(prop, dict):
            raise ValueError(f"参数 {key} 的模式无效")
        kind = prop.get("type")
        if kind in types and (
            not isinstance(value, types[kind])
            or kind in ("integer", "number")
            and isinstance(value, bool)
        ):
            raise ValueError(f"参数 {key} 类型错误")
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError(f"参数 {key} 不在枚举范围")
    return args


def _builtin_result_payload(output):
    """内置技能的 stdout 可能混有 console 日志，取最后一行可解析的 JSON。"""
    for line in reversed((output or "").strip().splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


class Skill:
    def __init__(
        self, name, description, filename, schema=None, trusted=False,
        directory=None, body=None,
    ):
        self.name, self.description, self.filename = name, description, filename
        self.parameters_schema = schema or {}
        if not isinstance(self.parameters_schema, dict):
            raise ValueError("PARAMETERS必须是JSON Schema对象")
        self.has_parameters = bool(
            self.parameters_schema.get("properties")
            or self.parameters_schema.get("required")
        )
        self.trusted = trusted
        self.directory = Path(directory) if directory else None
        self.body = body  # SKILL.md 正文（仅目录技能）
        self.is_folder = directory is not None
        self.instructions_only = self.is_folder and filename == INSTRUCTIONS_ONLY


class SkillManager:
    def __init__(self, skills_dir=SKILLS_DIR):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.skills = {}
        self.stats_path = self.skills_dir / "usage.json"
        self.reload_skills()

    def _ignored_names(self):
        raw = load_config().get("skills", {}).get("ignore", [])
        if not isinstance(raw, list):
            return set()
        return {str(name).strip() for name in raw if isinstance(name, str) and name.strip()}

    def reload_skills(self):
        result = {}
        builtin_ctx = self.skills_dir.resolve() == SKILLS_DIR.resolve()
        # 被忽略的技能：不注入提示词、不进 Jev 候选、execute_skill 报不存在。
        ignored = self._ignored_names() if builtin_ctx else set()
        trusted = load_config()["skills"].get("trusted_python", [])
        for path in self.skills_dir.glob("*.py"):
            try:
                if path.stat().st_size > 100000:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                if not any(
                    isinstance(n, ast.FunctionDef) and n.name == "execute"
                    for n in tree.body
                ):
                    continue
                values = {}
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id in (
                                "NAME",
                                "DESCRIPTION",
                                "PARAMETERS",
                                "SCHEMA",
                            ):
                                values[target.id] = ast.literal_eval(node.value)
                doc = ast.get_docstring(tree) or ""
                desc = values.get("DESCRIPTION") or next(
                    (
                        line.split(":", 1)[1].strip()
                        for line in doc.splitlines()
                        if line.lower().startswith("description:")
                    ),
                    path.stem,
                )
                result[path.stem] = Skill(
                    path.stem,
                    desc,
                    path.name,
                    values.get("PARAMETERS", values.get("SCHEMA", {})),
                    trusted=path.name in trusted,
                )
            except (ValueError, SyntaxError, OSError):
                continue
        # Fixed built-ins are independent of mutable generated Python skills.
        # STT 需要可配置云端 + 本地推理，已收回内置（core/stt.py）；
        # ego_lite 仍是目录技能（skills/ego-lite），实现保留在 core/ego.py。
        if builtin_ctx:
            from core.default_skills import REGISTRY

            for name, spec in REGISTRY.items():
                if name in ignored:
                    continue
                if name == "anysearch" and not load_config()["anysearch"]["enabled"]:
                    continue
                if name == "stt" and not load_config()["stt"]["enabled"]:
                    continue
                result[name] = Skill(
                    name,
                    spec["description"],
                    ":builtin:",
                    spec["parameters"],
                    trusted=True,
                    body=spec.get("instructions"),
                )
        # Agent Skills 目录形态：skills/<name>/SKILL.md（渐进式披露）。
        folder_skills, folder_diagnostics = agent_skills.scan(self.skills_dir)
        self.folder_diagnostics = folder_diagnostics
        for name, askill in folder_skills.items():
            if name in ignored:
                continue
            # 目录技能的开关联动（设置页停用即隐藏，同原内置行为）。
            if builtin_ctx:
                section = {"anysearch": "anysearch", "ego_lite": "ego_lite"}.get(name)
                if section is not None and not load_config()[section]["enabled"]:
                    continue
            if name in result:
                # 内置/已有技能优先；目录里的 SKILL.md 作为其指令文档，供披露时读取。
                existing = result[name]
                if existing.filename in (":builtin:",) and askill.body:
                    existing.body = askill.body
                    existing.directory = askill.directory
                continue
            result[name] = Skill(
                name,
                askill.description,
                askill.entry if askill.has_entry else INSTRUCTIONS_ONLY,
                askill.parameters,
                trusted=True,
                directory=askill.directory,
                body=askill.body,
            )
        with self.lock:
            self.skills = result
            self._stamp = self._directory_signature()

    def _directory_signature(self):
        files = []
        for path in self.skills_dir.iterdir():
            if path.suffix != ".py":
                continue
            try:
                stat = path.stat()
                files.append((path.name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        # 目录技能（SKILL.md / parameters.json / entry 脚本）变化也要触发重载。
        for md in self.skills_dir.glob("*/SKILL.md"):
            try:
                stat = md.stat()
                files.append((md.as_posix(), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        for extra in self.skills_dir.glob("*/parameters.json"):
            try:
                stat = extra.stat()
                files.append((extra.as_posix(), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        for script in self.skills_dir.glob("*/scripts/*"):
            try:
                if script.is_file():
                    stat = script.stat()
                    files.append((script.as_posix(), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        cfg = load_config()
        return (
            tuple(sorted(files)),
            tuple(cfg["skills"]["trusted_python"]),
            tuple(sorted(cfg["skills"].get("ignore", []))),
            cfg["anysearch"]["enabled"],
            cfg["ego_lite"]["enabled"],
            cfg["stt"]["enabled"],
        )

    def refresh_if_changed(self):
        with self.lock:
            if self._directory_signature() != self._stamp:
                self.reload_skills()

    @staticmethod
    def _name(name):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
            raise ValueError("技能名只能是英文、数字、下划线")
        return name

    def list_skill_names(self):
        self.refresh_if_changed()
        with self.lock:
            return [n for n, s in self.skills.items() if s.trusted]

    def get_skill(self, name):
        self.refresh_if_changed()
        with self.lock:
            return self.skills.get(name)

    def get_candidate_options_for_jev(self):
        return self.list_skill_names() + ["chat_only"]

    def describe(self, query="", limit=16, stable=False):
        """Describe trusted skills.

        stable=True keeps a query-independent order so the same skill set always
        renders byte-identical text. That is what lets the catalog live inside the
        cached system prefix instead of being re-prefilled on every tool round.
        """
        from core.memory import keywords

        self.refresh_if_changed()
        terms = keywords(query)
        with self.lock:
            skills = [s for s in self.skills.values() if s.trusted]
            if stable:
                # name is the only ordering key that does not depend on the request.
                skills.sort(key=lambda s: s.name)
            else:
                skills.sort(
                    key=lambda s: sum(
                        k in (s.description + " " + s.name).lower() for k in terms
                    ),
                    reverse=True,
                )
            return [
                {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters_schema,
                    # 渐进披露：模型用 read 直接读这个绝对路径，不再经技能工具。
                    "filePath": self.skill_md_path(s),
                }
                for s in skills[:limit]
            ]

    @staticmethod
    def skill_md_path(skill) -> str:
        """技能 SKILL.md 的绝对路径（<skills> 段的 <location>）。"""
        base = skill.directory if skill.directory else SKILLS_DIR / skill.name
        return str(base / "SKILL.md").replace("\\", "/")

    def execute_skill(
        self,
        name,
        context=None,
        dispatch=None,
        cancel_event=None,
        media_scope="local",
        timeout=None,
        background=False,
    ):
        skill = self.get_skill(name)
        if not skill:
            if (
                self.skills_dir.resolve() == SKILLS_DIR.resolve()
                and name in self._ignored_names()
            ):
                raise ValueError(
                    f"技能 {name} 已被忽略（skills.ignore），如需启用请先在设置页移除该名字"
                )
            if (self.skills_dir / (self._name(name) + ".py")).exists():
                raise ValueError(f"技能 {name} 文件存在但加载失败，请检查execute入口及静态DESCRIPTION/PARAMETERS格式")
            raise ValueError(f"技能 {name} 不存在，请先list_skills确认，创建新技能走 skill-creator 流程（write 写 skills/<name>/SKILL.md）；不要猜测技能名")
        if not skill.trusted:
            raise ValueError(
                f"技能 {name} 已加载但未授权；activate_skill 只能重新扫描，不能授予权限。"
                f"单文件技能需把文件名加入配置 skills.trusted_python，"
                f"或改造成目录形态 skills/{name}/SKILL.md（目录技能默认可信）"
            )
        # Agent Skills 渐进式披露：无参调用时返回 SKILL.md 完整指令。
        # 有 SKILL.md 正文且带必填参数的技能，无参调用先披露（模型还不该知道怎么填参）；
        # 无必填参数的技能（如 system_status）无参即执行。
        _ctx = context or {}
        if (
            skill.body
            and not _ctx
            and (skill.instructions_only or skill.parameters_schema.get("required"))
        ):
            result = {
                "ok": True,
                "skill": name,
                "instructions": skill.body,
                "skill_dir": str(skill.directory) if skill.directory else "",
                "scripts": agent_skills.load_skill(skill.directory).scripts() if skill.directory else [],
                "output": "以上是技能的完整说明与步骤。请按说明执行任务；带 arguments 再次调用以执行动作。",
            }
            self.record_usage(name, True)
            return result
        args = validate_args(skill.parameters_schema, context or {})
        try:
            if skill.filename == ":builtin:":
                # 本地 whisper 在 CPU 上转写长音频明显超过默认 45s，单独放宽。
                builtin_timeout = 300 if name == "stt" else 45
                result = run_bounded(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("default_skill_worker.py")),
                    ],
                    timeout=builtin_timeout,
                    max_bytes=120000,
                    input_text=json.dumps(
                        {"name": name, "arguments": args}, ensure_ascii=False
                    ),
                    cancel_event=cancel_event,
                )
                # Preserve semantic failure, rather than treating printed errors as success.
                if not result.get("timeout") and not result.get("cancelled"):
                    payload = _builtin_result_payload(result.get("output"))
                    if payload is None:
                        result["ok"] = False
                    else:
                        result = {
                            **{k: v for k, v in result.items() if k != "output"},
                            **payload,
                        }
            else:
                from core.workspace import workspace_for
                from core.media_store import MediaStore
                worker_args = dict(args)
                worker_args["_minibot"] = {"media_scope": media_scope, "media_root": str(MediaStore().root),
                                           "workspace": str(workspace_for(media_scope)),
                                           "retention_days": load_config()["memory"]["retention_days"]}
                from core.task_manager import TaskManager
                script_path = (skill.directory / skill.filename) if skill.is_folder else (self.skills_dir / skill.filename)
                cmd = [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("skill_worker.py")),
                    str(script_path),
                ]
                effective_timeout = timeout
                if effective_timeout is None:
                    skills_cfg = load_config().get("skills", {})
                    effective_timeout = 120 if name == "image_gen" else skills_cfg.get("timeout_sec", 15)

                tm = TaskManager.get_instance()
                task = tm.start_task(
                    cmd,
                    name=f"skill:{name}",
                    cwd=str(workspace_for(media_scope)),
                    input_text=json.dumps(worker_args, ensure_ascii=False),
                    metadata={"skill_name": name, "media_scope": media_scope, "arguments": args},
                )

                if background:
                    result = {
                        "ok": True,
                        "status": "running",
                        "task_id": task.task_id,
                        "name": name,
                        "output": f"技能 [{name}] 已在后台启动运行。\n任务ID: {task.task_id}\n你可以调用 check_task 或 wait_task 查询状态与结果。",
                    }
                else:
                    if cancel_event:
                        def _watch_cancel():
                            cancel_event.wait()
                            task.cancel()
                        threading.Thread(target=_watch_cancel, daemon=True).start()

                    result = tm.wait_task(task.task_id, timeout_sec=effective_timeout)
                if not result.get("timeout") and not result.get("cancelled") and result.get("output", "").startswith("__MINIBOT_RESULT__"):
                    payload = json.loads(result["output"][len("__MINIBOT_RESULT__"):])
                    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
                        raise ValueError("技能返回了无效结构化结果")
                    transport_ok = result["ok"]
                    result = {**{k: v for k, v in result.items() if k != "output"}, **payload}
                    result["ok"] = transport_ok and payload["ok"]
            self.record_usage(name, result.get("ok", False))
            return result
        except Exception:
            self.record_usage(name, False)
            raise

    def record_usage(self, name, success):
        with self.lock:
            try:
                stats = json.loads(self.stats_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stats = {}
            entry = stats.setdefault(name, {"success": 0, "failure": 0})
            entry["success" if success else "failure"] += 1
            self._atomic(self.stats_path, stats)

    @staticmethod
    def _atomic(path, obj):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def register_new_skill(self, filename, code_content, description=None, parameters=None, activate=False):
        # Generated executable code is a candidate, never imported during registration.
        name = self._name(Path(filename).stem)
        if filename not in (name, name + ".py"):
            raise ValueError("不允许技能目录穿越")
        if len(code_content.encode("utf-8")) > 100000:
            raise ValueError("技能源码超过100KB")
        tree = ast.parse(code_content)
        if not any(
            isinstance(node, ast.FunctionDef) and node.name == "execute"
            for node in tree.body
        ):
            raise ValueError("技能必须包含execute(context)入口")
        metadata = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("PARAMETERS", "SCHEMA"):
                        metadata[target.id] = ast.literal_eval(node.value)
        schema = parameters if parameters is not None else metadata.get("PARAMETERS", metadata.get("SCHEMA"))
        if schema is None:
            # Recover common context.get()/context[key] arguments without importing code.
            properties, required = {}, []
            entry = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
            positional = entry.args.posonlyargs + entry.args.args
            if not positional:
                raise ValueError("execute必须接收一个context参数")
            context_name = positional[0].arg
            for node in ast.walk(entry):
                key, default = None, None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == context_name and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant):
                    key = node.args[0].value
                    if len(node.args) > 1:
                        try:
                            default = ast.literal_eval(node.args[1])
                        except (ValueError, TypeError):
                            pass
                elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == context_name and isinstance(node.slice, ast.Constant):
                    key = node.slice.value
                    if isinstance(key, str) and not key.startswith("_") and key not in required:
                        required.append(key)
                # `_` 前缀是框架注入的运行时字段（如 _minibot），不是用户入参。
                if isinstance(key, str) and not key.startswith("_"):
                    kind = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}.get(type(default))
                    properties[key] = {"type": kind, "default": default} if kind else {}
            schema = {"type": "object", "properties": properties, "required": required}
        if not isinstance(schema, dict):
            raise ValueError("parameters必须是JSON Schema对象")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError("required必须是参数名列表")
        # 运行时字段由框架注入，即使被误写进 required 也不能拦技能执行。
        injected = [key for key in required if key.startswith("_")]
        if injected:
            schema = {**schema, "required": [key for key in required if not key.startswith("_")]}
        validate_args({**schema, "required": []}, {})
        if description is not None:
            code_content += "\nDESCRIPTION = " + repr(description) + "\n"
        if metadata.get("PARAMETERS", metadata.get("SCHEMA")) != schema:
            code_content += "\nPARAMETERS = " + repr(schema) + "\n"
        # 新技能脚本必须能被 bash 直接跑：缺 __main__ 时自动补 CLI 入口。
        if 'if __name__ == "__main__"' not in code_content:
            code_content += (
                '\n\nif __name__ == "__main__":\n'
                '    import sys\n'
                '    from pathlib import Path\n'
                '    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))\n'
                '    from core.skill_cli import main\n'
                '    raise SystemExit(main(execute))\n'
            )
        if len(code_content.encode("utf-8")) > 100000:
            raise ValueError("技能源码及元数据超过100KB")
        # 统一保存为 Agent Skills 目录形态：skills/<name>/{SKILL.md, scripts/<name>.py}
        skill_dir = self.skills_dir / name
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        script_path = scripts_dir / (name + ".py")
        if script_path.exists():
            script_path.with_suffix(".py.bak").write_bytes(script_path.read_bytes())
        script_path.write_text(code_content, encoding="utf-8")
        self._atomic(skill_dir / "parameters.json", schema)
        self._write_skill_md(skill_dir, name, description, schema)
        self.reload_skills()
        return True

    def _write_skill_md(self, skill_dir, name, description, schema):
        """生成/更新 SKILL.md：保留已有正文，只刷新 frontmatter 与 parameters 说明。"""
        import json as _json
        existing = skill_dir / "SKILL.md"
        body = ""
        if existing.exists():
            _, body = agent_skills.parse_frontmatter(existing.read_text(encoding="utf-8"))
        if description is None:
            description = (body.splitlines() or [""])[0][:300] or name
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        param_lines = []
        for key, prop in props.items():
            req = "必填" if key in schema.get("required", []) else "可选"
            ptype = prop.get("type", "any")
            pdesc = prop.get("description", "")
            param_lines.append(f"- `{key}` ({ptype}, {req})：{pdesc}")
        usage = "\n".join(param_lines) if param_lines else "无参数，传 `{}` 即可。"
        # 生成可直接复制的真实命令与参数示例，文档里不留占位符。
        example = {
            key: {"string": "示例文本", "integer": 1, "number": 1, "boolean": True}.get(
                (prop or {}).get("type"), ""
            )
            for key, prop in props.items()
            if key in set(schema.get("required", []) or [])
        }
        example_json = _json.dumps(example, ensure_ascii=False)
        script_path = f"skills/{name}/scripts/{name}.py"
        content = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"entry: scripts/{name}.py\n"
            f"---\n\n"
            f"# {name}\n\n"
            f"本文件是**纯 Markdown 指令**：先用 `read` 读完，再用 `bash` 直接跑本技能的脚本\n"
            f"（传 `cwd=project`，命令中的路径相对项目根目录）：\n\n"
            f"    python {script_path} --arguments '{example_json}'\n\n"
            f"脚本在 `skills/{name}/scripts/{name}.py`，末尾的 `__main__` 块调用 `core.skill_cli.main` "
            f"解析参数并输出一行 JSON；需要媒体文件时先用 `get_media` 取 `file_path` 再传路径。\n\n"
            f"## 参数\n\n{usage}\n"
        )
        if body and body not in content:
            content += "\n## 使用说明\n\n" + body + "\n"
        existing.write_text(content, encoding="utf-8")

    def activate_python_skill(self, name):
        name = self._name(name)
        if self.skills_dir.resolve() == SKILLS_DIR.resolve():
            from core.default_skills import REGISTRY
            if name in REGISTRY:
                raise ValueError("不能覆盖内置技能")
        script = self.skills_dir / name / "scripts" / (name + ".py")
        legacy = self.skills_dir / (name + ".py")
        if not script.exists() and legacy.exists():
            # 旧版单文件技能迁移到目录形态（scripts 换名备份）。
            register_new = self.register_new_skill(
                name + ".py", legacy.read_text(encoding="utf-8")
            )
            if register_new:
                legacy.with_suffix(".py.migrated.bak").write_bytes(legacy.read_bytes())
                legacy.unlink()
        if not script.exists():
            raise ValueError(
                f"找不到技能 {name} 的脚本；正确形态是 skills/{name}/scripts/{name}.py。"
                "新建或改造技能走 skill-creator 流程"
            )
        self.reload_skills()
        skill = self.skills.get(name)
        if not skill or not skill.trusted:
            raise ValueError("技能启用失败，请检查源码和元数据")
        return {"name": name, "status": "active", "path": str(script), "parameters": skill.parameters_schema}

    def unavailable_skills(self):
        self.refresh_if_changed()
        with self.lock:
            result = [{"name": name, "status": "untrusted", "next_action": "activate_skill"}
                      for name, skill in self.skills.items() if not skill.trusted]
            return result[:100]
