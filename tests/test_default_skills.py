import io
import json
import os
from unittest.mock import patch
from test_core import Fixture
from core import config
from core.default_skills import anysearch, anysearch_request
from core.skill_manager import SkillManager
from core import ego
import unittest
import tempfile
from pathlib import Path
from core.process import run_bounded


class DefaultSkillTests(Fixture):
    def test_first_start_guide_once_and_menu_can_reopen(self):
        import launcher
        import contextlib

        output = io.StringIO()
        with (
            patch.object(launcher, "PROJECT_ROOT", self.root),
            patch(
                "core.ego.status",
                return_value={
                    "environment": {"supported": True},
                    "installed_cli": False,
                },
            ),
            contextlib.redirect_stdout(output),
        ):
            launcher._show_default_skill_guide()
            launcher._show_default_skill_guide()
            self.assertEqual(
                output.getvalue().count("[首次启动 / 默认技能安装引导]"), 1
            )
            launcher._show_default_skill_guide(force=True)
            self.assertEqual(
                output.getvalue().count("[首次启动 / 默认技能安装引导]"), 2
            )
            self.assertIn("https://decision.vllm-sr.ai/", output.getvalue())
            self.assertIn("[6]", output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows cmd quoting")
    def test_windows_cmd_path_with_spaces_keeps_script_on_stdin(self):
        with tempfile.TemporaryDirectory(prefix="mini bot cmd ") as folder:
            executable = Path(folder) / "ego-browser.cmd"
            executable.write_text("@echo off\necho %1\nmore\n", encoding="ascii")
            text = "literal $() ` not a command"
            result = run_bounded(
                ego._command(str(executable), ["nodejs"]), input_text=text, timeout=5
            )
        self.assertTrue(result["ok"])
        self.assertIn("nodejs", result["output"])
        self.assertIn(text, result["output"])

    def test_windows_installer_uses_signed_exe_and_mac_uses_script(self):
        class Response:
            def __init__(self):
                self.stream = io.BytesIO(b"test installer")

            def __enter__(self):
                return self.stream

            def __exit__(self, *args):
                pass

        with (
            patch("core.ego.ROOT", self.root),
            patch(
                "core.ego.environment",
                return_value={"supported": True, "system": "Windows"},
            ),
            patch("core.ego.find_executable", return_value=""),
            patch("core.ego.urlopen", return_value=Response()),
            patch(
                "core.ego.run_bounded",
                return_value={
                    "ok": True,
                    "output": '{"status":"Valid","subject":"CN=Citro Labs"}',
                },
            ) as run,
            patch("core.ego.os.startfile", create=True) as launch,
        ):
            self.assertEqual(ego.install()["state"], "needs_onboarding")
            launch.assert_called_once()
            self.assertEqual(run.call_args.args[0][0], "powershell")
        with (
            patch("core.ego.ROOT", self.root),
            patch(
                "core.ego.environment",
                return_value={"supported": True, "system": "Darwin"},
            ),
            patch("core.ego.find_executable", return_value=""),
            patch("core.ego.urlopen", return_value=Response()),
            patch(
                "core.ego.run_bounded", return_value={"ok": True, "output": "installed"}
            ) as run,
        ):
            self.assertEqual(ego.install()["state"], "needs_onboarding")
            self.assertEqual(run.call_args.args[0][0], "sh")

    def test_builtin_discovery_and_disabled_flags(self):
        manager = SkillManager()
        self.assertIn("anysearch", manager.list_skill_names())
        self.assertIn("ego_lite", manager.list_skill_names())
        config.save_config({"anysearch": {"enabled": False}})
        self.assertNotIn("anysearch", manager.list_skill_names())
        self.assertIn("ego_lite", manager.list_skill_names())
        # Arbitrary user skill folders do not gain default trusted executables.
        self.assertNotIn(
            "anysearch", SkillManager(self.root / "custom").list_skill_names()
        )

    def test_search_secret_storage_and_request_contract(self):
        key = "as_sk_TEST_ONLY_NOT_REAL"
        config.save_config({"anysearch": {"api_key": key}})
        self.assertNotIn(key, config.CONFIG_FILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(config.load_config()["anysearch"]["api_key"], key)

        class Response:
            def __enter__(self):
                return io.BytesIO(
                    b'{"code":0,"data":{"results":[{"url":"https://example.com"}]}}'
                )

            def __exit__(self, *args):
                pass

        with patch("core.default_skills.urlopen", return_value=Response()) as transport:
            result = anysearch({"action": "search", "query": "test", "max_results": 2})
        req = transport.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.anysearch.com/v1/search")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + key)
        self.assertEqual(json.loads(req.data), {"query": "test", "max_results": 2})
        self.assertTrue(result["untrusted_external_content"])
        with patch("core.default_skills.urlopen", return_value=Response()) as transport:
            anysearch_request("/v1/search", {"query": "test"}, key="")
            self.assertIsNone(transport.call_args.args[0].get_header("Authorization"))

    def test_batch_partial_failure_and_validation(self):
        with patch(
            "core.default_skills.anysearch_request",
            side_effect=[{"data": {}}, ValueError("quota")],
        ):
            result = anysearch(
                {"action": "batch_search", "queries": [{"query": "a"}, {"query": "b"}]}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["data"]["results"]), 2)
        for args in (
            {"action": "search", "query": ""},
            {"action": "search", "query": "test", "max_results": 11},
            {"action": "batch_search", "queries": []},
            {"action": "extract", "url": "file:///secret"},
        ):
            with self.assertRaises(ValueError):
                anysearch(args)

    def test_remote_platform_does_not_install_or_launch(self):
        with (
            patch.dict(os.environ, {"SSH_CONNECTION": "test"}),
            patch("core.ego.urlopen") as download,
        ):
            self.assertFalse(ego.environment()["supported"])
            self.assertFalse(ego.install()["ok"])
            download.assert_not_called()
        with (
            patch("platform.system", return_value="Linux"),
            patch("core.ego.urlopen") as download,
        ):
            self.assertFalse(ego.install()["ok"])
            download.assert_not_called()

    def test_browser_guide_pagination_and_stdin(self):
        guide = (
            "\n".join(str(i) for i in range(65)) + "\n[ego-browser:skill] end of guide"
        )
        with (
            patch("core.ego.environment", return_value={"supported": True}),
            patch("core.ego.find_executable", return_value="ego-browser"),
            patch(
                "core.ego.run_bounded",
                return_value={"ok": True, "output": guide, "truncated": False},
            ) as run,
        ):
            result = ego.execute({"action": "guide", "line_count": 40})
            self.assertEqual(result["next_line"], 41)
            result = ego.execute({"action": "guide", "start_line": 41})
            self.assertIsNone(result["next_line"])
            code = 'cliLog("literal `$ shell characters")'
            ego.execute({"action": "run", "script": code})
            self.assertEqual(run.call_args.kwargs["input_text"], code)
            self.assertNotIn(code, str(run.call_args.args[0]))
        with (
            patch("core.ego.environment", return_value={"supported": True}),
            patch("core.ego.find_executable", return_value="ego-browser"),
            patch(
                "core.ego.run_bounded",
                return_value={"ok": True, "output": "truncated guide"},
            ),
        ):
            self.assertFalse(ego.execute({"action": "guide"})["ok"])
