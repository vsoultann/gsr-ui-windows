"""Whole-app resource use, offscreen (no windows, hotkeys stubbed): python tools/app_usage.py [CLIP]

Measures the app process (+ its ffmpeg child): idle in the background, with the replay buffer
running, and while the gallery player plays CLIP.
"""
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import config, engine, gallery, hotkeys, overlay  # noqa: E402

hotkeys.Hotkeys.set = lambda self, b: {}
hotkeys.Hotkeys.stop = lambda self: None
overlay.App._tray = lambda self: setattr(self, "tray", type("T", (), {"setToolTip": lambda *a: None, "hide": lambda *a: None})())
overlay.App._update_tray = lambda self: None
overlay.App.notify = lambda self, *a, **k: None

qapp = QApplication(sys.argv[:1])
import ctypes  # noqa: E402
event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)   # like main.py's single-instance event
app = overlay.App(qapp, event, daemon=True)
me = psutil.Process()


def sample(seconds, session=None):
    """Runs the real Qt event loop (like the app does) and samples once a second."""
    from PySide6.QtCore import QEventLoop, QTimer
    cache = {}

    def procs():
        """This process plus the session's current ffmpeg (it is replaced if capture restarts)."""
        out = [me]
        proc = session and session._proc
        if proc and proc.poll() is None:
            if proc.pid not in cache:
                cache[proc.pid] = psutil.Process(proc.pid)
                cache[proc.pid].cpu_percent(None)
            out.append(cache[proc.pid])
        return out
    me.cpu_percent(None)
    cpu, ram = [], []
    loop = QEventLoop()

    def tick():
        try:
            _tick()
        except Exception as e:     # a failing slot must not leave the loop running forever
            print("sample error:", e, flush=True)
            loop.quit()

    def _tick():
        ps = procs()
        c = r = 0
        for p in ps:
            try:
                c += p.cpu_percent(None)
                r += p.memory_full_info().uss
            except psutil.Error:
                pass
        cpu.append(c / psutil.cpu_count())
        ram.append(r / 2 ** 20)
        if len(cpu) >= seconds:
            loop.quit()
    t = QTimer()
    t.timeout.connect(tick)
    t.start(1000)
    QTimer.singleShot(int(seconds * 1000 + 5000), loop.quit)
    loop.exec()
    t.stop()
    return {"cpu_avg_%": round(statistics.mean(cpu[1:]), 2), "cpu_max_%": round(max(cpu[1:]), 2),
            "ram_MB (private)": round(max(ram), 1)}


res = {"idle in background": sample(8)}
ro = config.copy_cfg(app.cfg["replay"]["record_options"])
target = engine.Target(next(o for o in app.outputs if o.primary))
s = engine.Session("replay", ro, target, app.caps, app.ffmpeg, replay_time=app.cfg["replay"]["replay_time"],
                   container="mp4")
s.start()
time.sleep(2)
res[f"replay buffer on ({ro['fps']} fps, {app.cfg['replay']['replay_time']} s)"] = sample(12, s)
restarts = s._run - 1
s.stop()
res["capture restarts during the replay sample"] = restarts
if len(sys.argv) > 1:
    mp = app.media_player()
    mp.audio.setMuted(True)
    mp.open(Path(sys.argv[1]))
    time.sleep(0.5)
    res["playing a clip in the gallery"] = sample(8)
    mp.close()
print(json.dumps(res, indent=1))
