"""Fast logic tests that need no screen or capture: python tools/selftest.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import config, engine, gallery, gui, hotkeys, pages, system  # noqa: E402

app = QApplication(sys.argv)
gui.theme.load()
gui.theme.set_window_size(1920, 1080)
failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# hotkeys
check("parse alt+f10", hotkeys.parse("alt+f10") == (0x1, 0x79))
check("parse ctrl+shift+a", hotkeys.parse("ctrl+shift+a") == (0x6, ord("A")))
check("parse printscreen", hotkeys.parse("printscreen") == (0, 0x2C))
check("parse rejects garbage", hotkeys.parse("alt+notakey") is None)
check("display order", pages.hotkey_display("shift+alt+f10") == "Shift+Alt+F10", pages.hotkey_display("shift+alt+f10"))
check("display printscreen", pages.hotkey_display("ctrl+printscreen") == "Ctrl+Printscreen")
check("default hotkeys parse", all(hotkeys.parse(v) for v in config.default_hotkeys().values()))

# config
d = config.defaults()
merged = config._merge(config.defaults(), {"replay": {"replay_time": 30}, "main": {"tint_color": "#112233"}})
check("config merge keeps other keys", merged["replay"]["replay_time"] == 30 and "container" in merged["replay"])
check("defaults use upstream hotkeys", d["hotkeys"]["main.show_hide_hotkey"] == "alt+z")

# colors
check("tint by id", gui.parse_tint("teal") == gui.TINTS["teal"])
check("tint by hex", gui.parse_tint("#FF8800") == (255, 136, 0, 255))
check("tint fallback", gui.parse_tint("#zzz") == gui.TINTS["amd"])
check("contrast on yellow", gui.text_on(gui.TINTS["yellow"])[0] == 0)
check("contrast on blue", gui.text_on(gui.TINTS["intel"])[0] == 255)

# file names
tmp = Path(tempfile.mkdtemp())
names = []
for _ in range(3):
    p = engine.unique_path(tmp / "Replay_2026-01-01_10-00-00.mp4")
    p.write_text("x")
    names.append(p.name)
check("unique_path", names == ["Replay_2026-01-01_10-00-00.mp4", "Replay_2026-01-01_10-00-00_2.mp4",
                               "Replay_2026-01-01_10-00-00_3.mp4"], names)

# capture math
out = system.Output(0, 1, r"\\.\DISPLAY1", 2560, 1440, True, system.INTEL, "test")
check("scaled_size fit", engine.scaled_size((2560, 1440), (1920, 1080)) == (1920, 1080))
check("scaled_size keeps small", engine.scaled_size((1280, 720), (1920, 1080)) is None)
check("scaled_size even", all(v % 2 == 0 for v in engine.scaled_size((1441, 1081), (1000, 1000))))
f = engine.capture_filter(engine.Target(out, (10, 20, 801, 451)), 120, True, False)
check("crop filter even size", "video_size=800x450" in f and "offset_x=10" in f, f)
check("cfr adds fps filter", f.endswith(",fps=120"), f)
check("polls faster than target", "framerate=150" in f, f)
ro = config.record_options()
ro["change_video_resolution"], ro["video_width"], ro["video_height"] = True, 1280, 720
chain = engine.video_chain(engine.Target(out), ro, "qsv")
check("qsv zero-copy scale", "hwmap=derive_device=qsv" in chain and "scale_qsv=w=1280:h=720" in chain, chain)
chain = engine.video_chain(engine.Target(out), ro, "nvenc")
check("nvenc download path", "hwdownload" in chain and chain.endswith("format=bgr0"), chain)
ro["webcam_source"] = "Cam"
ro["webcam_camera_width"], ro["webcam_camera_height"] = 1280, 720
chain = engine.video_chain(engine.Target(out), ro, "qsv", webcam_index=2)
check("webcam overlay", "[2:v]scale=" in chain and "overlay=" in chain, chain)
q = engine.quality_args("qsv", "h264_qsv", config.record_options("very_high"))
check("quality args scoped to video", "-global_quality:v" in q, q)
caps = {"h264_qsv", "av1_qsv", "h264_nvenc", "libx264"}
check("encoder auto picks display GPU", engine.resolve_encoder("auto", out, caps) == ("qsv", "h264_qsv"))
check("encoder av1", engine.resolve_encoder("av1", out, caps) == ("qsv", "av1_qsv"))
check("encoder hevc falls back to h264", engine.resolve_encoder("hevc", out, caps)[1] == "h264_qsv")
check("software encoder", engine.resolve_encoder("h264_software", out, caps) == ("cpu", "libx264"))

# gallery scan
root = Path(tempfile.mkdtemp())
(root / "Game").mkdir()
for name in ("Replay_a.mp4", "Video_b.mkv", "Screenshot_c.jpg", "notes.txt", "Replay_empty.mp4", "Game/Replay_d.mp4"):
    (root / name).write_bytes(b"" if "empty" in name else b"data")
    time.sleep(0.01)
cfg = config.defaults()
for k in ("replay", "record", "screenshot"):
    cfg[k]["save_directory"] = str(root)
empty_nv = Path(tempfile.mkdtemp())
clips = gallery.scan(cfg, nvidia_dir=empty_nv)
check("gallery finds clips", sorted(c.path.name for c in clips) == ["Replay_a.mp4", "Replay_d.mp4", "Screenshot_c.jpg", "Video_b.mkv"],
      [c.path.name for c in clips])
check("gallery newest first", clips[0].path.name == "Replay_d.mp4")
check("gallery game folder", next(c for c in clips if c.path.name == "Replay_d.mp4").game == "Game")
check("gallery kinds", {c.kind for c in clips} == {"replay", "record", "screenshot"})
nv = Path(tempfile.mkdtemp())
(nv / "Counter-strike 2").mkdir()
for name in ("Counter-strike 2/Counter-strike 2 2026.06.18 - 18.29.51.03.DVR.mp4",
             "Counter-strike 2/Counter-strike 2 2026.06.18 - 18.30.00.04.mp4", "Desktop 2026.01.01.png"):
    (nv / name).write_bytes(b"data")
nclips = {c.path.name: c for c in gallery.scan(cfg, nvidia_dir=nv)}
check("gallery includes nvidia clips", len(nclips) == 7, list(nclips))
check("nvidia DVR is replay", nclips["Counter-strike 2 2026.06.18 - 18.29.51.03.DVR.mp4"].kind == "replay")
check("nvidia recording", nclips["Counter-strike 2 2026.06.18 - 18.30.00.04.mp4"].kind == "record")
check("nvidia game folder", nclips["Counter-strike 2 2026.06.18 - 18.30.00.04.mp4"].game == "Counter-strike 2")
check("fmt_time", gallery.fmt_time(62) == "1:02" and gallery.fmt_time(3725) == "1:02:05")

# widgets
win = gui.Win()
e = gui.Entry("body", "60", 100)
e.set_number_mode(True, 1, 500)
gui._focused_entry = e
for ev in (gui.Event(gui.Event.KEY, key=Qt.Key.Key_Backspace), gui.Event(gui.Event.KEY, key=Qt.Key.Key_Backspace),
           gui.Event(gui.Event.TEXT, text="9"), gui.Event(gui.Event.TEXT, text="x"), gui.Event(gui.Event.TEXT, text="99")):
    e.on_event(ev, win, (0, 0))
check("entry number mode typing", e.get_text() == "999", e.get_text())
gui.unfocus_entries()
check("entry clamps on blur", e.get_text() == "500", e.get_text())
cb = gui.ComboBox("body")
seen = []
cb.on_selection_changed = lambda t, i: seen.append(i)
for t, i in (("A", "a"), ("B", "b")):
    cb.add_item(t, i)
cb.set_selected_item("b")
check("combobox select fires", cb.get_selected_id() == "b" and seen == ["b"])
cb.set_item_enabled("b", False)
check("combobox disabled item resets", cb.get_selected_id() == "a")

# translations
config.translation.load_language("es")
check("spanish translation", config.TR("Record") != "Record", config.TR("Record"))
config.translation.load_language("en")
check("english passthrough", config.TR("Record") == "Record")

# timer resolution helper is importable and audio devices enumerate
check("audio devices", isinstance(__import__("gsr.audio").audio.list_devices()["outputs"], list))
check("outputs", len(system.list_outputs()) >= 1)

print(f"\n{len(failures)} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
