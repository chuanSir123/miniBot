import unittest
from unittest.mock import patch
from core.console import log


class ConsoleTests(unittest.TestCase):
    def test_flushed_bounded_redacted_events(self):
        with patch("core.console.print") as output:
            log(
                "Harness",
                "tool.end",
                api_key="secret",
                result={
                    "access_token": "token",
                    "output": "Bearer private",
                    "_image": b"private",
                },
                text="x" * 5000,
            )
        args, kwargs = output.call_args
        self.assertTrue(kwargs["flush"])
        self.assertIn("[Harness] tool.end", args[0])
        self.assertNotIn("private", args[0])
        self.assertNotIn("secret", args[0])
        self.assertNotIn("token", args[0].replace("access_token", ""))
        self.assertLess(len(args[0]), 1600)
