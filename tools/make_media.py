"""Render README screenshots and GIFs offscreen with a synthetic background and demo clips.

    python tools/make_media.py docs/media
"""
import math
import os
import random
from datetime import datetime
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEventLoop, QPointF, QRect, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap, QPolygonF, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import config, gallery, gui, hotkeys, overlay, pages, system  # noqa: E402
gallery.nvidia_capture_dir = lambda: Path(tempfile.mkdtemp())   # never show the real NVIDIA clips
from gsr.gui import Event, Win, color_theme, theme  # noqa: E402
from gsr.notify import Notification  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/media")
OUT.mkdir(parents=True, exist_ok=True)
W, H = 1920, 1080
SHOT_W, GIF_W = 1280, 960

# ---------------------------------------------------------------- isolated app
tmp = Path(tempfile.mkdtemp(prefix="gsr-media-"))
config.CONFIG_PATH = tmp / "config.json"       # never touch the real config
hotkeys.Hotkeys.set = lambda self, b: {}
hotkeys.Hotkeys.stop = lambda self: None
overlay.App._tray = lambda self: setattr(self, "tray", type("T", (), {"setToolTip": lambda *a: None, "hide": lambda *a: None})())
overlay.App._update_tray = lambda self: None
overlay.App.notify = lambda self, *a, **k: None
overlay.App._wait_show = lambda self, e: None
overlay.App._refresh_cache = lambda self: None
system.autostart_enabled = lambda: False

qapp = QApplication(sys.argv)
app = overlay.App(qapp, None, daemon=True)
app.tick.stop()
app.cfg["main"]["tint_color"] = "intel"
color_theme.tint_color = gui.parse_tint("intel")

# demo clips for the gallery
clips_dir = tmp / "Videos"
(clips_dir / "Minecraft").mkdir(parents=True)
ff = system.find_ffmpeg()
demo = [("Replay_2026-11-07_21-14-02.mp4", "mandelbrot=s=640x360", 62), ("Replay_2026-11-07_20-51-37.mp4", "testsrc2=s=640x360", 30),
        ("Minecraft/Replay_2026-11-07_19-03-11.mp4", "life=s=640x360:mold=10:r=30:ratio=0.1:death_color=#1d3557:life_color=#e63946", 60),
        ("Video_2026-11-06_23-40-55.mp4", "cellauto=s=640x360:rule=110", 754), ("Video_2026-11-06_22-10-09.mp4", "gradients=s=640x360:speed=0.05", 95),
        ("Screenshot_2026-11-06_21-58-31.jpg", "mandelbrot=s=1280x720:start_scale=0.6", 0)]
for name, src, dur in demo:
    path = clips_dir / name
    if name.endswith(".jpg"):
        args = ["-f", "lavfi", "-i", src, "-frames:v", "1"]
    else:
        args = ["-f", "lavfi", "-i", f"{src}:r=30" if "life" not in src else src, "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p"]
        # fake the duration shown in the gallery: write the real file, then patch the cached duration below
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args, str(path)], creationflags=0x08000000)
    stamp = datetime.strptime(path.stem.split("_", 1)[1], "%Y-%m-%d_%H-%M-%S").timestamp()
    os.utime(path, (stamp, stamp))     # gallery dates match the file names
for k in ("replay", "record", "screenshot"):
    app.cfg[k]["save_directory"] = str(clips_dir)
demo_durations = {(clips_dir / n).resolve(): d for n, _, d in demo if d}


# ---------------------------------------------------------------- background
def make_background():
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    sky = QLinearGradient(0, 0, 0, H)
    sky.setColorAt(0, QColor(28, 36, 74))
    sky.setColorAt(0.55, QColor(201, 105, 92))
    sky.setColorAt(1, QColor(250, 190, 120))
    p.fillRect(0, 0, W, H, sky)
    sun = QRadialGradient(QPointF(W * 0.68, H * 0.58), H * 0.35)
    sun.setColorAt(0, QColor(255, 236, 190, 255))
    sun.setColorAt(0.25, QColor(255, 200, 140, 160))
    sun.setColorAt(1, QColor(255, 160, 120, 0))
    p.fillRect(0, 0, W, H, sun)
    rnd = random.Random(7)
    for layer, (col, base, amp) in enumerate(((QColor(70, 52, 92), 0.62, 0.13), (QColor(46, 34, 66), 0.72, 0.10),
                                             (QColor(24, 20, 40), 0.84, 0.07))):
        pts = [QPointF(0, H)]
        x, y = 0.0, H * base
        while x <= W + 40:
            pts.append(QPointF(x, y))
            x += rnd.uniform(40, 120)
            y = H * base - rnd.uniform(0, H * amp)
        pts.append(QPointF(W, H))
        p.setPen(QColor(0, 0, 0, 0))
        p.setBrush(col)
        p.drawPolygon(QPolygonF(pts))
    p.end()
    return QPixmap.fromImage(img)


app.overlay.bg = make_background()
theme.set_window_size(W, H)
app.visible = True
app.recreate_frontpage()
win = Win()
win.frame_delta = 1 / 30


def cursor(p, x, y):
    s = H / 1080 * 1.4
    pts = [(0, 0), (0, 17), (4.5, 13), (8, 20.5), (10.5, 19.5), (7, 12.5), (12.5, 12.5)]
    poly = QPolygonF([QPointF(x + a * s, y + b * s) for a, b in pts])
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(0, 0, 0))
    p.setBrush(QColor(255, 255, 255))
    p.drawPolygon(poly)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)


def frame(mouse=None, delta=1 / 30):
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    win.p = p
    win.mouse = mouse or (-100.0, -100.0)
    win.frame_delta = delta
    app.draw(win)
    if mouse:
        cursor(p, *mouse)
    p.end()
    return img


def to_pil(img, width):
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    pil = Image.frombuffer("RGB", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGB", img.bytesPerLine())
    return pil.resize((width, int(width * img.height() / img.width())), Image.LANCZOS)


def save_shot(name, img):
    to_pil(img, SHOT_W).save(OUT / f"{name}.png", optimize=True)
    print("shot", name)


def save_gif(name, frames, ms):
    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=255) for f in frames]
    pal[0].save(OUT / f"{name}.gif", save_all=True, append_images=pal[1:], duration=ms, loop=0, optimize=True, disposal=1)
    print("gif", name, len(frames), "frames", (OUT / f"{name}.gif").stat().st_size // 1024, "KB")


def move(x, y):
    win.mouse = (x, y)
    app.on_event(Event(Event.MOVE, x, y), win)


def click(x, y):
    move(x, y)
    app.on_event(Event(Event.PRESS, x, y, 1), win)
    app.on_event(Event(Event.RELEASE, x, y, 1), win)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def lerp_path(a, b, n):
    return [(a[0] + (b[0] - a[0]) * (1 - math.cos(math.pi * i / (n - 1))) / 2,
             a[1] + (b[1] - a[1]) * (1 - math.cos(math.pi * i / (n - 1))) / 2) for i in range(n)]


# ---------------------------------------------------------------- front page + dropdown GIF
frame(None)          # widgets get their positions when drawn
rb = app.replay_btn
center = (rb.position[0] + rb.size[0] / 2, rb.position[1] + rb.size[1] / 2)
save_shot("01-overlay", frame((W * 0.62, H * 0.66)))
frames = [to_pil(frame(None), GIF_W)] * 8
for m in lerp_path((W * 0.62, H * 0.66), center, 14):
    move(*m)
    frames.append(to_pil(frame(m), GIF_W))
click(*center)
item_h = rb.items[0]["text"].bounds()[1] + 2 * int(0.00694 * H)
first = rb.position[1] + rb.size[1] + item_h * 0.5
for i in range(5):
    for m in lerp_path((center[0], first + item_h * max(0, i - 1)), (center[0], first + item_h * i), 5 if i else 8):
        move(*m)
        frames.append(to_pil(frame(m), GIF_W))
    frames += [frames[-1]] * 3
save_shot("02-dropdown", frame((center[0], first)))
save_gif("overlay", frames, 60)
click(W * 0.5, H * 0.9)   # close the dropdown

# ---------------------------------------------------------------- notification GIF
import gsr.notify as notify_mod  # noqa: E402


class FakeClock:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now


notify_mod.time = FakeClock          # drive the real state machine frame by frame
notif = Notification()
bg_frame = app.overlay.bg.toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
frames = []
notif.show_message(QRect(0, 0, W, H), "Saved a 1 minute replay of this monitor", 1.5, (255, 255, 255, 255),
                   color_theme.tint_color, "replay")
notif.timer.stop()
while notif.active:
    FakeClock.now += 1 / 30
    notif._tick()
    img = QImage(bg_frame)
    p = QPainter(img)
    nimg = QImage(notif.w, notif.h, QImage.Format.Format_ARGB32_Premultiplied)
    nimg.fill(0)
    notif.render(nimg)
    p.drawImage(W - notif.w, H // 13, nimg)
    p.end()
    gh = GIF_W * H // W
    frames.append(to_pil(img, GIF_W).crop((int(GIF_W * 0.5), int(gh * 0.055), GIF_W, int(gh * 0.2))))
frames += [frames[-1]] * 10
save_gif("notification", frames, 33)

# ---------------------------------------------------------------- settings pages
app.page_stack.push(pages.SettingsPage(pages.SettingsPage.REPLAY, app, app.page_stack))
top = app.page_stack.top()
top.view_radio.set_selected_item("advanced", True, True)
save_shot("03-replay-settings", frame(None))
top.scroll.scroll_y = top.scroll.scroll_target_y = -900
save_shot("04-replay-settings-2", frame(None))
app.page_stack.pop()
app.open_settings_page()
save_shot("05-global-settings", frame(None))

# accent color GIF on the global settings page
g = app.page_stack.top()
frames = []
for tint in ("intel", "amd", "nvidia", "purple", "pink", "orange", "yellow", "cyan", "teal", "white"):
    g.swatches.on_select(tint)
    for _ in range(6):
        frames.append(to_pil(frame(None, 1 / 12), GIF_W))
save_gif("accent-colors", frames, 90)
g.swatches.on_select("intel")
app.page_stack.pop()

# ---------------------------------------------------------------- gallery + player GIF
gal = gallery.GalleryPage(app, app.page_stack)
app.page_stack.push(gal)
for _ in range(60):
    frame(None)
    if all(c.thumb_state == 2 for c in gal.grid.clips):
        break
    wait(200)
for c in gal.grid.clips:
    c.duration = demo_durations.get(c.path.resolve(), c.duration)
save_shot("06-gallery", frame((W * 0.45, H * 0.4)))
frames = []
target = None
for m in lerp_path((W * 0.75, H * 0.75), (W * 0.42, H * 0.33), 16):
    move(*m)
    frames.append(to_pil(frame(m), GIF_W))
target = gal.grid.clips[gal.grid.mouse_over] if gal.grid.mouse_over >= 0 else gal.grid.clips[0]
frames += [frames[-1]] * 6
gal.open_clip(target)
app.media_player().audio.setVolume(0)
for i in range(45):
    wait(66)
    frames.append(to_pil(frame((W * 0.2, H * 0.9)), GIF_W))
save_shot("07-player", frame(None))
save_gif("gallery", frames, 66)
app.page_stack.pop()
app.media_player().close()

shutil.rmtree(tmp, ignore_errors=True)
print("done")
