"""Port of gsr-notify (gpu-screen-recorder-notification src/main.cpp, GPL-3.0).

Same geometry and animation: a bar in bg_color slides in from the right edge of the
monitor, then the black content panel slides over it (leaving a thin bg_color strip on
the left), the icon and text fade in, and after the timeout everything plays in reverse.
"""
import ctypes
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from .gui import Sprite, Text, theme

# (state, seconds); the 'pause' with None is the notification timeout
STATES = [("slide_in_window", 0.15), ("pause", 0.03), ("slide_in_content", 0.10), ("fade_in_content", 0.20),
          ("pause", None), ("fade_out_content", 0.001), ("slide_out_content", 0.10), ("pause", 0.10),
          ("slide_out_window", 0.10)]
BUILTIN_ICONS = {"record": "notify_record", "replay": "notify_replay", "stream": "notify_stream",
                 "screenshot": "notify_screenshot", "notice": "gpu_screen_recorder_logo"}


def lerp(a, b, t):
    return a + (b - a) * t


class Notification(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput |
                         Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.active = False

    def show_message(self, screen_geo, text, timeout, icon_color, bg_color, icon):
        mw, mh = screen_geo.width(), screen_geo.height()
        units = int(mh / 60.0 / 1.8)
        key = f"notify_{units}"
        theme.register_font(key, units, True)
        self.text = Text(text, key)
        self.text.wrap_width = mw // 5
        tb = self.text.bounds()
        text_padding_vertical = units
        wh = mh // 13
        self.cpl = max(2.0, float(int(wh * 0.05)))
        self.icon = None
        logo_pad = float(int(wh * 0.4))
        between = 0.0
        pad_right = float(int(wh * 0.4))
        name = BUILTIN_ICONS.get(icon)
        if name and name in theme.textures:
            self.icon = Sprite(name)
            self.icon.set_height(int(wh * 0.4))
            logo_pad = float(int((wh - self.icon.size[1]) * 0.4))
            between = float(int(logo_pad * 0.7))
        self.logo_pad, self.between = logo_pad, between
        icon_w = self.icon.size[0] if self.icon else 0.0
        self.w = int(self.cpl + logo_pad + icon_w + between + tb[0] + pad_right)
        self.h = max(wh, int(text_padding_vertical + tb[1] + text_padding_vertical))
        self.icon_color = icon_color
        self.bg_color = bg_color
        self.states = [(s, timeout if d is None else d) for s, d in STATES]
        self.index = 0
        self.state_start = time.monotonic()
        self.clip = 0.0
        self.content_x = self.w + self.cpl
        self.alpha = 0.0
        self.setGeometry(screen_geo.x() + mw - self.w, screen_geo.y() + mh // 13, self.w, self.h)
        if not self.isVisible():
            self.show()
            exclude_from_capture(self)
        self.raise_()
        self.active = True
        self.timer.start(8)

    def _tick(self):
        state, dur = self.states[self.index]
        t = min(1.0, (time.monotonic() - self.state_start) / dur) if dur > 0 else 1.0
        if state == "slide_in_window":
            self.clip = self.w * t
        elif state == "slide_in_content":
            self.content_x = lerp(self.w + self.cpl, self.cpl, t)
        elif state == "fade_in_content":
            self.alpha = t
        elif state == "fade_out_content":
            self.alpha = 1.0 - t
        elif state == "slide_out_content":
            self.content_x = lerp(self.cpl, self.w + self.cpl, t)
        elif state == "slide_out_window":
            self.clip = self.w * (1.0 - t)
        if state != "pause":
            self.update()
        if t >= 1.0:
            self.index += 1
            self.state_start = time.monotonic()
            if self.index >= len(self.states):
                self.timer.stop()
                self.active = False
                self.hide()

    def paintEvent(self, _):
        if not self.active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        clip = int(self.clip)
        if clip <= 0:
            p.end()
            return
        p.setClipRect(QRectF(self.w - clip, 0, clip, self.h))
        p.fillRect(QRectF(0, 0, self.w, self.h), QColor(*self.bg_color[:3]))
        cx = float(int(self.content_x))
        p.fillRect(QRectF(cx, 0, self.w - self.cpl, self.h), QColor(0, 0, 0))
        a = int(self.alpha * 255)
        text_x = cx + self.cpl + self.logo_pad
        if self.icon:
            self.icon.pos = (float(int(cx + self.cpl + self.logo_pad)), float(int(self.h * 0.5 - self.icon.size[1] * 0.5)))
            self.icon.color = tuple(self.icon_color[:3]) + (a,)
            if a > 0:
                self.icon.draw(p)
            text_x = self.icon.pos[0] + self.icon.size[0] + self.between
        if a > 0:
            self.text.color = (255, 255, 255, a)
            self.text.pos = (float(int(text_x)), float(int(self.h * 0.5 - self.text.bounds()[1] * 0.5)))
            self.text.draw(p)
        p.end()


def exclude_from_capture(widget):
    """Keep our UI out of recordings/screenshots (WDA_EXCLUDEFROMCAPTURE, Windows 10 2004+)."""
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11)
    except Exception:
        pass
