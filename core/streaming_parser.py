# -*- coding: utf-8 -*-
"""Streaming Parser for Tool Call Arguments.

Enables real-time parsing of streaming JSON arguments from LLM/VLM tool calls,
such as write_file and edit_file, allowing diff-style dynamic line counters
(+X, -Y) and live code snippet previews before the tool call completes.
"""
import re
from pathlib import Path
from typing import Dict, Any


def unescape_partial_json_str(s: str) -> str:
    """Safely unescape JSON string characters without crashing on incomplete trailing escapes."""
    res = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt == 'n':
                res.append('\n')
                i += 2
                continue
            elif nxt == 'r':
                res.append('\r')
                i += 2
                continue
            elif nxt == 't':
                res.append('\t')
                i += 2
                continue
            elif nxt == '"':
                res.append('"')
                i += 2
                continue
            elif nxt == '\\':
                res.append('\\')
                i += 2
                continue
            elif nxt == '/':
                res.append('/')
                i += 2
                continue
            elif nxt == 'u' and i + 5 < n:
                try:
                    res.append(chr(int(s[i + 2 : i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        res.append(c)
        i += 1
    return "".join(res)


def extract_partial_field(json_str: str, field_name: str) -> str:
    """Extract a string field value from an incomplete or streaming JSON string."""
    if not json_str:
        return ""
    # Case 1: Completed field with closing quote: "fieldName": "value"
    pattern = rf'"{re.escape(field_name)}"\s*:\s*"((?:[^"\\]|\\.)*)"'
    m = re.search(pattern, json_str)
    if m:
        return unescape_partial_json_str(m.group(1))

    # Case 2: Incomplete field with opened quote: "fieldName": "partial...
    pattern_open = rf'"{re.escape(field_name)}"\s*:\s*"(.*)'
    m_open = re.search(pattern_open, json_str, re.DOTALL)
    if m_open:
        return unescape_partial_json_str(m_open.group(1))

    return ""


def parse_tool_delta(tool_name: str, partial_args: str, scope: str = "main") -> Dict[str, Any]:
    """Parse a tool's in-flight arguments and compute real-time diff and preview metrics."""
    result: Dict[str, Any] = {
        "tool": tool_name or "",
        "is_file_op": False,
        "file_path": "",
        "action": "call",
        "lines_added": 0,
        "lines_deleted": 0,
        "total_chars": len(partial_args or ""),
        "snippet": "",
    }

    if not tool_name:
        return result

    tool_lower = tool_name.lower()

    if tool_lower in ("write_file", "edit_file"):
        result["is_file_op"] = True

        if tool_lower == "write_file":
            file_path = extract_partial_field(partial_args, "file_path") or extract_partial_field(partial_args, "path")
            content = extract_partial_field(partial_args, "content")
            result["file_path"] = file_path
            result["action"] = "write"

            if content:
                # Count lines accurately
                raw_lines = content.split("\n")
                result["lines_added"] = len(raw_lines) if content else 0
                snippet_lines = raw_lines[-5:]
                result["snippet"] = "\n".join(snippet_lines)
            else:
                result["lines_added"] = 0
                result["snippet"] = ""

            # Check if file already exists in workspace
            if file_path:
                try:
                    from core.workspace import resolve_file
                    target = resolve_file(file_path, scope)
                    if target.is_file():
                        existing_text = target.read_text(encoding="utf-8", errors="replace")
                        result["lines_deleted"] = len(existing_text.splitlines())
                        result["action"] = "overwrite"
                    else:
                        result["action"] = "create"
                except Exception:
                    pass

        elif tool_lower == "edit_file":
            file_path = extract_partial_field(partial_args, "file_path") or extract_partial_field(partial_args, "path")
            target_text = extract_partial_field(partial_args, "target")
            repl_text = extract_partial_field(partial_args, "replacement")
            result["file_path"] = file_path
            result["action"] = "edit"

            if target_text:
                result["lines_deleted"] = len(target_text.split("\n"))
            if repl_text:
                raw_lines = repl_text.split("\n")
                result["lines_added"] = len(raw_lines)
                result["snippet"] = "\n".join(raw_lines[-5:])
            elif target_text:
                result["snippet"] = "\n".join(target_text.split("\n")[-5:])

    elif tool_lower == "execute_command":
        result["action"] = "command"
        cmd = extract_partial_field(partial_args, "command")
        result["snippet"] = cmd

    elif tool_lower == "capture_screen":
        result["action"] = "capture"
        reason = extract_partial_field(partial_args, "reason")
        fp = extract_partial_field(partial_args, "file_path")
        result["file_path"] = fp
        result["snippet"] = reason

    return result
