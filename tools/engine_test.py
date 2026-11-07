"""Headless engine test: python tools/engine_test.py OUT_DIR"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gsr import config, engine, system  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "engine_out")
out.mkdir(parents=True, exist_ok=True)
ff = system.find_ffmpeg()
probe = str(Path(ff).with_name("ffprobe.exe"))
caps = set(json.load(open(config.DATA_DIR / "info.json"))["caps"])
outputs = system.list_outputs()
target = engine.Target(next(o for o in outputs if o.primary))
cfg = config.defaults()


def info(path):
    r = subprocess.run([probe, "-v", "error", "-show_entries",
                        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,start_time:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    streams = [{k: s.get(k) for k in ("codec_type", "codec_name", "width", "height", "avg_frame_rate", "start_time")}
               for s in d.get("streams", [])]
    return {"duration": d.get("format", {}).get("duration"), "streams": streams}


def run(name, fn):
    t = time.time()
    try:
        res = fn()
        print(f"[OK]   {name} ({time.time() - t:.1f}s): {res}")
    except Exception as e:
        print(f"[FAIL] {name}: {e!r}")
        for log in config.DATA_DIR.glob("ffmpeg-*.log"):
            print("   ", log.name, log.read_text(errors="replace")[-600:])


def replay():
    ro = cfg["replay"]["record_options"]
    s = engine.Session("replay", ro, target, caps, ff, replay_time=10, container="mp4")
    s.start()
    time.sleep(13)
    p, dur = s.save_replay(out)
    p1, _ = s.save_replay(out, 4)
    s.stop()
    assert not s.error, s.error
    return {"full": info(p), "reported": dur, "last4s": info(p1)["duration"]}


def record_pause_two_tracks():
    ro = config.copy_cfg(cfg["record"]["record_options"])
    ro["audio_tracks_list"] = [{"name": "Desktop", "audio_inputs": ["default_output"]},
                               {"name": "Speakers", "audio_inputs": ["device:Speakers"]}]
    ro["video_quality"] = "very_high"
    s = engine.Session("record", ro, target, caps, ff, container="matroska")
    s.start()
    time.sleep(3)
    s.set_paused(True)
    time.sleep(2)
    s.set_paused(False)
    time.sleep(3)
    s.stop()
    p = s.finish_recording(out)
    assert not s.error, s.error
    return {"elapsed": round(s.elapsed(), 1), "file": p.name, **info(p)}


def stream_local():
    ro = cfg["streaming"]["record_options"]
    s = engine.Session("stream", ro, target, caps, ff, stream_url=str(out / "stream_test.flv"), stream_format="flv",
                       force_h264=True)
    s.start()
    time.sleep(5)
    s.stop()
    assert not s.error, s.error
    return info(out / "stream_test.flv")


def screenshot():
    p = engine.take_screenshot(ff, target, cfg["screenshot"], out / "shot.jpg")
    return {"bytes": p.stat().st_size, **info(p)}


def region_cbr():
    ro = config.copy_cfg(cfg["record"]["record_options"])
    ro["video_quality"] = "custom"
    ro["video_bitrate"] = 6000
    ro["change_video_resolution"] = True
    ro["video_width"], ro["video_height"] = 640, 360
    s = engine.Session("record", ro, engine.Target(target.output, (100, 100, 801, 451)), caps, ff, container="mp4")
    s.start()
    time.sleep(4)
    s.stop()
    return info(s.finish_recording(out))


run("replay buffer + save", replay)
run("record w/ pause, 2 audio tracks, mkv", record_pause_two_tracks)
run("stream (local flv)", stream_local)
run("screenshot", screenshot)
run("region + CBR + scaled", region_cbr)
