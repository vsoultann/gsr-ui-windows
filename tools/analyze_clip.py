"""Frame-timing analysis of a clip: python tools/analyze_clip.py FILE

Reports container fps, real frame intervals, and how many frames are visually identical
to the previous one (duplicated captures = stutter even when the fps number looks right).
"""
import json
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gsr import system  # noqa: E402

path = sys.argv[1]
ff = system.find_ffmpeg()
probe = str(Path(ff).with_name("ffprobe.exe"))
meta = json.loads(subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames:format=duration",
                                  "-of", "json", path], capture_output=True, text=True).stdout)
print("stream:", meta["streams"][0], "duration:", meta["format"]["duration"])

pts = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                      "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
t = [float(x) for x in pts if x and x != "N/A"]
d = [round((b - a) * 1000, 2) for a, b in zip(t, t[1:])]
print(f"frames: {len(t)}  interval ms: median {statistics.median(d):.2f}  max {max(d):.2f}  "
      f">1.5x median: {sum(1 for x in d if x > statistics.median(d) * 1.5)}")

# mpdecimate drops frames that are (near) identical to the previous one
res = subprocess.run([ff, "-hide_banner", "-i", path, "-an", "-vf", "mpdecimate=hi=64*4:lo=64:frac=0.1,showinfo",
                      "-f", "null", "-"], capture_output=True, text=True)
unique = res.stderr.count("pts_time:")
print(f"visually unique frames: {unique}/{len(t)}  ({unique / max(1, len(t)) * 100:.0f}%) "
      f"-> effective fps ~{unique / max(0.001, t[-1] - t[0]):.1f}")
