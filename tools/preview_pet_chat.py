"""Render the actual Qt chat panels without opening windows or contacting a model."""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor, QImage, QPainter, QFontDatabase, QFont
from channels.pet_channel import DesktopPetWidget

if __name__ == "__main__":
    app = QApplication([])
    # Windows offscreen Qt has no system font discovery. Load a local installed
    # font for this renderer only; the normal Windows GUI discovers fonts itself.
    if not QFontDatabase().families():
        for name in ("msyh.ttc", "segoeui.ttf"):
            path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
            if path.exists():
                font_id = QFontDatabase.addApplicationFont(str(path))
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    app.setFont(QFont(families[0], 10))
                    break

    widget = DesktopPetWidget(
        str(Path(__file__).resolve().parents[1] / "assets/pets/ikunchick"),
        lambda _: None,
        lambda: None,
    )
    widget.apply_config({"scale": 0.5})
    widget.move(250, 220)
    widget.show()
    widget.set_task("评估主动交互 Bot 项目", "已运行命令")
    app.processEvents()
    canvas = QImage(430, 310, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(QColor("#f3f3f3"))
    painter = QPainter(canvas)
    for panel, y in ((widget.bubble, 24), (widget, 102), (widget.toolbar, 218)):
        picture = panel.grab().toImage()
        painter.drawImage((430 - picture.width()) // 2, y, picture)
    painter.end()
    path = Path(__file__).resolve().parents[1] / "docs" / "pet-codex-layout-preview.png"
    if not canvas.save(str(path)):
        raise RuntimeError("Failed to save preview")
    widget.close()
    print(str(path))
