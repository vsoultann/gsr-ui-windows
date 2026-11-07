"""Uninstall (run as 'GPU Screen Recorder.exe uninstall' from Settings -> Apps).

Removes the program folder, shortcuts, autostart and the Apps entry. Clips, screenshots and
settings are kept.
"""
import ctypes
import os
import subprocess
import sys
import winreg
from pathlib import Path

APP_NAME = "GPU Screen Recorder"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GPUScreenRecorderWindows"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MB_YESNO, MB_ICONQUESTION, MB_ICONINFO, IDYES = 0x4, 0x20, 0x40, 6


def _delete_value(key, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def main():
    box = ctypes.windll.user32.MessageBoxW
    if box(None, f"Remove {APP_NAME} from this PC?\n\nYour clips, screenshots and settings are kept.",
           f"Uninstall {APP_NAME}", MB_YESNO | MB_ICONQUESTION) != IDYES:
        return 0
    exe_name = Path(sys.executable).name
    # stop other running instances (and their ffmpeg children), not ourselves
    subprocess.run(["taskkill", "/F", "/T", "/FI", f"PID ne {os.getpid()}", "/IM", exe_name],
                   capture_output=True, creationflags=0x08000000)
    _delete_value(RUN_KEY, APP_NAME)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    start_menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
    for lnk in (start_menu, Path(buf.value) / f"{APP_NAME}.lnk"):
        try:
            lnk.unlink()
        except OSError:
            pass
    install_dir = Path(sys.executable).parent
    if getattr(sys, "frozen", False) and (install_dir / "_internal").exists():
        # the folder holds this running exe, so delete it from a detached shell once we've exited
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{install_dir}"',
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
    box(None, f"{APP_NAME} was removed.", f"Uninstall {APP_NAME}", MB_ICONINFO)
    return 0
