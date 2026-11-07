"""Reproduce capture stalls: record while the Python process is busy (like the UI painting).

    python tools/stall_test.py OUT_DIR FPS [busy]
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gsr import config, engine, system  # noqa: E402

out = Path(sys.argv[1])
fps = int(sys.argv[2])
busy = len(sys.argv) > 3 and sys.argv[3] == "busy"
out.mkdir(parents=True, exist_ok=True)
ff = system.find_ffmpeg()
probe = str(Path(ff).with_name("ffprobe.exe"))
caps = set(json.load(open(config.DATA_DIR / "info.json"))["caps"])
target = engine.Target(next(o for o in system.list_outputs() if o.primary))
ro = config.copy_cfg(config.defaults()["record"]["record_options"])
ro["fps"] = fps
ro["audio_tracks_list"] = [] if "noaudio" in sys.argv else [{"name": "", "audio_inputs": ["default_output"]}]
s = engine.Session("record", ro, target, caps, ff, container="mp4")
s.start()
stop = time.time() + 10


def burn():
    x = 0
    while time.time() < stop:
        for i in range(20000):
            x += i * i


if busy:
    threads = [threading.Thread(target=burn) for _ in range(3)]
    for t in threads:
        t.start()
time.sleep(10)
s.stop()
p = s.finish_recording(out)
t = [float(x) for x in subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                                       "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout.split() if x != "N/A"]
d = [b - a for a, b in zip(t, t[1:])]
span = t[-1] - t[0]
print(f"fps={fps} busy={busy}: {len(t)} frames over {span:.2f}s = {len(t) / span:.1f} fps, "
      f"max gap {max(d) * 1000:.0f} ms, gaps>2x: {sum(1 for x in d if x > 2 / fps)}")
