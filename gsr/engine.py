"""ffmpeg-based capture engine: replay buffer, recording (with pause), streaming and screenshots.

Capture: DXGI Desktop Duplication (ffmpeg ddagrab) for monitors/regions, Windows Graphics Capture
(gfxcapture) for single windows. Encoding: QuickSync / NVENC / AMF / CPU,
picked from the GPU driving the captured display. Audio: WASAPI sources through named pipes.
"""
import math
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import system
from .audio import Source
from .config import DATA_DIR

SEGMENT_SECONDS = 2
CQ = {"medium": 32, "high": 28, "very_high": 24, "ultra": 20}
CPU_ENCODER = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}
VENDOR_FAMILY = {system.INTEL: "qsv", system.NVIDIA: "nvenc", system.AMD: "amf"}
CONTAINER_EXT = {"mp4": "mp4", "matroska": "mkv", "flv": "flv", "mov": "mov", "mpegts": "ts"}


def stamp(t=None):
    return datetime.fromtimestamp(t or time.time()).strftime("%Y-%m-%d_%H-%M-%S")


def unique_path(path):
    """Never overwrite: Replay_<time>.mp4, then Replay_<time>_2.mp4, ..."""
    path = Path(path)
    n = 2
    while path.exists():
        path = path.with_name(f"{path.stem.rsplit('_', 1)[0] if n > 2 else path.stem}_{n}{path.suffix}")
        n += 1
    return path


def encoder_name(family, codec):
    return CPU_ENCODER[codec] if family == "cpu" else f"{codec}_{family}"


def families_for(output):
    native = VENDOR_FAMILY.get(output.vendor)
    return [f for f in (native, "qsv", "nvenc", "amf") if f]


def supported_codecs(output, caps):
    """Codecs the GPU that drives this display can encode (what gsr --info reports)."""
    fams = families_for(output)
    res = {c: any(encoder_name(f, c) in caps for f in fams[:1]) for c in ("h264", "hevc", "av1")}
    if not any(res.values()):
        res = {c: any(encoder_name(f, c) in caps for f in fams) for c in ("h264", "hevc", "av1")}
    res["h264_software"] = "libx264" in caps
    return res


def resolve_encoder(codec_opt, output, caps):
    if codec_opt == "h264_software":
        return "cpu", "libx264"
    order = ["h264", "hevc", "av1"] if codec_opt == "auto" else [codec_opt, "h264"]
    for c in order:
        for fam in families_for(output):
            if encoder_name(fam, c) in caps:
                return fam, encoder_name(fam, c)
    return "cpu", "libx264"


def _even(v):
    return max(2, int(v) // 2 * 2)


class Target:
    """What to capture: a DXGI output plus an optional crop rectangle (output coordinates) or a window."""

    def __init__(self, output, crop=None, label="this monitor", hwnd=None, window_size=None):
        self.output = output
        self.crop = crop
        self.label = label
        self.hwnd = hwnd
        self.window_size = window_size

    @property
    def size(self):
        if self.hwnd and self.window_size:
            return self.window_size
        return (self.crop[2], self.crop[3]) if self.crop else (self.output.width, self.output.height)


def capture_filter(target, fps, cursor, vfr, backend="dda"):
    """Video source filter. 'dda' = DXGI Desktop Duplication (default). 'gfx' = Windows Graphics Capture,
    used for single-window capture; on Windows 10 it draws a yellow border, so it is not used for monitors."""
    o = target.output
    if backend == "gfx" or target.hwnd:
        if target.hwnd:
            s = f"gfxcapture=hwnd={int(target.hwnd)}:max_framerate={fps}:capture_cursor={int(cursor)}"
        else:
            s = f"gfxcapture=monitor_idx={system.monitor_index_for(o)}:max_framerate={fps}:capture_cursor={int(cursor)}"
            if target.crop:
                x, y, w, h = target.crop
                w, h = _even(w), _even(h)
                s += f":crop_left={int(x)}:crop_top={int(y)}:crop_right={max(0, o.width - int(x) - w)}:crop_bottom={max(0, o.height - int(y) - h)}"
        return s if vfr else s + f",fps={fps}"
    # Desktop Duplication: no capture border. ddagrab paces itself with Sleep(), so it polls a bit faster
    # than the target (the process timer is raised to 1 ms, see system.raise_timer_resolution), repeating
    # the last frame while the screen is still so the timeline never stalls; the fps filter then produces
    # an exact constant frame rate.
    poll = int(math.ceil(fps * 1.25))
    s = f"ddagrab=output_idx={o.index}:framerate={poll}:draw_mouse={int(cursor)}:dup_frames=1"
    if target.crop:
        x, y, w, h = target.crop
        s += f":offset_x={int(x)}:offset_y={int(y)}:video_size={_even(w)}x{_even(h)}"
    return s + f",fps={fps}"


def scaled_size(src, limit):
    w, h = src
    lw, lh = limit
    if lw <= 0 or lh <= 0 or (w <= lw and h <= lh):
        return None
    sc = min(lw / w, lh / h)
    return _even(w * sc), _even(h * sc)


def webcam_args(ro):
    """dshow input args for the configured webcam, or [] when none is set."""
    name = ro.get("webcam_source")
    if not name:
        return []
    args = ["-f", "dshow", "-rtbufsize", "100M", "-thread_queue_size", "512"]
    fmt = ro.get("webcam_video_format", "auto")
    if fmt == "mjpeg":
        args += ["-vcodec", "mjpeg"]
    elif fmt == "yuyv":
        args += ["-pixel_format", "yuyv422"]
    w, h, fps = ro.get("webcam_camera_width", 0), ro.get("webcam_camera_height", 0), ro.get("webcam_camera_fps", 0)
    if w and h:
        args += ["-video_size", f"{w}x{h}"]
    if fps:
        args += ["-framerate", str(fps)]
    return args + ["-i", f"video={name}"]


def video_chain(target, ro, family, webcam_index=None, backend="dda"):
    vfr = ro["framerate_mode"] in ("vfr", "content")
    src = capture_filter(target, int(ro["fps"]), ro["record_cursor"], vfr, backend)
    size = (_even(target.size[0]), _even(target.size[1]))
    scale = scaled_size(size, (ro["video_width"], ro["video_height"])) if ro["change_video_resolution"] else None
    full = ro["color_range"] == "full"
    if family == "qsv" and target.output.vendor == system.INTEL and not full and webcam_index is None:
        return src + ",hwmap=derive_device=qsv,format=qsv" + (f",scale_qsv=w={scale[0]}:h={scale[1]}" if scale else "")
    chain = src + ",hwdownload,format=bgra"
    if scale or full:
        chain += f",scale={(scale or size)[0]}:{(scale or size)[1]}:flags=bilinear" + (":out_range=full" if full else "")
    pix = {"nvenc": "bgr0", "cpu": "yuv420p"}.get(family, "nv12")
    if full and pix == "bgr0":
        pix = "yuv420p"
    if webcam_index is not None:
        ow, oh = scale or size
        cw = _even(ow * ro.get("webcam_width", 30) / 100)
        cam_w, cam_h = ro.get("webcam_camera_width") or 16, ro.get("webcam_camera_height") or 9
        ch = _even(cw * cam_h / cam_w)
        x = int(ow * ro.get("webcam_x", 0) / 100)
        y = int(oh * ro.get("webcam_y", 0) / 100)
        flip = ",hflip" if ro.get("webcam_flip_horizontally") else ""
        return (f"{chain}[scr];[{webcam_index}:v]scale={cw}:{ch}{flip},format=bgra[cam];"
                f"[scr][cam]overlay=x={x}:y={y}:eof_action=pass:shortest=0,format={pix}")
    return chain + ",format=" + pix


def quality_args(family, enc, ro):
    q = ro["video_quality"]
    if q == "custom":
        b = int(ro["video_bitrate"])
        if family == "nvenc":
            return ["-preset", "p4", "-tune", "hq", "-rc", "cbr", "-b:v", f"{b}k"]
        if family == "amf":
            return ["-quality", "balanced", "-rc", "cbr", "-b:v", f"{b}k"]
        base = ["-b:v", f"{b}k", "-maxrate:v", f"{b}k", "-bufsize:v", f"{b * 2}k"]
        if family == "qsv":
            return ["-preset", "faster"] + base
        return (["-preset", "veryfast"] if enc == "libx264" else ["-preset", "ultrafast"] if enc == "libx265" else ["-preset", "10"]) + base
    v = CQ.get(q, 24)
    if family == "qsv":
        return ["-preset", "faster", "-global_quality:v", str(v)]
    if family == "nvenc":
        return ["-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", str(v), "-b:v", "0"]
    if family == "amf":
        return ["-quality", "balanced", "-rc", "cqp", "-qp_i", str(v), "-qp_p", str(v)]
    if enc == "libsvtav1":
        return ["-preset", "10", "-crf", str(v + 8)]
    return ["-preset", "veryfast" if enc == "libx264" else "ultrafast", "-crf", str(v)]


class Session:
    """One ffmpeg pipeline. kind: replay | record | stream."""

    def __init__(self, kind, ro, target, caps, ffmpeg, *, replay_time=60, container="mp4", stream_url=None,
                 stream_format="flv", exclude_metadata=False, force_h264=False):
        self.kind = kind
        self.ro = ro
        self.target = target
        self.ffmpeg = ffmpeg
        self.replay_time = int(replay_time)
        self.container = container
        self.stream_url = stream_url
        self.stream_format = stream_format
        self.exclude_metadata = exclude_metadata
        codec = "h264" if force_h264 else ro["video_codec"]
        self.family, self.encoder = resolve_encoder(codec, target.output, caps)
        # one folder per session (pid + time) so two running instances can never mix buffers
        self.workdir = DATA_DIR / f"{kind}-{os.getpid()}-{stamp()}"
        self.started_at = None
        self.error = None
        self.notice = None
        self.parts = []
        self.paused = False
        self.paused_total = 0.0
        self._paused_at = None
        self._run = 0
        self._want = False
        self._proc = None
        self._sources = []
        self._logf = None
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()   # held while a replay is being written out
        self.backend = "dda"
        self._launched_at = 0.0

    @property
    def running(self):
        return self._want

    def elapsed(self):
        if not self.started_at:
            return 0.0
        now = self._paused_at or time.time()
        return now - self.started_at - self.paused_total

    # ---------------------------------------------------------- lifecycle

    def start(self):
        cleanup_stale_sessions()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._want = True
        self.started_at = time.time()
        self._launch()
        threading.Thread(target=self._watch, daemon=True).start()

    def _open_sources(self):
        tracks = []
        for track in self.ro["audio_tracks_list"]:
            srcs = []
            for input_id in track["audio_inputs"]:
                try:
                    srcs.append(Source(input_id))
                except Exception as e:
                    self.notice = f"Audio device unavailable: {e}"
            if srcs:
                tracks.append((track.get("name", ""), srcs))
            if self.kind == "stream":
                break  # upstream streams a single audio track
        return tracks

    def _launch(self):
        self._run += 1
        if self.kind == "replay":
            target = self.workdir / f"r{self._run}_%03d.ts"
        elif self.kind == "record":
            target = self.workdir / f"part{self._run}.ts"
            self.parts.append(target)
        else:
            target = self.stream_url
        tracks = self._open_sources()
        self._sources = [s for _, srcs in tracks for s in srcs]
        cmd = self._command(target, tracks)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._logf = open(DATA_DIR / f"ffmpeg-{self.kind}.log", "w", encoding="utf-8", errors="replace")
        shown = [a if not (self.stream_url and a == self.stream_url) else "<stream url>" for a in cmd]
        self._logf.write(" ".join(map(str, shown)) + "\n\n")
        self._logf.flush()
        self._launched_at = time.time()
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._logf,
                                      creationflags=system.CREATE_NO_WINDOW | 0x8000)
        system.raise_timer_resolution(self._proc)
        for s in self._sources:
            s.start()
        threading.Thread(target=self._read_progress, args=(self._proc, list(self._sources)), daemon=True).start()

    def _read_progress(self, proc, sources):
        """Align audio to video once ffmpeg reports its first encoded frames."""
        fps = float(self.ro["fps"])
        aligned = False
        for raw in proc.stdout:
            if aligned:
                continue
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("frame="):
                try:
                    n = int(line[6:])
                except ValueError:
                    continue
                if n > 0:
                    v = (n + 2) / fps   # +2 frames for encoder latency
                    for s in sources:
                        s.align(v)
                    aligned = True

    def _command(self, target, tracks):
        ro = self.ro
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-progress", "pipe:1", "-stats_period", "0.05",
               "-init_hw_device", f"d3d11va=dd:{self.target.output.adapter}", "-filter_hw_device", "dd"]
        idx = 0
        # Video and every audio track get their own filtergraph: ffmpeg runs each graph on its own
        # thread, and a shared graph stalls ddagrab while it waits for real-time audio input.
        audio_graphs = []
        amaps = []
        for t, (name, srcs) in enumerate(tracks):
            parts, labels = [], []
            for s in srcs:
                cmd += s.ffmpeg_args()
                parts.append(f"[{idx}:a]asetpts=N/SR/TB,aresample=48000[t{t}i{idx}]")
                labels.append(f"[t{t}i{idx}]")
                idx += 1
            if len(labels) == 1:
                parts[-1] = parts[-1].replace(labels[0], f"[a{t}]")
            else:
                parts.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0[a{t}]")
            audio_graphs.append(";".join(parts))
            amaps.append((t, name))
        cam = webcam_args(ro)
        cmd += cam
        fps = int(ro["fps"])
        cmd += ["-filter_complex", video_chain(self.target, ro, self.family, idx if cam else None, self.backend) + "[v]"]
        for g in audio_graphs:
            cmd += ["-filter_complex", g]
        cmd += ["-map", "[v]"]
        for t, _ in amaps:
            cmd += ["-map", f"[a{t}]"]
        if ro["framerate_mode"] in ("vfr", "content"):
            cmd += ["-fps_mode", "vfr"]
        cmd += ["-c:v", self.encoder, *quality_args(self.family, self.encoder, ro), "-g:v", str(fps * SEGMENT_SECONDS)]
        if ro["color_range"] == "full":
            cmd += ["-color_range", "pc"]
        if amaps:
            use_aac = ro["audio_codec"] == "aac" or self.container in ("flv", "mov") or (
                self.kind == "stream" and self.stream_format == "flv")
            cmd += ["-c:a", "aac" if use_aac else "libopus", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
            for t, name in amaps:
                if name and not self.exclude_metadata:
                    cmd += [f"-metadata:s:a:{t}", f"title={name}"]
        if self.exclude_metadata:
            cmd += ["-map_metadata", "-1"]
        if self.kind == "replay":
            wrap = self.replay_time // SEGMENT_SECONDS + 4
            cmd += ["-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
                    "-segment_wrap", str(wrap), "-reset_timestamps", "1", str(target)]
        elif self.kind == "record":
            cmd += ["-f", "mpegts", str(target)]
        else:
            cmd += ["-f", self.stream_format, str(target)]
        return cmd

    def _teardown(self, graceful=False):
        proc, sources = self._proc, self._sources
        self._proc, self._sources = None, []
        if proc and proc.poll() is None:
            try:
                if graceful:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for s in sources:
            s.stop()
        if self._logf:
            self._logf.close()
            self._logf = None

    def _watch(self):
        failures = []
        last_prune = 0.0
        while self._want:
            time.sleep(0.5)
            with self._lock:
                if not self._want:
                    break
                if self.paused:
                    continue
                proc = self._proc
                switch = any(s.device_changed for s in self._sources)
                if proc is not None and proc.poll() is None and not switch:
                    if self.kind == "replay" and time.time() - last_prune > 10:
                        self._prune()
                        last_prune = time.time()
                    continue
                if not switch:
                    failures = [t for t in failures if time.time() - t < 30] + [time.time()]
                self._teardown()
                if len(failures) >= 5:
                    self._want = False
                    self.error = self._log_tail() or "ffmpeg keeps exiting"
                    break
            time.sleep(0 if switch else 1)
            with self._lock:
                if self._want and not self.paused:
                    try:
                        self._launch()
                    except Exception as e:
                        self.notice = str(e)

    def stop(self):
        with self._lock:
            self._want = False
            self._teardown(graceful=True)
        if self.kind != "record":   # recordings keep their parts until finish_recording()
            with self._save_lock:      # don't delete segments a save is still reading
                shutil.rmtree(self.workdir, ignore_errors=True)

    def set_paused(self, paused):
        with self._lock:
            if paused == self.paused or self.kind != "record":
                return
            self.paused = paused
            if paused:
                self._paused_at = time.time()
                self._teardown(graceful=True)
            else:
                self.paused_total += time.time() - self._paused_at
                self._paused_at = None
                self._launch()

    def _log_tail(self):
        try:
            lines = (DATA_DIR / f"ffmpeg-{self.kind}.log").read_text(encoding="utf-8", errors="replace").splitlines()
            return next((l for l in reversed(lines[1:]) if l.strip()), "")
        except OSError:
            return ""

    # ---------------------------------------------------------- replay

    def _prune(self):
        keep_after = time.time() - self.replay_time - 30
        for p in self.workdir.glob("*.ts"):
            try:
                if p.stat().st_mtime < keep_after:
                    p.unlink()
            except OSError:
                pass

    def buffered_seconds(self):
        return min(self.replay_time, time.time() - self.started_at) if self.started_at else 0.0

    def save_replay(self, dest_dir, seconds=None):
        with self._save_lock:
            return self._save_replay(dest_dir, seconds)

    def _save_replay(self, dest_dir, seconds=None):
        seconds = min(seconds or self.replay_time, self.replay_time)
        now = time.time()
        segs = []
        for p in self.workdir.glob("*.ts"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > 0 and st.st_mtime > now - seconds:
                segs.append((st.st_mtime, p))
        if not segs:
            raise RuntimeError("Replay buffer is still empty")
        segs.sort()
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = unique_path(dest_dir / f"Replay_{stamp(now)}.{CONTAINER_EXT.get(self.container, 'mp4')}")
        self._concat([p for _, p in segs], out, self.workdir / "save.txt")
        return out, min(seconds, self.buffered_seconds())

    def clear_replay(self):
        now = time.time()
        for p in self.workdir.glob("*.ts"):
            try:
                if p.stat().st_mtime < now - 0.5:
                    p.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------- recording

    def finish_recording(self, dest_dir):
        parts = [p for p in self.parts if p.exists() and p.stat().st_size > 0]
        if not parts:
            raise RuntimeError("Nothing was recorded")
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = unique_path(dest_dir / f"Video_{stamp(self.started_at)}.{CONTAINER_EXT.get(self.container, 'mp4')}")
        self._concat(parts, out, self.workdir / "parts.txt")
        shutil.rmtree(self.workdir, ignore_errors=True)
        return out

    def _concat(self, files, out, listfile):
        listfile.write_text("".join("file '{}'\n".format(str(f).replace("'", "'\\''")) for f in files), encoding="utf-8")
        args = ["-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-map", "0", "-c", "copy"]
        if out.suffix in (".mp4", ".mov"):
            args += ["-movflags", "+faststart"]
        res = system.run_ffmpeg(self.ffmpeg, args + [str(out)], timeout=900)
        if res.returncode != 0 or not out.exists():
            lines = res.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "ffmpeg failed")


def cleanup_stale_sessions():
    """Remove session folders left behind by processes that are no longer running."""
    for d in DATA_DIR.glob("*-*-*"):
        parts = d.name.split("-")
        if not d.is_dir() or len(parts) < 3 or not parts[1].isdigit():
            continue
        if int(parts[1]) != os.getpid() and not system.pid_alive(int(parts[1])):
            shutil.rmtree(d, ignore_errors=True)


def take_screenshot(ffmpeg, target, opts, out_path):
    quality = {"medium": 8, "high": 5, "very_high": 3, "ultra": 1}.get(opts["image_quality"], 3)
    chain = capture_filter(target, 30, opts["record_cursor"], True, "dda") + ",hwdownload,format=bgra"
    if opts["change_image_resolution"]:
        sc = scaled_size(target.size, (opts["image_width"], opts["image_height"]))
        if sc:
            chain += f",scale={sc[0]}:{sc[1]}:flags=lanczos"
    args = ["-loglevel", "error", "-y", "-init_hw_device", f"d3d11va=dd:{target.output.adapter}",
            "-filter_hw_device", "dd", "-filter_complex", chain, "-frames:v", "1"]
    if out_path.suffix == ".jpg":
        args += ["-pix_fmt", "yuvj444p", "-q:v", str(quality)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res = system.run_ffmpeg(ffmpeg, args + [str(out_path)], timeout=20)
    if res.returncode != 0 or not out_path.exists():
        lines = res.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "screenshot failed")
    return out_path
