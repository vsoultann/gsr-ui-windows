"""GPU Screen Recorder UI for Windows.

    main.py                 start and show the UI (like `gsr-ui`)
    main.py launch-daemon   start in the background; Alt+Z opens the UI
"""
import ctypes
import os
import sys
import traceback

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gsr import config, system  # noqa: E402


def main():
    show_event = system.single_instance()
    if show_event is None:
        return  # another instance is running; it has been asked to open its UI
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.dec05eba.gpu_screen_recorder")
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    from gsr.overlay import App
    app = App(qapp, show_event, daemon="launch-daemon" in sys.argv)
    qapp._gsr_app = app
    sys.exit(qapp.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
