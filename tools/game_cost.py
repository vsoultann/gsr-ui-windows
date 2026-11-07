"""How many frames does a GPU-bound 'game' (tools/gpu_load.py, offscreen) lose while recording?
Alternates recording off/on in rounds so drift in background load cancels out.

    python tools/game_cost.py [ROUNDS] [FPS ...]
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perf_test as pt  # noqa: E402

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
fps_list = [int(x) for x in sys.argv[2:]] or [60]
SLICE = 8
for fps in fps_list:
    game = pt.Game(rounds * SLICE * 2 + 6)
    time.sleep(3)
    off, on = [], []
    for _ in range(rounds):
        t = time.time()
        time.sleep(SLICE)
        off.append(game.avg(t + 1, time.time()))
        ro = pt.ro_for(fps=fps)
        s = pt.engine.Session("replay", ro, pt.target, pt.caps, pt.ff, replay_time=30, container="mp4")
        s.start()
        time.sleep(1.5)
        t = time.time()
        time.sleep(SLICE - 1.5)
        on.append(game.avg(t + 0.5, time.time()))
        s.stop()
    game.proc.kill()
    a, b = statistics.mean(off), statistics.mean(on)
    print(f"replay {fps} fps: game {a:.0f} fps without -> {b:.0f} fps with  ({(1 - b / a) * 100:+.1f}% cost)"
          f"   rounds off {off} on {on}")
