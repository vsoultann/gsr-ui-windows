"""Windows plumbing: ffmpeg discovery, DXGI display enumeration, encoder probing, misc OS helpers."""
import ctypes
import os
import re
import shutil
import subprocess
import sys
import winreg
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

INTEL, NVIDIA, AMD = 0x8086, 0x10DE, 0x1002


def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


def resource(name):
    return Path(getattr(sys, "_MEIPASS", app_dir())) / name


def find_ffmpeg():
    local = app_dir() / "ffmpeg.exe"
    if local.exists():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for p in winget.glob("*FFmpeg*/**/bin/ffmpeg.exe"):
        return str(p)
    return None


def run_ffmpeg(ffmpeg, args, timeout=None):
    return subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True,
                          creationflags=CREATE_NO_WINDOW, timeout=timeout)


# ---------------------------------------------------------------- displays

@dataclass
class Output:
    adapter: int        # d3d11va adapter index (what ffmpeg's -init_hw_device takes)
    index: int          # output index on that adapter (ddagrab output_idx)
    device: str         # \\.\DISPLAY1
    width: int
    height: int
    primary: bool
    vendor: int
    gpu: str
    left: int = 0
    top: int = 0

    @property
    def label(self):
        return self.device.replace("\\\\.\\", "")


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _AdapterDesc1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint),
                ("DeviceId", ctypes.c_uint), ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint),
                ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("LuidLow", wintypes.DWORD),
                ("LuidHigh", wintypes.LONG), ("Flags", ctypes.c_uint)]


class _OutputDesc(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32), ("Desktop", wintypes.RECT),
                ("AttachedToDesktop", wintypes.BOOL), ("Rotation", ctypes.c_uint),
                ("Monitor", wintypes.HANDLE)]


def _com(obj, index, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def list_outputs():
    """Enumerate monitors the same way ffmpeg does (DXGI adapter index + output index)."""
    outputs = []
    try:
        iid = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
        factory = ctypes.c_void_p()
        ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory))
        a = 0
        while True:
            adapter = ctypes.c_void_p()
            if _com(factory, 12, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(factory, a, ctypes.byref(adapter)):
                break  # DXGI_ERROR_NOT_FOUND
            desc = _AdapterDesc1()
            _com(adapter, 10, ctypes.POINTER(_AdapterDesc1))(adapter, ctypes.byref(desc))
            o = 0
            while True:
                out = ctypes.c_void_p()
                if _com(adapter, 7, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(adapter, o, ctypes.byref(out)):
                    break
                od = _OutputDesc()
                _com(out, 7, ctypes.POINTER(_OutputDesc))(out, ctypes.byref(od))
                r = od.Desktop
                if od.AttachedToDesktop:
                    outputs.append(Output(a, o, od.DeviceName, r.right - r.left, r.bottom - r.top,
                                          r.left == 0 and r.top == 0, desc.VendorId, desc.Description, r.left, r.top))
                _release(out)
                o += 1
            _release(adapter)
            a += 1
        _release(factory)
    except Exception:
        pass
    if not outputs:
        outputs.append(Output(0, 0, r"\\.\DISPLAY1", 1920, 1080, True, 0, "Unknown GPU"))
    outputs.sort(key=lambda x: x.device)
    return outputs


# ---------------------------------------------------------------- devices / encoders

def list_mics(ffmpeg):
    """[(friendly name, dshow alternative name)] for audio capture devices."""
    res = run_ffmpeg(ffmpeg, ["-list_devices", "true", "-f", "dshow", "-i", "dummy"], timeout=15)
    lines = res.stderr.decode("utf-8", "replace").splitlines()
    mics = []
    for i, line in enumerate(lines):
        m = re.search(r'"(.+)" \(audio\)', line)
        if m and i + 1 < len(lines):
            alt = re.search(r'Alternative name "(.+)"', lines[i + 1])
            mics.append((m.group(1), alt.group(1) if alt else m.group(1)))
    return mics


HW_ENCODERS = [f"{c}_{f}" for f in ("qsv", "nvenc", "amf") for c in ("h264", "hevc", "av1")]
CPU_ENCODERS = ["libx264", "libx265", "libsvtav1"]


def _works(ffmpeg, enc):
    res = run_ffmpeg(ffmpeg, ["-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=640x360:r=30",
                              "-frames:v", "3", "-c:v", enc, "-f", "null", "-"], timeout=20)
    return res.returncode == 0


def probe_encoders(ffmpeg):
    """Encoders that actually open on this machine (being compiled in isn't enough)."""
    with ThreadPoolExecutor(6) as pool:
        names = HW_ENCODERS + CPU_ENCODERS
        return {n for n, ok in zip(names, pool.map(lambda n: _works(ffmpeg, n), names)) if ok}


# ---------------------------------------------------------------- misc OS helpers

def foreground_app():
    """Name of the app in focus, used to sort clips into per-game folders."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return "Desktop"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return "Desktop"
        name = Path(buf.value).stem
    finally:
        kernel32.CloseHandle(handle)
    if name.lower() in ("explorer", "winclip", "python", "pythonw"):
        return "Desktop"
    name = re.sub(r"(-Win64-Shipping|-Win64|_x64|\.x64)$", "", name, flags=re.I)
    return re.sub(r'[<>:"/\\|?*]', "_", name) or "Desktop"


def foreground_title():
    """Title of the focused window, sanitized for use as a folder name (upstream: game folder)."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", buf.value).strip().rstrip(".")
    return title or "Game"


def cursor_pos():
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]


def output_at(outputs, x, y):
    """DXGI output containing the given desktop point (falls back to the primary)."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)
    mi = _MonitorInfo()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        for o in outputs:
            if o.device == mi.szDevice:
                return o
    return next((o for o in outputs if o.primary), outputs[0])


def window_rect_at(x, y):
    """(hwnd, title, left, top, right, bottom) of the top-level window under a desktop point."""
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(x, y)), 2)
    if not hwnd:
        return None
    r = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return hwnd, buf.value, r.left, r.top, r.right, r.bottom


def monitor_index_for(output):
    """Index of an output in EnumDisplayMonitors order (gfxcapture monitor_idx)."""
    user32 = ctypes.windll.user32
    names = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def cb(hmon, hdc, rc, lp):
        mi = _MonitorInfo()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        names.append(mi.szDevice)
        return True
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return names.index(output.device) if output.device in names else 0


def hmonitor_for(output):
    """HMONITOR handle (system-wide value) of an output."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def cb(hmon, hdc, rc, lp):
        mi = _MonitorInfo()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        if mi.szDevice == output.device:
            found.append(hmon)
        return True
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return int(found[0]) if found else 0


def window_under(x, y, exclude=()):
    """Topmost visible top-level window at a desktop point, skipping our own windows:
    (hwnd, title, left, top, right, bottom) or None."""
    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    hits = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lp):
        if hwnd in exclude or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:   # WS_EX_TOOLWINDOW
            return True
        r = wintypes.RECT()
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.left <= x < r.right and r.top <= y < r.bottom and r.right - r.left > 1:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            hits.append((hwnd, buf.value, r.left, r.top, r.right, r.bottom))
            return False
        return True
    user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def output_rect(output):
    """Desktop rectangle (left, top) of an output, via its GDI device name."""
    user32 = ctypes.windll.user32
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def cb(hmon, hdc, rc, lp):
        mi = _MonitorInfo()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        if mi.szDevice == output.device:
            result.append((mi.rcMonitor.left, mi.rcMonitor.top))
        return True
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return result[0] if result else (0, 0)


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def power_supply_connected():
    ps = _PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(ps)):
        return True
    return ps.ACLineStatus != 0


def set_scroll_lock_led(on):
    """The LED indicator: toggle Scroll Lock so its keyboard LED reflects capture state."""
    user32 = ctypes.windll.user32
    if bool(user32.GetKeyState(0x91) & 1) != bool(on):
        user32.keybd_event(0x91, 0x46, 0x1, 0)
        user32.keybd_event(0x91, 0x46, 0x3, 0)


def fullscreen_app_focused():
    """True when the focused window covers its whole monitor and isn't the desktop (game detection)."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("WorkerW", "Progman", "Shell_TrayWnd"):
        return False
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    hmon = user32.MonitorFromWindow(hwnd, 2)
    mi = _MonitorInfo()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


def raise_timer_resolution(proc):
    """Run timeBeginPeriod(1) inside a child process we started.

    ffmpeg's ddagrab paces frames with Sleep(); at the default 15.6 ms timer tick that caps capture at
    ~64 fps. Since Windows 10 2004 the timer resolution is per process, so it has to be requested from
    inside ffmpeg. winmm.dll is mapped at the same address in every process, so its timeBeginPeriod can
    be started there as a thread with the period as its argument.
    """
    k32 = ctypes.windll.kernel32
    k32.CreateRemoteThread.restype = wintypes.HANDLE
    k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                       ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    addr = ctypes.cast(ctypes.windll.winmm.timeBeginPeriod, ctypes.c_void_p).value

    def run():
        for delay in (0.15, 1.0):
            import time
            time.sleep(delay)
            if proc.poll() is not None:
                return
            h = k32.CreateRemoteThread(int(proc._handle), None, 0, addr, 1, 0, None)
            if h:
                k32.CloseHandle(h)
    import threading
    threading.Thread(target=run, daemon=True).start()


def pid_alive(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259   # STILL_ACTIVE


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "GPU Screen Recorder"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" launch-daemon'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}" "{Path(__file__).parent.parent / "main.py"}" launch-daemon'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    """Returns 0 on success like upstream's set_xdg_autostart."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return 0
    except OSError:
        return 1


_kernel32 = ctypes.windll.kernel32
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]


def single_instance():
    """Returns a 'show' event handle if this is the first instance, else signals the running one and returns None."""
    _kernel32.CreateMutexW(None, False, "Local\\gsr-ui.Instance")
    first = _kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    event = _kernel32.CreateEventW(None, False, False, "Local\\gsr-ui.Show")
    if not first:
        _kernel32.SetEvent(event)
        return None
    return event


def wait_event_result(handle, timeout_ms):
    """WaitForSingleObject: 0 = signalled, 0x102 = timeout, anything else = failure."""
    return _kernel32.WaitForSingleObject(handle, timeout_ms) & 0xFFFFFFFF


def wait_event(handle, timeout_ms):
    return wait_event_result(handle, timeout_ms) == 0
