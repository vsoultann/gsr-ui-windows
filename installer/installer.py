"""GPU Screen Recorder for Windows: setup program.

Per-user install (no admin): %LOCALAPPDATA%\\Programs\\GPU Screen Recorder, Start Menu shortcut,
optional desktop shortcut and autostart, and an entry in Settings -> Apps whose uninstall runs
"GPU Screen Recorder.exe uninstall".

    installer.py                    show the setup window
    installer.py --extract-only DIR  install files into DIR without shortcuts/registry (for tests)
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import winreg
import zipfile
from pathlib import Path

APP_NAME = "GPU Screen Recorder"
VERSION = "1.0.0"
EXE = f"{APP_NAME}.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GPUScreenRecorderWindows"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
HOTKEYS = [("Alt+Z", "Open / close the overlay"), ("Alt+F10", "Save replay"), ("Alt+Shift+F10", "Replay on / off"),
           ("Alt+F9", "Start / stop recording"), ("PrtSc", "Screenshot")]


def res(name):
    base = os.environ.get("GSR_SETUP_RES") or getattr(sys, "_MEIPASS", Path(__file__).parent)
    return Path(base) / name


def default_dir():
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP_NAME


# ---------------------------------------------------------------- install steps (no UI)

def stop_running_app():
    subprocess.run(["taskkill", "/F", "/T", "/IM", EXE], capture_output=True, creationflags=0x08000000)
    time.sleep(0.5)


def extract(payload, target, progress):
    """Unzip the app into target, reporting (fraction, file name)."""
    with zipfile.ZipFile(payload) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        total = sum(i.file_size for i in infos) or 1
        prefix = os.path.commonprefix([i.filename for i in infos])
        prefix = prefix[:prefix.rfind("/") + 1] if "/" in prefix else ""
        done = 0
        internal = target / "_internal"
        if internal.exists():   # drop files from an older version
            import shutil
            shutil.rmtree(internal, ignore_errors=True)
        for info in infos:
            rel = info.filename[len(prefix):]
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    progress(done / total, Path(rel).name)
    return target / EXE


def shortcut(lnk, target_exe, args=""):
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{target_exe}';"
          f"$s.Arguments='{args}';$s.WorkingDirectory='{target_exe.parent}';$s.IconLocation='{target_exe},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, creationflags=0x08000000)


def start_menu_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir():
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)   # CSIDL_DESKTOPDIRECTORY
    return Path(buf.value)


def register(exe, desktop, autostart):
    shortcut(start_menu_dir() / f"{APP_NAME}.lnk", exe)
    if desktop:
        shortcut(desktop_dir() / f"{APP_NAME}.lnk", exe)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if autostart:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" launch-daemon')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    size_kb = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, value in (("DisplayName", f"{APP_NAME} (Windows port)"), ("DisplayVersion", VERSION),
                            ("Publisher", "gsr-ui-windows"), ("DisplayIcon", str(exe)),
                            ("InstallLocation", str(exe.parent)), ("UninstallString", f'"{exe}" uninstall'),
                            ("URLInfoAbout", "https://github.com/vsoultann/gsr-ui-windows")):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


# ---------------------------------------------------------------- UI

def run_ui():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    from PySide6.QtCore import QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPainterPath, QPixmap
    from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPushButton,
                                   QVBoxLayout, QWidget)

    TINT = QColor(8, 109, 183)
    BG, PANEL, MUTED = QColor(25, 30, 34), QColor(38, 43, 47), QColor(150, 150, 150)

    app = QApplication(sys.argv)
    family = "Segoe UI"
    for f in (res("fonts")).glob("*.ttf"):
        fams = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(f)))
        family = fams[0] if fams else family
    app.setFont(QFont(family, 10))
    app.setWindowIcon(QIcon(str(res("logo.png"))))

    class Slideshow(QWidget):
        """Showcase screenshots, crossfading every few seconds, with position dots."""
        HOLD, FADE = 3.6, 0.7

        def __init__(self, images):
            super().__init__()
            self.images = [QPixmap(str(p)) for p in images]
            self.index = 0
            self.t0 = time.monotonic()
            timer = QTimer(self)
            timer.timeout.connect(self._tick)
            timer.start(33)

        def _tick(self):
            if time.monotonic() - self.t0 >= self.HOLD + self.FADE:
                self.index = (self.index + 1) % len(self.images)
                self.t0 = time.monotonic()
            self.update()

        def mousePressEvent(self, e):
            self.index = (self.index + 1) % len(self.images)
            self.t0 = time.monotonic()

        def _draw_cover(self, p, pix, opacity):
            if pix.isNull():
                return
            r = self.rect()
            sc = max(r.width() / pix.width(), r.height() / pix.height())
            w, h = pix.width() * sc, pix.height() * sc
            p.setOpacity(opacity)
            p.drawPixmap(QRectF((r.width() - w) / 2, (r.height() - h) / 2, w, h), pix, QRectF(pix.rect()))
            p.setOpacity(1.0)

        def paintEvent(self, _):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.fillRect(self.rect(), QColor(0, 0, 0))
            if not self.images:
                return
            el = time.monotonic() - self.t0
            self._draw_cover(p, self.images[self.index], 1.0)
            if el > self.HOLD:
                k = min(1.0, (el - self.HOLD) / self.FADE)
                self._draw_cover(p, self.images[(self.index + 1) % len(self.images)], k * k * (3 - 2 * k))
            # dots
            n, d, gap = len(self.images), 7, 9
            x = self.width() / 2 - (n * d + (n - 1) * gap) / 2
            y = self.height() - 22
            for i in range(n):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255) if i == self.index else QColor(255, 255, 255, 90))
                p.drawEllipse(QRectF(x + i * (d + gap), y, d, d))

    class Progress(QWidget):
        def __init__(self):
            super().__init__()
            self.value = 0.0
            self.setFixedHeight(6)

        def paintEvent(self, _):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = QRectF(self.rect())
            path = QPainterPath()
            path.addRoundedRect(r, 3, 3)
            p.fillPath(path, QColor(0, 0, 0, 140))
            if self.value > 0:
                f = QPainterPath()
                f.addRoundedRect(QRectF(0, 0, max(6.0, r.width() * self.value), r.height()), 3, 3)
                p.fillPath(f, TINT)

    class KeyCap(QLabel):
        pass

    class Setup(QWidget):
        progressed = Signal(float, str)
        finished_ok = Signal(str)
        failed = Signal(str)

        def __init__(self):
            super().__init__(None, Qt.WindowType.FramelessWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setFixedSize(980, 580)
            self.setWindowTitle(f"{APP_NAME} Setup")
            self.target = default_dir()
            self.exe = None
            self._drag = None

            left = QWidget()
            left.setFixedWidth(420)
            left.setObjectName("left")
            lay = QVBoxLayout(left)
            lay.setContentsMargins(36, 34, 36, 28)
            lay.setSpacing(0)

            head = QHBoxLayout()
            logo = QLabel()
            logo.setPixmap(QPixmap(str(res("logo.png"))).scaled(46, 46, Qt.AspectRatioMode.KeepAspectRatio,
                                                                Qt.TransformationMode.SmoothTransformation))
            head.addWidget(logo)
            titles = QVBoxLayout()
            titles.setSpacing(0)
            t = QLabel(APP_NAME)
            t.setObjectName("title")
            sub = QLabel(f"for Windows  ·  v{VERSION}")
            sub.setObjectName("muted")
            titles.addWidget(t)
            titles.addWidget(sub)
            head.addSpacing(12)
            head.addLayout(titles)
            head.addStretch()
            lay.addLayout(head)
            lay.addSpacing(22)

            desc = QLabel("Instant replay, recording, livestreaming and screenshots on the GPU, "
                          "with the GPU Screen Recorder overlay one hotkey away.")
            desc.setWordWrap(True)
            desc.setObjectName("desc")
            lay.addWidget(desc)
            lay.addSpacing(22)

            # --- options (before install)
            self.options = QWidget()
            ol = QVBoxLayout(self.options)
            ol.setContentsMargins(0, 0, 0, 0)
            ol.setSpacing(8)
            loc_row = QHBoxLayout()
            self.loc = QLabel()
            self.loc.setObjectName("path")
            self.loc.setMinimumWidth(10)
            change = QPushButton("Change")
            change.setObjectName("link")
            change.setCursor(Qt.CursorShape.PointingHandCursor)
            change.clicked.connect(self.pick_dir)
            loc_row.addWidget(self.loc, 1)
            loc_row.addWidget(change)
            lbl = QLabel("Install location")
            lbl.setObjectName("muted")
            ol.addWidget(lbl)
            ol.addLayout(loc_row)
            ol.addSpacing(6)
            self.desktop = QCheckBox("Desktop shortcut")
            self.desktop.setChecked(True)
            self.autostart = QCheckBox("Start with Windows (in the background)")
            self.autostart.setChecked(True)
            ol.addWidget(self.desktop)
            ol.addWidget(self.autostart)
            lay.addWidget(self.options)

            # --- progress (during install)
            self.progress_box = QWidget()
            pl = QVBoxLayout(self.progress_box)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(8)
            row = QHBoxLayout()
            self.status = QLabel("Preparing…")
            self.status.setObjectName("status")
            self.percent = QLabel("0%")
            self.percent.setObjectName("percent")
            row.addWidget(self.status, 1)
            row.addWidget(self.percent)
            self.bar = Progress()
            self.detail = QLabel("")
            self.detail.setObjectName("muted")
            pl.addLayout(row)
            pl.addWidget(self.bar)
            pl.addWidget(self.detail)
            self.progress_box.hide()
            lay.addWidget(self.progress_box)

            lay.addStretch()

            # --- hotkey hints
            hints = QLabel("Default hotkeys")
            hints.setObjectName("muted")
            lay.addWidget(hints)
            lay.addSpacing(8)
            for keys, what in HOTKEYS:
                r = QHBoxLayout()
                r.setSpacing(10)
                cap = KeyCap(keys)
                cap.setObjectName("key")
                cap.setFixedWidth(108)
                cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
                w = QLabel(what)
                w.setObjectName("hint")
                r.addWidget(cap)
                r.addWidget(w, 1)
                lay.addLayout(r)
                lay.addSpacing(5)
            lay.addSpacing(18)

            # --- buttons
            btns = QHBoxLayout()
            self.primary = QPushButton("Install")
            self.primary.setObjectName("primary")
            self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
            self.primary.clicked.connect(self.on_primary)
            self.secondary = QPushButton("Cancel")
            self.secondary.setObjectName("secondary")
            self.secondary.setCursor(Qt.CursorShape.PointingHandCursor)
            self.secondary.clicked.connect(self.close)
            btns.addWidget(self.primary, 1)
            btns.addWidget(self.secondary)
            lay.addLayout(btns)

            shots = sorted(res("showcase").glob("*.png"))
            self.slides = Slideshow(shots)
            close = QPushButton("×", self.slides)
            close.setObjectName("close")
            close.setFixedSize(34, 30)
            close.clicked.connect(self.close)
            close.move(980 - 420 - 34 - 8, 8)

            root = QHBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(0)
            root.addWidget(left)
            root.addWidget(self.slides, 1)
            self.update_loc()

            self.progressed.connect(self.on_progress)
            self.finished_ok.connect(self.on_finished)
            self.failed.connect(self.on_failed)
            self.setStyleSheet(f"""
                QWidget {{ color: #ffffff; font-family: '{family}'; font-size: 13px; }}
                #left {{ background: {BG.name()}; }}
                #title {{ font-size: 20px; font-weight: bold; }}
                #muted {{ color: {MUTED.name()}; font-size: 12px; }}
                #desc {{ color: #d0d0d0; font-size: 13px; }}
                #path {{ background: rgba(0,0,0,120); padding: 7px 9px; font-size: 12px; }}
                #status {{ font-size: 13px; }}
                #percent {{ font-size: 13px; font-weight: bold; color: {TINT.name()}; }}
                #key {{ background: {PANEL.name()}; border-bottom: 2px solid rgba(0,0,0,160); padding: 4px 6px;
                       font-size: 12px; font-weight: bold; }}
                #hint {{ color: #c8c8c8; font-size: 12px; }}
                QPushButton#primary {{ background: {TINT.name()}; border: none; padding: 11px; font-size: 14px;
                                       font-weight: bold; }}
                QPushButton#primary:hover {{ background: {TINT.lighter(115).name()}; }}
                QPushButton#primary:disabled {{ background: {PANEL.name()}; color: {MUTED.name()}; }}
                QPushButton#secondary {{ background: {PANEL.name()}; border: none; padding: 11px 18px; font-size: 14px; }}
                QPushButton#secondary:hover {{ background: #30363b; }}
                QPushButton#secondary:disabled {{ color: #5d6368; }}
                QPushButton#link {{ background: transparent; border: none; color: {TINT.lighter(140).name()};
                                    padding: 7px 0 7px 10px; }}
                QPushButton#link:hover {{ text-decoration: underline; }}
                QPushButton#close {{ background: rgba(0,0,0,110); border: none; font-size: 20px; padding-bottom: 3px; }}
                QPushButton#close:hover {{ background: #c42b1c; }}
                QCheckBox {{ spacing: 9px; font-size: 13px; }}
                QCheckBox::indicator {{ width: 15px; height: 15px; background: rgba(0,0,0,140);
                                        border: 1px solid #50565b; }}
                QCheckBox::indicator:checked {{ background: {TINT.name()}; border: 1px solid {TINT.name()};
                                                image: url({res('check.png').as_posix()}); }}
            """)

        # window dragging (frameless)
        def mousePressEvent(self, e):
            if e.button() == Qt.MouseButton.LeftButton:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, e):
            if self._drag is not None:
                self.move(e.globalPosition().toPoint() - self._drag)

        def mouseReleaseEvent(self, e):
            self._drag = None

        def paintEvent(self, _):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), 10, 10)
            p.setClipPath(path)
            p.fillRect(self.rect(), BG)
            p.fillRect(QRectF(0, 0, 420, 3), TINT)

        def update_loc(self):
            fm = self.loc.fontMetrics()
            self.loc.setText(fm.elidedText(str(self.target), Qt.TextElideMode.ElideMiddle, 250))
            self.loc.setToolTip(str(self.target))

        def pick_dir(self):
            d = QFileDialog.getExistingDirectory(self, "Install location", str(self.target.parent))
            if d:
                self.target = Path(d) / APP_NAME if Path(d).name != APP_NAME else Path(d)
                self.update_loc()

        def on_primary(self):
            if self.exe:   # finished: launch
                subprocess.Popen([str(self.exe)], cwd=str(self.exe.parent), creationflags=0x00000008)
                self.close()
                return
            self.options.hide()
            self.progress_box.show()
            self.primary.setEnabled(False)
            self.primary.setText("Installing…")
            self.secondary.setEnabled(False)
            threading.Thread(target=self.work, daemon=True).start()

        def work(self):
            try:
                self.progressed.emit(0.0, "Closing GPU Screen Recorder if it's running")
                stop_running_app()
                self.target.mkdir(parents=True, exist_ok=True)
                last = [0.0]

                def prog(f, name):
                    now = time.monotonic()
                    if now - last[0] > 0.03 or f >= 1:
                        last[0] = now
                        self.progressed.emit(f * 0.95, f"Copying {name}")
                exe = extract(res("payload.zip"), self.target, prog)
                self.progressed.emit(0.96, "Creating shortcuts")
                register(exe, self.desktop.isChecked(), self.autostart.isChecked())
                self.progressed.emit(1.0, "Done")
                self.finished_ok.emit(str(exe))
            except Exception as e:
                self.failed.emit(str(e))

        def on_progress(self, f, text):
            self.bar.value = f
            self.bar.update()
            self.percent.setText(f"{int(f * 100)}%")
            if text.startswith("Copying"):
                self.status.setText("Installing files…")
                self.detail.setText(text)
            else:
                self.status.setText(text)
                self.detail.setText("")

        def on_finished(self, exe):
            self.exe = Path(exe)
            self.status.setText("Installed")
            self.detail.setText("Press Alt+Z in any game to open the overlay.")
            self.primary.setEnabled(True)
            self.primary.setText("Launch")
            self.secondary.setEnabled(True)
            self.secondary.setText("Close")

        def on_failed(self, msg):
            self.status.setText("Installation failed")
            self.detail.setText(msg)
            self.detail.setWordWrap(True)
            self.primary.setEnabled(True)
            self.primary.setText("Retry")
            self.secondary.setEnabled(True)
            self.options.show()

    w = Setup()
    w.show()
    snap = os.environ.get("GSR_SETUP_SNAPSHOT")   # dev: render the states to PNGs and exit
    if snap:
        def shoot(name):
            w.grab().save(str(Path(snap) / f"setup-{name}.png"))

        def states():
            shoot("ready")
            w.options.hide()
            w.progress_box.show()
            w.primary.setEnabled(False)
            w.primary.setText("Installing…")
            w.secondary.setEnabled(False)
            w.on_progress(0.63, "Copying avcodec-62.dll")
            w.slides.index = 3
            QTimer.singleShot(200, lambda: (shoot("installing"), w.on_progress(1.0, "Done"),
                                            w.on_finished(str(default_dir() / EXE)), w.slides.__setattr__("index", 4),
                                            QTimer.singleShot(200, lambda: (shoot("done"), app.quit()))))
        QTimer.singleShot(400, states)
    return app.exec()


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.gsr-ui-windows.setup")
    except Exception:
        pass
    if "--extract-only" in sys.argv:
        target = Path(sys.argv[sys.argv.index("--extract-only") + 1])
        exe = extract(res("payload.zip"), target, lambda f, n: None)
        print(exe, exe.exists())
        return 0
    return run_ui()


if __name__ == "__main__":
    sys.exit(main())
