"""Render the overlay offscreen to PNGs (no windows, no hotkeys) for visual checks.

    python tools/render_test.py OUT_DIR
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import gallery, hotkeys, overlay, pages  # noqa: E402
gallery.nvidia_capture_dir = lambda: Path(tempfile.mkdtemp())   # never show the real NVIDIA clips
from gsr.gui import Event, Win, theme  # noqa: E402

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "render_out")
out_dir.mkdir(parents=True, exist_ok=True)

hotkeys.Hotkeys.set = lambda self, b: {}
hotkeys.Hotkeys.stop = lambda self: None
overlay.App._tray = lambda self: setattr(self, "tray", type("T", (), {"setToolTip": lambda *a: None, "hide": lambda *a: None})())
overlay.App._update_tray = lambda self: None
overlay.App.notify = lambda self, *a, **k: None
overlay.App._wait_show = lambda self, e: None
overlay.App._refresh_cache = lambda self: None

qapp = QApplication(sys.argv)
app = overlay.App(qapp, None, daemon=True)
app.tick.stop()
W, H = 1440, 1080
theme.set_window_size(W, H)
app.visible = True

bg = QPixmap(W, H)
p = QPainter(bg)
g = QLinearGradient(0, 0, W, H)
g.setColorAt(0, QColor(70, 110, 160))
g.setColorAt(1, QColor(170, 120, 80))
p.fillRect(0, 0, W, H, g)
p.end()
app.overlay.bg = bg
app.recreate_frontpage()

win = Win()
win.frame_delta = 1.0


def render(name, mouse=(0, 0), frames=3):
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    for _ in range(frames):
        img.fill(0)
        qp = QPainter(img)
        qp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        qp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        win.p = qp
        win.mouse = mouse
        app.draw(win)
        qp.end()
    img.save(str(out_dir / f"{name}.png"))
    print("saved", name)


def move(x, y):
    win.mouse = (x, y)
    app.on_event(Event(Event.MOVE, x, y), win)


def click(x, y, button=1):
    move(x, y)
    app.on_event(Event(Event.PRESS, x, y, button), win)
    app.on_event(Event(Event.RELEASE, x, y, button), win)


render("01_front")
rb = app.replay_btn
cx, cy = rb.position[0] + rb.size[0] / 2, rb.position[1] + rb.size[1] / 2
move(cx, cy)
render("02_hover_replay", (cx, cy))
click(cx, cy)
render("03_replay_dropdown", (cx, cy + rb.size[1] + 10))
# open replay settings via dropdown item "Settings" (last item)
ih = rb.items[0]["text"].bounds()[1] + 2 * int(0.00694 * H)
sy = rb.position[1] + rb.size[1] + ih * 4.5
move(cx, sy)
render("tmp", (cx, sy), frames=1)
click(cx, sy)
render("04_replay_settings", (0, 0))
app.page_stack.top().view_radio.set_selected_item("advanced", True, True)
render("05_replay_settings_advanced", (0, 0))
top = app.page_stack.top()
top.scroll.scroll_y = top.scroll.scroll_target_y = -700
render("06_replay_settings_scrolled", (0, 0))
app.page_stack.pop()
app.open_settings_page()
render("07_global_settings", (0, 0))
g = app.page_stack.top()
g.scroll.scroll_y = g.scroll.scroll_target_y = -500
render("08_global_scrolled", (0, 0))
g.configure_hotkey_start("replay.save_hotkey", "Save replay")
g.configure_hotkey_key(["alt"], None)
render("09_hotkey_config", (0, 0))
g._configure_end(g.configuring_prev)
app.page_stack.pop()
app.page_stack.push(pages.ScreenshotSettingsPage(app, app.page_stack))
render("10_screenshot_settings", (0, 0))
app.page_stack.pop()
app.page_stack.push(pages.SettingsPage(pages.SettingsPage.STREAM, app, app.page_stack))
render("11_stream_settings", (0, 0))
app.page_stack.pop()
app.page_stack.push(pages.SettingsPage(pages.SettingsPage.RECORD, app, app.page_stack))
render("12_record_settings", (0, 0))
app.page_stack.pop()
render("13_front_with_gallery_button")
g = gallery.GalleryPage(app, app.page_stack)
app.page_stack.push(g)
import time
for _ in range(40):
    render("14_gallery", (700, 500), frames=1)
    if g.grid.clips and all(c.thumb_state == 2 for c in g.grid.clips[:9]):
        break
    time.sleep(0.25)
render("14_gallery", (700, 500))
if g.grid.clips:
    clip = next((c for c in g.grid.clips if not c.is_image), g.grid.clips[0])
    g.open_clip(clip)
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop(); QTimer.singleShot(2500, loop.quit); loop.exec()
    app.media_player().audio.setVolume(0)
    render("15_player", (0, 0))
    app.page_stack.pop()
