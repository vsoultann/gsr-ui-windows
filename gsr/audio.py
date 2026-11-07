"""WASAPI audio capture (speaker loopback or microphones) fed to ffmpeg through named pipes.

Each source writes a constant-rate PCM stream. Until the video has started the
stream is silence; align() then shifts the stream's clock so that its timestamps
line up with the video timestamps ffmpeg is producing.
"""
import ctypes
import threading
import time
import uuid
from ctypes import wintypes

import pyaudiowpatch as pyaudio

kernel32 = ctypes.windll.kernel32
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
INVALID_HANDLE = wintypes.HANDLE(-1).value

CUSHION = 0.06
MAX_BACKLOG = 0.3


def list_devices():
    """{'outputs': [names], 'inputs': [names]} for WASAPI devices."""
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        outs, ins = [], []
        for dev in pa.get_device_info_generator_by_host_api(host_api_index=wasapi):
            if dev.get("isLoopbackDevice"):
                continue
            if dev["maxOutputChannels"] > 0:
                outs.append(dev["name"])
            elif dev["maxInputChannels"] > 0:
                ins.append(dev["name"])
        return {"outputs": outs, "inputs": ins}
    finally:
        pa.terminate()


def _resolve(pa, input_id):
    """Map a gsr audio input id to a PyAudio device dict to open as an input stream."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    if input_id == "default_output":
        return pa.get_default_wasapi_loopback()
    if input_id == "default_input":
        return pa.get_device_info_by_index(wasapi["defaultInputDevice"])
    name = input_id.split(":", 1)[1] if input_id.startswith("device:") else input_id
    for dev in pa.get_loopback_device_info_generator():
        if dev["name"].startswith(name):
            return dev
    for dev in pa.get_device_info_generator_by_host_api(host_api_index=wasapi["index"]):
        if dev["name"] == name and dev["maxInputChannels"] > 0 and not dev.get("isLoopbackDevice"):
            return dev
    raise RuntimeError(f"audio device not found: {name}")


class Source:
    def __init__(self, input_id):
        self.pa = pyaudio.PyAudio()
        try:
            dev = _resolve(self.pa, input_id)
        except Exception:
            self.pa.terminate()
            raise
        self.input_id = input_id
        self.device_name = dev["name"]
        self.dev_index = dev["index"]
        self.rate = int(dev["defaultSampleRate"])
        self.channels = max(1, min(8, int(dev["maxInputChannels"])))
        self.fb = 2 * self.channels
        self.pipe_name = r"\\.\pipe\gsr-audio-" + uuid.uuid4().hex
        self.pipe = kernel32.CreateNamedPipeW(self.pipe_name, 0x2, 0x0, 1, 1 << 20, 1 << 20, 0, None)
        if self.pipe == INVALID_HANDLE:
            self.pa.terminate()
            raise OSError("CreateNamedPipe failed")
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t0 = None
        self._aligned = False
        self._align_to = None
        self._stream = None
        self.device_changed = False

    def ffmpeg_args(self):
        return ["-f", "s16le", "-ar", str(self.rate), "-ac", str(self.channels),
                "-thread_queue_size", "4096", "-i", self.pipe_name]

    def _callback(self, data, frames, info, status):
        with self._lock:
            self._buf += data
        return (None, pyaudio.paContinue)

    def start(self):
        self._stream = self.pa.open(format=pyaudio.paInt16, channels=self.channels, rate=self.rate, input=True,
                                    input_device_index=self.dev_index, frames_per_buffer=480,
                                    stream_callback=self._callback)
        threading.Thread(target=self._pump, daemon=True).start()
        if self.input_id in ("default_output", "default_input"):
            threading.Thread(target=self._watch_default, daemon=True).start()

    def align(self, video_seconds):
        """Called once the video is running: make the next sample's timestamp equal video_seconds."""
        self._align_to = (time.perf_counter(), video_seconds)

    def _write(self, data):
        n = wintypes.DWORD()
        return kernel32.WriteFile(self.pipe, data, len(data), ctypes.byref(n), None)

    def _pump(self):
        kernel32.ConnectNamedPipe(self.pipe, None)   # blocks until ffmpeg opens the pipe
        if self._stop.is_set():
            return
        fb = self.fb
        cushion = int(CUSHION * self.rate) * fb
        backlog = int(MAX_BACKLOG * self.rate) * fb
        self._t0 = time.perf_counter()
        written = 0
        playing = False
        while not self._stop.is_set():
            time.sleep(0.01)
            if self._align_to and not self._aligned:
                t_a, v = self._align_to
                self._t0 = t_a - v   # stream time at t_a becomes v
                self._aligned = True
                with self._lock:
                    self._buf.clear()
            target = int((time.perf_counter() - self._t0) * self.rate)
            need = (target - written) * fb
            if need <= 0:
                continue
            chunk = b""
            with self._lock:
                if not self._aligned:
                    self._buf.clear()
                else:
                    if len(self._buf) > backlog:
                        del self._buf[:len(self._buf) - cushion]
                    if not playing and len(self._buf) >= cushion:
                        playing = True
                    if playing:
                        chunk = bytes(self._buf[:need])
                        del self._buf[:need]
                        if len(chunk) < need:
                            playing = False
            chunk += bytes(need - len(chunk))
            if not self._write(chunk):
                break
            written = target

    def _watch_default(self):
        while not self._stop.wait(3):
            try:
                pa = pyaudio.PyAudio()
                try:
                    name = _resolve(pa, self.input_id)["name"]
                finally:
                    pa.terminate()
                if name != self.device_name:
                    self.device_changed = True
                    return
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        self.pa.terminate()
        if self._t0 is None:
            # unblock a pump thread still waiting in ConnectNamedPipe
            try:
                h = kernel32.CreateFileW(self.pipe_name, 0x40000000, 0, None, 3, 0, None)
                if h and h != INVALID_HANDLE:
                    kernel32.CloseHandle(h)
            except Exception:
                pass
        kernel32.DisconnectNamedPipe(self.pipe)
        kernel32.CloseHandle(self.pipe)
