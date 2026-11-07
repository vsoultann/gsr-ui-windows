"""Port of gsr-ui's Overlay: the fullscreen Alt+Z UI and everything it controls."""
import ctypes
import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QImage, QPainter
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from . import audio, config, engine, gallery, gui, hotkeys, pages, system
from .config import TR, TRF
from .gui import Button, DropdownButton, Event, List, PageStack, Sprite, StaticPage, Text, Win, color_theme, theme
from .notify import Notification, exclude_from_capture

BG_COLOR = (0, 0, 0, 100)
WHITE = (255, 255, 255, 255)
RED = (255, 0, 0, 255)
REPLAY_SAVING_NOTIFICATION_TIMEOUT = 0.5
SHORT_TIMEOUT, TIMEOUT, ERROR_TIMEOUT = 2.0, 3.0, 5.0

QT_KEYS = {
    Qt.Key.Key_Print: "printscreen", Qt.Key.Key_Space: "space", Qt.Key.Key_Tab: "tab", Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter", Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end", Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown", Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Pause: "pause",
    Qt.Key.Key_ScrollLock: "scrolllock", Qt.Key.Key_Escape: "escape", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_QuoteLeft: "`", Qt.Key.Key_Minus: "-", Qt.Key.Key_Equal: "=", Qt.Key.Key_BracketLeft: "[",
    Qt.Key.Key_BracketRight: "]", Qt.Key.Key_Semicolon: ";", Qt.Key.Key_Apostrophe: "'", Qt.Key.Key_Comma: ",",
    Qt.Key.Key_Period: ".", Qt.Key.Key_Slash: "/", Qt.Key.Key_Backslash: "\\",
}
MODIFIER_KEYS = {Qt.Key.Key_Alt, Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_AltGr}


def qt_key_name(ev):
    k = ev.key()
    if k in MODIFIER_KEYS:
        return None
    if Qt.Key.Key_F1 <= k <= Qt.Key.Key_F24:
        return f"f{k - Qt.Key.Key_F1 + 1}"
    if Qt.Key.Key_A <= k <= Qt.Key.Key_Z or Qt.Key.Key_0 <= k <= Qt.Key.Key_9:
        name = chr(k).lower()
        return ("num" + name) if (ev.modifiers() & Qt.KeyboardModifier.KeypadModifier and name.isdigit()) else name
    return QT_KEYS.get(k, "unknown")


def qt_mods(ev):
    m = ev.modifiers()
    out = []
    if m & Qt.KeyboardModifier.ControlModifier:
        out.append("ctrl")
    if m & Qt.KeyboardModifier.ShiftModifier:
        out.append("shift")
    if m & Qt.KeyboardModifier.AltModifier:
        out.append("alt")
    if m & Qt.KeyboardModifier.MetaModifier:
        out.append("win")
    return out


def duration_string(sec):
    s = int(math.ceil(sec))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if h:
        parts.append(TRF("%d hour", h) if h == 1 else TRF("%d hours", h))
    if m:
        parts.append(TRF("%d minute", m) if m == 1 else TRF("%d minutes", m))
    if s or not parts:
        parts.append(TRF("%d second", s) if s == 1 else TRF("%d seconds", s))
    return " ".join(parts)


# ====================================================================== selection


class RegionSelector(QWidget):
    """Region / window picker over the monitor under the cursor (upstream RegionSelector)."""

    def __init__(self, mode, screen_geo, on_done):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.mode = mode
        self.on_done = on_done
        self.geo = screen_geo
        self.start = None
        self.cur = None
        self.hover = None
        self.setGeometry(screen_geo)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.hwnd = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._track)

    def begin(self):
        self.show()
        self.raise_()
        self.activateWindow()
        ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
        self.timer.start(30)

    def _track(self):
        if self.mode == "window":
            x, y = system.cursor_pos()
            info = system.window_under(x, y, exclude=(int(self.winId()),))
            if info != self.hover:
                self.hover = info
                self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        tint = QColor(*color_theme.tint_color[:3])
        border = max(2, int(self.geo.height() * 0.002))
        r = None
        if self.mode == "region" and self.start and self.cur:
            r = QRect(self.start, self.cur).normalized()
        elif self.mode == "window" and self.hover:
            _, _, l, t, rr, b = self.hover
            r = QRect(l - self.geo.x(), t - self.geo.y(), rr - l, b - t)
        if r:
            p.fillRect(r, QColor(0, 0, 0, 1))
            for rc in (QRect(r.left(), r.top(), r.width(), border), QRect(r.left(), r.bottom() - border + 1, r.width(), border),
                       QRect(r.left(), r.top(), border, r.height()), QRect(r.right() - border + 1, r.top(), border, r.height())):
                p.fillRect(rc, tint)
        p.end()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self.mode == "region":
            self.start = self.cur = e.position().toPoint()
        else:
            self.hwnd = self.hover[0] if self.hover else None
            self._finish(self.hover and (self.hover[2], self.hover[3], self.hover[4] - self.hover[2], self.hover[5] - self.hover[3]),
                         self.hover[1] if self.hover else "")

    def mouseMoveEvent(self, e):
        if self.mode == "region" and self.start:
            self.cur = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.mode == "region" and self.start:
            r = QRect(self.start, e.position().toPoint()).normalized()
            if r.width() < 4 or r.height() < 4:
                self._finish(None, "")
            else:
                self._finish((r.x() + self.geo.x(), r.y() + self.geo.y(), r.width(), r.height()), "")

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._finish(None, "")

    def _finish(self, rect_, title):
        self.timer.stop()
        self.hide()
        self.deleteLater()
        self.on_done(rect_, title, getattr(self, "hwnd", None))


# ====================================================================== overlay window


class OverlayWindow(QWidget):
    def __init__(self, app):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.app = app
        # Truly translucent (not an opaque window showing a screenshot): Windows keeps composing what is
        # behind us, so captures that exclude this window still get the live game instead of black.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.win = Win()
        self.bg = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.last_frame = time.monotonic()

    def paintEvent(self, _):
        now = time.monotonic()
        self.win.frame_delta = min(0.1, now - self.last_frame)
        self.last_frame = now
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.win.p = p
        pos = self.mapFromGlobal(QCursor.pos())
        self.win.mouse = (float(pos.x()), float(pos.y()))
        self.app.draw(self.win)
        p.end()

    def _ev(self, type_, e, button=None):
        pos = e.position()
        self.win.mouse = (pos.x(), pos.y())
        self.app.on_event(Event(type_, pos.x(), pos.y(), button), self.win)
        self.update()

    def mouseMoveEvent(self, e):
        self._ev(Event.MOVE, e)

    def mousePressEvent(self, e):
        b = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2}.get(e.button(), 3)
        self._ev(Event.PRESS, e, b)

    def mouseReleaseEvent(self, e):
        b = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2}.get(e.button(), 3)
        self._ev(Event.RELEASE, e, b)

    def wheelEvent(self, e):
        pos = e.position()
        self.app.on_event(Event(Event.WHEEL, pos.x(), pos.y(), delta=e.angleDelta().y() / 120.0), self.win)
        self.update()

    def keyPressEvent(self, e):
        self.app.on_key(e, self.win)
        self.update()

    def event(self, e):
        # deliver Tab / arrows to us instead of focus navigation
        if e.type() == e.Type.KeyPress:
            self.keyPressEvent(e)
            return True
        return super().event(e)


# ====================================================================== app


class App(QObject):
    VERSION = "1.13.5-win"
    hotkey_fired = Signal(str)
    show_requested = Signal()
    main_thread_call = Signal(object)

    def __init__(self, qapp, show_event, daemon):
        super().__init__()
        self.qapp = qapp
        self.cfg = config.load()
        config.translation.load_language(self.cfg["main"]["language"])
        gui.set_translator(TR)
        theme.load()
        self.ffmpeg = system.find_ffmpeg()
        self.outputs = system.list_outputs()
        primary = next((o for o in self.outputs if o.primary), self.outputs[0])
        theme.set_window_size(primary.width, primary.height)
        color_theme.tint_color = gui.parse_tint(self.cfg["main"]["tint_color"] or self.default_tint_name())
        self.caps, self._version, self._cameras, self._devices = self._load_cache()
        self.replay = self.record = self.stream = None
        self.replay_recording = False
        self.recording_target = None
        self.replay_target = None
        self.saving = False
        self.visible = False
        self.page_stack = PageStack()
        self.overlay = OverlayWindow(self)
        self.notification = Notification()
        self.hotkeys = hotkeys.Hotkeys(lambda name: self.hotkey_fired.emit(name))
        self.hotkey_fired.connect(self.on_hotkey)
        self.show_requested.connect(self.toggle_show)
        self.main_thread_call.connect(lambda fn: fn())
        self.close_pressed_inside = False
        self.game_hwnd = None
        self.rebind_hotkeys()
        self._tray()
        threading.Thread(target=self._refresh_cache, daemon=True).start()
        threading.Thread(target=self._wait_show, args=(show_event,), daemon=True).start()
        self.tick = QTimer()
        self.tick.timeout.connect(self._tick)
        self.tick.start(500)
        if self.hotkey_failures:
            taken = ", ".join(pages.hotkey_display(self.cfg["hotkeys"][k]) for k in self.hotkey_failures)
            QTimer.singleShot(5500 if daemon else 500, lambda: self.notify(
                f"These hotkeys are used by another program (e.g. NVIDIA overlay): {taken}. "
                "Disable that program's hotkeys or change them in Settings.",
                8.0, WHITE, RED, "notice", error=True))
        if daemon:
            msg = TRF("Press %s to open the GPU Screen Recorder UI", pages.hotkey_display(self.cfg["hotkeys"]["main.show_hide_hotkey"]))
            self.notify(msg, 5.0, WHITE, color_theme.tint_color, "notice", error=True)
        else:
            QTimer.singleShot(0, self.show)
        if self.cfg["replay"]["turn_on_replay_automatically_mode"] == "turn_on_at_system_startup":
            QTimer.singleShot(1500, lambda: self.replay_autostart_allowed() and self.on_press_start_replay(True))

    def call_main(self, fn):
        self.main_thread_call.emit(fn)

    def media_player(self):
        if not getattr(self, "_media", None):
            self._media = gallery.MediaPlayer()
        return self._media

    # ------------------------------------------------------------ info / caches

    def _load_cache(self):
        try:
            d = json.loads((config.DATA_DIR / "info.json").read_text(encoding="utf-8"))
            if d.get("ffmpeg") == self.ffmpeg:
                return set(d["caps"]), d.get("version", ""), d.get("cameras", []), d.get("devices")
        except (OSError, ValueError, KeyError):
            pass
        if self.ffmpeg:
            d = self._probe()
            return set(d["caps"]), d["version"], d["cameras"], d["devices"]
        return set(), "", [], None

    def _probe(self):
        caps = system.probe_encoders(self.ffmpeg)
        res = system.run_ffmpeg(self.ffmpeg, ["-version"], timeout=10)
        m = re.search(r"ffmpeg version (\S+)", res.stdout.decode("utf-8", "replace"))
        d = {"ffmpeg": self.ffmpeg, "caps": sorted(caps), "version": m[1].split("-")[0] if m else "",
             "cameras": list_cameras(self.ffmpeg), "devices": self._device_list()}
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / "info.json").write_text(json.dumps(d), encoding="utf-8")
        return d

    def _refresh_cache(self):
        if not self.ffmpeg:
            return
        d = self._probe()
        self.caps, self._version, self._cameras, self._devices = set(d["caps"]), d["version"], d["cameras"], d["devices"]

    def _device_list(self):
        try:
            devs = audio.list_devices()
        except Exception:
            devs = {"outputs": [], "inputs": []}
        return {"outputs": [["default_output", "Default output device"]] + [["device:" + n, n] for n in devs["outputs"]],
                "inputs": [["default_input", "Default input device"]] + [["device:" + n, n] for n in devs["inputs"]]}

    def audio_devices(self):
        d = self._devices or self._device_list()
        return {"outputs": [(a, TR(b)) for a, b in d["outputs"]],
                "inputs": [(a, TR(b)) for a, b in d["inputs"]]}

    def cameras(self):
        return [{"name": c["name"], "yuyv": [tuple(s) for s in c["yuyv"]], "mjpeg": [tuple(s) for s in c["mjpeg"]]}
                for c in self._cameras]

    def supported_codecs(self):
        out = next((o for o in self.outputs if o.primary), self.outputs[0])
        return engine.supported_codecs(out, self.caps)

    def engine_version(self):
        return f"FFmpeg {self._version}" if self._version else "unknown"

    def gpu_vendor(self):
        v = next((o for o in self.outputs if o.primary), self.outputs[0]).vendor
        return {system.INTEL: "intel", system.NVIDIA: "nvidia", system.AMD: "amd"}.get(v, "unknown")

    def default_tint_name(self):
        return {"intel": "intel", "nvidia": "nvidia"}.get(self.gpu_vendor(), "amd")

    def save_config(self):
        config.save(self.cfg)

    # ------------------------------------------------------------ hotkeys

    def rebind_hotkeys(self):
        self.hotkey_failures = {}
        if self.cfg["main"]["hotkeys_enable_option"] == "disable_hotkeys":
            self.hotkeys.stop()
            return
        bindings = {k: v for k, v in self.cfg["hotkeys"].items() if v}
        self.hotkey_failures = self.hotkeys.set(bindings)

    def unbind_hotkeys(self):
        self.hotkeys.stop()

    def on_hotkey(self, name):
        actions = {
            "main.show_hide_hotkey": self.toggle_show,
            "replay.start_stop_hotkey": lambda: self.on_press_start_replay(False),
            "replay.save_hotkey": lambda: self.on_press_save_replay(None),
            "replay.save_1_min_hotkey": lambda: self.on_press_save_replay(60),
            "replay.save_10_min_hotkey": lambda: self.on_press_save_replay(600),
            "record.start_stop_hotkey": lambda: self.on_press_start_record(),
            "record.pause_unpause_hotkey": self.toggle_pause,
            "record.start_stop_region_hotkey": lambda: self.on_press_start_record("region"),
            "record.start_stop_window_hotkey": lambda: self.on_press_start_record("window"),
            "streaming.start_stop_hotkey": self.on_press_start_stream,
            "screenshot.take_screenshot_hotkey": lambda: self.on_press_take_screenshot(),
            "screenshot.take_screenshot_region_hotkey": lambda: self.on_press_take_screenshot("region"),
            "screenshot.take_screenshot_window_hotkey": lambda: self.on_press_take_screenshot("window"),
        }
        fn = actions.get(name)
        if fn:
            fn()

    def _wait_show(self, event):
        while True:
            if system.wait_event(event, 0xFFFFFFFF):
                self.show_requested.emit()

    # ------------------------------------------------------------ notifications

    def notify(self, text, timeout, icon_color, bg_color, icon, error=False):
        if not error and self.cfg["main"]["notification_speed"] == "fast":
            timeout *= 0.3
        x, y = system.cursor_pos()
        out = system.output_at(self.outputs, x, y)
        screen = self._qscreen(out)
        self.notification.show_message(screen.geometry() if screen else QRect(0, 0, out.width, out.height),
                                       text, timeout, icon_color, bg_color, icon)

    def _qscreen(self, out):
        for s in QGuiApplication.screens():
            if s.name() == out.device:
                return s
        return QGuiApplication.primaryScreen()

    # ------------------------------------------------------------ show / hide

    def toggle_show(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def show(self):
        if self.visible:
            return
        x, y = system.cursor_pos()
        out = system.output_at(self.outputs, x, y)
        screen = self._qscreen(out)
        geo = screen.geometry()
        theme.set_window_size(geo.width(), geo.height())
        self.visible = True
        self.recreate_frontpage()
        self.overlay.setGeometry(geo)
        self.overlay.show()
        exclude_from_capture(self.overlay)
        self.overlay.raise_()
        self.overlay.activateWindow()
        ctypes.windll.user32.SetForegroundWindow(int(self.overlay.winId()))
        self.overlay.setFocus()
        self.overlay.timer.start(16)

    def hide(self):
        if not self.visible:
            return
        while not self.page_stack.empty():
            self.page_stack.pop()
        self.visible = False
        self.overlay.timer.stop()
        self.overlay.hide()
        self.overlay.bg = None
        gui.unfocus_entries()

    def recreate_frontpage(self):
        W, H = theme.window_width, theme.window_height
        self.top_bar_size = gui.fl((W, H * 0.06))
        self.top_bar_text = Text("GPU Screen Recorder", "top_bar")
        tb = self.top_bar_text.bounds()
        self.top_bar_text.pos = gui.fl((self.top_bar_size[0] * 0.5 - tb[0] * 0.5, self.top_bar_size[1] * 0.5 - tb[1] * 0.5))
        self.logo = Sprite("gpu_screen_recorder_logo")
        self.logo.set_height(int(self.top_bar_size[1] * 0.65))
        self.logo.pos = gui.fl(((self.top_bar_size[1] - self.logo.size[1]) * 0.5, self.top_bar_size[1] * 0.5 - self.logo.size[1] * 0.5))
        cs = math.floor(self.top_bar_size[1] * 0.35)
        self.close_size = (float(cs), float(cs))
        self.close_pos = gui.fl((W - cs - self.logo.pos[0], self.top_bar_size[1] * 0.5 - cs * 0.5))

        while not self.page_stack.empty():
            self.page_stack.pop()
        front = StaticPage(gui.fl((W, H)))
        self.page_stack.push(front)
        bh = int(H / 5.0)
        main = List(List.HORIZONTAL)
        main.set_spacing(0.0)
        hk = lambda k: pages.hotkey_display(self.cfg["hotkeys"].get(k, ""))

        rb = DropdownButton(TR("Instant Replay"), TR("Off"), "replay", (float(bh), float(bh)))
        rb.add_item(TR("Turn on"), "start", hk("replay.start_stop_hotkey"))
        rb.add_item(TR("Save"), "save", hk("replay.save_hotkey"))
        rb.add_item(TR("Save 1 min"), "save_1_min", hk("replay.save_1_min_hotkey"))
        rb.add_item(TR("Save 10 min"), "save_10_min", hk("replay.save_10_min_hotkey"))
        rb.add_item(TR("Settings"), "settings")
        for id_, icon in (("start", "play"), ("save", "save"), ("save_1_min", "save"), ("save_10_min", "save"), ("settings", "settings_extra_small")):
            rb.set_item_icon(id_, icon)

        def replay_click(id_):
            if id_ == "settings":
                page = pages.SettingsPage(pages.SettingsPage.REPLAY, self, self.page_stack)

                def changed():
                    if self.replay:
                        self.notify(TR("Replay settings have been modified. You may need to restart replay to apply the changes."),
                                    TIMEOUT, WHITE, color_theme.tint_color, "replay")
                page.on_config_changed = changed
                self.page_stack.push(page)
            elif id_ == "save":
                self.on_press_save_replay(None)
            elif id_ == "save_1_min":
                self.on_press_save_replay(60)
            elif id_ == "save_10_min":
                self.on_press_save_replay(600)
            elif id_ == "start":
                self.on_press_start_replay(False)
        rb.on_click = replay_click
        for id_ in ("save", "save_1_min", "save_10_min"):
            rb.set_item_enabled(id_, False)
        main.add_widget(rb)
        self.replay_btn = rb

        rec = DropdownButton(TR("Record"), TR("Not recording"), "record", (float(bh), float(bh)))
        rec.add_item(TR("Start"), "start", hk("record.start_stop_hotkey"))
        rec.add_item(TR("Pause"), "pause", hk("record.pause_unpause_hotkey"))
        rec.add_item(TR("Settings"), "settings")
        rec.set_item_icon("start", "play")
        rec.set_item_icon("pause", "pause")
        rec.set_item_icon("settings", "settings_extra_small")

        def record_click(id_):
            if id_ == "settings":
                page = pages.SettingsPage(pages.SettingsPage.RECORD, self, self.page_stack)

                def changed():
                    if self.record:
                        self.notify(TR("Recording settings have been modified. You may need to restart recording to apply the changes."),
                                    TIMEOUT, WHITE, color_theme.tint_color, "record")
                page.on_config_changed = changed
                self.page_stack.push(page)
            elif id_ == "pause":
                self.toggle_pause()
            elif id_ == "start":
                self.on_press_start_record()
        rec.on_click = record_click
        rec.set_item_enabled("pause", False)
        main.add_widget(rec)
        self.record_btn = rec

        st = DropdownButton(TR("Livestream"), TR("Not streaming"), "stream", (float(bh), float(bh)))
        st.add_item(TR("Start"), "start", hk("streaming.start_stop_hotkey"))
        st.add_item(TR("Settings"), "settings")
        st.set_item_icon("start", "play")
        st.set_item_icon("settings", "settings_extra_small")

        def stream_click(id_):
            if id_ == "settings":
                page = pages.SettingsPage(pages.SettingsPage.STREAM, self, self.page_stack)

                def changed():
                    if self.stream:
                        self.notify(TR("Streaming settings have been modified. You may need to restart streaming to apply the changes."),
                                    TIMEOUT, WHITE, color_theme.tint_color, "stream")
                page.on_config_changed = changed
                self.page_stack.push(page)
            elif id_ == "start":
                self.on_press_start_stream()
        st.on_click = stream_click
        main.add_widget(st)
        self.stream_btn = st

        msize = main.get_size()
        main.set_position(gui.fl((W * 0.5 - msize[0] * 0.5, H * 0.25 - msize[1] * 0.5)))
        front.add_widget(main)
        s = int(msize[1] * 0.33)
        settings_btn = Button("title", "", (float(s), float(s)), (0, 0, 0, 180))
        settings_btn.set_position(gui.fl(gui.add(gui.sub(gui.add(main.position, msize), (0.0, s)), (s * 0.333, 0.0))))
        settings_btn.set_bg_hover_color((0, 0, 0, 255))
        settings_btn.set_icon("settings_small")
        settings_btn.on_click = self.open_settings_page
        front.add_widget(settings_btn)
        shot_btn = Button("title", "", (float(s), float(s)), (0, 0, 0, 180))
        shot_btn.set_position(gui.fl(gui.add(gui.sub(gui.add(main.position, msize), (0.0, s * 2)), (s * 0.333, 0.0))))
        shot_btn.set_bg_hover_color((0, 0, 0, 255))
        shot_btn.set_icon("screenshot")
        shot_btn.set_icon_padding_scale(1.2)
        shot_btn.on_click = lambda: self.page_stack.push(pages.ScreenshotSettingsPage(self, self.page_stack))
        front.add_widget(shot_btn)
        gallery_btn = Button("title", "", (float(s), float(s)), (0, 0, 0, 180))
        gallery_btn.set_position(gui.fl(gui.add(gui.sub(gui.add(main.position, msize), (0.0, s * 3)), (s * 0.333, 0.0))))
        gallery_btn.set_bg_hover_color((0, 0, 0, 255))
        gallery_btn.set_icon("gallery")
        gallery_btn.set_icon_padding_scale(0.95)
        gallery_btn.on_click = lambda: self.page_stack.push(gallery.GalleryPage(self, self.page_stack))
        front.add_widget(gallery_btn)
        self.update_ui()

    def open_settings_page(self, scroll_y=0):
        page = pages.GlobalSettingsPage(self, self.page_stack)
        page.set_scroll_y(scroll_y)

        def lang_changed():
            config.translation.load_language(self.cfg["main"]["language"])
            y = page.get_scroll_y()
            self.save_config()
            QTimer.singleShot(0, lambda: (self.recreate_frontpage(), self.open_settings_page(y)))
        page.on_language_changed = lang_changed
        self.page_stack.push(page)

    def on_global_settings_closed(self):
        hk = lambda k: pages.hotkey_display(self.cfg["hotkeys"].get(k, ""))
        if self.visible and hasattr(self, "replay_btn"):
            self.replay_btn.set_item_description("start", hk("replay.start_stop_hotkey"))
            self.replay_btn.set_item_description("save", hk("replay.save_hotkey"))
            self.replay_btn.set_item_description("save_1_min", hk("replay.save_1_min_hotkey"))
            self.replay_btn.set_item_description("save_10_min", hk("replay.save_10_min_hotkey"))
            self.record_btn.set_item_description("start", hk("record.start_stop_hotkey"))
            self.record_btn.set_item_description("pause", hk("record.pause_unpause_hotkey"))
            self.stream_btn.set_item_description("start", hk("streaming.start_stop_hotkey"))

    def exit_program(self):
        self.hide()
        for s in (self.replay, self.stream):
            if s:
                s.stop()
        if self.record:
            rec = self.record
            rec.stop()
            try:
                rec.finish_recording(self._record_dest())
            except Exception:
                pass
        if self._led_wanted():
            system.set_scroll_lock_led(False)
        self.hotkeys.stop()
        self.tray.hide()
        self.qapp.quit()

    # ------------------------------------------------------------ drawing / events

    def draw(self, win):
        p = win.p
        W, H = theme.window_width, theme.window_height
        if self.overlay.bg is not None:   # only set by the offscreen render test
            p.drawPixmap(QRectF(0, 0, W, H), self.overlay.bg, QRectF(self.overlay.bg.rect()))
        p.fillRect(QRectF(0, 0, W, H), QColor(*BG_COLOR))
        gui.rect(p, (0.0, 0.0), self.top_bar_size, (0, 0, 0, 180))
        self.top_bar_text.draw(p)
        self.logo.draw(p)
        border = int(max(1.0, 0.0015 * H))
        pad = max(1.0, 0.003 * H)
        if gui.contains(self.close_pos, self.close_size, win.mouse):
            gui.outline(p, self.close_pos, self.close_size, color_theme.tint_color, border)
        cross = Sprite("cross")
        cross.set_size((self.close_size[0] - pad * 2, self.close_size[1] - pad * 2))
        cross.pos = (self.close_pos[0] + pad, self.close_pos[1] + pad)
        cross.draw(p)
        self.page_stack.draw(win, (0.0, 0.0))
        gui.draw_tooltip(win)

    def on_event(self, ev, win):
        if ev.type == Event.PRESS and ev.button == 1:
            self.close_pressed_inside = gui.contains(self.close_pos, self.close_size, (ev.x, ev.y))
        elif ev.type == Event.RELEASE and ev.button == 1 and self.close_pressed_inside:
            if gui.contains(self.close_pos, self.close_size, (ev.x, ev.y)):
                self.hide()
                return
        self.page_stack.on_event(ev, win, (0.0, 0.0))
        if self.page_stack.empty():
            self.hide()

    def on_key(self, e, win):
        top = self.page_stack.top()
        if isinstance(top, pages.GlobalSettingsPage) and top.configuring_hotkey():
            name = qt_key_name(e)
            top.configure_hotkey_key(qt_mods(e), name)
            return
        if e.key() == Qt.Key.Key_Escape:
            if gui._focused_entry:
                gui.unfocus_entries()
                return
            self.page_stack.pop()
            if self.page_stack.empty():
                self.hide()
            return
        mods = e.modifiers()
        self.page_stack.on_event(Event(Event.KEY, key=e.key(), mods=mods), win, (0.0, 0.0))
        if e.text() and not (mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            self.page_stack.on_event(Event(Event.TEXT, text=e.text()), win, (0.0, 0.0))

    # ------------------------------------------------------------ UI state

    def update_ui(self):
        if not self.visible or not hasattr(self, "replay_btn"):
            return
        rb, rec, st = self.replay_btn, self.record_btn, self.stream_btn
        if self.replay:
            rb.set_item_label("start", TR("Turn off"))
            rb.set_activated(True)
            rb.set_description(TR("On"))
            rb.set_item_icon("start", "stop")
            rb.set_item_enabled("save", True)
            rb.set_item_enabled("save_1_min", self.replay.replay_time >= 60)
            rb.set_item_enabled("save_10_min", self.replay.replay_time >= 600)
        else:
            rb.set_item_label("start", TR("Turn on"))
            rb.set_activated(False)
            rb.set_description(TR("Off"))
            rb.set_item_icon("start", "play")
            for id_ in ("save", "save_1_min", "save_10_min"):
                rb.set_item_enabled(id_, False)
        if self.record:
            rec.set_item_label("start", TR("Stop and save"))
            rec.set_activated(True)
            rec.set_item_icon("start", "stop")
            rec.set_item_enabled("pause", True)
            if self.record.paused:
                rec.set_description(TR("Paused"))
                rec.set_item_label("pause", TR("Unpause"))
                rec.set_item_icon("pause", "play")
            else:
                rec.set_description(TR("Recording"))
                rec.set_item_label("pause", TR("Pause"))
                rec.set_item_icon("pause", "pause")
        else:
            rec.set_item_label("start", TR("Start"))
            rec.set_activated(False)
            rec.set_description(TR("Not recording"))
            rec.set_item_icon("start", "play")
            rec.set_item_label("pause", TR("Pause"))
            rec.set_item_icon("pause", "pause")
            rec.set_item_enabled("pause", False)
        if self.stream:
            st.set_item_label("start", TR("Stop"))
            st.set_activated(True)
            st.set_description(TR("Streaming"))
            st.set_item_icon("start", "stop")
        else:
            st.set_item_label("start", TR("Start"))
            st.set_activated(False)
            st.set_description(TR("Not streaming"))
            st.set_item_icon("start", "play")

    def _state_changed(self):
        self.update_ui()
        self._update_led()
        self._update_tray()

    def _led_wanted(self):
        return any((s and s.ro["use_led_indicator"]) for s in (self.replay, self.record, self.stream))

    def _update_led(self):
        any_led_option = any(self.cfg[k]["record_options"]["use_led_indicator"] for k in ("replay", "record", "streaming"))
        if any_led_option or self._led_wanted():
            system.set_scroll_lock_led(self._led_wanted())

    def _tick(self):
        for name, icon in (("replay", "replay"), ("record", "record"), ("stream", "stream")):
            s = getattr(self, name)
            if s and s.error:
                setattr(self, name, None)
                text = {"replay": "Replay stopped because of an error", "record": "Failed to start/save recording",
                        "stream": "Streaming stopped because of an error. Verify if settings are correct"}[name]
                self.notify(TR(text), ERROR_TIMEOUT, RED, RED, icon, error=True)
                self._state_changed()
        if self.cfg["replay"]["turn_on_replay_automatically_mode"] == "turn_on_at_game_launch":
            self._game_tracker()

    def _game_tracker(self):
        user32 = ctypes.windll.user32
        if self.game_hwnd:
            if not user32.IsWindow(self.game_hwnd):
                self.game_hwnd = None
                if self.replay:
                    self.on_press_start_replay(True)
        elif not self.replay and not self.record and not self.stream and system.fullscreen_app_focused():
            if self.replay_autostart_allowed():
                self.game_hwnd = user32.GetForegroundWindow()
                self.on_press_start_replay(True)

    def replay_autostart_allowed(self):
        return not self.cfg["replay"]["only_start_replay_if_power_supply_connected"] or system.power_supply_connected()

    # ------------------------------------------------------------ capture target

    def _resolve_target(self, option, callback, kind_icon):
        """Turn a record_area_option into an engine Target, asking for a region/window when needed."""
        if not self.ffmpeg:
            self.notify("ffmpeg.exe was not found", ERROR_TIMEOUT, RED, RED, kind_icon, error=True)
            return
        if option in ("region", "window"):
            self.hide()
            x, y = system.cursor_pos()
            out = system.output_at(self.outputs, x, y)
            screen = self._qscreen(out)

            def done(rect_, title, hwnd=None):
                if not rect_:
                    self.notify(TR("No window selected") if option == "window" else TR("Failed to start region capture"),
                                TIMEOUT, WHITE, RED, "notice", error=True)
                    return
                cx, cy = rect_[0] + rect_[2] // 2, rect_[1] + rect_[3] // 2
                o = system.output_at(self.outputs, cx, cy)
                l = max(rect_[0], o.left) - o.left
                t = max(rect_[1], o.top) - o.top
                r = min(rect_[0] + rect_[2], o.left + o.width) - o.left
                b = min(rect_[1] + rect_[3], o.top + o.height) - o.top
                if option == "window" and hwnd:
                    callback(engine.Target(o, None, TR("window"), hwnd=hwnd, window_size=(rect_[2], rect_[3])))
                    return
                callback(engine.Target(o, (l, t, r - l, b - t), TR("region")))
            sel = RegionSelector(option, screen.geometry(), done)
            self._selector = sel
            sel.begin()
            return
        if option == "focused_monitor" or not option:
            x, y = system.cursor_pos()
            out = system.output_at(self.outputs, x, y)
        else:
            out = next((o for o in self.outputs if o.device == option), None)
            if not out:
                self.notify(TRF('Failed to start recording, capture target "%s" is invalid. Please change capture target in settings', option),
                            ERROR_TIMEOUT, RED, RED, kind_icon, error=True)
                return
        callback(engine.Target(out, None, TR("this monitor")))

    def _session(self, kind, target, **kw):
        sec = {"replay": "replay", "record": "record", "stream": "streaming"}[kind]
        ro = config.copy_cfg(self.cfg[sec]["record_options"])
        return engine.Session(kind, ro, target, self.caps, self.ffmpeg,
                              exclude_metadata=self.cfg["main"]["exclude_metadata"], **kw)

    def _dest(self, sec, game_folder):
        base = Path(self.cfg[sec]["save_directory"])
        return (base / system.foreground_title()) if game_folder else base

    def _record_dest(self):
        return self._dest("record", self.cfg["record"]["save_video_in_game_folder"])

    # ------------------------------------------------------------ replay

    def on_press_start_replay(self, disable_notification=False):
        if self.replay:
            self.replay.stop()
            self.replay = None
            self.game_hwnd = None if disable_notification else self.game_hwnd
            if self.replay_recording and self.record:
                self._stop_record()
            if self.cfg["replay"]["record_options"]["show_notifications"]:
                self.notify(TR("Replay stopped"), SHORT_TIMEOUT if disable_notification else TIMEOUT, WHITE,
                            color_theme.tint_color, "replay")
            self._state_changed()
            return
        if self.record:
            self.notify(TR("Unable to start replay when recording. Stop recording before starting replay."), ERROR_TIMEOUT, RED, RED, "record", error=True)
            return
        if self.stream:
            self.notify(TR("Unable to start replay when streaming. Stop streaming before starting replay."), ERROR_TIMEOUT, RED, RED, "stream", error=True)
            return

        def start(target):
            sec = self.cfg["replay"]
            try:
                self.replay = self._session("replay", target, replay_time=sec["replay_time"], container=sec["container"])
                self.replay.start()
            except Exception:
                self.replay = None
                self.notify(TR("Failed to launch gpu-screen-recorder to start replay"), ERROR_TIMEOUT, RED, RED, "replay", error=True)
                return
            self.replay_target = target
            if sec["record_options"]["show_notifications"]:
                self.notify(TRF("Started replaying %s", target.label), TIMEOUT, WHITE, color_theme.tint_color, "replay")
            self._state_changed()
        self._resolve_target(self.cfg["replay"]["record_options"]["record_area_option"], start, "replay")

    def on_press_save_replay(self, seconds):
        if not self.replay or self.saving:
            return
        self.saving = True
        session = self.replay
        sec = self.cfg["replay"]
        game = sec["save_video_in_game_folder"]
        folder = system.foreground_title() if game else None
        dest = Path(sec["save_directory"]) / folder if game else Path(sec["save_directory"])
        done = threading.Event()

        def slow():
            if not done.is_set() and sec["record_options"]["show_notifications"]:
                self.notify(TR("Saving replay, this might take some time"), TIMEOUT, WHITE, color_theme.tint_color, "replay")
        QTimer.singleShot(int(REPLAY_SAVING_NOTIFICATION_TIMEOUT * 1000), slow)

        def work():
            try:
                path, dur = session.save_replay(dest, seconds)
                ok = True
            except Exception:
                ok = False
            done.set()

            def finish():
                self.saving = False
                if not ok:
                    self.notify(TR("Failed to save replay, make sure the replay save directory is mounted and writable"),
                                ERROR_TIMEOUT, RED, RED, "replay", error=True)
                    return
                if sec["restart_replay_on_save"]:
                    session.clear_replay()
                if sec["record_options"]["show_notifications"]:
                    tgt = self.replay_target.label if self.replay_target else TR("this monitor")
                    if folder:
                        msg = TR('Saved a %s replay of %s to "%s"') % (duration_string(dur), tgt, folder[:40])
                    else:
                        msg = TR("Saved a %s replay of %s") % (duration_string(dur), tgt)
                    self.notify(msg, TIMEOUT, WHITE, color_theme.tint_color, "replay")
            self.call_main(finish)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ record

    def on_press_start_record(self, force=None):
        if self.record:
            self._stop_record()
            return
        option = force or self.cfg["record"]["record_options"]["record_area_option"]

        def start(target):
            sec = self.cfg["record"]
            try:
                self.record = self._session("record", target, container=sec["container"])
                self.record.start()
            except Exception:
                self.record = None
                self.notify(TR("Failed to launch gpu-screen-recorder to start recording"), ERROR_TIMEOUT, RED, RED, "record", error=True)
                return
            self.recording_target = target
            self.replay_recording = bool(self.replay)
            if sec["record_options"]["show_notifications"]:
                if self.replay:
                    self.notify(TR("Started recording in the replay session"), SHORT_TIMEOUT, color_theme.tint_color, color_theme.tint_color, "record")
                elif self.stream:
                    self.notify(TR("Started recording in the streaming session"), SHORT_TIMEOUT, color_theme.tint_color, color_theme.tint_color, "record")
                else:
                    self.notify(TRF("Started recording %s", target.label), TIMEOUT, WHITE, color_theme.tint_color, "record")
            self._state_changed()
        self._resolve_target(option, start, "record")

    def _stop_record(self):
        session = self.record
        self.record = None
        self.replay_recording = False
        sec = self.cfg["record"]
        game = sec["save_video_in_game_folder"]
        folder = system.foreground_title() if game else None
        dest = Path(sec["save_directory"]) / folder if game else Path(sec["save_directory"])
        duration = session.elapsed()
        self._state_changed()

        def work():
            session.stop()
            try:
                session.finish_recording(dest)
                ok = True
            except Exception:
                ok = False

            def finish():
                if not ok:
                    self.notify(TR("Failed to start/save recording"), ERROR_TIMEOUT, RED, RED, "record", error=True)
                elif sec["record_options"]["show_notifications"]:
                    tgt = session.target.label
                    if folder:
                        msg = TR('Saved a %s recording of %s to "%s"') % (duration_string(duration), tgt, folder[:40])
                    else:
                        msg = TR("Saved a %s recording of %s") % (duration_string(duration), tgt)
                    self.notify(msg, TIMEOUT, WHITE, color_theme.tint_color, "record")
            self.call_main(finish)
        threading.Thread(target=work, daemon=True).start()

    def toggle_pause(self):
        if not self.record:
            return
        paused = not self.record.paused
        threading.Thread(target=self.record.set_paused, args=(paused,), daemon=True).start()
        self.record.paused = paused
        if self.cfg["record"]["record_options"]["show_notifications"]:
            self.notify(TR("Recording has been paused") if paused else TR("Recording has been unpaused"),
                        TIMEOUT, WHITE, color_theme.tint_color, "record")
        self._state_changed()

    # ------------------------------------------------------------ stream

    def _stream_url(self):
        sec = self.cfg["streaming"]
        svc = sec["streaming_service"]
        if svc == "twitch":
            return "rtmp://live.twitch.tv/app/" + sec["twitch"]["stream_key"], "flv"
        if svc == "youtube":
            return "rtmp://a.rtmp.youtube.com/live2/" + sec["youtube"]["stream_key"], "flv"
        if svc == "rumble":
            return "rtmp://rtmp.rumble.com/live/" + sec["rumble"]["stream_key"], "flv"
        if svc == "kick":
            url = sec["kick"]["stream_url"]
            if url and not url.endswith("/"):
                url += "/"
            return url + sec["kick"]["stream_key"], "flv"
        url = sec["custom"]["url"]
        key = sec["custom"]["key"]
        if key:
            url = url + ("" if url.endswith("/") else "/") + key
        if not re.match(r"^\w+://", url):
            url = "rtmp://" + url
        fmt = {"mp4": "mp4", "flv": "flv", "mpegts": "mpegts", "hls": "hls"}.get(sec["custom"]["container"], "flv")
        return url, fmt

    def on_press_start_stream(self):
        if self.stream:
            self.stream.stop()
            self.stream = None
            if self.cfg["streaming"]["record_options"]["show_notifications"]:
                self.notify(TR("Streaming has stopped"), TIMEOUT, WHITE, color_theme.tint_color, "stream")
            self._state_changed()
            return
        if self.replay:
            self.notify(TR("Unable to start streaming when replay is turned on. Turn off replay before starting streaming."),
                        ERROR_TIMEOUT, RED, RED, "replay", error=True)
            return
        if self.record:
            self.notify(TR("Unable to start streaming when recording. Stop recording before starting streaming."),
                        ERROR_TIMEOUT, RED, RED, "record", error=True)
            return

        def start(target):
            url, fmt = self._stream_url()
            svc = self.cfg["streaming"]["streaming_service"]
            try:
                self.stream = self._session("stream", target, stream_url=url, stream_format=fmt,
                                            force_h264=svc in ("rumble", "kick") or fmt == "flv")
                self.stream.start()
            except Exception:
                self.stream = None
                self.notify(TR("Failed to launch gpu-screen-recorder to start streaming"), ERROR_TIMEOUT, RED, RED, "stream", error=True)
                return
            if self.cfg["streaming"]["record_options"]["show_notifications"]:
                self.notify(TRF("Started streaming %s", target.label), TIMEOUT, WHITE, color_theme.tint_color, "stream")
            self._state_changed()
        self._resolve_target(self.cfg["streaming"]["record_options"]["record_area_option"], start, "stream")

    # ------------------------------------------------------------ screenshot

    def on_press_take_screenshot(self, force=None):
        sc = self.cfg["screenshot"]
        option = force or sc["record_area_option"]

        def start(target):
            game = sc["save_screenshot_in_game_folder"]
            folder = system.foreground_title() if game else None
            base = Path(sc["save_directory"]) / folder if game else Path(sc["save_directory"])
            ext = "png" if sc["image_format"] == "png" else "jpg"
            to_disk = sc["save_screenshot_to_disk"]
            out = engine.unique_path((base if to_disk else config.DATA_DIR) / f"Screenshot_{engine.stamp()}.{ext}")
            if sc["use_led_indicator"]:
                system.set_scroll_lock_led(True)

            def work():
                try:
                    engine.take_screenshot(self.ffmpeg, target, sc, out)
                    ok = True
                except Exception:
                    ok = False

                def finish():
                    if sc["use_led_indicator"]:
                        QTimer.singleShot(200, self._update_led)
                    if not ok:
                        self.notify(TR("Failed to take a screenshot. Verify if settings are correct"), TIMEOUT, RED, RED, "screenshot", error=True)
                        return
                    if sc["save_screenshot_to_clipboard"]:
                        QGuiApplication.clipboard().setImage(QImage(str(out)))
                    if sc["custom_script"].strip():
                        try:
                            subprocess.Popen(f'{sc["custom_script"]} "{out}"', shell=True, creationflags=system.CREATE_NO_WINDOW)
                        except OSError:
                            pass
                    if not to_disk and not sc["custom_script"].strip():
                        QTimer.singleShot(2000, lambda: out.unlink(missing_ok=True))
                    if sc["show_notifications"]:
                        if folder:
                            msg = TR('Saved a screenshot of %s to "%s"') % (target.label, folder[:40])
                        else:
                            msg = TR("Saved a screenshot of %s") % target.label
                        self.notify(msg, TIMEOUT, WHITE, color_theme.tint_color, "screenshot")
                self.call_main(finish)
            threading.Thread(target=work, daemon=True).start()
        self._resolve_target(option, start, "screenshot")

    # ------------------------------------------------------------ tray

    def _tray(self):
        self.tray = QSystemTrayIcon(QIcon(str(gui.RES / "images" / "gpu_screen_recorder_logo.png")))
        menu = QMenu()
        show = QAction(TR("Show UI"), menu)
        show.triggered.connect(self.show)
        quit_ = QAction(TR("Exit program"), menu)
        quit_.triggered.connect(self.exit_program)
        menu.addAction(show)
        menu.addSeparator()
        menu.addAction(quit_)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: r == QSystemTrayIcon.ActivationReason.Trigger and self.toggle_show())
        self._update_tray()
        self.tray.show()

    def _update_tray(self):
        parts = [n for n, s in (("Replay", self.replay), ("Recording", self.record), ("Streaming", self.stream)) if s]
        self.tray.setToolTip("GPU Screen Recorder" + (" - " + ", ".join(parts) if parts else ""))


def list_cameras(ffmpeg):
    """dshow video devices with their YUYV / MJPEG modes: [{name, yuyv: [(w,h,fps)], mjpeg: [...]}]."""
    res = system.run_ffmpeg(ffmpeg, ["-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=15)
    names = re.findall(r'"([^"]+)" \(video\)', res.stderr.decode("utf-8", "replace"))
    cams = []
    for name in names:
        if "OBS" in name and "Virtual" in name:
            continue
        r = system.run_ffmpeg(ffmpeg, ["-list_options", "true", "-f", "dshow", "-i", f"video={name}"], timeout=15)
        cam = {"name": name, "yuyv": [], "mjpeg": []}
        for line in r.stderr.decode("utf-8", "replace").splitlines():
            m = re.search(r"(pixel_format=yuyv422|vcodec=mjpeg).*?max s=(\d+)x(\d+) fps=([\d.]+)", line)
            if m:
                setup = [int(m[2]), int(m[3]), int(round(float(m[4])))]
                key = "yuyv" if "yuyv" in m[1] else "mjpeg"
                if setup not in cam[key]:
                    cam[key].append(setup)
        cam["yuyv"] = [tuple(s) for s in cam["yuyv"]]
        cam["mjpeg"] = [tuple(s) for s in cam["mjpeg"]]
        if cam["yuyv"] or cam["mjpeg"]:
            cams.append(cam)
    return cams
