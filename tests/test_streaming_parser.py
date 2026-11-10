# -*- coding: utf-8 -*-
import unittest
import tempfile
import os
from pathlib import Path
from core.streaming_parser import unescape_partial_json_str, extract_partial_field, parse_tool_delta


class TestStreamingParser(unittest.TestCase):
    def test_unescape_partial_json_str(self):
        s = 'hello\\nworld\\t\\"quote\\"\\\\end'
        self.assertEqual(unescape_partial_json_str(s), 'hello\nworld\t"quote"\\end')

        # Test incomplete escape at end
        s_inc = 'hello\\nworld\\'
        self.assertEqual(unescape_partial_json_str(s_inc), 'hello\nworld\\')

        # Test unicode escape
        s_uni = 'Unicode: \\u4e2d\\u6587'
        self.assertEqual(unescape_partial_json_str(s_uni), 'Unicode: 中文')

    def test_extract_partial_field(self):
        # Full closed field
        json_full = '{"file_path": "index.html", "content": "hello"}'
        self.assertEqual(extract_partial_field(json_full, "file_path"), "index.html")
        self.assertEqual(extract_partial_field(json_full, "content"), "hello")

        # Incomplete field at end
        json_part = '{"file_path": "game.py", "content": "import sys\\nprint(\\"hi\\")'
        self.assertEqual(extract_partial_field(json_part, "file_path"), "game.py")
        self.assertEqual(extract_partial_field(json_part, "content"), 'import sys\nprint("hi")')

    def test_parse_tool_delta_write(self):
        partial = '{"file_path": "test.html", "content": "<!DOCTYPE html>\\n<html>\\n<head>\\n  <title>Test</title>\\n'
        parsed = parse_tool_delta("write", partial, scope="main")
        self.assertTrue(parsed["is_file_op"])
        self.assertEqual(parsed["file_path"], "test.html")
        self.assertEqual(parsed["action"], "create")
        self.assertEqual(parsed["lines_added"], 5)
        self.assertEqual(parsed["lines_deleted"], 0)
        self.assertIn("<title>Test</title>", parsed["snippet"])

    def test_parse_tool_delta_edit(self):
        partial = '{"file_path": "test.py", "target": "line1\\nline2\\n", "replacement": "line_new1\\nline_new2\\nline_new3\\n'
        parsed = parse_tool_delta("edit", partial, scope="main")
        self.assertTrue(parsed["is_file_op"])
        self.assertEqual(parsed["file_path"], "test.py")
        self.assertEqual(parsed["action"], "edit")
        self.assertEqual(parsed["lines_deleted"], 3)
        self.assertEqual(parsed["lines_added"], 4)
        self.assertIn("line_new3", parsed["snippet"])

    def test_parse_tool_delta_write(self):
        partial = '{"file_path": "skills/calc/SKILL.md", "content": "# 技能\\n正文\\n"}'
        parsed = parse_tool_delta("write", partial, scope="main")
        self.assertTrue(parsed["is_file_op"])
        self.assertEqual(parsed["file_path"], "skills/calc/SKILL.md")
        self.assertEqual(parsed["lines_added"], 3)


if __name__ == "__main__":
    unittest.main()
