# -*- coding: utf-8 -*-
import os
import unittest
import io
import json
from pathlib import Path
from PIL import Image

from core.browser_verifier import (
    find_browser_executable,
    resolve_web_target,
    compare_frames,
    create_animated_gif,
    capture_browser_or_screen,
    is_dynamic_web_content,
    create_frame_filmstrip,
    capture_cdp_sequential_frames,
)
from core.workspace import workspace_for
from core.harness import AgentHarness
from core.jev import JevClient


class DummyBrain:
    def chat(self, *args, **kwargs):
        return {"content": "ok"}


class TestBrowserVerifier(unittest.TestCase):
    def setUp(self):
        self.scope = "main"
        self.ws = workspace_for(self.scope)
        self.test_files = []

    def tearDown(self):
        for f in self.test_files:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass

    def test_find_browser(self):
        exe = find_browser_executable()
        if exe:
            self.assertTrue(os.path.exists(exe))

    def test_resolve_web_target(self):
        url, local = resolve_web_target("http://localhost:3000", self.scope)
        self.assertEqual(url, "http://localhost:3000")
        self.assertIsNone(local)

        test_html = self.ws / "unit_test_sample.html"
        test_html.write_text("<h1>Test</h1>", encoding="utf-8")
        self.test_files.append(test_html)

        url, local = resolve_web_target("unit_test_sample.html", self.scope)
        self.assertTrue(url.startswith("file:///"))
        self.assertIsNotNone(local)

    def test_is_dynamic_web_content(self):
        # Prompt-based dynamic detection
        self.assertTrue(is_dynamic_web_content(None, "制作一个旋转的自行车动画效果"))
        self.assertTrue(is_dynamic_web_content(None, "写一个倒计时 Canvas 游戏"))
        self.assertTrue(is_dynamic_web_content(None, "做一个小球跳跃动效网页"))
        self.assertFalse(is_dynamic_web_content(None, "写一个纯静态的帮助说明文本"))

        # File content-based dynamic detection
        static_html = self.ws / "static_sample.html"
        static_html.write_text("<h1>Hello</h1><p>Static paragraph</p>", encoding="utf-8")
        self.test_files.append(static_html)
        self.assertFalse(is_dynamic_web_content(str(static_html), ""))

        keyframes_html = self.ws / "keyframes_sample.html"
        keyframes_html.write_text("<style>@keyframes spin { from{transform:rotate(0deg);} to{transform:rotate(360deg);} }</style>", encoding="utf-8")
        self.test_files.append(keyframes_html)
        self.assertTrue(is_dynamic_web_content(str(keyframes_html), ""))

        raf_html = self.ws / "raf_sample.html"
        raf_html.write_text("<script>requestAnimationFrame(animate);</script>", encoding="utf-8")
        self.test_files.append(raf_html)
        self.assertTrue(is_dynamic_web_content(str(raf_html), ""))

        canvas_html = self.ws / "canvas_sample.html"
        canvas_html.write_text("<canvas id='game'></canvas>", encoding="utf-8")
        self.test_files.append(canvas_html)
        self.assertTrue(is_dynamic_web_content(str(canvas_html), ""))

    def test_create_frame_filmstrip(self):
        frames = []
        for i, color in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]):
            img = Image.new("RGB", (120, 80), color=color)
            fpath = self.ws / f"filmstrip_frame_{i}.png"
            img.save(fpath)
            self.test_files.append(fpath)
            frames.append(fpath)

        timestamps = [0.4, 0.9, 1.4, 1.9]
        strip_bytes = create_frame_filmstrip(frames, timestamps)
        self.assertIsNotNone(strip_bytes)
        self.assertGreater(len(strip_bytes), 100)

        # Verify it can be loaded as a valid image
        strip_img = Image.open(io.BytesIO(strip_bytes))
        self.assertIn(strip_img.format, ("JPEG", "PNG"))
        self.assertGreaterEqual(strip_img.width, 240)
        self.assertGreaterEqual(strip_img.height, 160)

    def test_capture_browser_or_screen_dynamic_auto_upgrade(self):
        exe = find_browser_executable()
        if not exe:
            self.skipTest("No browser available in system")

        html_path = self.ws / "test_dynamic_spinner.html"
        html_path.write_text(
            """<!DOCTYPE html>
            <html>
            <head>
              <style>
                @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
                .spinner { width: 60px; height: 60px; border: 6px solid #f3f3f3; border-top: 6px solid #3498db; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 40px auto; }
              </style>
            </head>
            <body>
              <div class="spinner"></div>
            </body>
            </html>
            """,
            encoding="utf-8",
        )
        self.test_files.append(html_path)

        # Call with frames=1, but target is dynamic HTML -> should auto-upgrade to 4 frames
        res = capture_browser_or_screen(
            file_path="test_dynamic_spinner.html",
            frames=1,
            scope=self.scope,
            reason="制作旋转圆环动画",
        )
        self.assertTrue(res.get("ok"))
        self.assertGreaterEqual(res.get("frames_captured", 0), 3)
        self.assertTrue(res.get("filmstrip_generated"))
        self.assertIn("_image", res)
        self.assertIsNotNone(res["_image"])
        if res.get("gif_path"):
            self.test_files.append(Path(res["gif_path"]))


if __name__ == "__main__":
    unittest.main()
