"""Headless checks of the gallery player (muted, offscreen): python tools/player_test.py CLIP [CLIP ...]

Per clip: time to first frame, delivered frame rate vs the clip's, frame conversion cost, playback
speed, seek accuracy, pause/resume, end of clip + replay, then rapid clip switching and whether
the file can be deleted right after closing.
"""
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import gallery, system  # noqa: E402

app = QApplication(sys.argv[:1])
clips = [Path(p) for p in sys.argv[1:]]
fails = []


def pump(seconds, until=None):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.001)
    return bool(until and until())


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def _try_unlink(path):
    try:
        path.unlink()
        return True
    except OSError:
        return False


mp = gallery.MediaPlayer()
mp.audio.setMuted(True)
frame_times = []
mp.sink.videoFrameChanged.connect(lambda f: frame_times.append(time.perf_counter()))
ui_late = []


def ui_tick():
    """A 16 ms timer like the overlay's repaint timer: how late does it fire while a clip plays?"""
    now = time.perf_counter()
    if ui_tick.last:
        ui_late.append((now - ui_tick.last) * 1000 - 16)
    ui_tick.last = now


ui_tick.last = None
timer = QTimer()
timer.timeout.connect(ui_tick)
timer.start(16)
probe = str(Path(system.find_ffmpeg()).with_name("ffprobe.exe"))


def clip_fps(path):
    import subprocess
    r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate,codec_name",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    n, d = r[1].split("/")
    return r[0], float(n) / float(d)


for path in clips:
    codec, fps = clip_fps(path)
    print(f"\n{path.name}  ({codec}, {fps:.0f} fps)")
    p = mp.player
    frame_times.clear()
    t0 = time.perf_counter()
    mp.open(path)
    got = pump(5, lambda: mp.frame is not None)
    check("first frame", got, f"{(time.perf_counter() - t0) * 1000:.0f} ms")
    if not got:
        print("  error:", p.errorString())
        continue
    pump(2, lambda: p.duration() > 0)
    dur = p.duration()
    check("duration known", dur > 0, f"{dur / 1000:.1f}s")
    # steady playback for 3 s
    pump(0.5)
    frame_times.clear()
    ui_late.clear()
    c0 = mp.converted
    pos0, w0 = p.position(), time.perf_counter()
    pump(3)
    wall = time.perf_counter() - w0
    delivered = len(frame_times) / wall
    speed = (p.position() - pos0) / 1000 / wall
    iv = [(b - a) * 1000 for a, b in zip(frame_times, frame_times[1:])]
    check("delivers the clip's frame rate", delivered >= fps * 0.95, f"{delivered:.1f}/{fps:.0f} fps")
    check("real-time speed", 0.95 <= speed <= 1.05, f"{speed:.3f}x")
    check("no long frame stalls", not iv or max(iv) < 3 * 1000 / fps + 25,
          f"max interval {max(iv):.1f} ms" if iv else "")
    shown = (mp.converted - c0) / wall
    check("frames shown at display rate", shown >= min(fps, mp.DISPLAY_FPS) * 0.9, f"{shown:.1f} images/s")
    late = sorted(ui_late)
    p95 = late[int(len(late) * .95)] if late else 0
    check("UI thread stays responsive", bool(late) and p95 < 12,
          f"16 ms timer late by p95 {p95:.1f} ms, max {late[-1]:.1f} ms" if late else "")
    # pause
    mp.toggle()
    pump(0.3)
    a = p.position()
    pump(1)
    check("pause holds position", not mp.playing() and abs(p.position() - a) < 5, f"{a} -> {p.position()}")
    mp.toggle()
    pump(0.5)
    check("resume plays", mp.playing() and p.position() > a)
    # seek to the middle (like a seek-bar click) and to near the end
    for target in (dur // 2, dur // 5):
        frame_times.clear()
        t = time.perf_counter()
        p.setPosition(int(target))
        ok = pump(2, lambda: len(frame_times) > 0 and abs(p.position() - target) < 1500)
        check(f"seek to {target / 1000:.1f}s", ok, f"pos {p.position() / 1000:.2f}s after {(time.perf_counter() - t) * 1000:.0f} ms")
    # end of clip, then play again from the start
    p.setPosition(max(0, dur - 600))
    ended = pump(4, lambda: p.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia)
    check("reaches end", ended, str(p.mediaStatus()))
    mp.toggle()
    pump(0.6)
    check("play after end restarts", mp.playing() and p.position() < 2000, f"pos {p.position()}")
    # drag-style seeking: many positions quickly
    for i in range(30):
        p.setPosition(int(dur * i / 30))
        pump(0.01)
    pump(0.5)
    check("survives drag seeking", p.error() == QMediaPlayer.Error.NoError and mp.frame is not None, p.errorString())

if len(clips) >= 2:
    print("\nrapid switching")
    for i in range(12):
        mp.open(clips[i % len(clips)])
        pump(0.05)
    last = clips[11 % len(clips)]
    ok = pump(5, lambda: mp.playing() and p.position() > 200)
    check("ends on the last chosen clip, playing", ok and Path(p.source().toLocalFile()) == last,
          p.source().toLocalFile())

    print("\ndelete right after closing (like the Delete button)")
    tmp = Path(tempfile.mkdtemp()) / ("copy" + clips[0].suffix)
    shutil.copy(clips[0], tmp)
    mp.open(tmp)
    pump(5, lambda: mp.frame is not None)
    mp.close()
    t = time.perf_counter()
    ok = pump(2, lambda: _try_unlink(tmp))
    check("file can be deleted after closing (within the app's 2 s retry)", ok,
          f"after {(time.perf_counter() - t) * 1000:.0f} ms")

mp.close()
print(f"\n{len(fails)} failure(s)" if fails else "\nall passed")
