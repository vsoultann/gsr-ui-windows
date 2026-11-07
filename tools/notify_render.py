"""Render gsr-notify port frames offscreen: python tools/notify_render.py OUT.png"""
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import gui  # noqa: E402
from gsr.notify import Notification  # noqa: E402

app = QApplication(sys.argv)
gui.theme.load()
geo = QRect(0, 0, 1440, 1080)
cases = [("Saved a 1 minute replay of this monitor", (255, 255, 255, 255), (8, 109, 183, 255), "replay"),
         ("Started recording this monitor", (255, 255, 255, 255), (8, 109, 183, 255), "record"),
         ("Failed to save replay, make sure the replay save directory is mounted and writable", (255, 0, 0, 255), (255, 0, 0, 255), "replay"),
         ("Press Alt+Z to open the GPU Screen Recorder UI", (255, 255, 255, 255), (8, 109, 183, 255), "notice")]
moments = [0.07, 0.2, 0.33, 1.0]   # seconds into the animation
rows = []
for text, ic, bg, icon in cases:
    for m in moments:
        n = Notification()
        n.show_message(geo, text, 3.0, ic, bg, icon)
        n.timer.stop()
        t0 = time.monotonic() - m
        n.index, n.state_start = 0, t0
        # replay the state machine up to moment m
        elapsed = m
        for i, (s, d) in enumerate(n.states):
            n.index, n.state_start = i, time.monotonic() - min(elapsed, d)
            n._tick() if n.active else None
            if elapsed < d:
                break
            elapsed -= d
        img = QImage(n.w, n.h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor(90, 120, 90))
        n.render(img, QPoint(0, 0))
        rows.append(img)
W = max(i.width() for i in rows) + 20
H = sum(i.height() + 10 for i in rows) + 10
sheet = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
sheet.fill(QColor(90, 120, 90))
p = QPainter(sheet)
y = 10
for i in rows:
    p.drawImage(W - 10 - i.width(), y, i)
    y += i.height() + 10
p.end()
sheet.save(sys.argv[1])
print("saved", sys.argv[1])
