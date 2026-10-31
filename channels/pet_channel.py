import io
import json
import re
import threading
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from core.persona import names

try:
    from PyQt5 import QtCore
    from PyQt5.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
    from PyQt5.QtGui import QColor, QImage, QPainter, QPalette, QPixmap
    from PyQt5.QtWidgets import (
        QApplication,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMenu,
        QPushButton,
        QScrollArea,
        QVBoxLayout,
        QWidget,
    )
    from PyQt5 import QtGui

    HAS_PET_DEPS = True
except ImportError as exc:
    raise ImportError("桌宠模式需要安装 requirements-pet.txt") from exc

FRAME_WIDTH = 192
FRAME_HEIGHT = 208
COLS_PER_ROW = 8


class LightPanel(QWidget):
    """Paint an actual translucent surface; QWidget stylesheet backgrounds alone
    are unreliable on custom transparent top-level widgets on Windows.
    """

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.setPen(QColor(210, 210, 210, 220))
        painter.setBrush(QColor(255, 255, 255, 238))
        painter.drawRoundedRect(
            QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 18, 18
        )



def convert_markdown_to_pet_html(text: str, scope: str = "main", max_width: int = 270) -> str:
    if not text:
        return ""
    import re
    # 1. Resolve media:// references
    def _resolve_media(m):
        ref = m.group(1)
        try:
            from core.media_store import MediaStore
            ms = MediaStore()
            for s in (scope, "main", "local"):
                try:
                    p = ms.path(ref, s)
                    if p and p.exists():
                        return p.as_uri()
                except Exception:
                    pass
        except Exception:
            pass
        return m.group(0)

    resolved = re.sub(r"media://(img_[a-f0-9]{16})", _resolve_media, text)

    # 2. QTextDocument Markdown -> HTML
    from PyQt5.QtGui import QTextDocument, QFont
    doc = QTextDocument()
    font = QFont()
    font.setPointSize(10)
    font.setFamily("Microsoft YaHei, Segoe UI, sans-serif")
    doc.setDefaultFont(font)
    doc.setMarkdown(resolved)
    html = doc.toHtml()

    # 3. Constrain img tags: 按原始宽高比缩放，宽度与气泡输出框一致；小图不放大
    def _fix_img(m):
        tag = m.group(0)
        src_m = re.search(r'src="([^"]+)"', tag)
        src = src_m.group(1) if src_m else ""
        natural_w = natural_h = 0
        if src:
            try:
                from PyQt5.QtCore import QUrl as _QUrl
                from PyQt5.QtGui import QImage as _QImage
                # QImage 读不了 file:/// URI，先还原成本地路径
                if src.startswith("data:"):
                    import base64
                    img = _QImage.fromData(base64.b64decode(src.split(",", 1)[1]))
                elif src.startswith("file:"):
                    img = _QImage(_QUrl(src).toLocalFile())
                else:
                    img = _QImage(src)
                if not img.isNull():
                    natural_w, natural_h = img.width(), img.height()
            except Exception:
                natural_w = natural_h = 0
        if natural_w and natural_w > max_width:
            target_w = max_width
            target_h = max(1, round(natural_h * max_width / natural_w))
        elif natural_w:
            target_w, target_h = natural_w, natural_h
        else:
            # 拿不到原始尺寸时只限宽，交给 QTextDocument 等比处理
            target_w, target_h = max_width, 0
        tag = re.sub(r'\s(width|height)="[^"]*"', "", tag)
        core = tag.rstrip(">")
        if core.endswith("/"):
            core = core[:-1].rstrip()
        size = f' width="{target_w}"'
        if target_h:
            size += f' height="{target_h}"'
        return core + size + ">"
    html = re.sub(r"<img[^>]+>", _fix_img, html)
    return html

class SpeechBubble(LightPanel):
    """桌宠头顶浮动对话气泡"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 12, 16, 12)

        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.label.setOpenExternalLinks(True)
        self.label.setStyleSheet("""
            QLabel {
                color: #171717;
                background: transparent;
                font-size: 14px;
                font-weight: 400;
            }
        """)
        pname = (names() or ["喵喵"])[0]
        self.title = QLabel(pname, self)
        self.title.setTextFormat(Qt.PlainText)
        self.title.setStyleSheet(
            "color:#171717; background:transparent; font-size:13px; font-weight:600;"
        )
        self.status = QLabel("", self)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setStyleSheet(
            "color:#777777; background:transparent; font-size:12px;"
        )
        self.layout.addWidget(self.title)
        self.layout.addWidget(self.status)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:10px;}")
        self.scroll.viewport().setStyleSheet("background:transparent;")
        self.label.setParent(self.scroll)
        self.label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scroll.setWidget(self.label)
        self.layout.addWidget(self.scroll)
        self.available_height = 420

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)

    def stream_text(self, text: str):
        """流式追加或更新文本，保持气泡显示"""
        self.hide_timer.stop()
        # 图片宽度与输出框（label 内容宽 = 340 - 左右边距48）一致
        html = convert_markdown_to_pet_html(text, max_width=340 - 48)
        self.label.setText(html)
        self.label.setVisible(bool(text))
        self.label.ensurePolished()
        w = 340
        self.setFixedWidth(w)
        self.fit_available(self.available_height)
        if not self.isVisible():
            self.show()

    def show_message(self, text: str, duration_sec: int = 8):
        self.stream_text(text)
        self.hide_timer.start(duration_sec * 1000)

    def set_html(self, html: str):
        """直接设置渲染好的 HTML（分段视图：文本段 + 活动条）。"""
        self.hide_timer.stop()
        self.label.setText(html)
        self.label.setVisible(bool(html))
        self.label.ensurePolished()
        self.setFixedWidth(340)
        self.fit_available(self.available_height)
        if not self.isVisible():
            self.show()

    def show_html(self, html: str, duration_sec: int = 8):
        self.set_html(html)
        self.hide_timer.start(duration_sec * 1000)

    def fit_available(self, height):
        self.available_height = max(60, int(height))
        width = max(200, self.width())
        self.label.setFixedWidth(width - 48)
        body_height = self.label.heightForWidth(width - 48) if self.label.text() else 0
        self.label.setMinimumHeight(max(0, body_height))
        self.scroll.setVisible(bool(self.label.text()))
        # 用真实布局位置计算头部高度，避免 sizeHint 在高 DPI/字体差异下偏小，
        # 导致气泡被压矮后状态行和正文重叠。
        self.layout.activate()
        header = self.status.geometry().bottom() + self.layout.contentsMargins().bottom() + 2
        body = max(0, min(body_height+4, self.available_height-header))
        self.scroll.setFixedHeight(body)
        self.setFixedHeight(min(self.available_height, header+body))


class HUDLineEdit(QLineEdit):
    def __init__(self, hud, parent=None):
        super().__init__(parent)
        self.hud = hud

    def keyPressEvent(self, event):
        if event.matches(QtGui.QKeySequence.Paste):
            cb = QApplication.clipboard()
            md = cb.mimeData()
            if md.hasImage():
                qimg = cb.image()
                if not qimg.isNull():
                    self.hud.attach_qimage(qimg)
                    return
            if md.hasUrls():
                urls = md.urls()
                for u in urls:
                    lf = u.toLocalFile()
                    if lf and Path(lf).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'):
                        self.hud.attach_file(lf)
                        return
        super().keyPressEvent(event)


class _SttBridge(QtCore.QObject):
    """跨线程回调：录音转写在后台线程执行，结果经信号回到 UI 线程。"""
    finished = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)


class InputHUD(LightPanel):
    """桌宠右下角呼出的紧凑输入框 (支持图片输入与剪贴板粘贴)"""

    def __init__(self, on_submit_callback, parent=None):
        super().__init__(parent)
        self.on_submit = on_submit_callback
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.attached_image_bytes = None
        self.attached_image_name = ""

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 8)
        main_layout.setSpacing(4)

        # 预览栏（缩略图 + 文件名 + 删除按钮）
        self.preview_bar = QWidget(self)
        preview_layout = QHBoxLayout(self.preview_bar)
        preview_layout.setContentsMargins(4, 2, 4, 2)
        preview_layout.setSpacing(6)
        self.preview_thumb = QLabel(self.preview_bar)
        self.preview_thumb.setFixedSize(24, 24)
        self.preview_thumb.setScaledContents(True)
        self.preview_label = QLabel(self.preview_bar)
        self.preview_label.setStyleSheet("color:#374151; font-size:12px;")
        self.preview_del_btn = QPushButton("✕", self.preview_bar)
        self.preview_del_btn.setFixedSize(18, 18)
        self.preview_del_btn.setCursor(Qt.PointingHandCursor)
        self.preview_del_btn.setStyleSheet("border:0; background:transparent; color:#9ca3af; font-weight:bold;")
        self.preview_del_btn.clicked.connect(self._clear_attached_image)
        preview_layout.addWidget(self.preview_thumb)
        preview_layout.addWidget(self.preview_label)
        preview_layout.addStretch()
        preview_layout.addWidget(self.preview_del_btn)
        self.preview_bar.hide()
        main_layout.addWidget(self.preview_bar)

        # 输入栏（图片选择按钮 + 输入框 + 发送按钮）
        row = QHBoxLayout()
        row.setSpacing(6)

        self.attach_btn = QPushButton("📷", self)
        self.attach_btn.setCursor(Qt.PointingHandCursor)
        self.attach_btn.setToolTip("选择图片或直接 Ctrl+V 粘贴")
        self.attach_btn.setFixedSize(32, 32)
        self.attach_btn.setStyleSheet("""
            QPushButton {
                background: #f3f4f6;
                color: #4b5563;
                border: 1px solid #d1d5db;
                border-radius: 14px;
                font-size: 14px;
            }
            QPushButton:hover {
                background: #e5e7eb;
            }
        """)
        self.attach_btn.clicked.connect(self._pick_file)
        row.addWidget(self.attach_btn)

        self.input_edit = HUDLineEdit(self, self)
        self.input_edit.setPlaceholderText("输入消息或指令...")
        self.input_edit.setStyleSheet("""
            QLineEdit {
                background: rgba(245, 245, 245, 230);
                color: #171717;
                border: 1px solid #dedede;
                border-radius: 14px;
                padding: 8px 12px;
                font-size: 14px;
                selection-background-color: #dbeafe;
                selection-color: #171717;
                min-width: 220px;
            }
            QLineEdit:focus {
                border-color: #a3a3a3;
            }
        """)
        palette = self.input_edit.palette()
        palette.setColor(QPalette.PlaceholderText, QColor("#737373"))
        self.input_edit.setPalette(palette)
        self.input_edit.returnPressed.connect(self._handle_submit)
        row.addWidget(self.input_edit)

        self.send_btn = QPushButton("发送", self)
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: #e5e7eb;
                color: #171717;
                border: 1px solid #d4d4d4;
                border-radius: 14px;
                padding: 8px 12px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #d1d5db;
            }
        """)
        self.send_btn.clicked.connect(self._handle_submit)
        row.addWidget(self.send_btn)

        main_layout.addLayout(row)

    def attach_file(self, file_path: str):
        try:
            p = Path(file_path)
            data = p.read_bytes()
            pm = QPixmap(str(p))
            if not pm.isNull():
                self.attached_image_bytes = data
                self.attached_image_name = p.name
                self.preview_thumb.setPixmap(pm)
                self.preview_label.setText(p.name[:20])
                self.preview_bar.show()
                self.adjustSize()
                if hasattr(self.parent(), "_sync_subwidgets_pos"):
                    self.parent()._sync_subwidgets_pos()
        except Exception:
            pass

    def attach_qimage(self, qimg: QImage):
        try:
            pm = QPixmap.fromImage(qimg)
            from PyQt5.QtCore import QBuffer, QIODevice
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            qimg.save(buf, "PNG")
            data = bytes(buf.data())
            if data:
                self.attached_image_bytes = data
                self.attached_image_name = "剪贴板图片.png"
                self.preview_thumb.setPixmap(pm)
                self.preview_label.setText("剪贴板图片")
                self.preview_bar.show()
                self.adjustSize()
                if hasattr(self.parent(), "_sync_subwidgets_pos"):
                    self.parent()._sync_subwidgets_pos()
        except Exception:
            pass

    def _pick_file(self):
        fpath, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
        )
        if fpath:
            self.attach_file(fpath)

    def _clear_attached_image(self):
        self.attached_image_bytes = None
        self.attached_image_name = ""
        self.preview_thumb.clear()
        self.preview_label.clear()
        self.preview_bar.hide()
        self.adjustSize()
        if hasattr(self.parent(), "_sync_subwidgets_pos"):
            self.parent()._sync_subwidgets_pos()

    def _handle_submit(self):
        text = self.input_edit.text().strip()
        input_images = []
        if self.attached_image_bytes:
            try:
                from core.media_store import MediaStore
                ms = MediaStore()
                asset = ms.add(self.attached_image_bytes, scope="main", origin="user")
                data_url = ms.data_url(asset["media_ref"], "main")
                input_images.append(data_url)
            except Exception as exc:
                print("Failed to save attached image:", exc)

        if not text and not input_images:
            return
        if not text and input_images:
            text = "请看这张图片"

        self._clear_attached_image()
        self.input_edit.clear()
        self.hide()
        try:
            self.on_submit(text, input_images)
        except TypeError:
            self.on_submit(text)



class Toolbar(LightPanel):
    """Compact white toolbar: chat input · voice record (middle) · notice (right)."""

    def __init__(self, pet):
        super().__init__(pet)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 4, 7, 4)
        layout.setSpacing(2)
        # 布局：输入聊天（左） · 录音（中） · 消息提醒（右）
        self.compose = QPushButton("", self)
        self.mic = QPushButton("", self)
        self.notice = QPushButton("", self)
        from PyQt5.QtGui import QIcon, QPen

        for button, kind, title, callback in (
            (self.compose, "compose", "输入聊天", pet.toggle_input),
            (self.mic, "mic", "点击录音，再次点击发送", pet.toggle_recording),
            (self.notice, "bell", "查看最近回复（再点一次收起）", pet.toggle_notice),
        ):
            icon = QPixmap(24, 24)
            icon.fill(Qt.transparent)
            painter = QPainter(icon)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor("#262626"), 1.4))
            if kind == "compose":
                painter.drawRoundedRect(4, 6, 14, 14, 4, 4)
                painter.setPen(QPen(QColor("#ffffff"), 4))
                painter.drawLine(12, 5, 19, 5)
                painter.setPen(QPen(QColor("#262626"), 1.4))
                painter.drawLine(10, 13, 19, 4)
                painter.drawLine(10, 13, 9, 16)
                painter.drawLine(9, 16, 12, 15)
            elif kind == "mic":
                painter.drawRoundedRect(9, 4, 6, 10, 3, 3)
                painter.drawArc(7, 9, 10, 10, 180 * 16, 180 * 16)
                painter.drawLine(12, 19, 12, 20)
                painter.drawLine(9, 20, 15, 20)
            elif kind == "bell":
                painter.drawArc(7, 4, 10, 12, 0, 180 * 16)
                painter.drawLine(7, 10, 6, 17)
                painter.drawLine(17, 10, 18, 17)
                painter.drawLine(6, 17, 18, 17)
                painter.drawArc(10, 17, 4, 4, 180 * 16, 180 * 16)
            painter.end()
            button.setIcon(QIcon(icon))
            button.setIconSize(QSize(22, 22))
            button.setFixedSize(30, 30)
            button.setToolTip(title)
            button.setAccessibleName(title)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton{border:0;background:transparent;border-radius:10px;} QPushButton:hover{background:#eeeeee;}"
            )
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.badge = QLabel("", self)
        self.badge.setStyleSheet(
            "background:#3b82f6;border:1px solid white;border-radius:4px;"
        )
        self.badge.setFixedSize(8, 8)
        self.badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.adjustSize()
        self.badge.move(95, 5)
        self.badge.hide()

    def set_mic_state(self, state):
        """录音按钮三态：idle=麦克风 / recording=红色停止块 / busy=转写中时钟"""
        from PyQt5.QtGui import QIcon, QPen

        icon = QPixmap(24, 24)
        icon.fill(Qt.transparent)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.Antialiasing)
        if state == "recording":
            painter.setPen(QPen(QColor("#b91c1c"), 1.4))
            painter.setBrush(QColor("#ef4444"))
            painter.drawRoundedRect(7, 7, 10, 10, 2, 2)
        elif state == "busy":
            painter.setPen(QPen(QColor("#262626"), 1.6))
            painter.drawArc(6, 6, 12, 12, 0, 360 * 16)
            painter.drawLine(12, 12, 12, 7)
            painter.drawLine(12, 12, 16, 12)
        else:
            painter.setPen(QPen(QColor("#262626"), 1.4))
            painter.drawRoundedRect(9, 4, 6, 10, 3, 3)
            painter.drawArc(7, 9, 10, 10, 180 * 16, 180 * 16)
            painter.drawLine(12, 19, 12, 20)
            painter.drawLine(9, 20, 15, 20)
        painter.end()
        self.mic.setIcon(QIcon(icon))
        if state == "recording":
            self.mic.setStyleSheet(
                "QPushButton{border:0;background:#fee2e2;border-radius:10px;} QPushButton:hover{background:#fecaca;}"
            )
        else:
            self.mic.setStyleSheet(
                "QPushButton{border:0;background:transparent;border-radius:10px;} QPushButton:hover{background:#eeeeee;}"
            )


class DesktopPetWidget(QWidget):
    """
    IKun 鸡哥桌宠主窗口
    """

    signal_stream_text = QtCore.pyqtSignal(str)
    signal_finish_text = QtCore.pyqtSignal(str, int)
    signal_task = QtCore.pyqtSignal(str, str)
    # 与 Web 对齐的"正文分段"事件：工具/技能活动行、结果打标、草稿作废、新任务
    signal_activity = QtCore.pyqtSignal(str)
    signal_activity_done = QtCore.pyqtSignal(bool)
    signal_reset_stream = QtCore.pyqtSignal()
    signal_start_task = QtCore.pyqtSignal(str, str)

    def __init__(
        self,
        pet_dir: str,
        on_user_input: Callable[[str], None],
        on_open_settings: Callable[[], None],
    ):
        super().__init__()
        self.pet_dir = Path(pet_dir)
        self.on_user_input = on_user_input
        self.on_open_settings = on_open_settings

        # 连接线程安全信号
        self.signal_stream_text.connect(self.stream_say)
        self.signal_finish_text.connect(self.finish_say)
        self.signal_task.connect(self.set_task)
        self.signal_activity.connect(self.append_activity)
        self.signal_activity_done.connect(self.complete_activity)
        self.signal_reset_stream.connect(self.reset_stream_text)
        self.signal_start_task.connect(self.start_task)

        # 窗口属性：无边框、置顶、透明
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)

        self.scale = 1.0
        self.anim_row = 0  # 0=idle, 3=wave, 4=jump, 6=waiting
        self.current_frame = 0
        self.frames = OrderedDict()
        self.frame_width, self.frame_height = FRAME_WIDTH, FRAME_HEIGHT
        self.rows, self.columns = 9, 8
        self.bubble_duration = 8
        self._locked_probe = lambda: False

        self._load_spritesheet()

        # 拖拽位置记录
        self.drag_position = QPoint()

        # 气泡与输入框 HUD
        self.bubble = SpeechBubble(self)
        self.hud = InputHUD(self._on_input_submitted, self)
        self.toolbar = Toolbar(self)
        self.latest_reply = ""
        self._reply_finished = False
        # 与 Web 的消息块一致：正文 = 文本段 + 工具/技能活动行 按时序拼接。
        # 每段是 {"type": "text"|"activity", "text": str}，流式只改最后一段。
        self._segments = []
        self.task_title = "MiniBot"

        # ===== 语音输入（工具栏中间按钮）：点击开始录音，再点一次发送 =====
        self._recording = False
        self._transcribing = False
        self._audio = None
        self._pcm_buffer = None
        self._record_fmt = None
        self._stt_bridge = _SttBridge()
        self._stt_bridge.finished.connect(self._on_voice_text)
        self._stt_bridge.failed.connect(self._on_voice_error)

        # 动画定时器 (10 FPS 循环)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next_frame)
        self.timer.start(200)
        self.sleep_timer = QTimer(self)
        self.sleep_timer.timeout.connect(self._update_animation_state)
        self.sleep_timer.start(2000)

        # 临时动作定时器（如 wave 或 jump 播放完毕切回 idle）
        self.action_reset_timer = QTimer(self)
        self.action_reset_timer.setSingleShot(True)
        self.action_reset_timer.timeout.connect(self._reset_to_idle)

        # 悬停按钮显示标志
        self.is_hovered = False
        self.resize(int(FRAME_WIDTH * self.scale), int(FRAME_HEIGHT * self.scale))
        self._move_to_bottom_right()

    def _load_spritesheet(self):
        self.frames.clear()
        metadata = self.pet_dir / "pet.json"
        try:
            spec = (
                json.loads(metadata.read_text(encoding="utf-8"))
                if metadata.exists()
                else {}
            )
        except (OSError, ValueError):
            spec = {}
        self.sheet_path = (
            self.pet_dir / spec.get("spritesheetPath", "spritesheet.webp")
        ).resolve()
        if self.pet_dir.resolve() not in self.sheet_path.parents:
            raise ValueError("皮肤图片必须位于皮肤目录内")
        self.frame_width = int(spec.get("frameWidth", FRAME_WIDTH))
        self.frame_height = int(spec.get("frameHeight", FRAME_HEIGHT))
        self.columns = int(spec.get("columns", 8))
        self.rows = int(spec.get("rows", 9))
        if not (
            1 <= self.columns <= 32
            and 1 <= self.rows <= 32
            and 1 <= self.frame_width <= 1024
            and 1 <= self.frame_height <= 1024
        ):
            raise ValueError("非法皮肤尺寸")
        self._load_action(0)

    def _load_action(self, row):
        if row in self.frames:
            self.frames.move_to_end(row)
            return
        if row < 0 or row >= self.rows:
            return
        # Decode only when changing an action; keep at most two rows of pixmaps.
        image = QImage(str(self.sheet_path))
        if image.isNull():
            return
        if (
            image.width() < self.frame_width * self.columns
            or image.height() < self.frame_height * self.rows
        ):
            raise ValueError("精灵图尺寸与元数据不符")
        frames = []
        for col in range(self.columns):
            frame = image.copy(
                col * self.frame_width,
                row * self.frame_height,
                self.frame_width,
                self.frame_height,
            )
            # Sprite atlases pad short actions with transparent cells. Those are not frames.
            rgba = frame.convertToFormat(QImage.Format_RGBA8888)
            bits = rgba.constBits()
            bits.setsize(rgba.byteCount())
            if not any(bytes(bits)[3::4]):
                continue
            frames.append(QPixmap.fromImage(frame))
        self.frames[row] = frames
        while len(self.frames) > 2:
            self.frames.popitem(last=False)

    def apply_config(self, cfg):
        anchor = QPoint(self.x() + self.width() // 2, self.y() + self.height())
        screen = (
            QApplication.screenAt(self.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        area = screen.availableGeometry()
        above = self.y() - area.top() - 8
        if above >= 72:
            self.bubble.fit_available(above)
            by = self.y() - self.bubble.height() - 8
        else:
            # Near the top edge, use the free space below the pet and toolbar.
            below_y = self.y() + self.height() + 50
            self.bubble.fit_available(max(60, area.bottom()-below_y))
            by = below_y
        path = Path(cfg.get("pet_dir", str(self.pet_dir)))
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        if path.resolve() != self.pet_dir.resolve():
            self.pet_dir = path
            self.anim_row = 0
            self.current_frame = 0
            self._load_spritesheet()
        self.scale = max(0.25, min(float(cfg.get("scale", 1)), 3))
        self.bubble_duration = max(1, int(cfg.get("bubble_duration_sec", 8)))
        pname = (names() or ["喵喵"])[0]
        self.bubble.title.setText(pname)
        self.hud.input_edit.setPlaceholderText("输入消息或指令...")
        effective = min(
            self.scale,
            area.width() / self.frame_width,
            area.height() / self.frame_height,
        )
        size = (
            max(1, round(self.frame_width * effective)),
            max(1, round(self.frame_height * effective)),
        )
        if (self.width(), self.height()) != size:
            self.resize(*size)
            self.move(
                max(
                    area.left(),
                    min(
                        anchor.x() - self.width() // 2, area.right() - self.width() + 1
                    ),
                ),
                max(
                    area.top(),
                    min(anchor.y() - self.height(), area.bottom() - self.height() + 1),
                ),
            )
        self._sync_subwidgets_pos()
        self.update()

    def set_activity_probe(self, probe):
        self._locked_probe = probe

    def _update_animation_state(self):
        if not self.isVisible() or self._locked_probe():
            self.timer.stop()
        elif not self.timer.isActive():
            self.timer.start(200 if self.anim_row == 0 else 100)

    def hideEvent(self, event):
        self.timer.stop()
        self.bubble.hide()
        self.hud.hide()
        self.toolbar.hide()
        super().hideEvent(event)

    def showEvent(self, event):
        if hasattr(self, "timer"):
            self._update_animation_state()
            self.toolbar.show()
            self._sync_subwidgets_pos()
        super().showEvent(event)

    def _move_to_bottom_right(self):
        desktop = QApplication.primaryScreen().availableGeometry()
        x = desktop.right() - self.width() - 40
        y = desktop.bottom() - self.height() - 100
        self.move(x, y)
        self._sync_subwidgets_pos()

    def _sync_subwidgets_pos(self):
        # 气泡位置：在桌宠头顶居中偏上
        bx = self.x() + (self.width() - self.bubble.width()) // 2
        by = self.y() - self.bubble.height() - 8
        self.bubble.move(bx, by)

        # 输入框位置：在桌宠正下方居中
        hx = self.x() + (self.width() - self.hud.width()) // 2
        hy = self.y() + self.height() + 56
        screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        tx = self.x() + (self.width() - self.toolbar.width()) // 2
        ty = self.y() + self.height() + 6
        self.toolbar.move(
            max(area.left(), min(tx, area.right() - self.toolbar.width())),
            max(area.top(), min(ty, area.bottom() - self.toolbar.height())),
        )
        hx = max(area.left(), min(hx, area.right() - self.hud.width()))
        hy = max(area.top(), min(hy, area.bottom() - self.hud.height()))
        self.hud.move(hx, hy)
        self.bubble.move(
            max(area.left(), min(bx, area.right() - self.bubble.width())),
            max(area.top(), by),
        )

    def _next_frame(self):
        frames = self.frames.get(self.anim_row) or self.frames.get(0, [])
        if not frames:
            return
        self.current_frame = (self.current_frame + 1) % len(frames)
        self.update()

    def set_action(self, row: int, duration_ms: int = 3000):
        """切换动作：0=idle, 3=wave, 4=jump, 6=waiting"""
        self._load_action(row)
        if row in self.frames and self.frames[row]:
            if self.anim_row != row:
                self.anim_row = row
                self.current_frame = 0
            self.timer.setInterval(200 if row == 0 else 100)
            if row != 0:
                self.action_reset_timer.start(duration_ms)

    def _reset_to_idle(self):
        self._load_action(0)
        self.anim_row = 0
        self.current_frame = 0
        self.timer.setInterval(200)

    def say(self, text: str, action: int = 3):
        """让桌宠说话并在头顶弹气泡"""
        self.set_action(action, duration_ms=4000)
        self._sync_subwidgets_pos()
        self.bubble.show_message(text, self.bubble_duration)
        self._sync_subwidgets_pos()

    def stream_say(self, text: str):
        """流式更新桌宠气泡：气泡只显示当前这一步，新的内容覆盖旧的。"""
        if self._reply_finished:
            return
        self.set_action(6, duration_ms=1500)
        self._sync_subwidgets_pos()
        self.bubble.status.setText("正在回复…")
        self._set_last_text(text)
        self.bubble.set_html(self._render_current())
        self._sync_subwidgets_pos()

    def finish_say(self, text: str, action: int = 3):
        """最终回答完成呈现：定稿覆盖当前显示，完整过程可在消息键里回看。"""
        self._reply_finished = True
        self._notice_open = False
        self.set_action(action, duration_ms=5000)
        self._sync_subwidgets_pos()
        self.latest_reply = text
        self.toolbar.notice.setToolTip("查看最近回复（有新消息）")
        self.toolbar.badge.show()
        self._set_last_text(text)
        self.bubble.show_html(self._render_current(), self.bubble_duration)
        self.bubble.status.setText("回复完成")
        self._sync_subwidgets_pos()

    # --- 与 Web 一致的正文分段管理 ---
    def _md_fragment(self, text: str) -> str:
        """markdown → HTML 片段（剥掉 body 外壳，段落本身已是左对齐）。"""
        html = convert_markdown_to_pet_html(text, max_width=340 - 48)
        m = re.search(r"<body[^>]*>(.*)</body>", html, re.DOTALL)
        return m.group(1) if m else html

    def _activity_html(self, line: str) -> str:
        """一条工具/技能活动：通栏、左对齐的浅灰条（与 Web 工具块观感一致）。"""
        esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (
            '<table width="100%" cellspacing="0" cellpadding="3"><tr>'
            '<td bgcolor="#eef1f4">'
            f'<span style="color:#6b7280; font-size:9pt;">{esc}</span>'
            "</td></tr></table>"
        )

    def _render_segments(self) -> str:
        """完整过程记录：按时序把 文本段/活动条 拼成一条条左对齐的 HTML（消息键回看用）。"""
        parts = []
        for seg in self._segments:
            t = (seg.get("text") or "").strip()
            if not t:
                continue
            if seg.get("type") == "activity":
                parts.append(self._activity_html(t))
            else:
                parts.append(self._md_fragment(t))
        return "<br/>".join(parts)

    def _render_current(self) -> str:
        """气泡实时视图：只渲染最后一条非空分段——文本显示完被活动条覆盖，
        活动条执行完被下一段文本覆盖，一次只看当前一步。"""
        for seg in reversed(self._segments):
            t = (seg.get("text") or "").strip()
            if not t:
                continue
            if seg.get("type") == "activity":
                return self._activity_html(t)
            return self._md_fragment(t)
        return ""

    def _set_last_text(self, text):
        if self._segments and self._segments[-1].get("type") == "text":
            self._segments[-1]["text"] = text or ""
        else:
            self._segments.append({"type": "text", "text": text or ""})

    def append_activity(self, line: str):
        """显示当前正在执行的工具/技能（覆盖之前的文本）。"""
        line = (line or "").strip()
        if not line or self._reply_finished:
            return
        self._segments.append({"type": "activity", "text": line})
        self.bubble.status.setText(line)
        self.bubble.set_html(self._render_current())
        self._sync_subwidgets_pos()

    def complete_activity(self, ok: bool):
        """给最近一条还没被新文本隔开的活动行打 ✅/⚠️。"""
        if self._segments and self._segments[-1].get("type") == "activity":
            self._segments[-1]["text"] = self._segments[-1]["text"].rstrip() + (
                " ✅" if ok else " ⚠️"
            )
            self.bubble.set_html(self._render_current())
            self._sync_subwidgets_pos()

    def reset_stream_text(self):
        """草稿被作废（核验重试/连接重试/引导打断）时丢掉最后一段流式文本。"""
        if self._segments and self._segments[-1].get("type") == "text":
            self._segments.pop()
            self.bubble.set_html(self._render_current())
            self._sync_subwidgets_pos()

    def start_task(self, title, status="正在处理…"):
        """开启新任务：重置标题、正文分段与状态行。"""
        self._reply_finished = False
        self._notice_open = False
        self.task_title = (title or "MiniBot")[:32]
        self._segments = []
        self.bubble.title.setText(self.task_title)
        self.bubble.status.setText(status or "")
        self.bubble.stream_text("")
        self.bubble.setFixedWidth(330)
        self.bubble.fit_available(self.bubble.available_height)
        self.bubble.show()
        self._sync_subwidgets_pos()

    def set_task(self, title, status):
        """同一任务内的状态刷新：只动状态行，绝不清空正文。"""
        if title and title != self.task_title:
            # 兜底：未经 start_task 的标题变化（如主动提醒）按新任务处理
            self.start_task(title, status)
            return
        if self._reply_finished:
            return
        if status != self.bubble.status.text():
            self.bubble.status.setText(status)
            self.bubble.setFixedWidth(330)
            self.bubble.fit_available(self.bubble.available_height)
            self.bubble.show()
            self._sync_subwidgets_pos()

    def set_task_safe(self, title, status):
        self.signal_task.emit(title, status)

    def start_task_safe(self, title, status="正在处理…"):
        self.signal_start_task.emit(title or "", status or "正在处理…")

    def append_activity_safe(self, line):
        self.signal_activity.emit(line)

    def complete_activity_safe(self, ok=True):
        self.signal_activity_done.emit(bool(ok))

    def reset_stream_safe(self):
        self.signal_reset_stream.emit()

    def toggle_input(self):
        if self.hud.isVisible():
            self.hud.hide()
        else:
            self.hud.adjustSize()
            self._sync_subwidgets_pos()
            self.hud.show()
            self.hud.activateWindow()
            self.hud.input_edit.setFocus()

    def show_latest(self):
        self._notice_open = True
        self.toolbar.badge.hide()
        self.toolbar.notice.setToolTip("查看最近回复（再点一次收起）")
        html = self._render_segments()
        if html:
            self.bubble.set_html(html)
        else:
            self.bubble.stream_text(self.latest_reply or "暂时没有新消息")
        self._sync_subwidgets_pos()

    def toggle_notice(self):
        """消息键开关：展开最近回复，再次点击收起。"""
        if self.bubble.isVisible() and getattr(self, "_notice_open", False):
            self._notice_open = False
            self.bubble.hide()
            self._sync_subwidgets_pos()
        else:
            self.show_latest()

    def stream_say_safe(self, text: str):
        """跨线程安全流式调用"""
        self.signal_stream_text.emit(text)

    def finish_say_safe(self, text: str, action: int = 3):
        """跨线程安全结束调用"""
        self.signal_finish_text.emit(text, action)

    # ===== 语音输入：点击开始录音，再次点击 = 转写并直接发送 =====
    def toggle_recording(self):
        if self._transcribing:
            return
        if not self._recording:
            self._start_recording()
        else:
            self._stop_and_send()

    def voice_hint(self, text, seconds=4):
        """用气泡状态行做轻提示，稍后自动清除。"""
        self.bubble.status.setText(text)
        self.bubble.show()
        self._sync_subwidgets_pos()

        def _restore():
            if not self._recording and not self._transcribing:
                self.bubble.status.setText("")
                self._sync_subwidgets_pos()

        QTimer.singleShot(seconds * 1000, _restore)

    def _start_recording(self):
        try:
            from PyQt5.QtMultimedia import (
                QAudio,
                QAudioDeviceInfo,
                QAudioFormat,
                QAudioInput,
            )
        except ImportError:
            self.toolbar.mic.setEnabled(False)
            self.toolbar.mic.setToolTip("语音输入不可用：缺少 PyQt5 Multimedia 组件")
            self.voice_hint("语音输入不可用（缺少 Multimedia 组件）", 5)
            return
        try:
            dev = QAudioDeviceInfo.defaultInputDevice()
            if dev.isNull():
                raise RuntimeError("没有检测到可用的麦克风设备")
            fmt = QAudioFormat()
            fmt.setSampleRate(16000)
            fmt.setChannelCount(1)
            fmt.setSampleSize(16)
            fmt.setCodec("audio/pcm")
            fmt.setByteOrder(QAudioFormat.LittleEndian)
            fmt.setSampleType(QAudioFormat.SignedInt)
            if not dev.isFormatSupported(fmt):
                # 设备不支持 16k/mono/16bit 时退回首选格式，封装 wav 时按实际参数来
                fmt = dev.preferredFormat()
            self._record_fmt = fmt
            self._audio = QAudioInput(dev, fmt)
            self._pcm_buffer = QtCore.QBuffer()
            self._pcm_buffer.open(QtCore.QIODevice.WriteOnly)
            self._audio.start(self._pcm_buffer)
            # 注意：PyQt5 里状态/错误枚举挂在 QAudio 命名空间，不在 QAudioInput 上
            if self._audio.state() == QAudio.StoppedState:
                reason = {
                    QAudio.OpenError: "设备打开失败（可能被占用，或系统未授权麦克风）",
                    QAudio.IOError: "设备读写错误",
                }.get(self._audio.error(), "无法访问麦克风")
                raise RuntimeError(reason)
            self._recording = True
            self.toolbar.set_mic_state("recording")
            self.toolbar.mic.setToolTip("正在录音，点击发送")
            self.set_action(3, duration_ms=3600)
            self.voice_hint("正在录音… 点击麦克风发送", 3600)
        except Exception as exc:
            self._cleanup_recording()
            self.voice_hint(f"录音启动失败: {exc}", 6)

    def _cleanup_recording(self):
        self._recording = False
        self._audio = None
        self._pcm_buffer = None
        self._record_fmt = None
        self.toolbar.set_mic_state("idle")
        self.toolbar.mic.setToolTip("点击录音，再次点击发送")

    def _wav_from_pcm(self, pcm: bytes) -> bytes:
        fmt = self._record_fmt
        rate = fmt.sampleRate() if fmt else 16000
        channels = max(1, fmt.channelCount() if fmt else 1)
        width = max(1, (fmt.sampleSize() // 8) if fmt else 2)
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(pcm)
        return bio.getvalue()

    def _stop_and_send(self):
        try:
            self._audio.stop()
        except Exception:
            pass
        fmt = self._record_fmt
        rate = fmt.sampleRate() if fmt else 16000
        frame = max(1, (fmt.channelCount() if fmt else 1) * ((fmt.sampleSize() // 8) if fmt else 2))
        pcm = bytes(self._pcm_buffer.data()) if self._pcm_buffer else b""
        self._cleanup_recording()
        if len(pcm) < rate * frame // 4:  # 少于 0.25 秒
            self.voice_hint("录音太短，没听清，再试一次吧")
            return
        try:
            wav_bytes = self._wav_from_pcm(pcm)
        except Exception as exc:
            self.voice_hint(f"音频封装失败: {exc}", 6)
            return
        self._transcribing = True
        self.toolbar.set_mic_state("busy")
        self.toolbar.mic.setEnabled(False)
        self.voice_hint("正在转写语音…", 120)

        def _worker():
            import base64
            data_url = "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii")
            try:
                from core.stt import transcribe
                result = transcribe(data_url)
                self._stt_bridge.finished.emit(result.get("text", ""))
            except Exception as exc:
                self._stt_bridge.failed.emit(str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_voice_text(self, text):
        self._transcribing = False
        self.toolbar.mic.setEnabled(True)
        self._cleanup_recording()
        text = (text or "").strip()
        if not text:
            self.voice_hint("没有识别到文字，再试一次吧")
            return
        # 第二次点击即发送，直接走与输入框相同的提交链路
        self._on_input_submitted(text)

    def _on_voice_error(self, message):
        self._transcribing = False
        self.toolbar.mic.setEnabled(True)
        self._cleanup_recording()
        self.voice_hint(f"转写失败: {message}", 6)

    def _on_input_submitted(self, text: str, input_images: list = None):
        # 新任务：标题截断与 main.py 保持一致（[:20]），避免状态刷新被误判为新任务
        self.start_task(text[:20] if text else "MiniBot", "正在处理…")
        self.set_action(6, duration_ms=6000)  # 思考状态
        try:
            self.on_user_input(text, input_images=input_images)
        except TypeError:
            try:
                self.on_user_input(text, input_images)
            except TypeError:
                self.on_user_input(text)

    # --- 绘图事件 ---
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        frame_list = self.frames.get(self.anim_row) or self.frames.get(0, [])
        if frame_list:
            pixmap = frame_list[self.current_frame % len(frame_list)]
            target_rect = QRect(0, 0, self.width(), self.height())
            painter.drawPixmap(target_rect, pixmap)

    # --- 鼠标与交互事件 ---
    def enterEvent(self, event):
        self.is_hovered = True
        self.update()

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
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
