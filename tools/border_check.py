"""Does Windows draw the yellow capture border during gfxcapture? Samples screen edge pixels mid-capture."""
import subprocess
import sys
import time
from pathlib import Path

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gsr import system  # noqa: E402

ff = system.find_ffmpeg()


def yellow_ratio(img):
    w, h = img.size
    pts = [(x, 1) for x in range(0, w, 7)] + [(x, h - 2) for x in range(0, w, 7)] + \
          [(1, y) for y in range(0, h, 7)] + [(w - 2, y) for y in range(0, h, 7)]
    yellow = sum(1 for p in pts if (lambda c: c[0] > 200 and c[1] > 170 and c[2] < 90)(img.getpixel(p)))
    return yellow / len(pts)


before = yellow_ratio(ImageGrab.grab())
p = subprocess.Popen([ff, "-hide_banner", "-loglevel", "error", "-init_hw_device", "d3d11va=dd:0", "-filter_hw_device", "dd",
                      "-filter_complex", "gfxcapture=monitor_idx=0:max_framerate=60", "-t", "4", "-f", "null", "-"],
                     creationflags=0x08000000)
time.sleep(2)
during = yellow_ratio(ImageGrab.grab())
p.wait()
print(f"yellow edge pixels before: {before:.0%}  during capture: {during:.0%}")
