"""gsr-ui config with upstream defaults (Config.cpp / Config.hpp), stored as JSON."""
import copy
import ctypes
import json
import locale
import os
from pathlib import Path

from . import gui

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "gpu-screen-recorder"
CONFIG_PATH = CONFIG_DIR / "config_ui.json"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "gpu-screen-recorder"


def _known_folder(guid):
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]
    import uuid
    u = uuid.UUID(guid)
    g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    path = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(path)) == 0:
        res = path.value
        ctypes.windll.ole32.CoTaskMemFree(path)
        return res
    return None


def videos_dir():
    return _known_folder("18989B1D-99B5-455B-841C-AB7C74E4DDFC") or str(Path.home() / "Videos")


def pictures_dir():
    return _known_folder("33E28130-4E1E-4676-835A-98395C3BC3BB") or str(Path.home() / "Pictures")


def record_options(video_quality="very_high", bitrate=8000):
    return {
        "record_area_option": "focused_monitor",
        "record_area_width": 0, "record_area_height": 0,
        "video_width": 0, "video_height": 0,
        "fps": 60, "video_bitrate": bitrate,
        "change_video_resolution": False,
        "color_range": "limited",
        "video_quality": video_quality,
        "video_codec": "auto",
        "audio_codec": "opus",
        "framerate_mode": "auto",
        "advanced_view": False,
        "record_cursor": True,
        "show_notifications": True,
        "use_led_indicator": False,
        "audio_tracks_list": [{"name": "", "audio_inputs": ["default_output"], "application_audio_invert": False}],
        "region": None,
    }


def default_hotkeys():
    return {
        "main.show_hide_hotkey": "alt+z",
        "streaming.start_stop_hotkey": "alt+f8",
        "record.start_stop_hotkey": "alt+f9",
        "record.pause_unpause_hotkey": "alt+f7",
        "record.start_stop_region_hotkey": "ctrl+f9",
        "record.start_stop_window_hotkey": "shift+f9",
        "replay.start_stop_hotkey": "alt+shift+f10",
        "replay.save_hotkey": "alt+f10",
        "replay.save_1_min_hotkey": "alt+f11",
        "replay.save_10_min_hotkey": "alt+f12",
        "screenshot.take_screenshot_hotkey": "printscreen",
        "screenshot.take_screenshot_region_hotkey": "ctrl+printscreen",
        "screenshot.take_screenshot_window_hotkey": "shift+printscreen",
    }


def defaults():
    vids, pics = videos_dir(), pictures_dir()
    cfg = {
        "main": {
            "tint_color": "", "notification_speed": "normal", "language": "",
            "hotkeys_enable_option": "enable_hotkeys", "exclude_metadata": False,
        },
        "streaming": {
            "record_options": record_options("custom", 8000),
            "streaming_service": "twitch",
            "twitch": {"stream_key": ""}, "youtube": {"stream_key": ""}, "rumble": {"stream_key": ""},
            "kick": {"stream_url": "", "stream_key": ""},
            "custom": {"url": "", "key": "", "container": "flv"},
        },
        "record": {
            "record_options": record_options("very_high", 40000),
            "save_video_in_game_folder": False, "save_directory": vids, "container": "mp4",
        },
        "replay": {
            "record_options": record_options("custom", 40000),
            "turn_on_replay_automatically_mode": "dont_turn_on_automatically",
            "save_video_in_game_folder": False, "restart_replay_on_save": False,
            "only_start_replay_if_power_supply_connected": False,
            "save_directory": vids, "container": "mp4", "replay_time": 60, "replay_storage": "ram",
        },
        "screenshot": {
            "record_area_option": "focused_monitor", "image_width": 0, "image_height": 0,
            "change_image_resolution": False, "image_quality": "very_high", "image_format": "jpg",
            "record_cursor": True, "save_screenshot_in_game_folder": False,
            "save_screenshot_to_clipboard": False, "save_screenshot_to_disk": True,
            "show_notifications": True, "use_led_indicator": False,
            "save_directory": pics, "custom_script": "", "region": None,
        },
        "hotkeys": default_hotkeys(),
    }
    return cfg


def _merge(base, loaded):
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    cfg = defaults()
    try:
        _merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def copy_cfg(cfg):
    return copy.deepcopy(cfg)


# ---------------------------------------------------------------- translations

class Translation:
    def __init__(self):
        self.table = {}

    def load_language(self, code):
        self.table = {}
        if not code:
            code = system_language()
        if not code or code.startswith("en"):
            return
        path = gui.RES / "translations" / f"{code}.txt"
        if not path.exists():
            path = gui.RES / "translations" / f"{code.split('_')[0]}.txt"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            self.table[k.replace("\\n", "\n")] = v.replace("\\n", "\n")

    def tr(self, s):
        return self.table.get(s, s)


def system_language():
    buf = ctypes.create_unicode_buffer(85)
    if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
        return buf.value.replace("-", "_")
    return (locale.getlocale()[0] or "en")


translation = Translation()


def TR(s):
    return translation.tr(s)


def TRF(s, *args):
    return translation.tr(s) % args
