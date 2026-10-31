"""Exercise the actual cmd.exe entry point, not just Python imports."""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launcher

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_installed_modules_detected_without_reinstall(self):
        for requirement, module in [("Pillow", "PIL"), ("PyQt5", "PyQt5")]:
            self.assertEqual(
                launcher.check_module_installed(requirement),
                importlib.util.find_spec(module) is not None,
            )

    def test_child_exit_code_propagated(self):
        with patch.object(launcher.subprocess, "call", return_value=7) as call:
            self.assertEqual(launcher._run_main("local"), 7)
        self.assertEqual(call.call_args.kwargs["cwd"], ROOT)

    def test_menu_propagates_failure(self):
        old = Path.cwd()
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                patch("builtins.input", return_value="1"),
                patch.object(launcher, "install_deps", return_value=True),
                patch.object(launcher, "_check_first_time_config"),
                patch.object(launcher, "_run_main", return_value=7),
            ):
                self.assertEqual(launcher.main(), 7)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                patch("builtins.input", return_value="1"),
                patch.object(launcher, "install_deps", return_value=False),
            ):
                self.assertEqual(launcher.main(), 1)
        finally:
            os.chdir(old)


class DependencyInstallerTests(unittest.TestCase):
    def test_fallback_and_deduplication(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(launcher, "check_module_installed", return_value=False),
            patch.object(launcher.sys, "prefix", sys.base_prefix),
            patch.object(launcher.subprocess, "call", side_effect=[1, 0]) as call,
        ):
            self.assertTrue(
                launcher.install_deps(
                    ["PyQt5", "Pillow", "websockets>=14,<16", "Pillow"]
                )
            )
        self.assertEqual(call.call_count, 2)
        for index, entry in enumerate(call.call_args_list):
            command = entry.args[0]
            self.assertEqual(command.count("Pillow"), 1)
            self.assertEqual(
                command[command.index("--index-url") + 1],
                launcher.PACKAGE_INDEXES[index],
            )
            self.assertNotIn("--trusted-host", command)

    def test_exact_source_priority_and_official_last_fallback(self):
        self.assertEqual(
            launcher.PACKAGE_INDEXES,
            (launcher.TSINGHUA_INDEX, launcher.ALIYUN_INDEX, launcher.PYPI_INDEX),
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(launcher, "check_module_installed", return_value=False),
            patch.object(launcher.sys, "prefix", sys.base_prefix),
            patch.object(launcher.subprocess, "call", side_effect=[1, 1, 0]) as call,
        ):
            self.assertTrue(launcher.install_deps(["PyQt5"]))
        actual = [
            entry.args[0][entry.args[0].index("--index-url") + 1]
            for entry in call.call_args_list
        ]
        self.assertEqual(actual, list(launcher.PACKAGE_INDEXES))

    def test_existing_dependencies_do_not_run_pip(self):
        with (
            patch.object(launcher, "check_module_installed", return_value=True),
            patch.object(launcher.subprocess, "call") as call,
        ):
            self.assertTrue(launcher.install_deps(["Pillow", "Pillow"]))
        call.assert_not_called()

    def test_all_sources_fail_returns_failure(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(launcher, "check_module_installed", return_value=False),
            patch.object(launcher.subprocess, "call", return_value=1) as call,
        ):
            self.assertFalse(launcher.install_deps(["PyQt5"]))
        self.assertEqual(call.call_count, len(launcher.PACKAGE_INDEXES))

    def test_success_only_saves_index_in_project_venv(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(launcher, "check_module_installed", return_value=False),
            patch.object(launcher.sys, "prefix", str(ROOT / ".venv")),
            patch.object(launcher.subprocess, "call", return_value=0) as call,
        ):
            self.assertTrue(launcher.install_deps(["Pillow"]))
        self.assertEqual(call.call_count, 2)
        command = call.call_args_list[-1].args[0]
        self.assertIn("--site", command)
        self.assertEqual(command[-1], launcher.PACKAGE_INDEXES[0])


@unittest.skipUnless(os.name == "nt", "Windows batch tests")
class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name) / "Mini Bot (startup test)"
        cls.folder.mkdir()
        cls.batch = cls.folder / "start.bat"
        cls.batch.write_bytes((ROOT / "start.bat").read_bytes())
        # A real lightweight venv exercises path quoting and interpreter validation.
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(cls.folder / ".venv")],
            check=True,
            capture_output=True,
            timeout=30,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_stub(self, exit_code):
        (self.folder / "launcher.py").write_text(
            f'import sys\nprint("LAUNCHER_REACHED",flush=True)\nsys.exit({exit_code})\n',
            encoding="utf-8",
        )
        return subprocess.run(
            ["cmd.exe", "/d", "/c", "start.bat"],
            cwd=self.folder,
            input=b"\r\n",
            capture_output=True,
            timeout=10,
        )

    def test_success_in_path_containing_spaces_and_parentheses(self):
        result = self.run_stub(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"LAUNCHER_REACHED", result.stdout)
        self.assertNotIn(b"unexpected", result.stderr.lower())

    def test_failure_reaches_pause_and_preserves_exit_code(self):
        result = self.run_stub(7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn(b"MiniBot exited with code 7", result.stdout)
        # pause consumes supplied input (or EOF); its message must remain visible.
        self.assertIn(b"continue", result.stdout.lower())

    def test_real_project_entry_reaches_menu(self):
        if not (ROOT / ".venv/Scripts/python.exe").exists():
            self.skipTest("project venv not installed")
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "start.bat"],
            cwd=ROOT,
            input=b"0\r\n",
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"[1/2/3/4/5/6/0]", result.stdout)
