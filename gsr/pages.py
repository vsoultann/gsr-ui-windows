"""Ports of gsr-ui's SettingsPage, ScreenshotSettingsPage and GlobalSettingsPage."""
import re

from . import gui, system
from .config import TR, TRF, default_hotkeys
from .gui import (Button, CheckBox, ComboBox, CustomRenderer, Entry, GsrPage, Label, LineSeparator,
                  List, RadioButton, ScrollablePage, StaticPage, Subsection, Text, color_theme, theme)

V, HZ = List.VERTICAL, List.HORIZONTAL


def body():
    return "body"


def fsize():
    return theme.font_size("body")


def label(text):
    return Label("body", text, color_theme.text_color)


def vlist(*widgets):
    l = List(V)
    for w in widgets:
        l.add_widget(w)
    return l


def hlist(*widgets, center=False):
    l = List(HZ, List.CENTER if center else List.START)
    for w in widgets:
        l.add_widget(w)
    return l


def entry(text, width_mult):
    return Entry("body", text, 2.0 * fsize() * width_mult)


def hotkey_display(combo):
    """ConfigHotkey::to_string(false, false): 'Alt+F10'."""
    if not combo:
        return ""
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Super", "printscreen": "Printscreen",
             "pageup": "Page Up", "pagedown": "Page Down", "scrolllock": "Scroll Lock"}
    parts = combo.split("+")
    order = [m for m in ("ctrl", "shift", "alt", "win") if m in parts[:-1]]
    key = parts[-1]
    key = names.get(key, key.upper() if len(key) <= 3 else key.capitalize())
    return "+".join([names[m] for m in order] + [key])


def capture_items(box, outputs, window=True, region=True):
    if window:
        box.add_item(TR("Window"), "window")
    if region:
        box.add_item(TR("Region"), "region")
    box.add_item(TR("Focused monitor"), "focused_monitor")
    for o in outputs:
        box.add_item(TRF("Monitor %s (%dx%d)", o.label, o.width, o.height), o.device)


class SettingsPage(StaticPage):
    REPLAY, RECORD, STREAM = "replay", "record", "streaming"
    TITLES = {REPLAY: "Instant Replay", RECORD: "Record", STREAM: "Livestream"}

    def __init__(self, type_, app, page_stack):
        super().__init__((float(int(theme.window_width)), float(int(theme.window_height))))
        self.type = type_
        self.app = app
        self.cfg = app.cfg
        self.page_stack = page_stack
        self.on_config_changed = None
        self.audio_devices = app.audio_devices()
        self.content = GsrPage(TR(self.TITLES[type_]), TR("Settings"))
        self.content.add_button(TR("Back"), "back", color_theme.page_bg_color)
        self.content.on_click = lambda id_: id_ == "back" and page_stack.pop()
        self.add_widget(self.content)
        self.estimated = None
        self._build()
        {self.REPLAY: self._add_replay, self.RECORD: self._add_record, self.STREAM: self._add_stream}[type_]()
        self.load()

    # ------------------------------------------------------------ common sections

    def ro(self):
        return self.cfg[self.type]["record_options"]

    def _build(self):
        page_list = List(V)
        page_list.set_spacing(0.018)
        self.view_radio = RadioButton("body", RadioButton.HORIZONTAL)
        self.view_radio.add_item(TR("Simple view"), "simple")
        self.view_radio.add_item(TR("Advanced view"), "advanced")
        self.view_radio.set_horizontal_alignment(gui.Widget.CENTER)
        page_list.add_widget(self.view_radio)
        inner = self.content.get_inner_size()
        self.scroll = ScrollablePage((inner[0], inner[1] - (page_list.get_size()[1] + 0.018 * theme.window_height)))
        page_list.add_widget(self.scroll)
        self.settings_list = List(V)
        self.settings_list.set_spacing(0.018)
        self.settings_list.add_widget(self._capture_section())
        self.settings_list.add_widget(self._webcam_section())
        self.settings_list.add_widget(self._audio_section())
        self.settings_list.add_widget(self._video_section())
        self.scroll.add_widget(self.settings_list)
        self.content.add_widget(page_list)

        def area_changed(_, id_):
            self.video_res_list.set_visible(self.change_res.is_checked())
            return True
        self.record_area.on_selection_changed = area_changed
        self.change_res.on_changed = lambda c: self.video_res_list.set_visible(c)

        def quality_changed(_, id_):
            self.bitrate_list.set_visible(id_ == "custom")
            if self.estimated:
                self.estimated.set_visible(id_ == "custom")
            return True
        self.quality_box.on_selection_changed = quality_changed
        self.record_area.set_selected_item("focused_monitor")

    def _subsection(self, title, inner):
        return Subsection(TR(title) if title else "", inner, (self.scroll.get_inner_size()[0], 0.0))

    def _capture_section(self):
        self.record_area = ComboBox("body")
        capture_items(self.record_area, self.app.outputs)
        self.video_w = entry("1920", 4)
        self.video_w.set_number_mode(True, 1, 1 << 15)
        self.video_h = entry("1080", 4)
        self.video_h.set_number_mode(True, 1, 1 << 15)
        self.video_res_list = vlist(label(TR("Video resolution limit:")),
                                    hlist(self.video_w, label("x"), self.video_h, center=True))
        row = hlist(vlist(label(TR("Capture source:")), self.record_area), self.video_res_list, center=True)
        self.change_res = CheckBox("body", TR("Change video resolution"))
        return self._subsection("Capture", vlist(row, self.change_res))

    # webcam -------------------------------------------------------------

    def _webcam_section(self):
        self.cameras = self.app.cameras()
        self.webcam_box = ComboBox("body")
        self.webcam_box.add_item(TR("None"), "")
        for cam in self.cameras:
            self.webcam_box.add_item(cam["name"], cam["name"])
        self.webcam_format_box = ComboBox("body")
        self.webcam_setup_box = ComboBox("body")
        self.flip_cam = CheckBox("body", TR("Flip camera horizontally"))
        self.cam_selected = None
        self.cam_setup = None
        inner_w = self.scroll.get_inner_size()[0]
        sw = min(400.0, inner_w * 0.9)
        self.cam_screen = (sw, sw * 0.5625)
        self.cam_inner = (self.cam_screen[0] - 4, self.cam_screen[1] - 4)
        self.cam_pos = [0.0, 0.0]
        self.cam_size = [self.cam_inner[0] * 0.3, self.cam_inner[1] * 0.3]
        self.cam_moving = False
        self.cam_resizing = False
        self.cam_drawn = ((0, 0), (0, 0))
        loc = CustomRenderer(self.cam_screen)
        loc.draw_handler = self._draw_cam
        loc.event_handler = self._cam_event
        self.webcam_body = vlist(loc, label(TR("* Right click in the bottom right corner to resize the webcam")),
                                 self.flip_cam,
                                 hlist(vlist(label(TR("Video format:")), self.webcam_format_box),
                                       vlist(label(TR("Video setup:")), self.webcam_setup_box)))
        self.webcam_body.set_visible(False)

        def source_changed(_, id_):
            self.cam_selected = None
            self.cam_setup = None
            self.webcam_format_box.clear_items()
            cam = next((c for c in self.cameras if c["name"] == id_), None)
            self.webcam_body.set_visible(bool(cam))
            if not cam:
                return True
            self.webcam_format_box.add_item(TR("Auto (recommended)"), "auto")
            if cam["yuyv"]:
                self.webcam_format_box.add_item(TR("YUYV (Fastest)"), "yuyv")
            if cam["mjpeg"]:
                self.webcam_format_box.add_item(TR("Motion-JPEG (Slower)"), "mjpeg")
            self.cam_selected = cam
            self.webcam_format_box.set_selected_item(self.ro().get("webcam_video_format", "auto"), True, True)
            return True

        def format_changed(_, id_):
            cam = self.cam_selected
            self.webcam_setup_box.clear_items()
            if not cam:
                return True
            setups = cam["yuyv"] if id_ == "yuyv" else cam["mjpeg"] if id_ == "mjpeg" else cam["yuyv"] + cam["mjpeg"]
            seen = []
            for s in sorted(setups, key=lambda s: (s[0] * s[1], s[2]), reverse=True):
                if s not in seen:
                    seen.append(s)
                    self.webcam_setup_box.add_item("%dx%d@%dhz" % s, "%dx%d@%dhz" % s)
            ro = self.ro()
            want = "%dx%d@%dhz" % (ro.get("webcam_camera_width", 0), ro.get("webcam_camera_height", 0), ro.get("webcam_camera_fps", 0))
            self.webcam_setup_box.set_selected_item(self.webcam_setup_box.get_selected_id(), True, True)
            self.webcam_setup_box.set_selected_item(want)
            return True

        def setup_changed(_, id_):
            m = re.match(r"(\d+)x(\d+)@(\d+)hz", id_ or "")
            self.cam_setup = (int(m[1]), int(m[2]), int(m[3])) if m else None
            return True

        self.webcam_box.on_selection_changed = source_changed
        self.webcam_format_box.on_selection_changed = format_changed
        self.webcam_setup_box.on_selection_changed = setup_changed
        return self._subsection("Webcam", vlist(vlist(label(TR("Webcam source:")), self.webcam_box), self.webcam_body))

    def _clamp_aspect(self, size):
        cw, ch = self.cam_setup[0], self.cam_setup[1]
        if cw <= 0 or ch <= 0:
            return size
        w = size[0]
        h = w * ch / cw
        if h > size[1]:
            h = size[1]
            w = h * cw / ch
        return [w, h]

    def _draw_cam(self, win, pos, size):
        if not self.cam_selected or not self.cam_setup:
            return
        pos, size = gui.fl(pos), gui.fl(size)
        mx, my = win.mouse
        min_size = self._clamp_aspect([self.cam_inner[0] * 0.2, self.cam_inner[1] * 0.2])
        if self.cam_moving:
            self.cam_pos = [mx - 2 - self._grab[0] - pos[0], my - 2 - self._grab[1] - pos[1]]
        elif self.cam_resizing:
            self.cam_size = [self._size_start[0] + mx - self._resize_start[0], self._size_start[1] + my - self._resize_start[1]]
        self.cam_size = self._clamp_aspect(self.cam_size)
        iw, ih = self.cam_inner
        self.cam_pos[0] = min(max(0.0, self.cam_pos[0]), iw - self.cam_size[0])
        self.cam_pos[1] = min(max(0.0, self.cam_pos[1]), ih - self.cam_size[1])
        self.cam_size[0] = min(max(min_size[0], self.cam_size[0]), iw - self.cam_pos[0])
        self.cam_size[1] = min(max(min_size[1], self.cam_size[1]), ih - self.cam_pos[1])
        self.cam_size = self._clamp_aspect(self.cam_size)
        gui.outline(win.p, pos, size, (255, 0, 0, 255), 2)
        t = Text(TR("Screen"), "body")
        t.pos = gui.fl(gui.sub(gui.add(pos, gui.mul(size, 0.5)), gui.mul(t.bounds(), 0.5)))
        t.draw(win.p)
        dp = gui.fl((pos[0] + 2 + self.cam_pos[0], pos[1] + 2 + self.cam_pos[1]))
        ds = (self.cam_size[0], self.cam_size[1])
        self.cam_drawn = (dp, ds)
        gui.outline(win.p, dp, ds, (0, 255, 0, 255), 2)
        t = Text(TR("Webcam"), "body")
        t.pos = gui.fl(gui.sub(gui.add(dp, gui.mul(ds, 0.5)), gui.mul(t.bounds(), 0.5)))
        t.draw(win.p)

    def _cam_event(self, ev, win, pos, size):
        dp, ds = self.cam_drawn
        if ev.type == gui.Event.PRESS:
            pt = (ev.x, ev.y)
            if ev.button == 1 and not self.cam_resizing:
                self.cam_moving = gui.contains(dp, ds, pt)
                self._grab = gui.sub(pt, dp)
            elif ev.button == 2 and not self.cam_moving:
                corner = (dp[0] + ds[0] - 16, dp[1] + ds[1] - 16)
                self.cam_resizing = gui.contains(corner, (30, 30), pt)
                self._resize_start = pt
                self._size_start = list(self.cam_size)
        elif ev.type == gui.Event.RELEASE:
            if ev.button == 1:
                self.cam_moving = False
            elif ev.button == 2:
                self.cam_resizing = False
        return True

    # audio --------------------------------------------------------------

    def _audio_section(self):
        self.tracks_list = List(V)
        add_track = Button("body", TR("Add audio track"), (0.0, 0.0), (0, 0, 0, 120))
        add_track.on_click = lambda: self._add_track()
        add_track.set_visible(self.type != self.STREAM)
        self.audio_codec_box = ComboBox("body")
        self.audio_codec_box.add_item(TR("Opus (Recommended)"), "opus")
        self.audio_codec_box.add_item(TR("AAC"), "aac")
        self.audio_codec_list = vlist(label(TR("Audio codec:")), self.audio_codec_box)
        self.audio_section = self._subsection("Audio", vlist(add_track, self.tracks_list, self.audio_codec_list))
        return self.audio_section

    def _device_box(self, output):
        box = ComboBox("body")
        for id_, desc in (self.audio_devices["outputs"] if output else self.audio_devices["inputs"]):
            box.add_item(desc, id_)
        return box

    def _add_device(self, inputs_list, output, selected=None):
        row = List(HZ, List.CENTER)
        row.userdata = output
        row.add_widget(label(TR("Output device:") if output else TR("Input device:   ")))
        box = self._device_box(output)
        if selected:
            box.set_selected_item(selected, False)
        row.add_widget(box)
        trash = Button("body", "", (0.0, 0.0), (0, 0, 0, 0))
        trash.set_icon("trash")
        trash.set_icon_padding_scale(0.75)
        trash.on_click = lambda: inputs_list.remove_widget(row)
        row.add_widget(trash)
        inputs_list.add_widget(row)
        return row

    def _retitle_tracks(self):
        for i, sub in enumerate(self.tracks_list.widgets):
            sub.title_label.set_text(TRF("Audio track #%d", i + 1))

    def _add_track(self, track=None):
        n = len(self.tracks_list.widgets) + 1
        inputs = List(V)
        items = List(V)
        sub = Subsection("", items, (self.audio_section.get_inner_size()[0], 0.0))
        sub.bg_color = (35, 40, 44, 255)
        title = Label("title", TRF("Audio track #%d", n), color_theme.text_color)
        sub.title_label = title
        trash = Button("body", "", (0.0, 0.0), (0, 0, 0, 0))
        trash.set_icon("trash")
        trash.set_icon_padding_scale(0.75)

        def remove():
            self.tracks_list.remove_widget(sub)
            self._retitle_tracks()
        trash.on_click = remove
        title_row = hlist(title, trash, center=True)
        title_row.set_visible(self.type != self.STREAM)
        add_out = Button("body", TR("Add output device"), (0.0, 0.0), (0, 0, 0, 120))
        add_out.on_click = lambda: self._refresh_devices() or self._add_device(inputs, True)
        add_in = Button("body", TR("Add input device"), (0.0, 0.0), (0, 0, 0, 120))
        add_in.on_click = lambda: self._refresh_devices() or self._add_device(inputs, False)
        items.add_widget(title_row)
        items.add_widget(hlist(add_out, add_in, center=True))
        items.add_widget(inputs)
        sub.inputs = inputs
        sub.track_name = (track or {}).get("name", "")
        for input_id in (track or {}).get("audio_inputs", []):
            is_out = input_id == "default_output" or any(i == input_id for i, _ in self.audio_devices["outputs"])
            self._add_device(inputs, is_out, input_id)
        self.tracks_list.add_widget(sub)
        return sub

    def _refresh_devices(self):
        self.audio_devices = self.app.audio_devices()

    # video --------------------------------------------------------------

    def _video_section(self):
        self.quality_box = ComboBox("body")
        rec = self.type in (self.REPLAY, self.STREAM)
        self.quality_box.add_item(TR("Constant bitrate (Recommended)") if rec else TR("Constant bitrate"), "custom")
        self.quality_box.add_item(TR("Medium"), "medium")
        self.quality_box.add_item(TR("High"), "high")
        self.quality_box.add_item(TR("Very high") if rec else TR("Very high (Recommended)"), "very_high")
        self.quality_box.add_item(TR("Ultra"), "ultra")
        self.quality_box.set_selected_item("custom" if rec else "very_high", False)
        self.bitrate = entry("8000", 5)
        self.bitrate.set_number_mode(True, 1, 500000)
        bitrate_row = hlist(self.bitrate, center=True)
        if self.type == self.STREAM:
            mbps = label("")
            bitrate_row.add_widget(mbps)
            self.bitrate.on_changed = lambda t: mbps.set_text("%.2fMbps" % (int(t or 0) / 1024.0))
        self.bitrate_list = vlist(label(TR("Video bitrate (Kbps):")), bitrate_row)
        self.color_range_box = ComboBox("body")
        self.color_range_box.add_item(TR("Limited"), "limited")
        self.color_range_box.add_item(TR("Full"), "full")
        self.color_range_list = vlist(label(TR("Color range:")), self.color_range_box)
        quality_row = hlist(vlist(label(TR("Video quality:")), self.quality_box), self.bitrate_list, self.color_range_list)

        self.codec_box = ComboBox("body")
        sup = self.app.supported_codecs()
        self.codec_box.add_item(TR("Auto (Recommended)"), "auto")
        if sup.get("h264"):
            self.codec_box.add_item(TR("H264"), "h264")
        if sup.get("hevc"):
            self.codec_box.add_item(TR("HEVC"), "hevc")
        if sup.get("av1"):
            self.codec_box.add_item(TR("AV1"), "av1")
        if sup.get("h264_software"):
            self.codec_box.add_item(TR("H264 Software Encoder (Slow, not recommended)"), "h264_software")
        self.codec_list = vlist(label(TR("Video codec:")), self.codec_box)

        self.fps_entry = entry("60", 3)
        self.fps_entry.set_number_mode(True, 1, 500)
        self.fps_mode_box = ComboBox("body")
        self.fps_mode_box.add_item(TR("Auto (Recommended)"), "auto")
        self.fps_mode_box.add_item(TR("Constant"), "cfr")
        self.fps_mode_box.add_item(TR("Variable"), "vfr")
        self.fps_mode_box.add_item(TR("Sync to content"), "content")
        self.fps_mode_list = vlist(label(TR("Frame rate mode:")), self.fps_mode_box)
        fps_row = hlist(vlist(label(TR("Frame rate:")), self.fps_entry), self.fps_mode_list)
        self.cursor_cb = CheckBox("body", TR("Record cursor"))
        self.cursor_cb.set_checked(True)
        return self._subsection("Video", vlist(quality_row, self.codec_list, fps_row, self.cursor_cb))

    def view_changed(self, advanced):
        for w in (self.color_range_list, self.audio_codec_list, self.codec_list, self.fps_mode_list):
            w.set_visible(advanced)
        self.scroll.reset_scroll()

    # ------------------------------------------------------------ page specific

    def _save_dir_section(self, text):
        btn = Button("body", self.cfg[self.type].get("save_directory", ""), (0.0, 0.0), (0, 0, 0, 120))
        self.save_dir_btn = btn

        def choose():
            page = GsrPage(TR("File"), TR("Settings"))
            page.add_button(TR("Save"), "save", color_theme.tint_color)
            page.add_button(TR("Cancel"), "cancel", color_theme.page_bg_color)
            chooser = gui.FileChooser(btn.get_text(), page.get_inner_size())
            page.add_widget(chooser)

            def clicked(id_):
                if id_ == "save":
                    btn.set_text(chooser.get_current_directory())
                self.page_stack.pop()
            page.on_click = clicked
            holder = StaticPage(self.size)
            holder.add_widget(page)
            self.page_stack.push(holder)
        btn.on_click = choose
        return vlist(label(text), btn)

    def _container_section(self):
        self.container_box = ComboBox("body")
        for name, id_ in (("mp4", "mp4"), ("mkv", "matroska"), ("flv", "flv"), ("mov", "mov")):
            self.container_box.add_item(name, id_)
        return vlist(label(TR("Container:")), self.container_box)

    def _indicator(self, notif_text, led_text):
        self.notif_cb = CheckBox("body", TR(notif_text))
        self.notif_cb.set_checked(True)
        self.led_cb = CheckBox("body", TR(led_text))
        return vlist(self.notif_cb, self.led_cb)

    def _wire_view(self):
        def changed(_, id_):
            self.view_changed(id_ == "advanced")
            return True
        self.view_radio.on_selection_changed = changed

    def _add_replay(self):
        self.replay_time = entry("60", 4)
        self.replay_time.set_number_mode(True, 1, 86400)
        self.replay_time_label = label("00h:00m:00s")
        file_row = hlist(self._save_dir_section(TR("Directory to save replays:")), self._container_section(),
                         vlist(label(TR("Replay duration in seconds:")), hlist(self.replay_time, self.replay_time_label, center=True)))
        self.estimated = label("")
        self.settings_list.add_widget(self._subsection("File info", vlist(file_row, self.estimated)))
        self.storage_radio = RadioButton("body", RadioButton.HORIZONTAL)
        self.storage_radio.add_item(TR("RAM"), "ram")
        self.storage_radio.add_item(TR("Disk (Not recommended on SSDs)"), "disk")
        self.storage_radio.on_selection_changed = lambda _, id_: self._update_estimate(id_) or True
        self.game_folder_cb = CheckBox("body", TR("Save video in a folder based on the games name%s") % "")
        self.restart_cb = CheckBox("body", TR("Restart replay on save"))
        general = vlist(vlist(label(TR("Where should temporary replay data be stored?")), self.storage_radio),
                        self.game_folder_cb, self.restart_cb)
        self.settings_list.add_widget(self._subsection("General", general))
        self.settings_list.add_widget(self._subsection("Replay indicator", self._indicator(
            "Show replay notifications", "Show replay status with scroll lock LED")))
        self.autostart_radio = RadioButton("body", RadioButton.VERTICAL)
        self.autostart_radio.add_item(TR("Don't turn on replay automatically"), "dont_turn_on_automatically")
        self.autostart_radio.add_item(TR("Turn on replay when this program starts"), "turn_on_at_system_startup")
        self.autostart_radio.add_item(TR("Turn on replay when starting a game"), "turn_on_at_game_launch")
        self.power_cb = CheckBox("body", TR("Only turn on replay if a power supply is connected"))
        self.settings_list.add_widget(self._subsection("Autostart", vlist(self.autostart_radio, self.power_cb)))
        self._wire_view()

        def time_changed(t):
            self._update_estimate(self.storage_radio.get_selected_id())
            s = int(t or 0)
            self.replay_time_label.set_text("%02dh:%02dm:%02ds" % (s // 3600, s // 60 % 60, s % 60))
        self.replay_time.on_changed = time_changed
        self.bitrate.on_changed = lambda t: self._update_estimate(self.storage_radio.get_selected_id())

    def _update_estimate(self, storage):
        if not self.estimated:
            return
        if self.type == self.REPLAY:
            mb = int(self.replay_time.get_text() or 0) * int(self.bitrate.get_text() or 0) / 8192.0
            self.estimated.set_text(TR("Estimated video max file size %s: %.2fMB.\nChange video bitrate or replay duration to change file size.")
                                    % (TR("in RAM") if storage == "ram" else TR("on disk"), mb))
        else:
            bps = int(self.bitrate.get_text() or 0) * 1000 // 8
            self.estimated.set_text(TR("Estimated video file size per minute (excluding audio): %.2fMB") % (60.0 * bps / 1e6 * 1.024))

    def _add_record(self):
        file_row = hlist(self._save_dir_section(TR("Directory to save videos:")), self._container_section())
        self.estimated = label("")
        self.settings_list.add_widget(self._subsection("File info", vlist(file_row, self.estimated)))
        self.game_folder_cb = CheckBox("body", TR("Save video in a folder based on the games name%s") % "")
        self.settings_list.add_widget(self._subsection("General", vlist(self.game_folder_cb)))
        self.settings_list.add_widget(self._subsection("Recording indicator", self._indicator(
            "Show recording notifications", "Show recording status with scroll lock LED")))
        self._wire_view()
        self.bitrate.on_changed = lambda t: self._update_estimate(None)

    def _key_entry_row(self, mult=0.9):
        e = entry("", 20)
        e.set_masked(True)
        s = e.get_size()[1] * mult
        btn = Button("body", "", (s, s), (0, 0, 0, 0))
        btn.set_icon("masked")

        def toggle():
            btn.set_icon("unmasked" if e.is_masked() else "masked")
            e.set_masked(not e.is_masked())
        btn.on_click = toggle
        return e, hlist(e, btn, center=True)

    def _add_stream(self):
        self.service_box = ComboBox("body")
        for name, id_ in (("Twitch", "twitch"), ("YouTube", "youtube"), ("Rumble", "rumble"), ("Kick", "kick"), ("Custom", "custom")):
            self.service_box.add_item(TR(name), id_)
        self.twitch_key, twitch_row = self._key_entry_row()
        self.youtube_key, youtube_row = self._key_entry_row()
        self.rumble_key, rumble_row = self._key_entry_row()
        self.key_list = vlist(label(TR("Stream key:")), twitch_row, youtube_row, rumble_row)
        self.kick_url = entry("", 20)
        self.kick_key, kick_row = self._key_entry_row(1.0)
        self.kick_list = vlist(hlist(vlist(label(TR("Stream URL:")), self.kick_url)), label(TR("Stream key:")), kick_row)
        self.custom_url = entry("", 20)
        self.stream_container_box = ComboBox("body")
        for name, id_ in (("mp4", "mp4"), ("flv", "flv"), ("ts", "mpegts"), ("m3u8", "hls")):
            self.stream_container_box.add_item(name, id_)
        self.custom_key, custom_row = self._key_entry_row()
        self.custom_list = vlist(hlist(vlist(label(TR("Stream URL:")), self.custom_url),
                                       vlist(label(TR("Container:")), self.stream_container_box)),
                                 label(TR("Stream key:")), custom_row)
        info = hlist(vlist(label(TR("Stream service:")), self.service_box), self.key_list, self.kick_list, self.custom_list)
        self.settings_list.add_widget(self._subsection("Streaming info", info))
        self.settings_list.add_widget(self._subsection("Streaming indicator", self._indicator(
            "Show streaming notifications", "Show streaming status with scroll lock LED")))

        def service_changed(_, id_):
            self.key_list.set_visible(id_ not in ("custom", "kick"))
            self.custom_list.set_visible(id_ == "custom")
            self.kick_list.set_visible(id_ == "kick")
            twitch_row.set_visible(id_ == "twitch")
            youtube_row.set_visible(id_ == "youtube")
            rumble_row.set_visible(id_ == "rumble")
            return True
        self.service_box.on_selection_changed = service_changed
        service_changed("Twitch", "twitch")
        self._wire_view()

    # ------------------------------------------------------------ load / save

    def load(self):
        ro = self.ro()
        sec = self.cfg[self.type]
        self.record_area.set_selected_item(ro["record_area_option"], True, True)
        self.change_res.set_checked(ro["change_video_resolution"])
        self.video_w.set_text(str(ro["video_width"] or 1920))
        self.video_h.set_text(str(ro["video_height"] or 1080))
        self.webcam_box.set_selected_item(ro.get("webcam_source", ""), True, True)
        self.flip_cam.set_checked(ro.get("webcam_flip_horizontally", False))
        if self.cam_setup:
            iw, ih = self.cam_inner
            self.cam_pos = [ro.get("webcam_x", 0) / 100 * iw, ro.get("webcam_y", 0) / 100 * ih]
            self.cam_size = [ro.get("webcam_width", 30) / 100 * iw, ro.get("webcam_height", 30) / 100 * ih]
        for track in ro["audio_tracks_list"]:
            self._add_track(track)
            if self.type == self.STREAM:
                break
        if self.type == self.STREAM and not self.tracks_list.widgets:
            self._add_track()
        self.audio_codec_box.set_selected_item(ro["audio_codec"], False)
        self.quality_box.set_selected_item(ro["video_quality"], True, True)
        self.bitrate.set_text(str(ro["video_bitrate"]))
        self.color_range_box.set_selected_item(ro["color_range"], False)
        self.codec_box.set_selected_item(ro["video_codec"], False)
        self.fps_entry.set_text(str(ro["fps"]))
        self.fps_mode_box.set_selected_item(ro["framerate_mode"], False)
        self.cursor_cb.set_checked(ro["record_cursor"])
        self.notif_cb.set_checked(ro["show_notifications"])
        self.led_cb.set_checked(ro["use_led_indicator"])
        self.view_radio.set_selected_item("advanced" if ro["advanced_view"] else "simple", True, True)
        if self.type == self.REPLAY:
            self.container_box.set_selected_item(sec["container"], False)
            self.storage_radio.set_selected_item(sec["replay_storage"], False)
            self.replay_time.set_text(str(sec["replay_time"]))
            self.game_folder_cb.set_checked(sec["save_video_in_game_folder"])
            self.restart_cb.set_checked(sec["restart_replay_on_save"])
            self.autostart_radio.set_selected_item(sec["turn_on_replay_automatically_mode"], False)
            self.power_cb.set_checked(sec["only_start_replay_if_power_supply_connected"])
            self._update_estimate(sec["replay_storage"])
        elif self.type == self.RECORD:
            self.container_box.set_selected_item(sec["container"], False)
            self.game_folder_cb.set_checked(sec["save_video_in_game_folder"])
            self._update_estimate(None)
        else:
            self.service_box.set_selected_item(sec["streaming_service"], True, True)
            self.twitch_key.set_text(sec["twitch"]["stream_key"])
            self.youtube_key.set_text(sec["youtube"]["stream_key"])
            self.rumble_key.set_text(sec["rumble"]["stream_key"])
            self.kick_url.set_text(sec["kick"]["stream_url"])
            self.kick_key.set_text(sec["kick"]["stream_key"])
            self.custom_url.set_text(sec["custom"]["url"])
            self.custom_key.set_text(sec["custom"]["key"])
            self.stream_container_box.set_selected_item(sec["custom"]["container"], False)
        self.quality_box.on_selection_changed("", self.quality_box.get_selected_id())

    def save(self):
        before = repr(self.cfg[self.type])
        ro = self.ro()
        sec = self.cfg[self.type]
        ro["record_area_option"] = self.record_area.get_selected_id()
        ro["change_video_resolution"] = self.change_res.is_checked()
        ro["video_width"] = int(self.video_w.get_text() or 0)
        ro["video_height"] = int(self.video_h.get_text() or 0)
        ro["webcam_source"] = self.webcam_box.get_selected_id()
        ro["webcam_flip_horizontally"] = self.flip_cam.is_checked()
        ro["webcam_video_format"] = self.webcam_format_box.get_selected_id() or "auto"
        if self.cam_setup:
            ro["webcam_camera_width"], ro["webcam_camera_height"], ro["webcam_camera_fps"] = self.cam_setup
            iw, ih = self.cam_inner
            ro["webcam_x"] = round(self.cam_pos[0] / iw * 100)
            ro["webcam_y"] = round(self.cam_pos[1] / ih * 100)
            ro["webcam_width"] = round(self.cam_size[0] / iw * 100)
            ro["webcam_height"] = round(self.cam_size[1] / ih * 100)
        tracks = []
        for sub in self.tracks_list.widgets:
            inputs = [row.widgets[1].get_selected_id() for row in sub.inputs.widgets]
            tracks.append({"name": sub.track_name, "audio_inputs": [i for i in inputs if i],
                           "application_audio_invert": False})
        ro["audio_tracks_list"] = tracks
        ro["audio_codec"] = self.audio_codec_box.get_selected_id()
        ro["video_quality"] = self.quality_box.get_selected_id()
        ro["video_bitrate"] = int(self.bitrate.get_text() or 0)
        ro["color_range"] = self.color_range_box.get_selected_id()
        ro["video_codec"] = self.codec_box.get_selected_id()
        ro["fps"] = int(self.fps_entry.get_text() or 60)
        ro["framerate_mode"] = self.fps_mode_box.get_selected_id()
        ro["record_cursor"] = self.cursor_cb.is_checked()
        ro["show_notifications"] = self.notif_cb.is_checked()
        ro["use_led_indicator"] = self.led_cb.is_checked()
        ro["advanced_view"] = self.view_radio.get_selected_id() == "advanced"
        if self.type in (self.REPLAY, self.RECORD):
            sec["save_directory"] = self.save_dir_btn.get_text()
            sec["container"] = self.container_box.get_selected_id()
            sec["save_video_in_game_folder"] = self.game_folder_cb.is_checked()
        if self.type == self.REPLAY:
            sec["replay_storage"] = self.storage_radio.get_selected_id()
            sec["replay_time"] = int(self.replay_time.get_text() or 60)
            sec["restart_replay_on_save"] = self.restart_cb.is_checked()
            sec["turn_on_replay_automatically_mode"] = self.autostart_radio.get_selected_id()
            sec["only_start_replay_if_power_supply_connected"] = self.power_cb.is_checked()
        elif self.type == self.STREAM:
            sec["streaming_service"] = self.service_box.get_selected_id()
            sec["twitch"]["stream_key"] = self.twitch_key.get_text()
            sec["youtube"]["stream_key"] = self.youtube_key.get_text()
            sec["rumble"]["stream_key"] = self.rumble_key.get_text()
            sec["kick"]["stream_url"] = self.kick_url.get_text()
            sec["kick"]["stream_key"] = self.kick_key.get_text()
            sec["custom"]["url"] = self.custom_url.get_text()
            sec["custom"]["key"] = self.custom_key.get_text()
            sec["custom"]["container"] = self.stream_container_box.get_selected_id()
        self.app.save_config()
        if repr(self.cfg[self.type]) != before and self.on_config_changed:
            self.on_config_changed()

    def on_navigate_away_from_page(self):
        self.save()


class ScreenshotSettingsPage(StaticPage):
    def __init__(self, app, page_stack):
        super().__init__((float(int(theme.window_width)), float(int(theme.window_height))))
        self.app = app
        self.cfg = app.cfg["screenshot"]
        self.page_stack = page_stack
        self.content = GsrPage(TR("Screenshot"), TR("Settings"))
        self.content.add_button(TR("Back"), "back", color_theme.page_bg_color)
        self.content.on_click = lambda id_: id_ == "back" and page_stack.pop()
        self.add_widget(self.content)
        self.scroll = ScrollablePage(self.content.get_inner_size())
        sw = (self.scroll.get_inner_size()[0], 0.0)
        lst = List(V)
        lst.set_spacing(0.018)

        self.area = ComboBox("body")
        capture_items(self.area, app.outputs)
        self.img_w = entry("1920", 4)
        self.img_w.set_number_mode(True, 1, 1 << 15)
        self.img_h = entry("1080", 4)
        self.img_h.set_number_mode(True, 1, 1 << 15)
        self.res_list = vlist(label(TR("Image resolution limit:")), hlist(self.img_w, label("x"), self.img_h, center=True))
        self.change_res = CheckBox("body", TR("Change image resolution"))
        lst.add_widget(Subsection(TR("Capture"), vlist(hlist(vlist(label(TR("Capture source:")), self.area), self.res_list, center=True),
                                                       self.change_res), sw))
        self.quality = ComboBox("body")
        for name, id_ in (("Medium", "medium"), ("High", "high"), ("Very high (Recommended)", "very_high"), ("Ultra", "ultra")):
            self.quality.add_item(TR(name), id_)
        self.cursor_cb = CheckBox("body", TR("Record cursor"))
        lst.add_widget(Subsection(TR("Image"), vlist(vlist(label(TR("Image quality:")), self.quality), self.cursor_cb), sw))
        self.dir_btn = Button("body", self.cfg["save_directory"], (0.0, 0.0), (0, 0, 0, 120))
        self.dir_btn.on_click = self._choose_dir
        self.format_box = ComboBox("body")
        self.format_box.add_item("jpg", "jpg")
        self.format_box.add_item("png", "png")
        lst.add_widget(Subsection(TR("File info"), hlist(vlist(label(TR("Directory to save screenshots:")), self.dir_btn),
                                                         vlist(label(TR("Image format:")), self.format_box)), sw))
        self.game_cb = CheckBox("body", TR("Save screenshot in a folder based on the games name"))
        self.clip_cb = CheckBox("body", TR("Save screenshot to clipboard"))
        self.disk_cb = CheckBox("body", TR("Save screenshot to disk"))
        lst.add_widget(Subsection(TR("General"), vlist(self.game_cb, self.clip_cb, self.disk_cb), sw))
        self.notif_cb = CheckBox("body", TR("Show screenshot notifications"))
        self.led_cb = CheckBox("body", TR("Blink scroll lock led when taking a screenshot"))
        lst.add_widget(Subsection(TR("Screenshot indicator"), vlist(self.notif_cb, self.led_cb), sw))
        self.script = entry("", 25)
        lst.add_widget(Subsection(TR("Script"), vlist(label(TR("Command to open the screenshot with:")), self.script), sw))
        self.scroll.add_widget(lst)
        self.content.add_widget(self.scroll)
        self.change_res.on_changed = lambda c: self.res_list.set_visible(c)
        self.area.on_selection_changed = lambda _, id_: self.res_list.set_visible(self.change_res.is_checked()) or True
        self.load()

    def _choose_dir(self):
        page = GsrPage(TR("File"), TR("Settings"))
        page.add_button(TR("Save"), "save", color_theme.tint_color)
        page.add_button(TR("Cancel"), "cancel", color_theme.page_bg_color)
        chooser = gui.FileChooser(self.dir_btn.get_text(), page.get_inner_size())
        page.add_widget(chooser)

        def clicked(id_):
            if id_ == "save":
                self.dir_btn.set_text(chooser.get_current_directory())
            self.page_stack.pop()
        page.on_click = clicked
        holder = StaticPage(self.size)
        holder.add_widget(page)
        self.page_stack.push(holder)

    def load(self):
        c = self.cfg
        self.area.set_selected_item(c["record_area_option"], True, True)
        self.change_res.set_checked(c["change_image_resolution"])
        self.img_w.set_text(str(c["image_width"] or 1920))
        self.img_h.set_text(str(c["image_height"] or 1080))
        self.quality.set_selected_item(c["image_quality"], False)
        self.cursor_cb.set_checked(c["record_cursor"])
        self.format_box.set_selected_item(c["image_format"], False)
        self.game_cb.set_checked(c["save_screenshot_in_game_folder"])
        self.clip_cb.set_checked(c["save_screenshot_to_clipboard"])
        self.disk_cb.set_checked(c["save_screenshot_to_disk"])
        self.notif_cb.set_checked(c["show_notifications"])
        self.led_cb.set_checked(c["use_led_indicator"])
        self.script.set_text(c["custom_script"])

    def on_navigate_away_from_page(self):
        c = self.cfg
        c["record_area_option"] = self.area.get_selected_id()
        c["change_image_resolution"] = self.change_res.is_checked()
        c["image_width"] = int(self.img_w.get_text() or 0)
        c["image_height"] = int(self.img_h.get_text() or 0)
        c["image_quality"] = self.quality.get_selected_id()
        c["record_cursor"] = self.cursor_cb.is_checked()
        c["save_directory"] = self.dir_btn.get_text()
        c["image_format"] = self.format_box.get_selected_id()
        c["save_screenshot_in_game_folder"] = self.game_cb.is_checked()
        c["save_screenshot_to_clipboard"] = self.clip_cb.is_checked()
        c["save_screenshot_to_disk"] = self.disk_cb.is_checked()
        c["show_notifications"] = self.notif_cb.is_checked()
        c["use_led_indicator"] = self.led_cb.is_checked()
        c["custom_script"] = self.script.get_text()
        self.app.save_config()


HOTKEY_ROWS = [
    ("main.show_hide_hotkey", "Show/hide UI:", "Show/hide UI"),
    ("replay.start_stop_hotkey", "Turn replay on/off:", "Turn replay on/off"),
    ("replay.save_hotkey", "Save replay:", "Save replay"),
    ("replay.save_1_min_hotkey", "Save 1 minute replay:", "Save 1 minute replay"),
    ("replay.save_10_min_hotkey", "Save 10 minute replay:", "Save 10 minute replay"),
    ("record.start_stop_hotkey", "Start/stop recording:", "Start/stop recording"),
    ("record.pause_unpause_hotkey", "Pause/unpause recording:", "Pause/unpause recording"),
    ("record.start_stop_region_hotkey", "Start/stop recording a region:", "Start/stop recording a region"),
    ("record.start_stop_window_hotkey", "Start/stop recording a window:", "Start/stop recording a window"),
    ("streaming.start_stop_hotkey", "Start/stop streaming:", "Start/stop streaming"),
    ("screenshot.take_screenshot_hotkey", "Take a screenshot:", "Take a screenshot"),
    ("screenshot.take_screenshot_region_hotkey", "Take a screenshot of a region:", "Take a screenshot of a region"),
    ("screenshot.take_screenshot_window_hotkey", "Take a screenshot of a window:", "Take a screenshot of a window"),
]


class GlobalSettingsPage(StaticPage):
    def __init__(self, app, page_stack):
        super().__init__((float(int(theme.window_width)), float(int(theme.window_height))))
        self.app = app
        self.cfg = app.cfg
        self.page_stack = page_stack
        self.on_language_changed = None
        self.content = GsrPage(TR("Global"), TR("Settings"))
        self.content.add_button(TR("Back"), "back", color_theme.page_bg_color)
        self.content.on_click = lambda id_: id_ == "back" and page_stack.pop()
        self.add_widget(self.content)
        self.configuring = None
        self.scroll = ScrollablePage(self.content.get_inner_size())
        sw = (self.scroll.get_inner_size()[0], 0.0)
        lst = List(V)
        lst.set_spacing(0.018)

        self.swatches = gui.ColorSwatches(list(gui.TINTS.items()))
        self.custom_tint = entry("", 4)
        self.custom_tint.max_width = 2.0 * fsize() * 4.5

        def set_tint(value):
            color_theme.tint_color = gui.parse_tint(value)
            self.cfg["main"]["tint_color"] = value
            self.swatches.selected = value if value in gui.TINTS else None

        self.swatches.on_select = set_tint

        def custom_changed(text):
            v = text.strip().lstrip("#")
            if len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v):
                set_tint("#" + v.lower())
        self._custom_changed = custom_changed
        self.custom_tint.on_changed = custom_changed
        appearance = vlist(label(TR("Accent color")), self.swatches,
                           hlist(label(TR("Custom color (hex):")), self.custom_tint, center=True))
        lst.add_widget(Subsection(TR("Appearance"), appearance, sw))

        self.startup_radio = RadioButton("body", RadioButton.HORIZONTAL)
        self.startup_radio.add_item(TR("Yes"), "start_on_system_startup")
        self.startup_radio.add_item(TR("No"), "dont_start_on_system_startup")

        def startup_changed(_, id_):
            enable = id_ == "start_on_system_startup"
            status = system.set_autostart(enable)
            if status != 0:
                app.notify(TR("Failed to add GPU Screen Recorder to system startup") if enable else
                           TR("Failed to remove GPU Screen Recorder from system startup"), 3.0,
                           (255, 255, 255, 255), (255, 0, 0, 255), "notice", error=True)
            return status == 0
        self.startup_radio.on_selection_changed = startup_changed
        lst.add_widget(Subsection(TR("Startup"), vlist(label(TR("Start program on system startup?")), self.startup_radio), sw))

        kb_list = List(V)
        kb_sub = Subsection(TR("Keyboard hotkeys"), kb_list, sw)
        self.hk_enable = RadioButton("body", RadioButton.VERTICAL)
        self.hk_enable.add_item(TR("Yes"), "enable_hotkeys")
        self.hk_enable.add_item(TR("No"), "disable_hotkeys")

        def hk_enable_changed(_, id_):
            self.cfg["main"]["hotkeys_enable_option"] = id_
            app.rebind_hotkeys()
            return True
        self.hk_enable.on_selection_changed = hk_enable_changed
        kb_list.add_widget(label(TR("Enable keyboard hotkeys?")))
        kb_list.add_widget(self.hk_enable)
        kb_list.add_widget(LineSeparator(kb_sub.get_inner_size()[0]))
        self.hk_buttons = {}
        for key, text, action in HOTKEY_ROWS:
            btn = Button("body", "", (0.0, 0.0), (0, 0, 0, 120))
            btn.on_click = lambda k=key, a=action: self.configure_hotkey_start(k, a)
            self.hk_buttons[key] = btn
            kb_list.add_widget(hlist(label(TR(text)), btn, center=True))
        kb_list.add_widget(label(TR("Press ESC to go back to the previous page/close the UI.")))
        clear = Button("body", TR("Clear hotkeys"), (0.0, 0.0), (0, 0, 0, 120))
        reset = Button("body", TR("Reset hotkeys to default"), (0.0, 0.0), (0, 0, 0, 120))

        def do_clear():
            for k in self.cfg["hotkeys"]:
                self.cfg["hotkeys"][k] = ""
            self.load_hotkeys()
            app.rebind_hotkeys()

        def do_reset():
            self.cfg["hotkeys"] = default_hotkeys()
            self.load_hotkeys()
            app.rebind_hotkeys()
        clear.on_click = do_clear
        reset.on_click = do_reset
        kb_list.add_widget(hlist(clear, reset, center=True))
        lst.add_widget(kb_sub)

        gen_list = List(V)
        gen_sub = Subsection(TR("General"), gen_list, sw)
        self.notif_speed = RadioButton("body", RadioButton.HORIZONTAL)
        self.notif_speed.add_item(TR("Normal"), "normal")
        self.notif_speed.add_item(TR("Fast"), "fast")
        self.notif_speed.on_selection_changed = lambda _, id_: self.cfg["main"].__setitem__("notification_speed", id_) or True
        self.language = ComboBox("body")
        self.language.add_item(TR("System language"), "")
        for name, code in (("English", "en"), ("Español", "es"), ("Français", "fr"), ("Magyar", "hu"), ("Português", "pt"),
                           ("日本語", "ja"), ("Русский", "ru"), ("Українська", "uk"), ("Türkçe", "tr"), ("简体中文", "zh_CN")):
            self.language.add_item(name, code)

        def lang_changed(_, id_):
            self.cfg["main"]["language"] = id_
            if self.on_language_changed:
                self.on_language_changed()
            return True
        self.language.on_selection_changed = lang_changed
        row = List(HZ)
        row.set_spacing(0.02)
        row.add_widget(vlist(label(TR("Notification speed")), self.notif_speed))
        row.add_widget(vlist(label(TR("Language")), self.language))
        gen_list.add_widget(row)
        self.metadata_cb = CheckBox("body", TR("Exclude personal metadata from the video (such as audio track title)"))
        gen_list.add_widget(self.metadata_cb)
        gen_list.add_widget(LineSeparator(gen_sub.get_inner_size()[0]))
        exit_btn = Button("body", TR("Exit program"), (0.0, 0.0), (0, 0, 0, 120))
        exit_btn.on_click = app.exit_program
        gen_list.add_widget(hlist(exit_btn))
        lst.add_widget(gen_sub)

        info = vlist(label(TRF("GSR version: %s", app.engine_version())),
                     label(TRF("GSR-UI version: %s", app.VERSION)),
                     label(TRF("GPU vendor: %s", app.gpu_vendor())))
        lst.add_widget(Subsection(TR("Application info"), info, sw))
        self.scroll.add_widget(lst)
        self.content.add_widget(self.scroll)

        overlay = CustomRenderer(self.size)
        overlay.draw_handler = self._draw_hotkey_overlay
        overlay.set_visible(False)
        self.hotkey_overlay = overlay
        self.add_widget(overlay)
        self.load()

    def load_hotkeys(self):
        for key, btn in self.hk_buttons.items():
            btn.set_text(hotkey_display(self.cfg["hotkeys"].get(key, "")))

    def load(self):
        tint = self.cfg["main"]["tint_color"] or self.app.default_tint_name()
        self.swatches.selected = tint if tint in gui.TINTS else None
        if tint.startswith("#"):
            self.custom_tint.on_changed = None
            self.custom_tint.set_text(tint)
            self.custom_tint.on_changed = self._custom_changed
        self.startup_radio.set_selected_item("start_on_system_startup" if system.autostart_enabled() else "dont_start_on_system_startup", False)
        self.hk_enable.set_selected_item(self.cfg["main"]["hotkeys_enable_option"] if self.cfg["main"]["hotkeys_enable_option"] == "disable_hotkeys" else "enable_hotkeys", False)
        self.notif_speed.set_selected_item(self.cfg["main"]["notification_speed"], False)
        self.language.set_selected_item(self.cfg["main"]["language"], False)
        self.metadata_cb.set_checked(self.cfg["main"]["exclude_metadata"])
        self.load_hotkeys()

    def on_navigate_away_from_page(self):
        self.cfg["main"]["exclude_metadata"] = self.metadata_cb.is_checked()
        self.app.save_config()
        self.app.on_global_settings_closed()

    def get_scroll_y(self):
        return self.scroll.scroll_target_y

    def set_scroll_y(self, y):
        self.scroll.scroll_y = self.scroll.scroll_target_y = y

    # hotkey configuration -----------------------------------------------

    def configure_hotkey_start(self, key, action):
        self.configuring = key
        self.configuring_action = TR(action)
        self.configuring_prev = self.cfg["hotkeys"].get(key, "")
        self.hk_buttons[key].set_text("")
        self.hotkey_overlay.set_visible(True)
        self.app.unbind_hotkeys()

    def configuring_hotkey(self):
        return self.configuring is not None

    def configure_hotkey_key(self, combo_mods, key_name):
        """Feed a key press while configuring. Returns True when finished."""
        if key_name == "escape":
            self._configure_end(self.configuring_prev)
        elif key_name == "backspace":
            self._configure_end("")
        elif key_name is None:
            self.hk_buttons[self.configuring].set_text(hotkey_display("+".join(combo_mods + ["x"]))[:-1])
            return False
        else:
            if len(key_name) == 1 and key_name.isalnum() and not combo_mods:
                return False   # alpha-numerical keys need a modifier
            self._configure_end("+".join(combo_mods + [key_name]))
        return True

    def _configure_end(self, combo):
        if combo:
            for k, v in self.cfg["hotkeys"].items():
                if v == combo and k != self.configuring:
                    self.cfg["hotkeys"][k] = ""
        self.cfg["hotkeys"][self.configuring] = combo
        self.configuring = None
        self.hotkey_overlay.set_visible(False)
        self.load_hotkeys()
        self.app.save_config()
        self.app.rebind_hotkeys()

    def _draw_hotkey_overlay(self, win, pos, size):
        if not self.configuring:
            return
        p = win.p
        H = theme.window_height
        title = Text(TRF('Press a key combination to use for the hotkey: "%s"', self.configuring_action), "title")
        hk = Text(self.hk_buttons[self.configuring].get_text(), "top_bar")
        desc = Text(TR("Alpha-numerical keys can't be used alone in hotkeys, they have to be used one or more of these keys: Alt, Ctrl, Shift and Super.\nPress Esc to cancel or Backspace to remove the hotkey."), "body")
        maxw = max(title.bounds()[0], hk.bounds()[0], desc.bounds()[0])
        ph = pv = int(H * 0.01)
        bg_size = gui.fl((maxw + ph * 2, H * 0.13))
        bg_pos = gui.fl((theme.window_width * 0.5 - bg_size[0] * 0.5, H * 0.5 - bg_size[1] * 0.5))
        gui.rect(p, bg_pos, bg_size, color_theme.page_bg_color)
        tint_size = gui.fl((bg_size[0], 0.004 * H))
        gui.rect(p, (bg_pos[0], bg_pos[1] - tint_size[1]), tint_size, color_theme.tint_color)
        title.pos = gui.fl((bg_pos[0] + bg_size[0] * 0.5 - title.bounds()[0] * 0.5, bg_pos[1] + pv))
        desc.pos = gui.fl((bg_pos[0] + bg_size[0] * 0.5 - desc.bounds()[0] * 0.5, bg_pos[1] + bg_size[1] - desc.bounds()[1] - pv))
        title.draw(p)
        tb = title.pos[1] + title.bounds()[1]
        hk.pos = gui.fl((bg_pos[0] + bg_size[0] * 0.5 - hk.bounds()[0] * 0.5, tb + (desc.pos[1] - tb) * 0.5 - hk.bounds()[1] * 0.5))
        hk.draw(p)
        caret = gui.fl((max(2.0, 0.002 * H), hk.bounds()[1]))
        gui.rect(p, gui.fl((hk.pos[0] + hk.bounds()[0] + int(0.001 * H), hk.pos[1] + hk.bounds()[1] * 0.5 - caret[1] * 0.5)), caret, (255, 255, 255, 255))
        desc.draw(p)
