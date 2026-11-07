"""Performance + output checks for the capture engine (no windows are shown).

    python tools/perf_test.py OUT_DIR [phase ...]

phases: perf (CPU/RAM/GPU and game-fps cost per setting, with and without a GPU load),
        quality (bitrate/quality/codec settings on moving synthetic content through the real pipeline),
        stream (end-to-end RTMP to a local ffmpeg server)
"""
import json
import math
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gsr import config, engine, system  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "perf_out")
out.mkdir(parents=True, exist_ok=True)
phases = sys.argv[2:] or ["perf", "quality", "stream"]
ff = system.find_ffmpeg()
probe = str(Path(ff).with_name("ffprobe.exe"))
caps = set(json.load(open(config.DATA_DIR / "info.json"))["caps"])
target = engine.Target(next(o for o in system.list_outputs() if o.primary))
results = {}


# ------------------------------------------------------------------ measuring

class GpuSampler:
    """Samples the Windows 'GPU Engine' counters (what Task Manager shows) once a second."""

    def __init__(self):
        cmd = ("$ErrorActionPreference='SilentlyContinue'; while ($true) { "
               "$s = (Get-Counter '\\GPU Engine(*)\\Utilization Percentage').CounterSamples | "
               "? { $_.CookedValue -gt 0 } | % { $_.InstanceName + '=' + [math]::Round($_.CookedValue, 2) }; "
               "'@' + ($s -join ';'); [Console]::Out.Flush() }")
        self.proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd], stdout=subprocess.PIPE,
                                     text=True, creationflags=0x08000000)
        self.samples = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            if line.startswith("@"):
                d = {}
                for kv in line[1:].strip().split(";"):
                    if "=" in kv:
                        k, v = kv.rsplit("=", 1)
                        d[k] = float(v)
                self.samples.append((time.time(), d))

    def window(self, t0, t1, pids):
        """Average utilisation per engine type: for the given pids, and for the whole GPU (max per engine)."""
        rows = [d for t, d in self.samples if t0 <= t <= t1]
        if not rows:
            return {}
        mine, engines = {}, {}
        for d in rows:
            per_engine = {}
            for inst, v in d.items():
                try:
                    pid = int(inst.split("pid_")[1].split("_")[0])
                    etype = inst.split("engtype_")[1]
                    eng = inst.split("luid_")[1].split("_phys")[0] + "/" + inst.split("_eng_")[1].split("_")[0]
                except (ValueError, IndexError):
                    continue
                per_engine[(eng, etype)] = per_engine.get((eng, etype), 0) + v
                if pid in pids:
                    mine[etype] = mine.get(etype, 0) + v / len(rows)
            for k, v in per_engine.items():
                engines[k] = engines.get(k, 0) + min(v, 100) / len(rows)
        busiest = {}
        for (eng, etype), v in engines.items():
            busiest[etype] = max(busiest.get(etype, 0), v)
        return {"ours": {k: round(v, 1) for k, v in mine.items() if v >= 0.05},
                "gpu_busiest_engine": {k: round(v, 1) for k, v in busiest.items() if v >= 0.5}}

    def close(self):
        self.proc.kill()


class ProcSampler:
    """CPU% (of the whole machine, like Task Manager) and memory of a set of processes."""

    def __init__(self, procs):
        self.procs = procs
        self.cpu, self.rss, self.private = [], [], []
        self._stop = False
        for p in procs:
            p.cpu_percent(None)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        n = psutil.cpu_count()
        while not self._stop:
            time.sleep(1)
            c = r = pv = 0
            for p in self.procs:
                try:
                    c += p.cpu_percent(None) / n
                    m = p.memory_full_info()
                    r += m.rss
                    pv += m.private
                except psutil.Error:
                    pass
            self.cpu.append(c)
            self.rss.append(r)
            self.private.append(pv)

    def stop(self):
        self._stop = True
        return {"cpu_avg_%": round(statistics.mean(self.cpu[1:] or [0]), 2),
                "cpu_max_%": round(max(self.cpu[1:] or [0]), 2),
                "ram_MB": round(max(self.rss or [0]) / 2 ** 20, 1),
                "private_MB": round(max(self.private or [0]) / 2 ** 20, 1)}


class Game:
    """tools/gpu_load.py in a child process; collects its frame rate."""

    def __init__(self, seconds, cap=0):
        args = [sys.executable, str(ROOT / "tools" / "gpu_load.py"), str(seconds)] + (["--cap", str(cap)] if cap else [])
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, text=True, creationflags=0x08000000)
        self.fps = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            d = json.loads(line)
            if "fps" in d:
                self.fps.append((time.time(), d["fps"]))

    def avg(self, t0, t1):
        v = [f for t, f in self.fps if t0 <= t <= t1]
        return round(statistics.mean(v), 1) if v else None

    def wait(self):
        self.proc.wait()


def probe_video(path):
    r = subprocess.run([probe, "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate:format=duration,size",
                        "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
    a = [s["codec_name"] for s in d.get("streams", []) if s["codec_type"] == "audio"]
    dur = float(d.get("format", {}).get("duration", 0) or 0)
    pts = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.split()
    t = sorted(float(x) for x in pts if x and x != "N/A")
    gaps = [(b - a) * 1000 for a, b in zip(t, t[1:])]
    sizes = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.split()
    vbits = sum(int(s) for s in sizes if s.isdigit()) * 8
    span = (t[-1] - t[0]) if len(t) > 1 else 0
    return {"codec": v.get("codec_name"), "size": f"{v.get('width')}x{v.get('height')}",
            "fps": round((len(t) - 1) / span, 2) if span else 0, "frames": len(t), "duration": round(dur, 2),
            "max_gap_ms": round(max(gaps), 1) if gaps else None,
            "gaps_over_1.5x": sum(1 for g in gaps if gaps and g > 1.5 * statistics.median(gaps)),
            "video_kbps": round(vbits / span / 1000) if span else 0, "audio": a}


def ro_for(**kw):
    ro = config.copy_cfg(config.defaults()["record"]["record_options"])
    ro.update(kw)
    return ro


def record(ro, seconds, name, sampler=None, game=None):
    s = engine.Session("record", ro, target, caps, ff, container="mp4")
    s.start()
    time.sleep(1.5)     # let ffmpeg start
    ff_proc = psutil.Process(s._proc.pid)
    ff_s, py_s = ProcSampler([ff_proc]), ProcSampler([psutil.Process()])
    t0 = time.time()
    time.sleep(seconds)
    t1 = time.time()
    usage = {"ffmpeg": ff_s.stop(), "app_side(audio capture etc)": py_s.stop()}
    gpu = sampler.window(t0, t1, {ff_proc.pid, psutil.Process().pid}) if sampler else {}
    gfps = game.avg(t0, t1) if game else None
    s.stop()
    path = s.finish_recording(out)
    final = out / f"{name}.mp4"
    final.unlink(missing_ok=True)
    path.rename(final)
    res = {"usage": usage, "gpu": gpu, "file": probe_video(final), "error": s.error, "notice": s.notice}
    if gfps is not None:
        res["game_fps_while_recording"] = gfps
    return res


def show(name, r):
    results[name] = r
    print(f"\n== {name}\n{json.dumps(r, indent=1, default=str)}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))


# ------------------------------------------------------------------ phases

def phase_perf():
    gpu = GpuSampler()
    time.sleep(3)
    t0 = time.time()
    time.sleep(6)
    show("idle_gpu", gpu.window(t0, time.time(), set()))
    cases = [("h264 60fps", ro_for(fps=60)), ("h264 120fps", ro_for(fps=120)),
             ("h264 60fps very_high", ro_for(fps=60, video_quality="very_high")),
             ("av1 60fps", ro_for(fps=60, video_codec="av1")),
             ("h264 60fps cbr 20000", ro_for(fps=60, video_quality="custom", video_bitrate=20000)),
             ("h264 60fps vfr", ro_for(fps=60, framerate_mode="vfr")),
             ("h264 60fps 1280x720", ro_for(fps=60, change_video_resolution=True, video_width=1280, video_height=720)),
             ("h264_software 60fps", ro_for(fps=60, video_codec="h264_software"))]
    for name, ro in cases:
        show(f"desktop | {name}", record(ro, 12, "perf_" + name.replace(" ", "_"), gpu))
    # game load: baseline fps alone, then while recording
    for name, ro in [("h264 60fps", ro_for(fps=60)), ("h264 120fps", ro_for(fps=120)),
                     ("av1 60fps", ro_for(fps=60, video_codec="av1"))]:
        g = Game(30)
        time.sleep(3)
        t0 = time.time()
        time.sleep(8)
        base = g.avg(t0, time.time())
        r = record(ro, 12, "game_" + name.replace(" ", "_"), gpu, g)
        r["game_fps_alone"] = base
        if base and r.get("game_fps_while_recording"):
            r["game_fps_cost_%"] = round((1 - r["game_fps_while_recording"] / base) * 100, 1)
        g.proc.kill()
        show(f"game load | {name}", r)
    gpu.close()


def synthetic(monkey_fps_source):
    """Replace the screen source with moving test content, keeping the rest of the real pipeline."""
    orig = engine.capture_filter

    def fake(tgt, fps, cursor, vfr, backend="dda"):
        w, h = engine._even(tgt.size[0]), engine._even(tgt.size[1])
        src = f"testsrc2=size={w}x{h}:rate={fps},format=bgra,hwupload"
        return src if vfr else src + f",fps={fps}"
    engine.capture_filter = fake
    return orig


def phase_quality():
    orig = synthetic(True)
    try:
        for q in ("medium", "high", "very_high", "ultra"):
            show(f"quality | {q}", record(ro_for(fps=60, video_quality=q), 8, f"q_{q}"))
        for kbps in (3000, 8000, 20000, 50000):
            show(f"bitrate | {kbps}k", record(ro_for(fps=60, video_quality="custom", video_bitrate=kbps), 10, f"b_{kbps}"))
        for fps in (30, 60, 120, 144):
            show(f"fps | {fps}", record(ro_for(fps=fps), 8, f"f_{fps}"))
        for codec in ("h264", "hevc", "av1", "h264_software", "av1_software"):
            show(f"codec | {codec}", record(ro_for(fps=60, video_codec=codec), 8, f"c_{codec}"))
    finally:
        engine.capture_filter = orig


def phase_stream():
    gpu = GpuSampler()
    for name, extra in [("rtmp 60fps 8000k (default)", {}), ("rtmp 120fps 6000k", {"fps": 120, "video_bitrate": 6000})]:
        recv = out / f"stream_{name.split()[1]}.flv"
        recv.unlink(missing_ok=True)
        server = subprocess.Popen([ff, "-hide_banner", "-loglevel", "error", "-y", "-listen", "1", "-timeout", "20",
                                   "-i", "rtmp://127.0.0.1:19350/live/testkey", "-c", "copy", str(recv)],
                                  stderr=subprocess.PIPE, creationflags=0x08000000)
        time.sleep(1.5)
        ro = config.copy_cfg(config.defaults()["streaming"]["record_options"])
        ro.update(extra)
        s = engine.Session("stream", ro, target, caps, ff, stream_url="rtmp://127.0.0.1:19350/live/testkey",
                           stream_format="flv", force_h264=True)
        s.start()
        time.sleep(1.5)
        procs = ProcSampler([psutil.Process(s._proc.pid), psutil.Process()])
        t0 = time.time()
        time.sleep(15)
        usage = procs.stop()
        g = gpu.window(t0, time.time(), {s._proc.pid})
        s.stop()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        show(f"stream | {name}", {"usage": usage, "gpu": g, "received": probe_video(recv), "error": s.error,
                                  "notice": s.notice, "server_stderr": server.stderr.read().decode(errors="replace")[-300:]})
    gpu.close()


if __name__ == "__main__":
    for ph in phases:
        {"perf": phase_perf, "quality": phase_quality, "stream": phase_stream}[ph]()
    print("\nwrote", out / "results.json")
