import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from pathlib import Path

try:
    from PyQt5.QtWidgets import QApplication
    from channels.pet_channel import DesktopPetWidget

    AVAILABLE = True
except ImportError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, "optional PyQt5 not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from PyQt5.QtGui import QFontDatabase, QFont

        if not QFontDatabase().families():
            for name in ("msyh.ttc", "segoeui.ttf"):
                path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
                if path.exists():
                    font_id = QFontDatabase.addApplicationFont(str(path))
                    families = QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        cls.app.setFont(QFont(families[0], 10))
                        break

    def test_lazy_cache_hot_settings_and_hidden_animation(self):
        widget = DesktopPetWidget(
            str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
            lambda text: None,
            lambda: None,
        )
        try:
            self.assertEqual(len(widget.frames), 1)
            for row in range(9):
                widget.set_action(row)
            self.assertLessEqual(len(widget.frames), 2)
            widget.apply_config({"scale": 0.5, "bubble_duration_sec": 3})
            self.assertEqual(widget.width(), 96)
            self.assertEqual(widget.bubble_duration, 3)
            widget.show()
            self.app.processEvents()
            widget.hide()
            self.app.processEvents()
            self.assertFalse(widget.timer.isActive())
            widget.set_activity_probe(lambda: True)
            widget.show()
            widget._update_animation_state()
            self.assertFalse(widget.timer.isActive())
        finally:
            widget.close()
            widget.bubble.close()
            widget.hud.close()
            widget.deleteLater()
            self.app.processEvents()

    def test_toolbar_task_lifecycle_and_input(self):
        received = []
        widget = DesktopPetWidget(
            str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
            received.append,
            lambda: None,
        )
        try:
            widget.apply_config({"scale": 0.5})
            widget.move(300, 250)
            widget.show()
            self.app.processEvents()
            self.assertTrue(widget.toolbar.isVisible())
            self.assertGreater(widget.toolbar.y(), widget.y() + widget.height())
            widget.toolbar.compose.click()
            self.assertTrue(widget.hud.isVisible())
            widget.hud.input_edit.setText("评估项目")
            widget.hud.send_btn.click()
            self.assertEqual(received, ["评估项目"])
            self.assertEqual(widget.bubble.title.text(), "评估项目")
            widget.set_task("", "已运行命令")
            self.assertEqual(widget.bubble.status.text(), "已运行命令")
            widget.finish_say("已处理完成")
            self.assertTrue(widget.toolbar.badge.isVisible())
            widget.toolbar.notice.click()
            self.assertFalse(widget.toolbar.badge.isVisible())
            self.assertTrue(widget.bubble.isVisible())
            # 消息键是开关：再次点击收起
            widget.toolbar.notice.click()
            self.assertFalse(widget.bubble.isVisible())
            # 工具栏新布局：compose(左) · mic(中) · notice(右)，expand 已移除
            self.assertTrue(hasattr(widget.toolbar, "mic"))
            self.assertFalse(hasattr(widget.toolbar, "expand"))
            widget.hide()
            self.assertFalse(widget.toolbar.isVisible())
            self.assertFalse(widget.hud.isVisible())
        finally:
            widget.close()
            widget.deleteLater()
            self.app.processEvents()

    def test_transparent_padding_is_not_played(self):
        from PyQt5.QtGui import QImage
        from PyQt5.QtCore import Qt

        widget = DesktopPetWidget(
            str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
            lambda _: None,
            lambda: None,
        )
        try:
            self.assertEqual(widget.windowType(), Qt.Tool)
            widget.show()
            self.app.processEvents()
            counts = [6, 8, 8, 4, 5, 8, 6, 6, 6]
            for row, count in enumerate(counts):
                widget.set_action(row)
                self.assertEqual(len(widget.frames[row]), count)
                for _ in range(count * 2 + 1):
                    widget._next_frame()
                    self.assertLess(widget.current_frame, count)
                    image = (
                        widget.grab().toImage().convertToFormat(QImage.Format_RGBA8888)
                    )
                    bits = image.constBits()
                    bits.setsize(image.byteCount())
                    self.assertTrue(
                        any(bytes(bits)[3::4]),
                        f"row {row} frame {widget.current_frame} must remain visible",
                    )
        finally:
            widget.close()
            widget.deleteLater()
            self.app.processEvents()

    def test_growing_pet_remains_inside_screen(self):
        widget = DesktopPetWidget(
            str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
            lambda _: None,
            lambda: None,
        )
        try:
            area = self.app.primaryScreen().availableGeometry()
            widget.move(
                area.right() - widget.width() + 1, area.bottom() - widget.height() + 1
            )
            for scale in (3, 0.25, 2, 1):
                widget.apply_config({"scale": scale})
                self.assertTrue(area.contains(widget.geometry()))
            self.assertEqual(widget.width(), 192)
        finally:
            widget.close()
            widget.deleteLater()
            self.app.processEvents()

    def test_stream_updates_do_not_restart_animation(self):
        widget = DesktopPetWidget(
            str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
            lambda _: None,
            lambda: None,
        )
        try:
            widget.set_action(6)
            widget._next_frame()
            frame = widget.current_frame
            widget.stream_say("hello")
            widget.stream_say("hello world")
            self.assertEqual(widget.current_frame, frame)
            self.assertIs(widget.bubble.parentWidget(), widget)
            self.assertIs(widget.hud.parentWidget(), widget)
        finally:
            widget.close()
            widget.deleteLater()
            self.app.processEvents()

    def test_chat_panels_have_real_white_surface_and_dark_text(self):
        from PyQt5.QtGui import QPalette
        from channels.pet_channel import SpeechBubble, InputHUD

        bubble = SpeechBubble()
        hud = InputHUD(lambda _: None)
        try:
            bubble.show_message("连接成功，我是MiniBot。")
            hud.input_edit.setText("你是谁")
            hud.show()
            self.app.processEvents()
            for panel in (bubble, hud):
                image = panel.grab().toImage()
                background = image.pixelColor(image.width() // 2, image.height() - 5)
                self.assertGreater(background.red(), 240)
                self.assertGreater(background.alpha(), 200)
                self.assertLess(background.alpha(), 255)
            # Assert real glyphs are rendered, not just a palette setting on an
            # otherwise empty transparent window.
            rendered = bubble.grab().toImage()
            dark = sum(
                1
                for y in range(rendered.height())
                for x in range(rendered.width())
                if rendered.pixelColor(x, y).alpha() > 200
                and max(
                    rendered.pixelColor(x, y).red(),
                    rendered.pixelColor(x, y).green(),
                    rendered.pixelColor(x, y).blue(),
                )
                < 80
            )
            self.assertGreater(dark, 20)
            text = bubble.label.palette().color(QPalette.WindowText)
            input_text = hud.input_edit.palette().color(QPalette.Text)
            self.assertLess(max(text.red(), text.green(), text.blue()), 50)
            self.assertLess(
                max(input_text.red(), input_text.green(), input_text.blue()), 50
            )
        finally:
            bubble.close()
            hud.close()
            bubble.deleteLater()
            hud.deleteLater()
            self.app.processEvents()
