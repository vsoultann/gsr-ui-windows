"""Global hotkeys via RegisterHotKey (works while games have focus, no keyboard hooks)."""
import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
MODIFIERS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}

_NAMED = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "printscreen": 0x2C,
    "pause": 0x13, "scrolllock": 0x91, "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB,
    "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
}



def _vk(key):
    if key in _NAMED:
        return _NAMED[key]
    if key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x6F + int(key[1:])
    if key.startswith("num") and key[3:].isdigit():
        return 0x60 + int(key[3:])
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    return None


def parse(combo):
    mods, vk = 0, None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif part and vk is None:
            vk = _vk(part)
            if vk is None:
                return None
        else:
            return None
    return (mods, vk) if vk else None



class Hotkeys:
    def __init__(self, on_fire):
        self.on_fire = on_fire
        self._thread = None
        self._tid = None

    def set(self, bindings):
        """Register {name: combo}; returns {name: reason} for the ones that failed."""
        self.stop()
        failures = {}
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(bindings, failures, ready), daemon=True)
        self._thread.start()
        ready.wait(2)
        return failures

    def stop(self):
        if self._thread and self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread = self._tid = None

    def _run(self, bindings, failures, ready):
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create the message queue
        self._tid = kernel32.GetCurrentThreadId()
        ids = {}
        for i, (name, combo) in enumerate(bindings.items(), 1):
            if not combo:
                continue
            parsed = parse(combo)
            if not parsed:
                failures[name] = "invalid key"
            elif not user32.RegisterHotKey(None, i, parsed[0] | MOD_NOREPEAT, parsed[1]):
                failures[name] = "already used by another app"
            else:
                ids[i] = name
        ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                self.on_fire(ids[msg.wParam])
        for i in ids:
            user32.UnregisterHotKey(None, i)
