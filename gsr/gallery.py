"""Gallery: browse, play and delete saved replays, recordings and screenshots inside the overlay.

Not part of upstream gsr-ui; built from the same widgets (GsrPage frame, RadioButton filter,
ScrollablePage grid like the file chooser) so it looks and behaves like the other pages.
"""
import ctypes
import hashlib
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

from . import gui, system
from .config import DATA_DIR, TR
from .gui import (Button, Event, GsrPage, Label, List, RadioButton, ScrollablePage, Sprite, StaticPage, Text,
                  Widget, color_theme, theme)

KINDS = {"Replay": "replay", "Video": "record", "Screenshot": "screenshot"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".flv", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
THUMBS = DATA_DIR / "thumbs"
MAX_ITEMS = 500
_pool = ThreadPoolExecutor(2)


class Clip:
    def __init__(self, path, kind, st):
        self.path = path
        self.kind = kind
        self.mtime = st.st_mtime
        self.size = st.st_size
        self.is_image = path.suffix.lower() in IMAGE_EXT
        self.duration = None
        self.thumb = None
        self.thumb_state = 0   # 0 = not requested, 1 = loading, 2 = ready/failed
        self.title = Text(time.strftime("%Y-%m-%d  %H:%M", time.localtime(self.mtime)), "body")
        self.game = ""


def scan(cfg):
    """Clips in the configured save folders (and their per-game subfolders), newest first."""
    roots = {cfg["replay"]["save_directory"], cfg["record"]["save_directory"], cfg["screenshot"]["save_directory"]}
    clips, seen = [], set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for pattern in ("*", "*/*"):
            for p in root.glob(pattern):
                prefix = p.name.split("_", 1)[0]
                ext = p.suffix.lower()
                if prefix not in KINDS or ext not in VIDEO_EXT | IMAGE_EXT or p in seen:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size > 0:
                    seen.add(p)
                    clip = Clip(p, KINDS[prefix], st)
                    clip.game = p.parent.name if p.parent != root else ""   # per-game subfolder
                    clips.append(clip)
    clips.sort(key=lambda c: c.mtime, reverse=True)
    return clips[:MAX_ITEMS]


def _load_thumb(clip, ffmpeg):
    """Worker thread: make (or reuse) a small jpg for the clip and read the video duration."""
    try:
        key = hashlib.md5(f"{clip.path}|{clip.mtime}|{clip.size}".encode()).hexdigest()
        THUMBS.mkdir(parents=True, exist_ok=True)
        jpg, meta = THUMBS / f"{key}.jpg", THUMBS / f"{key}.txt"
        if not jpg.exists():
            args = [ffmpeg, "-hide_banner", "-y"]
            if not clip.is_image:
                args += ["-ss", "1"]
            args += ["-i", str(clip.path), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(jpg)]
            res = subprocess.run(args, capture_output=True, creationflags=system.CREATE_NO_WINDOW, timeout=30)
            m = re.search(rb"Duration: (\d+):(\d+):(\d+)", res.stderr)
            if m and not clip.is_image:
                meta.write_text(str(int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])))
            if not jpg.exists() and not clip.is_image:   # clips shorter than 1 s
                subprocess.run([ffmpeg, "-hide_banner", "-y", "-i", str(clip.path), "-frames:v", "1", "-vf",
                                "scale=480:-2", str(jpg)], capture_output=True,
                               creationflags=system.CREATE_NO_WINDOW, timeout=30)
        if meta.exists():
            clip.duration = int(meta.read_text() or 0)
        img = QImage(str(jpg))
        clip.thumb = None if img.isNull() else img
    except Exception:
        clip.thumb = None
    clip.thumb_state = 2


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def recycle(path):
    """Move a file to the Recycle Bin (undoable) instead of deleting it outright."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW(None, 3, str(path) + "\0", None, 0x40 | 0x10 | 0x4 | 0x400, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


class ClipGrid(Widget):
    """Thumbnail grid (same hover/selection drawing as the file chooser)."""
    COLUMNS = 3

    def __init__(self, page, size):
        super().__init__()
        self.page = page
        self.size = size
        self.inner_size = size
        self.clips = []
        self.mouse_over = -1
        self.empty_text = Text(TR("No clips yet. Saved replays, recordings and screenshots show up here."), "body")

    def on_event(self, ev, win, offset):
        if self.visible and ev.type == Event.PRESS and ev.button == 1 and 0 <= self.mouse_over < len(self.clips):
            self.page.open_clip(self.clips[self.mouse_over])
            return False
        return True

    def draw(self, win, offset):
        self.mouse_over = -1
        if not self.visible:
            return
        p = win.p
        H = theme.window_height
        (sx, sy), (sw, sh) = win.get_scissor()
        dp = gui.add(self.position, offset)
        if not self.clips:
            self.empty_text.color = (150, 150, 150, 255)
            self.empty_text.pos = gui.fl(dp)
            self.empty_text.draw(p)
            self.inner_size = (self.size[0], self.empty_text.bounds()[1])
            return
        spacing = int(0.012 * H)
        pad = int(0.006 * H)
        iw = float(int((self.size[0] - spacing * (self.COLUMNS - 1)) / self.COLUMNS))
        tw, th = iw - pad * 2, float(int((iw - pad * 2) * 9 / 16))
        text_h = self.clips[0].title.bounds()[1]
        ih = pad + th + pad + text_h + pad
        border = max(1.0, 0.0015 * H)
        hover_ok = not self.has_parent_with_selected_child_widget()
        for i, clip in enumerate(self.clips):
            col, row = i % self.COLUMNS, i // self.COLUMNS
            ipos = (dp[0] + col * (iw + spacing), dp[1] + row * (ih + spacing))
            if ipos[1] + ih < sy or ipos[1] > sy + sh:
                continue
            if clip.thumb_state == 0:
                clip.thumb_state = 1
                _pool.submit(_load_thumb, clip, self.page.app.ffmpeg)
            gui.rect(p, gui.fl(ipos), (iw, ih), (0, 0, 0, 120))
            tpos = gui.fl((ipos[0] + pad, ipos[1] + pad))
            gui.rect(p, tpos, (tw, th), (0, 0, 0, 255))
            if clip.thumb is not None:
                sc = min(tw / clip.thumb.width(), th / clip.thumb.height())
                w, h = clip.thumb.width() * sc, clip.thumb.height() * sc
                p.drawImage(QRectF(tpos[0] + (tw - w) / 2, tpos[1] + (th - h) / 2, w, h), clip.thumb)
            icon = Sprite("notify_" + clip.kind)
            icon.set_height(int(th * 0.2))
            icon.pos = gui.fl((tpos[0] + pad, tpos[1] + pad))
            gui.rect(p, gui.fl((tpos[0], tpos[1])), (icon.size[0] + pad * 2, icon.size[1] + pad * 2), (0, 0, 0, 160))
            icon.draw(p)
            if clip.duration is not None:
                d = Text(fmt_time(clip.duration), "body")
                db = d.bounds()
                box = (db[0] + pad * 2, db[1] + pad)
                bpos = gui.fl((tpos[0] + tw - box[0], tpos[1] + th - box[1]))
                gui.rect(p, bpos, box, (0, 0, 0, 180))
                d.pos = gui.fl((bpos[0] + pad, bpos[1] + pad * 0.5))
                d.draw(p)
            clip.title.pos = gui.fl((ipos[0] + pad, ipos[1] + pad + th + pad))
            clip.title.draw(p)
            if clip.game:
                g = Text(clip.game, "body", (150, 150, 150, 255))
                gb = g.bounds()
                avail = iw - pad * 3 - clip.title.bounds()[0]
                if avail > 30:
                    win.push_scissor((ipos[0] + iw - pad - min(gb[0], avail), ipos[1]), (min(gb[0], avail), ih))
                    g.pos = gui.fl((ipos[0] + iw - pad - min(gb[0], avail), clip.title.pos[1]))
                    g.draw(p)
                    win.pop_scissor()
            if (hover_ok and self.mouse_over == -1 and sx <= win.mouse[0] <= sx + sw and sy <= win.mouse[1] <= sy + sh
                    and gui.contains(ipos, (iw, ih), win.mouse)):
                gui.outline(p, ipos, (iw, ih), color_theme.tint_color, border)
                self.mouse_over = i
        rows = (len(self.clips) + self.COLUMNS - 1) // self.COLUMNS
        self.inner_size = (self.size[0], rows * (ih + spacing) - spacing)

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)

    def get_inner_size(self):
        return self.inner_size if self.visible else (0.0, 0.0)


def _frame(title, bottom):
    page = GsrPage(TR(title), TR(bottom))
    page.icon = "gallery"
    page.size_scale = (0.5, 0.7)
    return page


class GalleryPage(StaticPage):
    def __init__(self, app, page_stack):
        super().__init__((float(int(theme.window_width)), float(int(theme.window_height))))
        self.app = app
        self.page_stack = page_stack
        self.content = _frame("Gallery", "Clips")
        self.content.add_button(TR("Back"), "back", color_theme.page_bg_color)
        self.content.add_button(TR("Open folder"), "folder", color_theme.page_bg_color)
        self.content.on_click = self._clicked
        self.add_widget(self.content)
        self.all_clips = scan(app.cfg)

        page_list = List(List.VERTICAL)
        page_list.set_spacing(0.018)
        self.filter = RadioButton("body", RadioButton.HORIZONTAL)
        for text, id_ in (("All", "all"), ("Instant Replay", "replay"), ("Record", "record"), ("Screenshot", "screenshot")):
            self.filter.add_item(TR(text), id_)
        self.filter.set_horizontal_alignment(Widget.CENTER)
        self.filter.on_selection_changed = lambda _, id_: self._apply_filter(id_) or True
        page_list.add_widget(self.filter)
        inner = self.content.get_inner_size()
        self.scroll = ScrollablePage((inner[0], inner[1] - (page_list.get_size()[1] + 0.018 * theme.window_height)))
        self.grid = ClipGrid(self, self.scroll.get_inner_size())
        self.scroll.add_widget(self.grid)
        page_list.add_widget(self.scroll)
        self.content.add_widget(page_list)
        self._apply_filter("all")

    def _apply_filter(self, kind):
        self.grid.clips = [c for c in self.all_clips if kind == "all" or c.kind == kind]
        self.scroll.reset_scroll()

    def _clicked(self, id_):
        if id_ == "back":
            self.page_stack.pop()
        elif id_ == "folder":
            folder = self.app.cfg["replay"]["save_directory"]
            Path(folder).mkdir(parents=True, exist_ok=True)
            self.app.hide()
            os.startfile(folder)

    def open_clip(self, clip):
        self.page_stack.push(PlayerPage(self.app, self.page_stack, self, clip))

    def remove_clip(self, clip):
        if clip in self.all_clips:
            self.all_clips.remove(clip)
        self._apply_filter(self.filter.get_selected_id())


class MediaPlayer(QObject):
    """QMediaPlayer whose frames are painted by our own widget instead of a video window."""

    def __init__(self):
        super().__init__()
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.sink = QVideoSink()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.frame = None
        self.sink.videoFrameChanged.connect(self._on_frame)

    def _on_frame(self, frame):
        img = frame.toImage()
        if not img.isNull():
            self.frame = img

    def open(self, path):
        self.frame = None
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def close(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.frame = None

    def playing(self):
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def toggle(self):
        if self.playing():
            self.player.pause()
        else:
            if self.player.duration() and self.player.position() >= self.player.duration() - 50:
                self.player.setPosition(0)
            self.player.play()


class VideoView(Widget):
    def __init__(self, page, size):
        super().__init__()
        self.page = page
        self.size = size

    def on_event(self, ev, win, offset):
        if self.visible and ev.type == Event.PRESS and ev.button == 1 and not self.page.clip.is_image:
            if gui.contains(gui.add(self.position, offset), self.size, (ev.x, ev.y)):
                self.page.media.toggle()
                return False
        return True

    def draw(self, win, offset):
        if not self.visible:
            return
        pos = gui.fl(gui.add(self.position, offset))
        gui.rect(win.p, pos, self.size, (0, 0, 0, 255))
        img = self.page.image if self.page.clip.is_image else self.page.media.frame
        if img is not None and img.width() > 0:
            sc = min(self.size[0] / img.width(), self.size[1] / img.height())
            w, h = img.width() * sc, img.height() * sc
            win.p.drawImage(QRectF(pos[0] + (self.size[0] - w) / 2, pos[1] + (self.size[1] - h) / 2, w, h), img)
        if not self.page.clip.is_image and not self.page.media.playing():
            s = Sprite("play")
            s.set_height(int(self.size[1] * 0.18))
            s.color = (255, 255, 255, 200)
            s.pos = gui.fl((pos[0] + self.size[0] / 2 - s.size[0] / 2, pos[1] + self.size[1] / 2 - s.size[1] / 2))
            s.draw(win.p)

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)


class SeekBar(Widget):
    def __init__(self, page, width):
        super().__init__()
        self.page = page
        self.width = width
        self.dragging = False

    def _h(self):
        return float(int(0.022 * theme.window_height))

    def _ratio(self, x, pos):
        return min(1.0, max(0.0, (x - pos[0]) / max(1.0, self.width)))

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        pos = gui.add(self.position, offset)
        player = self.page.media.player
        if ev.type == Event.PRESS and ev.button == 1 and gui.contains(pos, self.get_size(), (ev.x, ev.y)):
            self.dragging = True
            self.set_widget_as_selected_in_parent()
            player.setPosition(int(self._ratio(ev.x, pos) * player.duration()))
            return False
        if self.dragging and ev.type == Event.MOVE:
            player.setPosition(int(self._ratio(ev.x, pos) * player.duration()))
            return False
        if self.dragging and ev.type == Event.RELEASE:
            self.dragging = False
            self.remove_widget_as_selected_in_parent()
            return False
        return True

    def draw(self, win, offset):
        if not self.visible:
            return
        pos = gui.fl(gui.add(self.position, offset))
        h = self._h()
        track_h = max(2.0, float(int(h * 0.3)))
        ty = pos[1] + (h - track_h) / 2
        player = self.page.media.player
        ratio = player.position() / player.duration() if player.duration() > 0 else 0.0
        gui.rect(win.p, (pos[0], ty), (self.width, track_h), (0, 0, 0, 120))
        gui.rect(win.p, (pos[0], ty), (self.width * ratio, track_h), color_theme.tint_color)
        hover = self.dragging or gui.contains(pos, self.get_size(), win.mouse)
        knob = h * (0.9 if hover else 0.7)
        win.p.setPen(Qt.PenStyle.NoPen)
        win.p.setBrush(QColor(255, 255, 255))
        win.p.setRenderHint(win.p.RenderHint.Antialiasing, True)
        win.p.drawEllipse(QRectF(pos[0] + self.width * ratio - knob / 2, pos[1] + (h - knob) / 2, knob, knob))
        win.p.setRenderHint(win.p.RenderHint.Antialiasing, False)

    def get_size(self):
        return (self.width, self._h()) if self.visible else (0.0, 0.0)


class PlayerPage(StaticPage):
    """Plays a clip (or shows a screenshot) inside the same panel."""

    def __init__(self, app, page_stack, gallery, clip):
        super().__init__((float(int(theme.window_width)), float(int(theme.window_height))))
        self.app = app
        self.page_stack = page_stack
        self.gallery = gallery
        self.clip = clip
        self.media = app.media_player()
        self.image = QImage(str(clip.path)) if clip.is_image else None
        self.confirm_delete = False
        self.content = _frame("Gallery", {"replay": "Instant Replay", "record": "Record", "screenshot": "Screenshot"}[clip.kind])
        self.content.add_button(TR("Back"), "back", color_theme.page_bg_color)
        self.content.add_button(TR("Show in folder"), "reveal", color_theme.page_bg_color)
        self.content.add_button(TR("Delete"), "delete", color_theme.page_bg_color)
        self.content.on_click = self._clicked
        self.add_widget(self.content)
        inner = self.content.get_inner_size()
        H = theme.window_height
        lst = List(List.VERTICAL)
        lst.set_spacing(0.012)
        name = Label("body", clip.path.name, color_theme.text_color)
        controls_h = 0.0
        self.time_label = Label("body", "0:00 / 0:00", color_theme.text_color)
        row = None
        if not clip.is_image:
            self.play_btn = Button("body", "", (0.0, 0.0), (0, 0, 0, 120))
            self.play_btn.set_icon("pause")
            self.play_btn.on_click = self.media.toggle
            bw = self.play_btn.get_size()[0]
            spacing = float(int(0.009 * H))
            seek_w = inner[0] - bw - self.time_label.get_size()[0] - spacing * 2 - int(0.01 * H)
            self.seek = SeekBar(self, seek_w)
            row = List(List.HORIZONTAL, List.CENTER)
            row.add_widget(self.play_btn)
            row.add_widget(self.seek)
            row.add_widget(self.time_label)
            controls_h = row.get_size()[1] + int(0.012 * H)
        view_h = inner[1] - name.get_size()[1] - int(0.012 * H) - controls_h
        self.view = VideoView(self, (inner[0], float(int(view_h))))
        lst.add_widget(name)
        lst.add_widget(self.view)
        if row:
            lst.add_widget(row)
        self.content.add_widget(lst)
        if not clip.is_image:
            self.media.open(clip.path)

    def draw(self, win, offset):
        if not self.clip.is_image:
            player = self.media.player
            self.play_btn.set_icon("pause" if self.media.playing() else "play")
            self.time_label.set_text(f"{fmt_time(player.position() / 1000)} / {fmt_time(player.duration() / 1000)}")
        super().draw(win, offset)

    def on_event(self, ev, win, offset):
        if ev.type == Event.KEY and not self.clip.is_image:
            player = self.media.player
            if ev.key == Qt.Key.Key_Space:
                self.media.toggle()
                return False
            if ev.key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                step = -5000 if ev.key == Qt.Key.Key_Left else 5000
                player.setPosition(min(max(0, player.position() + step), player.duration()))
                return False
        return super().on_event(ev, win, offset)

    def _clicked(self, id_):
        if id_ == "back":
            self.page_stack.pop()
        elif id_ == "reveal":
            self.app.hide()
            subprocess.Popen(["explorer", "/select,", str(self.clip.path)])
        elif id_ == "delete":
            btn = self.content.buttons[2]
            if not self.confirm_delete:
                self.confirm_delete = True
                btn.set_text(TR("Confirm delete"))
                threading.Timer(3.0, self._cancel_delete).start()
                return
            self.media.close()
            if recycle(self.clip.path):
                self.gallery.remove_clip(self.clip)
            self.page_stack.pop()

    def _cancel_delete(self):
        self.confirm_delete = False
        self.app.call_main(lambda: self.content.buttons[2].set_text(TR("Delete")))

    def on_navigate_away_from_page(self):
        if not self.clip.is_image:
            self.media.close()
