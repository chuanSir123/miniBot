import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import QPoint, QRect, Qt, QTimer
    from PyQt5.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
    from PyQt5.QtWidgets import (
        QAction, QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
        QMenu, QPushButton, QVBoxLayout, QWidget
    )
    from PIL import Image
    HAS_PET_DEPS = True
except ImportError:
    HAS_PET_DEPS = False

FRAME_WIDTH = 192
FRAME_HEIGHT = 208
COLS_PER_ROW = 8


class SpeechBubble(QWidget):
    """桌宠头顶浮动对话气泡"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(12, 10, 12, 10)

        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("""
            QLabel {
                color: #f8fafc;
                font-size: 13px;
                font-weight: 500;
                line-height: 1.4;
            }
        """)
        self.layout.addWidget(self.label)

        self.setStyleSheet("""
            SpeechBubble {
                background-color: rgba(15, 23, 42, 0.92);
                border: 1px solid rgba(59, 130, 246, 0.5);
                border-radius: 10px;
            }
        """)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)

    def show_message(self, text: str, duration_sec: int = 8):
        self.label.setText(text)
        self.adjustSize()
        # 控制宽度在 160 ~ 280 像素之间
        w = max(180, min(self.sizeHint().width(), 280))
        self.setFixedWidth(w)
        self.adjustSize()
        self.show()
        self.hide_timer.start(duration_sec * 1000)


class InputHUD(QWidget):
    """桌宠右下角呼出的紧凑输入框"""

    def __init__(self, on_submit_callback, parent=None):
        super().__init__(parent)
        self.on_submit = on_submit_callback
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.input_edit = QLineEdit(self)
        self.input_edit.setPlaceholderText("问问鸡哥或输入指令...")
        self.input_edit.setStyleSheet("""
            QLineEdit {
                background: #0f172a;
                color: #f8fafc;
                border: 1px solid #3b82f6;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
                min-width: 220px;
            }
            QLineEdit:focus {
                border-color: #60a5fa;
            }
        """)
        self.input_edit.returnPressed.connect(self._handle_submit)
        layout.addWidget(self.input_edit)

        self.send_btn = QPushButton("发送", self)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: #2563eb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #1d4ed8;
            }
        """)
        self.send_btn.clicked.connect(self._handle_submit)
        layout.addWidget(self.send_btn)

        self.setStyleSheet("""
            InputHUD {
                background-color: rgba(30, 41, 59, 0.95);
                border: 1px solid #475569;
                border-radius: 8px;
            }
        """)

    def _handle_submit(self):
        text = self.input_edit.text().strip()
        if text:
            self.input_edit.clear()
            self.hide()
            self.on_submit(text)


class DesktopPetWidget(QWidget):
    """
    IKun 鸡哥桌宠主窗口
    """

    def __init__(self, pet_dir: str, on_user_input: Callable[[str], None], on_open_settings: Callable[[], None]):
        super().__init__()
        self.pet_dir = Path(pet_dir)
        self.on_user_input = on_user_input
        self.on_open_settings = on_open_settings

        # 窗口属性：无边框、置顶、透明
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)

        self.scale = 1.0
        self.anim_row = 0  # 0=idle, 3=wave, 4=jump, 6=waiting
        self.current_frame = 0
        self.frames: Dict[int, List[QPixmap]] = {}

        self._load_spritesheet()

        # 拖拽位置记录
        self.drag_position = QPoint()

        # 气泡与输入框 HUD
        self.bubble = SpeechBubble()
        self.hud = InputHUD(self._on_input_submitted)

        # 动画定时器 (10 FPS 循环)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next_frame)
        self.timer.start(100)

        # 临时动作定时器（如 wave 或 jump 播放完毕切回 idle）
        self.action_reset_timer = QTimer(self)
        self.action_reset_timer.setSingleShot(True)
        self.action_reset_timer.timeout.connect(self._reset_to_idle)

        # 悬停按钮显示标志
        self.is_hovered = False
        self.resize(int(FRAME_WIDTH * self.scale), int(FRAME_HEIGHT * self.scale))
        self._move_to_bottom_right()

    def _load_spritesheet(self):
        """解析 ikunchick/spritesheet.webp 并切片加载所有动作帧"""
        sheet_path = self.pet_dir / "spritesheet.webp"
        if not sheet_path.exists():
            print(f"[Pet] 警告：未找到 {sheet_path}")
            return

        try:
            pil_img = Image.open(sheet_path).convert("RGBA")
            # 8 列 x 9 行
            for row in range(9):
                self.frames[row] = []
                for col in range(COLS_PER_ROW):
                    box = (
                        col * FRAME_WIDTH,
                        row * FRAME_HEIGHT,
                        (col + 1) * FRAME_WIDTH,
                        (row + 1) * FRAME_HEIGHT
                    )
                    cropped = pil_img.crop(box)
                    # PIL -> QImage -> QPixmap
                    raw_data = cropped.tobytes("raw", "RGBA")
                    qimg = QImage(raw_data, FRAME_WIDTH, FRAME_HEIGHT, QImage.Format_RGBA8888)
                    self.frames[row].append(QPixmap.fromImage(qimg))
        except Exception as e:
            print(f"[Pet] 载入 spritesheet 失败: {e}")

    def _move_to_bottom_right(self):
        desktop = QApplication.primaryScreen().availableGeometry()
        x = desktop.width() - self.width() - 40
        y = desktop.height() - self.height() - 40
        self.move(x, y)
        self._sync_subwidgets_pos()

    def _sync_subwidgets_pos(self):
        # 气泡位置：在桌宠头顶居中偏上
        bx = self.x() + (self.width() - self.bubble.width()) // 2
        by = self.y() - self.bubble.height() - 8
        self.bubble.move(bx, by)

        # 输入框位置：在桌宠正下方居中
        hx = self.x() + (self.width() - self.hud.width()) // 2
        hy = self.y() + self.height() + 8
        self.hud.move(hx, hy)

    def _next_frame(self):
        self.current_frame = (self.current_frame + 1) % COLS_PER_ROW
        self.update()

    def set_action(self, row: int, duration_ms: int = 3000):
        """切换动作：0=idle, 3=wave, 4=jump, 6=waiting"""
        if row in self.frames and self.frames[row]:
            self.anim_row = row
            self.current_frame = 0
            if row != 0:
                self.action_reset_timer.start(duration_ms)

    def _reset_to_idle(self):
        self.anim_row = 0
        self.current_frame = 0

    def say(self, text: str, action: int = 3):
        """让桌宠说话并在头顶弹气泡"""
        self.set_action(action, duration_ms=4000)
        self._sync_subwidgets_pos()
        self.bubble.show_message(text)

    def _on_input_submitted(self, text: str):
        self.set_action(6, duration_ms=6000)  # 思考状态
        self.on_user_input(text)

    # --- 绘图事件 ---
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        frame_list = self.frames.get(self.anim_row) or self.frames.get(0, [])
        if frame_list:
            pixmap = frame_list[self.current_frame % len(frame_list)]
            target_rect = QRect(0, 0, self.width(), self.height())
            painter.drawPixmap(target_rect, pixmap)

        # 悬停时在右下角渲染交互提示图标
        if self.is_hovered:
            painter.setBrush(QColor(37, 99, 235, 220))
            painter.setPen(Qt.NoPen)
            btn_size = 28
            bx = self.width() - btn_size - 6
            by = self.height() - btn_size - 6
            painter.drawEllipse(bx, by, btn_size, btn_size)

            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont("Segoe UI Emoji", 12))
            painter.drawText(QRect(bx, by, btn_size, btn_size), Qt.AlignCenter, "💬")

    # --- 鼠标与交互事件 ---
    def enterEvent(self, event):
        self.is_hovered = True
        self.update()

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 检查是否点击了右下角的 💬 交互按钮
            btn_size = 32
            bx = self.width() - btn_size
            by = self.height() - btn_size
            if event.pos().x() >= bx and event.pos().y() >= by:
                # 切换输入框显示/隐藏
                if self.hud.isVisible():
                    self.hud.hide()
                else:
                    self._sync_subwidgets_pos()
                    self.hud.show()
                    self.hud.input_edit.setFocus()
                return

            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and not self.drag_position.isNull():
            self.move(event.globalPos() - self.drag_position)
            self._sync_subwidgets_pos()
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = QPoint()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e293b;
                color: #f8fafc;
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #3b82f6;
            }
        """)

        act_hud = menu.addAction("💬 呼出对话输入框")
        act_wave = menu.addAction("👋 打个招呼 (Wave)")
        act_jump = menu.addAction("🏀 坤坤招牌跳跃 (Jump)")
        menu.addSeparator()
        act_cfg = menu.addAction("⚙️ 偏好设置中心")
        act_exit = menu.addAction("❌ 退出桌宠")

        action = menu.exec_(self.mapToGlobal(event.pos()))
        if action == act_hud:
            self._sync_subwidgets_pos()
            self.hud.show()
            self.hud.input_edit.setFocus()
        elif action == act_wave:
            self.say("你干嘛~ 哎哟！", action=3)
        elif action == act_jump:
            self.say("全民制作人们大家好！", action=4)
        elif action == act_cfg:
            self.on_open_settings()
        elif action == act_exit:
            QApplication.quit()
