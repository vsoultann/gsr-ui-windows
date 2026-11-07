"""Python/Qt port of gsr-ui's widget toolkit (gpu-screen-recorder-ui src/gui, GPL-3.0).

Every widget is immediate-mode painted with the same proportions as upstream:
sizes are fractions of the window (monitor) height, colors are the upstream
colors, and the original images are used for icons.
"""
import math
import os
import string
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QImage, QPainter

RES = Path(__file__).parent

# ---------------------------------------------------------------- theme


class ColorTheme:
    def __init__(self):
        self.tint_color = (118, 185, 0, 255)
        self.page_bg_color = (38, 43, 47, 255)
        self.text_color = (255, 255, 255, 255)


# upstream's three (ids kept for config compatibility) followed by extra accent colors
TINTS = {
    "amd": (221, 0, 49, 255), "nvidia": (118, 185, 0, 255), "intel": (8, 109, 183, 255),
    "purple": (142, 68, 240, 255), "pink": (230, 60, 150, 255), "orange": (245, 124, 0, 255),
    "yellow": (232, 184, 0, 255), "cyan": (0, 172, 193, 255), "teal": (0, 150, 120, 255),
    "white": (215, 215, 215, 255),
}


def parse_tint(value, default="amd"):
    """Accent color from a TINTS id or a '#rrggbb' string."""
    if value in TINTS:
        return TINTS[value]
    v = (value or "").strip().lstrip("#")
    if len(v) == 6:
        try:
            return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), 255)
        except ValueError:
            pass
    return TINTS[default]


def text_on(color):
    """Black or white, whichever reads better on the given background color."""
    r, g, b = color[:3]
    return (0, 0, 0, 255) if (0.299 * r + 0.587 * g + 0.114 * b) > 165 else (255, 255, 255, 255)


class Theme:
    FONT_PX_PER_UNIT = 96 / 72   # upstream font descriptions are in points (pango, 96 DPI)

    def __init__(self):
        self.window_width = 0.0
        self.window_height = 0.0
        self.fonts = {}
        self.font_units = {}
        self.textures = {}
        self.double_click_timeout_seconds = 0.4
        self.family = "Sans"

    def load(self):
        for f in (RES / "fonts").glob("*.ttf"):
            fid = QFontDatabase.addApplicationFont(str(f))
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                self.family = fams[0]
        for png in (RES / "images").glob("*.png"):
            img = QImage(str(png)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            self.textures[png.stem] = img
        for png in (RES / "images" / "notify").glob("*.png"):   # gsr-notify's own icon set
            img = QImage(str(png)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            self.textures["notify_" + png.stem] = img

    def register_font(self, key, units, bold):
        """Add a font from a pango-style size description (points), e.g. gsr-notify's 'Sans Bold N'."""
        if key in self.fonts:
            return
        f = QFont(self.family)
        f.setPixelSize(max(1, int(round(units * self.FONT_PX_PER_UNIT))))
        f.setBold(bold)
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.fonts[key] = f
        self.font_units[key] = units

    def set_window_size(self, w, h):
        if abs(w - self.window_width) < 0.1 and abs(h - self.window_height) < 0.1:
            return
        self.window_width, self.window_height = float(w), float(h)
        specs = {
            "title": (True, round(max(16.0, h * 0.019) / 1.8)),
            "top_bar": (True, round(max(23.0, h * 0.03) / 1.8)),
            "body": (False, round(max(13.0, h * 0.015) / 1.8)),
        }
        for key, (bold, units) in specs.items():
            f = QFont(self.family)
            f.setPixelSize(max(1, int(round(units * self.FONT_PX_PER_UNIT))))
            f.setBold(bold)
            f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            self.fonts[key] = f
            self.font_units[key] = units
        _text_cache.clear()

    def font_size(self, key):
        """Equivalent of mgl::Text::get_font_size_from_font_description."""
        return self.font_units[key]


theme = Theme()
color_theme = ColorTheme()


def tr_noop(s):
    return s


# set by the app to the translation function
TR = tr_noop


def set_translator(fn):
    global TR
    TR = fn


# ---------------------------------------------------------------- textures

_tint_cache = {}


def texture(name, color=(255, 255, 255, 255), w=None, h=None):
    """Texture multiplied by color (like mgl::Sprite::set_color), smoothly pre-scaled to w x h."""
    key = (name, color, int(w or 0), int(h or 0))
    img = _tint_cache.get(key)
    if img is not None:
        return img
    base = theme.textures[name]
    if w and h:
        base = base.scaled(max(1, int(w)), max(1, int(h)), Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    img = QImage(base)
    if color[:3] != (255, 255, 255):
        p = QPainter(img)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.fillRect(img.rect(), QColor(*color[:3]))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        p.drawImage(0, 0, base)
        p.end()
    if len(_tint_cache) > 600:
        _tint_cache.clear()
    _tint_cache[key] = img
    return img


def texture_size(name):
    t = theme.textures[name]
    return t.width(), t.height()


class Sprite:
    """mgl::Sprite: a texture drawn at a position, scaled by set_height/set_width keeping aspect."""

    def __init__(self, name):
        self.name = name
        self.pos = (0.0, 0.0)
        tw, th = texture_size(name)
        self.size = (float(tw), float(th))
        self.color = (255, 255, 255, 255)

    def set_height(self, h):
        tw, th = texture_size(self.name)
        self.size = (float(int(tw * h / th)), float(int(h)))

    def set_width(self, w):
        tw, th = texture_size(self.name)
        self.size = (float(int(w)), float(int(th * w / tw)))

    def set_size(self, size):
        self.size = (float(size[0]), float(size[1]))

    def draw(self, p):
        w, h = self.size
        if w < 1 or h < 1:
            return
        img = texture(self.name, self.color[:3] + (255,), w, h)
        if self.color[3] != 255:
            p.save()
            p.setOpacity(self.color[3] / 255.0)
            p.drawImage(QRectF(self.pos[0], self.pos[1], w, h), img)
            p.restore()
        else:
            p.drawImage(QRectF(self.pos[0], self.pos[1], w, h), img)


# ---------------------------------------------------------------- text

_text_cache = {}


def _metrics(font_key):
    m = _text_cache.get(("m", font_key))
    if m is None:
        m = QFontMetricsF(theme.fonts[font_key])
        _text_cache[("m", font_key)] = m
    return m


class Text:
    """mgl::Text with optional word wrap and max rows."""

    def __init__(self, s, font_key="body", color=(255, 255, 255, 255)):
        self.s = str(s)
        self.font_key = font_key
        self.color = color
        self.pos = (0.0, 0.0)
        self.wrap_width = 0
        self.max_rows = 0

    def set_string(self, s):
        self.s = str(s)

    def get_font_size(self):
        return theme.font_size(self.font_key)

    def _lines(self):
        key = ("l", self.font_key, self.s, int(self.wrap_width), self.max_rows)
        lines = _text_cache.get(key)
        if lines is not None:
            return lines
        fm = _metrics(self.font_key)
        lines = []
        for para in self.s.split("\n"):
            if not self.wrap_width or fm.horizontalAdvance(para) <= self.wrap_width:
                lines.append(para)
                continue
            cur = ""
            for word in para.split(" "):
                cand = word if not cur else cur + " " + word
                if fm.horizontalAdvance(cand) <= self.wrap_width or not cur:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        if self.max_rows and len(lines) > self.max_rows:
            lines = lines[:self.max_rows]
            lines[-1] = fm.elidedText(lines[-1] + "...", Qt.TextElideMode.ElideRight, self.wrap_width or 10 ** 6)
        _text_cache[key] = lines
        return lines

    def bounds(self):
        fm = _metrics(self.font_key)
        if not self.s:
            return (0.0, float(math.ceil(fm.height())))
        lines = self._lines()
        w = max(fm.horizontalAdvance(l) for l in lines)
        h = fm.height() * len(lines)
        return (float(math.ceil(w)), float(math.ceil(h)))

    def draw(self, p):
        if not self.s:
            return
        fm = _metrics(self.font_key)
        p.setFont(theme.fonts[self.font_key])
        p.setPen(QColor(*self.color))
        y = self.pos[1] + fm.ascent()
        for line in self._lines():
            p.drawText(QRectF(self.pos[0], y - fm.ascent(), 10 ** 5, fm.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, line)
            y += fm.height()


# ---------------------------------------------------------------- drawing helpers

def fl(v):
    return (float(math.floor(v[0])), float(math.floor(v[1])))


def add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def mul(a, s):
    if isinstance(s, tuple):
        return (a[0] * s[0], a[1] * s[1])
    return (a[0] * s, a[1] * s)


def contains(pos, size, pt):
    return pos[0] <= pt[0] < pos[0] + size[0] and pos[1] <= pt[1] < pos[1] + size[1]


def rect(p, pos, size, color):
    if size[0] <= 0 or size[1] <= 0:
        return
    p.fillRect(QRectF(pos[0], pos[1], size[0], size[1]), QColor(*color))


def outline(p, pos, size, color, border):
    pos, size = fl(pos), fl(size)
    b = float(int(border))
    rect(p, pos, (size[0], b), color)
    rect(p, (pos[0], pos[1] + size[1] - b), (size[0], b), color)
    rect(p, (pos[0], pos[1] + b), (b, size[1] - b * 2), color)
    rect(p, (pos[0] + size[0] - b, pos[1] + b), (b, size[1] - b * 2), color)


def H():
    return theme.window_height


# ---------------------------------------------------------------- events / window


class Event:
    MOVE, PRESS, RELEASE, WHEEL, KEY, TEXT = range(6)

    def __init__(self, type_, x=0, y=0, button=None, delta=0.0, key=None, text="", mods=0):
        self.type = type_
        self.x, self.y = x, y
        self.button = button
        self.delta = delta
        self.key = key
        self.text = text
        self.mods = mods


class Win:
    """What widgets need from the window: painter, mouse position, scissor stack, frame delta."""

    def __init__(self):
        self.p = None
        self.mouse = (0.0, 0.0)
        self.frame_delta = 1 / 60
        self._scissors = []

    def get_scissor(self):
        if self._scissors:
            return self._scissors[-1]
        return ((0.0, 0.0), (theme.window_width, theme.window_height))

    def push_scissor(self, pos, size):
        (px, py), (pw, ph) = self.get_scissor()
        x0, y0 = max(px, pos[0]), max(py, pos[1])
        x1, y1 = min(px + pw, pos[0] + size[0]), min(py + ph, pos[1] + size[1])
        s = ((x0, y0), (max(0.0, x1 - x0), max(0.0, y1 - y0)))
        self._scissors.append(s)
        self.p.setClipRect(QRectF(s[0][0], s[0][1], s[1][0], s[1][1]))

    def pop_scissor(self):
        self._scissors.pop()
        if self._scissors:
            (x, y), (w, h) = self._scissors[-1]
            self.p.setClipRect(QRectF(x, y, w, h))
        else:
            self.p.setClipping(False)


# ---------------------------------------------------------------- tooltip

_tooltip_widget = None


def set_current_tooltip(w):
    global _tooltip_widget
    _tooltip_widget = w if (w and w.tooltip_text) else None


def remove_as_current_tooltip(w):
    global _tooltip_widget
    if _tooltip_widget is w:
        _tooltip_widget = None


def draw_tooltip(win):
    global _tooltip_widget
    w = _tooltip_widget
    if not w:
        return
    if not w.visible:
        _tooltip_widget = None
        return
    label = Text(w.tooltip_text, "body")
    pt, pb, pl, pr = (int(H() * 0.008),) * 4
    accent_h = int(H() * 0.0025)
    icon_h = label.get_font_size() * 2.0
    ts = fl(label.bounds())
    size = fl((pl + ts[0] + pr, accent_h + pt + icon_h + ts[1] + pb))
    pos = fl(win.mouse)
    bpos = (pos[0], pos[1] - size[1])
    rect(win.p, bpos, size, (0, 0, 0, 255))
    rect(win.p, bpos, fl((size[0], accent_h)), color_theme.tint_color)
    icon = Sprite("info")
    icon.set_height(icon_h)
    icon.pos = fl(add(bpos, (pl, accent_h + pt)))
    icon.draw(win.p)
    label.pos = fl(add(bpos, (pl, accent_h + pt + icon.size[1])))
    label.draw(win.p)


# ---------------------------------------------------------------- widgets


class Widget:
    LEFT, CENTER, RIGHT = 0, 1, 2

    def __init__(self):
        self.position = (0.0, 0.0)
        self.visible = True
        self.parent_widget = None
        self.selected_child_widget = None
        self.horizontal_alignment = Widget.LEFT
        self.tooltip_text = ""

    def set_position(self, pos):
        self.position = pos

    def get_position(self):
        return self.position

    def set_visible(self, v):
        self.visible = v

    def set_tooltip_text(self, t):
        self.tooltip_text = t

    def set_horizontal_alignment(self, a):
        self.horizontal_alignment = a

    def set_widget_as_selected_in_parent(self):
        if self.parent_widget:
            self.parent_widget.selected_child_widget = self
            self.parent_widget.set_widget_as_selected_in_parent()

    def remove_widget_as_selected_in_parent(self):
        if self.parent_widget and self.parent_widget.selected_child_widget is self:
            self.parent_widget.selected_child_widget = None
            self.parent_widget.remove_widget_as_selected_in_parent()

    def has_parent_with_selected_child_widget(self):
        w = self.parent_widget
        while w:
            if w.selected_child_widget:
                return True
            w = w.parent_widget
        return False

    def handle_tooltip_event(self, ev, pos, size):
        if ev.type == Event.MOVE:
            if contains(pos, size, (ev.x, ev.y)):
                set_current_tooltip(self)
            else:
                remove_as_current_tooltip(self)

    def on_event(self, ev, win, offset):
        return True

    def draw(self, win, offset):
        pass

    def get_size(self):
        return (0.0, 0.0)

    def get_inner_size(self):
        return self.get_size()

    def on_navigate_to_page(self):
        pass

    def on_navigate_away_from_page(self):
        pass


class _Container(Widget):
    def __init__(self):
        super().__init__()
        self.widgets = []

    def add_widget(self, w):
        w.parent_widget = self
        self.widgets.append(w)
        return w

    def remove_widget(self, w):
        if w in self.widgets:
            self.widgets.remove(w)
        if self.selected_child_widget is w:
            self.selected_child_widget = None

    def clear(self):
        self.widgets.clear()
        self.selected_child_widget = None

    def _events(self, ev, win, offset):
        sel = self.selected_child_widget
        if sel and not sel.on_event(ev, win, offset):
            return False
        for w in reversed(list(self.widgets)):
            if w is not sel and not w.on_event(ev, win, offset):
                return False
        return True


class List(_Container):
    VERTICAL, HORIZONTAL = 0, 1
    START, CENTER = 0, 1

    def __init__(self, orientation=0, alignment=0):
        super().__init__()
        self.orientation = orientation
        self.content_alignment = alignment
        self.spacing_scale = 0.009

    def set_spacing(self, s):
        self.spacing_scale = s

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        return self._events(ev, win, (0.0, 0.0))

    def draw(self, win, offset):
        if not self.visible:
            return
        draw_pos = add(self.position, offset)
        sel = self.selected_child_widget
        size = self.get_size()
        parent_inner = self.parent_widget.get_inner_size() if self.parent_widget else (0.0, 0.0)
        spacing = float(int(self.spacing_scale * H()))
        first = True
        for w in self.widgets:
            if not w.visible:
                continue
            if not first:
                draw_pos = add(draw_pos, (0, spacing) if self.orientation == List.VERTICAL else (spacing, 0))
            first = False
            ws = w.get_size()
            if self.orientation == List.VERTICAL:
                if w.horizontal_alignment == Widget.CENTER and parent_inner[0] > 0.001:
                    ox = float(int(parent_inner[0] * 0.5 - ws[0] * 0.5))
                elif self.content_alignment == List.CENTER:
                    ox = float(int(size[0] * 0.5 - ws[0] * 0.5))
                else:
                    ox = 0.0
                w.set_position(add(draw_pos, (ox, 0.0)))
                if w is not sel:
                    w.draw(win, (0.0, 0.0))
                draw_pos = add(draw_pos, (0.0, ws[1]))
            else:
                oy = float(int(size[1] * 0.5 - ws[1] * 0.5)) if self.content_alignment == List.CENTER else 0.0
                w.set_position(add(draw_pos, (0.0, oy)))
                if w is not sel:
                    w.draw(win, (0.0, 0.0))
                draw_pos = add(draw_pos, (ws[0], 0.0))
        if sel:
            sel.draw(win, (0.0, 0.0))

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        spacing = float(int(self.spacing_scale * H()))
        x = y = 0.0
        first = True
        for w in self.widgets:
            if not w.visible:
                continue
            ws = w.get_size()
            if self.orientation == List.VERTICAL:
                y += (0 if first else spacing) + ws[1]
                x = max(x, ws[0])
            else:
                x += (0 if first else spacing) + ws[0]
                y = max(y, ws[1])
            first = False
        return (x, y)


class Label(Widget):
    def __init__(self, font_key, text, color=None):
        super().__init__()
        self.text = Text(text, font_key, color or color_theme.text_color)

    def draw(self, win, offset):
        if not self.visible:
            return
        self.text.pos = fl(add(self.position, offset))
        self.text.draw(win.p)

    def set_text(self, s):
        self.text.set_string(s)

    def get_text(self):
        return self.text.s

    def set_wrap_width(self, w):
        self.text.wrap_width = w

    def get_size(self):
        return self.text.bounds() if self.visible else (0.0, 0.0)


class Image(Widget):
    SCALE, CLAMP = 0, 1

    def __init__(self, name, size=(0.0, 0.0), behavior=0):
        super().__init__()
        self.name = name
        self.size = size
        self.behavior = behavior
        self.on_mouse_move = None

    def draw(self, win, offset):
        if not self.visible:
            return
        pos = fl(add(self.position, offset))
        if self.on_mouse_move:
            self.on_mouse_move(contains(pos, self.get_size(), win.mouse))
        s = Sprite(self.name)
        s.set_size(self.get_size())
        s.pos = pos
        s.draw(win.p)

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        tw, th = texture_size(self.name)
        if self.size[0] < 0.001 and self.size[1] < 0.001:
            return (float(tw), float(th))
        sc = min(self.size[0] / tw, self.size[1] / th)
        if self.behavior == Image.CLAMP and sc > 1:
            sc = 1
        return (tw * sc, th * sc)


class LineSeparator(Widget):
    def __init__(self, width):
        super().__init__()
        self.width = width

    def draw(self, win, offset):
        if self.visible:
            rect(win.p, fl(add(self.position, offset)), self.get_size(), (55, 60, 64, 255))

    def get_size(self):
        return fl((self.width, max(1.0, 0.001 * H()))) if self.visible else (0.0, 0.0)


class CustomRenderer(Widget):
    def __init__(self, size):
        super().__init__()
        self.size = size
        self.draw_handler = None
        self.event_handler = None

    def on_event(self, ev, win, offset):
        if not self.visible or not self.event_handler:
            return True
        return self.event_handler(ev, win, add(self.position, offset), self.size)

    def draw(self, win, offset):
        if self.visible and self.draw_handler:
            self.draw_handler(win, add(self.position, offset), self.size)

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)


class Button(Widget):
    PT, PB, PL, PR = 0.004629, 0.004629, 0.007, 0.007

    def __init__(self, font_key, text, size=(0.0, 0.0), bg_color=(0, 0, 0, 120)):
        super().__init__()
        self.size = size
        self.bg_color = bg_color
        self.bg_hover_color = bg_color
        self.text = Text(text, font_key)
        self.icon = None
        self.border_scale = 0.0015
        self.icon_padding_scale = 1.0
        self.on_click = None

    def set_bg_hover_color(self, c):
        self.bg_hover_color = c

    def set_icon(self, name):
        self.icon = name

    def set_icon_padding_scale(self, s):
        self.icon_padding_scale = s

    def set_border_scale(self, s):
        self.border_scale = s

    def get_text(self):
        return self.text.s

    def set_text(self, s):
        self.text.set_string(s)

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        if ev.type == Event.PRESS and ev.button == 1:
            if contains(add(self.position, offset), fl(self.get_size()), (ev.x, ev.y)):
                if self.on_click:
                    self.on_click()
                return False
        return True

    def _button_height(self):
        if self.size[1] >= 0.0001:
            return self.size[1]
        return int(self.PT * H()) + self.text.bounds()[1] + int(self.PB * H())

    def _sprite(self):
        s = Sprite(self.icon)
        bh = self._button_height()
        pad = int(0.25 * self.icon_padding_scale * bh)
        s.set_height(bh - pad * 2)
        return s

    def draw(self, win, offset):
        if not self.visible:
            return
        draw_pos = add(self.position, offset)
        size = fl(self.get_size())
        inside = contains(draw_pos, size, win.mouse) and not self.has_parent_with_selected_child_widget()
        bg_pos = fl(draw_pos)
        rect(win.p, bg_pos, size, self.bg_hover_color if inside else self.bg_color)
        self.text.color = text_on(self.bg_color) if self.bg_color[3] > 200 else color_theme.text_color
        if self.icon:
            sp = self._sprite()
            if not self.text.s:
                sp.pos = fl(sub(add(bg_pos, mul(size, 0.5)), mul(sp.size, 0.5)))
            else:
                sp.pos = fl(add(draw_pos, (int(self.PL * H()), size[1] * 0.5 - sp.size[1] * 0.5)))
            sp.draw(win.p)
            pir = int(0.15 * self._button_height())
            self.text.pos = fl(add(sp.pos, (sp.size[0] + pir, sp.size[1] * 0.5 - self.text.bounds()[1] * 0.52)))
            self.text.draw(win.p)
        else:
            self.text.pos = fl(sub(add(draw_pos, mul(size, 0.5)), mul(self.text.bounds(), 0.5)))
            self.text.draw(win.p)
        if inside:
            oc = (255, 255, 255, 255) if self.bg_color == color_theme.tint_color else color_theme.tint_color
            outline(win.p, draw_pos, size, oc, max(1.0, self.border_scale * H()))

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        tb = self.text.bounds()
        w, h = self.size
        if h < 0.0001:
            h = self._button_height()
        if w < 0.0001:
            w = int(self.PL * H()) + tb[0] + int(self.PR * H())
            if self.icon:
                w += self._sprite().size[0] + (0.15 * h if tb[0] > 0.001 else 0)
        return (w, h)


class DropdownButton(Widget):
    def __init__(self, title, description, icon, size):
        super().__init__()
        self.size = size
        self.title = Text(title, "title")
        self.description = Text(description, "body", (150, 150, 150, 255))
        self.icon = icon
        self.items = []
        self.show_dropdown = False
        self.mouse_inside = False
        self.mouse_inside_item = -1
        self.activated = False
        self.on_click = None

    def add_item(self, text, id_, description=""):
        if any(i["id"] == id_ for i in self.items):
            return
        self.items.append({"text": Text(text, "title"), "desc": Text(description, "body"),
                           "icon": None, "id": id_, "enabled": True})

    def _item(self, id_):
        return next((i for i in self.items if i["id"] == id_), None)

    def set_item_label(self, id_, s):
        if self._item(id_):
            self._item(id_)["text"].set_string(s)

    def set_item_icon(self, id_, icon):
        if self._item(id_):
            self._item(id_)["icon"] = icon

    def set_item_description(self, id_, s):
        if self._item(id_):
            self._item(id_)["desc"].set_string(s)

    def set_item_enabled(self, id_, en):
        if self._item(id_):
            self._item(id_)["enabled"] = en

    def set_description(self, s):
        self.description.set_string(s)

    def set_activated(self, a):
        self.activated = a

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        if ev.type == Event.MOVE:
            dp = add(self.position, offset)
            self.mouse_inside = contains(add(dp, (1, 1)), sub(self.size, (1, 1)), (ev.x, ev.y))
        elif ev.type == Event.PRESS and ev.button == 1:
            clicked_inside = self.mouse_inside
            if self.show_dropdown and clicked_inside and self.mouse_inside_item == -1:
                self.show_dropdown = False
                self.remove_widget_as_selected_in_parent()
                return False
            self.show_dropdown = clicked_inside
            if self.show_dropdown:
                self.set_widget_as_selected_in_parent()
            else:
                self.remove_widget_as_selected_in_parent()
            if 0 <= self.mouse_inside_item < len(self.items):
                if self.on_click:
                    self.on_click(self.items[self.mouse_inside_item]["id"])
                return False
        return True

    def draw(self, win, offset):
        if not self.visible:
            return
        p = win.p
        dp = add(self.position, offset)
        pt, pb = int(0.00694 * H()), int(0.00694 * H())
        pl, pr = int(0.009259 * H()), int(0.009259 * H())
        icon_spacing = int(0.008 * H())
        border = int(0.003 * H())
        if self.show_dropdown:
            rect(p, dp, self.size, (0, 0, 0, 255))
            rect(p, dp, (self.size[0], border), color_theme.tint_color)
        elif self.mouse_inside:
            rect(p, dp, self.size, (0, 0, 0, 255))
            outline(p, dp, self.size, color_theme.tint_color, border)
        else:
            rect(p, dp, self.size, (0, 0, 0, 180))
        self.description.color = color_theme.tint_color if self.activated else (150, 150, 150, 255)
        margin = int(self.size[1] * 0.085)
        tb = self.title.bounds()
        self.title.pos = fl(add(dp, (self.size[0] * 0.5 - tb[0] * 0.5, margin)))
        self.title.draw(p)
        db = self.description.bounds()
        self.description.pos = fl(add(dp, (self.size[0] * 0.5 - db[0] * 0.5, self.size[1] - db[1] - margin)))
        self.description.draw(p)
        icon = Sprite(self.icon)
        icon.set_height(int(self.size[1] * 0.45))
        icon.color = color_theme.tint_color if self.activated else (255, 255, 255, 255)
        icon.pos = fl(sub(add(dp, mul(self.size, 0.5)), mul(icon.size, 0.5)))
        icon.draw(p)

        self.mouse_inside_item = -1
        if self.show_dropdown:
            max_h = sum(i["text"].bounds()[1] + pt + pb for i in self.items)
            bg_pos = add(dp, (0.0, self.size[1]))
            rect(p, bg_pos, (self.size[0], max_h), (0, 0, 0, 255))
            ipos = bg_pos
            for idx, item in enumerate(self.items):
                tb = item["text"].bounds()
                isize = (self.size[0], pt + tb[1] + pb)
                if idx > 0:
                    sep_h = max(1.0, isize[1] * 0.05)
                    rect(p, fl(sub(ipos, (0, sep_h * 0.5))), fl((isize[0], sep_h)), color_theme.page_bg_color)
                if self.mouse_inside_item == -1 and item["enabled"] and contains(ipos, isize, win.mouse):
                    outline(p, ipos, isize, color_theme.tint_color, border)
                    self.mouse_inside_item = idx
                col = (255, 255, 255, 255) if item["enabled"] else (255, 255, 255, 80)
                icon_offset = 0.0
                if item["icon"]:
                    s = Sprite(item["icon"])
                    s.set_height(int(isize[1] * 0.4))
                    s.pos = fl(add(ipos, (pl, isize[1] * 0.5 - s.size[1] * 0.5)))
                    s.color = col
                    s.draw(p)
                    icon_offset = s.size[0] + icon_spacing
                item["text"].pos = fl(add(ipos, (pl + icon_offset, isize[1] * 0.5 - tb[1] * 0.5)))
                item["text"].color = col
                item["text"].draw(p)
                db = item["desc"].bounds()
                item["desc"].pos = fl(add(ipos, (isize[0] - db[0] - pr, isize[1] * 0.5 - db[1] * 0.5)))
                item["desc"].color = (255, 255, 255, 120) if item["enabled"] else (255, 255, 255, 40)
                item["desc"].draw(p)
                ipos = add(ipos, (0.0, isize[1]))

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)


class ComboBox(Widget):
    PT, PB, PL, PR = 0.004629, 0.004629, 0.007, 0.007
    MAX_VISIBLE = 10

    def __init__(self, font_key="body"):
        super().__init__()
        self.font_key = font_key
        self.items = []
        self.selected_item = 0
        self.show_dropdown = False
        self.scroll_y = 0.0
        self.scroll_target_y = 0.0
        self.dropdown_rect = ((0, 0), (0, 0))
        self.on_selection_changed = None

    def font_size(self):
        return theme.font_size(self.font_key)

    def add_item(self, text, id_):
        if any(i["id"] == id_ for i in self.items):
            return
        t = Text(text, self.font_key)
        t.wrap_width = self.font_size() * 35
        t.max_rows = 2
        self.items.append({"text": t, "id": id_, "pos": (0, 0), "enabled": True})

    def clear_items(self):
        self.items.clear()
        self.selected_item = 0
        self.show_dropdown = False

    def set_selected_item(self, id_, trigger_event=True, trigger_even_if_same=False):
        for i, item in enumerate(self.items):
            if item["id"] == id_ and item["enabled"]:
                prev = self.selected_item
                self.selected_item = i
                if trigger_event and (trigger_even_if_same or i != prev) and self.on_selection_changed:
                    self.on_selection_changed(item["text"].s, item["id"])
                break

    def set_item_enabled(self, id_, en):
        for i, item in enumerate(self.items):
            if item["id"] == id_:
                item["enabled"] = en
                item["text"].color = (255, 255, 255, 255) if en else (255, 255, 255, 80)
                if self.selected_item == i:
                    self.selected_item = 0
                    self.show_dropdown = False

    def get_selected_id(self):
        return self.items[self.selected_item]["id"] if self.items else ""

    def get_selected_text(self):
        return self.items[self.selected_item]["text"].s if self.items else ""

    def _item_h(self, item):
        return int(self.PT * H()) + item["text"].bounds()[1] + int(self.PB * H())

    def _arrow_h(self):
        return (self.font_size() * 2.0 + int(self.PT * H()) + int(self.PB * H())) * 0.4

    def _max_w(self):
        pl, pr = int(self.PL * H()), int(self.PR * H())
        w = max((i["text"].bounds()[0] + pl + pr for i in self.items), default=0.0)
        if w <= 0.001:
            w = 50.0
        return w + pl + self._arrow_h()

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        sel = self.items[self.selected_item] if self.selected_item < len(self.items) else None
        h = int(self.PT * H()) + int(self.PB * H()) + (sel["text"].bounds()[1] if sel else self.font_size())
        return (self._max_w(), h)

    def on_event(self, ev, win, offset):
        if not self.visible or not self.items:
            return True
        if self.show_dropdown and ev.type == Event.WHEEL and contains(*self.dropdown_rect, win.mouse):
            self.scroll_target_y += ev.delta * 80
            return False
        if ev.type == Event.PRESS and ev.button == 1:
            mp = (ev.x, ev.y)
            isz = self.get_size()
            if self.show_dropdown and contains(*self.dropdown_rect, mp):
                for i, item in enumerate(self.items):
                    if contains(item["pos"], (isz[0], self._item_h(item)), mp) and item["enabled"]:
                        prev = self.selected_item
                        self.selected_item = i
                        self.show_dropdown = False
                        self.remove_widget_as_selected_in_parent()
                        if i != prev and self.on_selection_changed:
                            self.on_selection_changed(item["text"].s, item["id"])
                        return False
            dp = add(self.position, offset)
            if contains(dp, isz, mp):
                self.show_dropdown = not self.show_dropdown
                if self.show_dropdown:
                    self.set_widget_as_selected_in_parent()
                    self.scroll_y = self.scroll_target_y = 0.0
                else:
                    self.remove_widget_as_selected_in_parent()
            else:
                self.show_dropdown = False
                self.remove_widget_as_selected_in_parent()
        return True

    def draw(self, win, offset):
        if not self.visible:
            return
        dp = fl(add(self.position, offset))
        (self._draw_selected if self.show_dropdown else self._draw_unselected)(win, dp)

    def _draw_unselected(self, win, dp):
        p = win.p
        pt, pl, pr = int(self.PT * H()), int(self.PL * H()), int(self.PR * H())
        isz = self.get_size()
        rect(p, fl(dp), fl(isz), (0, 0, 0, 120))
        arrow = Sprite("combobox_arrow")
        arrow.set_height(self._arrow_h())
        arrow.pos = fl(add(dp, (isz[0] - arrow.size[0] - pr, isz[1] * 0.5 - arrow.size[1] * 0.5)))
        arrow.color = (255, 255, 255, 30)
        arrow.draw(p)
        if self.selected_item < len(self.items):
            if contains(dp, isz, win.mouse) and not self.has_parent_with_selected_child_widget():
                outline(p, dp, isz, color_theme.tint_color, max(1.0, 0.0015 * H()))
            t = self.items[self.selected_item]["text"]
            t.pos = fl(add(dp, (pl, pt)))
            t.draw(p)

    def _draw_selected(self, win, dp):
        p = win.p
        pt, pl = int(self.PT * H()), int(self.PL * H())
        (sx, sy), (sw, sh) = win.get_scissor()
        isz = self.get_size()
        max_w = self._max_w()
        total = sum(self._item_h(i) for i in self.items)
        wanted = sum(self._item_h(i) for i in self.items[:self.MAX_VISIBLE])
        below = sy + sh - (dp[1] + isz[1])
        above = dp[1] - sy
        flip = False
        if dp[1] + isz[1] + wanted <= sy + sh:
            lh = wanted
        elif above > below:
            flip, lh = True, min(wanted, above)
        else:
            lh = min(wanted, below)
        lh = max(0.0, lh)
        top = dp[1] - lh if flip else dp[1] + isz[1]
        max_scroll = max(0.0, total - lh)
        has_sb = max_scroll > 0.001
        sbw = max(5.0, 0.004 * H()) if has_sb else 0.0
        self.dropdown_rect = ((dp[0], top), (max_w, lh))
        diff = self.scroll_target_y - self.scroll_y
        self.scroll_y = self.scroll_target_y if abs(diff) < 0.1 else self.scroll_y + diff * min(1.0, win.frame_delta * 10)
        self.scroll_y = min(0.0, max(-max_scroll, self.scroll_y))
        self.scroll_target_y = min(0.0, max(-int(max_scroll), self.scroll_target_y))
        rt, rb = min(dp[1], top), max(dp[1] + isz[1], top + lh)
        rect(p, fl((dp[0], rt)), fl((max_w, rb - rt)), (0, 0, 0, 255))
        if self.selected_item < len(self.items):
            outline(p, dp, isz, color_theme.tint_color, max(1.0, 0.0015 * H()))
            t = self.items[self.selected_item]["text"]
            t.pos = fl(add(dp, (pl, pt)))
            t.draw(p)
        win.push_scissor((dp[0], top), (max_w, lh))
        inside_done = False
        ip = (dp[0], top + self.scroll_y)
        for item in self.items:
            ih = self._item_h(item)
            item["pos"] = ip
            if not inside_done and item["enabled"] and contains(*self.dropdown_rect, win.mouse):
                hs = (max_w - sbw, ih)
                if contains(ip, hs, win.mouse):
                    inside_done = True
                    rect(p, fl(ip), fl(hs), color_theme.tint_color)
            hovered = inside_done and item["pos"] == ip and contains(ip, (max_w - sbw, ih), win.mouse)
            base = item["text"].color
            if hovered and item["enabled"]:
                item["text"].color = text_on(color_theme.tint_color)
            item["text"].pos = fl(add(ip, (pl, pt)))
            item["text"].draw(p)
            item["text"].color = base
            ip = add(ip, (0.0, ih))
        win.pop_scissor()
        if has_sb:
            sbh = max(10.0, lh * (lh / total))
            amount = min(1.0, max(0.0, -self.scroll_y / max_scroll))
            rect(p, fl((dp[0] + max_w - sbw, top + amount * (lh - sbh))), fl((sbw, sbh)), (200, 200, 200, 255))


class CheckBox(Widget):
    def __init__(self, font_key, text):
        super().__init__()
        self.text = Text(text, font_key)
        self.checked = False
        self.anim = 0.0
        self.on_changed = None

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        self.handle_tooltip_event(ev, add(self.position, offset), self.get_size())
        if ev.type == Event.PRESS and ev.button == 1 and contains(add(self.position, offset), self.get_size(), (ev.x, ev.y)):
            self.checked = not self.checked
            if self.on_changed:
                self.on_changed(self.checked)
            return False
        return True

    def _box(self):
        s = Sprite("checkbox_background")
        s.set_height(int(self.text.bounds()[1]))
        return s

    def draw(self, win, offset):
        if not self.visible:
            return
        dp = add(self.position, offset)
        self.anim = min(1.0, max(0.0, self.anim + win.frame_delta * 10 * (1 if self.checked else -1)))
        unc = tuple(int(c * 0.6) for c in (25, 30, 34)) + (255,)
        chk = tuple(int(c * 0.6) for c in color_theme.tint_color[:3]) + (255,)
        bg = self._box()
        bg.color = tuple(int(unc[i] + (chk[i] - unc[i]) * self.anim) for i in range(3)) + (255,)
        bg.pos = fl(dp)
        bg.draw(win.p)
        circ = Sprite("checkbox_circle")
        circ.set_height(int(bg.size[1]))
        circ.color = color_theme.tint_color
        circ.pos = fl(add(dp, ((bg.size[0] - circ.size[0]) * self.anim, 0.0)))
        circ.draw(win.p)
        tb = self.text.bounds()
        self.text.pos = fl(add(dp, (bg.size[0] + 0.005 * H(), bg.size[1] * 0.5 - tb[1] * 0.5)))
        self.text.draw(win.p)

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        tb = self.text.bounds()
        b = fl(self._box().size)
        return (tb[0] + b[0] + 0.005 * H(), max(tb[1], b[1]))

    def set_checked(self, c, animated=False):
        self.checked = c
        if not animated:
            self.anim = 1.0 if c else 0.0
        if self.on_changed:
            self.on_changed(c)

    def is_checked(self):
        return self.checked


class RadioButton(Widget):
    VERTICAL, HORIZONTAL = 0, 1
    PT, PB, PL, PR, SP = 0.004629, 0.004629, 0.007, 0.007, 0.007

    def __init__(self, font_key, orientation):
        super().__init__()
        self.font_key = font_key
        self.orientation = orientation
        self.items = []
        self.selected_item = 0
        self.on_selection_changed = None

    def add_item(self, text, id_):
        self.items.append({"text": Text(text, self.font_key), "id": id_})

    def _isize(self, item):
        b = item["text"].bounds()
        return (b[0] + int(self.PL * H()) + int(self.PR * H()), b[1] + int(self.PT * H()) + int(self.PB * H()))

    def _advance(self, pos, isz):
        sp = self.SP * H()
        return add(pos, (0.0, isz[1] + sp) if self.orientation == self.VERTICAL else (isz[0] + sp, 0.0))

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        if ev.type == Event.PRESS and ev.button == 1:
            dp = add(self.position, offset)
            for i, item in enumerate(self.items):
                isz = self._isize(item)
                if contains(dp, isz, (ev.x, ev.y)):
                    if self.selected_item != i and self.on_selection_changed:
                        if not self.on_selection_changed(item["text"].s, item["id"]):
                            return False
                    self.selected_item = i
                    return False
                dp = self._advance(dp, isz)
        return True

    def draw(self, win, offset):
        if not self.visible or not self.items:
            return
        can_select = not self.has_parent_with_selected_child_widget()
        dp = add(self.position, offset)
        for i, item in enumerate(self.items):
            isz = self._isize(item)
            rect(win.p, fl(dp), fl(isz), color_theme.tint_color if i == self.selected_item else (0, 0, 0, 120))
            if can_select and contains(dp, isz, win.mouse):
                bc = (255, 255, 255, 255) if i == self.selected_item else color_theme.tint_color
                outline(win.p, dp, isz, bc, max(1.0, 0.0015 * H()))
            item["text"].pos = fl(sub(add(dp, mul(isz, 0.5)), mul(item["text"].bounds(), 0.5)))
            item["text"].color = text_on(color_theme.tint_color) if i == self.selected_item else color_theme.text_color
            item["text"].draw(win.p)
            dp = self._advance(dp, isz)

    def get_size(self):
        if not self.visible or not self.items:
            return (0.0, 0.0)
        w = h = 0.0
        for item in self.items:
            b = item["text"].bounds()
            if self.orientation == self.VERTICAL:
                w = max(w, b[0] + int(self.PL * H()) + int(self.PR * H()))
                h += b[1] + int(self.PT * H()) + int(self.PB * H())
            else:
                w += b[0] + int(self.PL * H()) + int(self.PR * H())
                h = theme.font_size(self.font_key) * 2.0 + int(self.PT * H()) + int(self.PB * H())
        if len(self.items) > 1:
            extra = (len(self.items) - 1) * self.SP * H()
            if self.orientation == self.VERTICAL:
                h += extra
            else:
                w += extra
        return (w, h)

    def set_selected_item(self, id_, trigger_event=True, trigger_even_if_same=False):
        for i, item in enumerate(self.items):
            if item["id"] == id_:
                if trigger_event and (trigger_even_if_same or self.selected_item != i) and self.on_selection_changed:
                    if not self.on_selection_changed(item["text"].s, item["id"]):
                        break
                self.selected_item = i
                break

    def get_selected_id(self):
        return self.items[self.selected_item]["id"] if self.items else ""


_focused_entry = None


class Entry(Widget):
    PT, PB, PL, PR = 0.004629, 0.004629, 0.007, 0.007

    def __init__(self, font_key, text, max_width):
        super().__init__()
        self.font_key = font_key
        self.max_width = max_width
        self.text = Text("", font_key)
        self.value = ""
        self.caret = 0
        self.masked = False
        self.number_mode = None
        self.on_changed = None
        self.set_text(text)

    def set_number_mode(self, enabled, lo, hi):
        self.number_mode = (lo, hi) if enabled else None

    def set_masked(self, m):
        self.masked = m

    def is_masked(self):
        return self.masked

    def focused(self):
        return _focused_entry is self

    def set_text(self, s):
        self.value = str(s)
        self.caret = len(self.value)
        if self.on_changed:
            self.on_changed(self.value)

    def get_text(self):
        return self.value

    def _clamp(self):
        if self.number_mode:
            lo, hi = self.number_mode
            try:
                v = int(self.value or "0")
            except ValueError:
                v = lo
            v = min(hi, max(lo, v))
            if str(v) != self.value:
                self.value = str(v)
                self.caret = len(self.value)
                if self.on_changed:
                    self.on_changed(self.value)

    def on_event(self, ev, win, offset):
        global _focused_entry
        if not self.visible:
            return True
        if ev.type == Event.PRESS and ev.button == 1:
            if contains(add(self.position, offset), self.get_size(), (ev.x, ev.y)):
                _focused_entry = self
                self.caret = len(self.value)
            elif self.focused():
                _focused_entry = None
                self._clamp()
            return True
        if not self.focused():
            return True
        if ev.type == Event.KEY:
            k = ev.key
            changed = False
            if k == Qt.Key.Key_Backspace and self.caret > 0:
                self.value = self.value[:self.caret - 1] + self.value[self.caret:]
                self.caret -= 1
                changed = True
            elif k == Qt.Key.Key_Delete and self.caret < len(self.value):
                self.value = self.value[:self.caret] + self.value[self.caret + 1:]
                changed = True
            elif k == Qt.Key.Key_Left:
                self.caret = max(0, self.caret - 1)
            elif k == Qt.Key.Key_Right:
                self.caret = min(len(self.value), self.caret + 1)
            elif k == Qt.Key.Key_Home:
                self.caret = 0
            elif k == Qt.Key.Key_End:
                self.caret = len(self.value)
            elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                _focused_entry = None
                self._clamp()
            elif k == Qt.Key.Key_V and ev.mods & Qt.KeyboardModifier.ControlModifier:
                self._insert(QGuiApplication.clipboard().text().replace("\n", " "))
                changed = True
            else:
                return False
            if changed and self.on_changed:
                self.on_changed(self.value)
            return False
        if ev.type == Event.TEXT:
            if ev.text and ev.text.isprintable():
                self._insert(ev.text)
                if self.on_changed:
                    self.on_changed(self.value)
            return False
        return True

    def _insert(self, s):
        if self.number_mode:
            s = "".join(c for c in s if c in string.digits)
        self.value = self.value[:self.caret] + s + self.value[self.caret:]
        self.caret += len(s)

    def _shown(self):
        return "*" * len(self.value) if self.masked else self.value

    def draw(self, win, offset):
        if not self.visible:
            return
        dp = fl(add(self.position, offset))
        size = self.get_size()
        rect(win.p, dp, size, (0, 0, 0, 255) if self.focused() else (0, 0, 0, 120))
        if self.focused():
            outline(win.p, dp, size, color_theme.tint_color, max(1.0, 0.0015 * H()))
        pl, pt = int(self.PL * H()), int(self.PT * H())
        self.text.set_string(self._shown())
        self.text.color = color_theme.text_color
        win.push_scissor(dp, size)
        self.text.pos = (dp[0] + pl, dp[1] + pt)
        self.text.draw(win.p)
        if self.focused():
            fm = _metrics(self.font_key)
            cx = dp[0] + pl + fm.horizontalAdvance(self._shown()[:self.caret])
            rect(win.p, fl((cx, dp[1] + pt)), (max(1.0, 0.0015 * H()), fm.height()), color_theme.text_color)
        win.pop_scissor()

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        fm = _metrics(self.font_key)
        h = int(self.PT * H()) + fm.height() + int(self.PB * H())
        return (float(int(self.max_width)), float(math.ceil(h)))


def unfocus_entries():
    global _focused_entry
    if _focused_entry:
        _focused_entry._clamp()
    _focused_entry = None


class Subsection(Widget):
    MT, MB, ML, MR, TS = 0.012, 0.012, 0.015, 0.015, 0.010

    def __init__(self, title, inner, size):
        super().__init__()
        self.label = Label("title", title or "")
        self.inner = inner
        inner.parent_widget = self
        self.size = size
        self.bg_color = (25, 30, 34, 255)

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        return self.inner.on_event(ev, win, (0.0, 0.0))

    def draw(self, win, offset):
        if not self.visible:
            return
        dp = add(self.position, offset)
        rect(win.p, fl(dp), fl(self.get_size()), self.bg_color)
        dp = add(dp, (self.ML * H(), self.MT * H()))
        if self.label.get_text():
            self.label.draw(win, dp)
            dp = add(dp, (0.0, self.label.get_size()[1] + self.TS * H()))
        self.inner.set_position(dp)
        self.inner.draw(win, (0.0, 0.0))

    def _margins(self):
        return ((self.ML + self.MR) * H(), (self.MT + self.MB) * H())

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        m = self._margins()
        th = self.label.get_size()[1] + self.TS * H() if self.label.get_text() else 0.0
        isz = self.inner.get_size()
        w, h = isz[0] + m[0], th + isz[1] + m[1]
        if abs(self.size[0]) > 0.001:
            w = self.size[0]
        if abs(self.size[1]) > 0.001:
            h = self.size[1]
        return (w, h)

    def get_inner_size(self):
        return sub(self.get_size(), self._margins()) if self.visible else (0.0, 0.0)


class ScrollablePage(_Container):
    def __init__(self, size):
        super().__init__()
        self.size = size
        self.scroll_y = 0.0
        self.scroll_target_y = 0.0
        self.scrollbar_rect = ((0, 0), (0, 0))
        self.dragging = False
        self.drag_start = (0, 0)
        self.drag_scroll_start = 0.0

    def sbw(self):
        return max(5.0, 0.004 * H())

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)

    def set_size(self, s):
        self.size = s

    def get_inner_size(self):
        if not self.visible:
            return (0.0, 0.0)
        return sub(self.size, (self.sbw() + max(2.0, 0.004 * H()), 0.0))

    def reset_scroll(self):
        self.scroll_y = self.scroll_target_y = 0.0

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        offset = add(self.position, offset)
        cs = self.get_inner_size()
        if ev.type == Event.PRESS and contains(*self.scrollbar_rect, (ev.x, ev.y)):
            self.set_widget_as_selected_in_parent()
            self.dragging = True
            self.drag_start = (ev.x, ev.y)
            self.drag_scroll_start = self.scroll_y
            return False
        if ev.type == Event.RELEASE and self.dragging:
            self.dragging = False
            self.remove_widget_as_selected_in_parent()
            return False
        if ev.type in (Event.PRESS, Event.MOVE) and not contains(offset, cs, (ev.x, ev.y)):
            return True
        if not self._events(ev, win, add(offset, (0.0, self.scroll_y))):
            return False
        if ev.type == Event.WHEEL:
            self.scroll_target_y += ev.delta * 80
            return False
        return True

    def draw(self, win, offset):
        self.scrollbar_rect = ((0, 0), (0, 0))
        if not self.visible or not self.widgets:
            self.reset_scroll()
            return
        sbw = self.sbw()
        sb_pos = add(add(self.position, offset), (self.size[0] - sbw, 0.0))
        offset = add(self.position, offset)
        cs = self.get_inner_size()
        win.push_scissor(offset, cs)
        sel = self.selected_child_widget
        doff = add(offset, (0.0, self.scroll_y))
        top, bottom = 999999.0, 0.0
        for w in [x for x in self.widgets if x is not sel] + ([sel] if sel else []):
            w.draw(win, doff)
            wp = add(w.get_position(), doff)
            wi = w.get_inner_size()
            top = min(top, wp[1])
            bottom = max(bottom, wp[1] + wi[1])
        child_h = bottom - top
        diff = self.scroll_target_y - self.scroll_y
        self.scroll_y = self.scroll_target_y if abs(diff) < 0.1 else self.scroll_y + diff * min(1.0, win.frame_delta * 10)
        limit = child_h - self.size[1]
        if self.scroll_y > 0.001 or child_h < self.size[1]:
            self.scroll_y = self.scroll_target_y = 0.0
        elif self.scroll_y < -limit:
            self.scroll_y = self.scroll_target_y = -limit
        win.pop_scissor()
        ratio = min(1.0, self.size[1] / child_h) if child_h > 0.001 else 1.0
        sbh = max(10.0, self.size[1] * ratio)
        empty = self.size[1] - sbh
        if ratio < 0.999:
            amount = min(1.0, max(0.0, -self.scroll_y / (child_h - self.size[1])))
            sp, ss = fl(add(sb_pos, (0.0, amount * empty))), fl((sbw, sbh))
            rect(win.p, sp, ss, (200, 200, 200, 255))
            self.scrollbar_rect = (sp, ss)
        if self.dragging and empty > 0:
            moved = win.mouse[1] - self.drag_start[1]
            self.scroll_y = min(0.0, max(-limit, self.drag_scroll_start - moved / empty * limit))
            self.scroll_target_y = self.scroll_y


class GsrPage(_Container):
    """Settings page frame: page_bg box with tint border, left label box, buttons on the right."""

    def __init__(self, top_text, bottom_text):
        super().__init__()
        self.top_text = Text(top_text, "title")
        self.bottom_text = Text(bottom_text, "title")
        self.margin = 0.02
        self.buttons = []
        self.on_click = None
        self.icon = "settings"
        self.size_scale = (0.3333, 0.7)

    def add_button(self, text, id_, color):
        b = Button("title", text, fl((theme.window_width / 10, theme.window_height / 15)), color)
        b.set_border_scale(0.003)
        b.on_click = lambda: self.on_click and self.on_click(id_)
        self.buttons.append(b)

    def border(self):
        return 0.004 * H()

    def hspacing(self):
        return theme.window_width / 50

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        return fl((theme.window_width * self.size_scale[0], theme.window_height * self.size_scale[1]))

    def content_pos(self):
        s = self.get_size()
        return fl((theme.window_width * 0.5 - s[0] * 0.5, theme.window_height * 0.5 - s[1] * 0.5))

    def get_inner_size(self):
        m = int(self.margin * H())
        return sub(self.get_size(), (m * 2, m * 2 + self.border()))

    def _children_pos(self):
        m = int(self.margin * H())
        return fl(add(self.content_pos(), (m, self.border() + m)))

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        for b in self.buttons:
            if not b.on_event(ev, win, (0.0, 0.0)):
                return False
        return self._events(ev, win, self._children_pos())

    def draw(self, win, offset):
        if not self.visible:
            return
        p = win.p
        cp, cs = self.content_pos(), self.get_size()
        rect(p, cp, cs, color_theme.page_bg_color)
        rect(p, cp, fl((cs[0], self.border())), color_theme.tint_color)
        # left label box
        bsz = fl((theme.window_width / 10, theme.window_width / 10))
        bpos = sub(fl(cp), fl((bsz[0] + self.hspacing(), 0.0)))
        rect(p, bpos, bsz, (0, 0, 0, 255))
        margin = int(bsz[1] * 0.085)
        self.top_text.pos = fl(add(bpos, (bsz[0] * 0.5 - self.top_text.bounds()[0] * 0.5, margin)))
        self.top_text.draw(p)
        icon = Sprite(self.icon)
        icon.set_height(int(bsz[1] * 0.5))
        icon.pos = fl(sub(add(bpos, mul(bsz, 0.5)), mul(icon.size, 0.5)))
        icon.draw(p)
        bb = self.bottom_text.bounds()
        self.bottom_text.pos = fl(add(bpos, (bsz[0] * 0.5 - bb[0] * 0.5, bsz[1] - bb[1] - margin)))
        self.bottom_text.draw(p)
        oy = 0.0
        for b in self.buttons:
            b.set_position(fl(add(cp, (cs[0] + self.hspacing(), oy))))
            b.draw(win, (0.0, 0.0))
            oy += b.get_size()[1] + 0.015 * H()
        pos = self._children_pos()
        win.push_scissor(pos, self.get_inner_size())
        sel = self.selected_child_widget
        for w in self.widgets:
            if w is not sel:
                w.draw(win, pos)
        if sel:
            sel.draw(win, pos)
        win.pop_scissor()


class StaticPage(_Container):
    def __init__(self, size):
        super().__init__()
        self.size = size

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        return self._events(ev, win, add(self.position, offset))

    def draw(self, win, offset):
        if not self.visible:
            return
        dp = add(self.position, offset)
        sel = self.selected_child_widget
        win.push_scissor(dp, self.size)
        for w in self.widgets:
            if w is not sel:
                w.draw(win, self.position)
        if sel:
            sel.draw(win, dp)
        win.pop_scissor()

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)


class PageStack(Widget):
    def __init__(self):
        super().__init__()
        self.stack = []

    def on_event(self, ev, win, offset):
        if not self.visible or not self.stack:
            return True
        return self.stack[-1].on_event(ev, win, add(self.position, offset))

    def draw(self, win, offset):
        if self.visible and self.stack:
            self.stack[-1].draw(win, add(self.position, offset))

    def push(self, page):
        page.on_navigate_to_page()
        self.stack.append(page)

    def pop(self):
        if self.stack:
            unfocus_entries()
            self.stack[-1].on_navigate_away_from_page()
            self.stack.pop()

    def top(self):
        return self.stack[-1] if self.stack else None

    def empty(self):
        return not self.stack


class FileChooserBody(Widget):
    def __init__(self, chooser, size):
        super().__init__()
        self.chooser = chooser
        self.size = size
        self.inner_size = size
        self.folders = []
        self.selected_item = -1
        self.mouse_over_item = -1
        self.clicks = 0
        self.last_click = 0.0

    def on_event(self, ev, win, offset):
        import time
        if not self.visible:
            return True
        if ev.type == Event.PRESS and ev.button == 1:
            now = time.monotonic()
            self.clicks = self.clicks + 1 if now - self.last_click <= theme.double_click_timeout_seconds else 1
            self.last_click = now
            prev = self.selected_item
            self.selected_item = self.mouse_over_item
            if self.selected_item != prev:
                self.clicks = 1
            if self.selected_item != -1 and self.clicks % 2 == 0:
                self.chooser.open_subdirectory(self.folders[self.selected_item].s)
        return True

    def draw(self, win, offset):
        self.mouse_over_item = -1
        if not self.visible:
            return
        (sx, sy), (sw, sh) = win.get_scissor()
        dp = add(self.position, offset)
        cpt = cpb = cpl = cpr = int(0.03 * H())
        cols = 5
        fw = float(int((self.size[0] - (cpl + cpr) * cols) / cols))
        step = cpr + fw + cpl
        fpos = add(dp, (cpl, cpt))
        end_newline = False
        for i, folder in enumerate(self.folders):
            spr = Sprite("folder")
            spr.set_width(fw)
            spr.pos = fl(sub(fpos, (0.0, spr.size[1] * 0.3)))
            ipos = sub(fpos, (cpl, cpt))
            isz = add(spr.size, (cpl + cpr, cpt + cpb))
            if i == self.selected_item:
                rect(win.p, fl(ipos), fl(isz), color_theme.tint_color)
            if (not self.has_parent_with_selected_child_widget() and self.mouse_over_item == -1
                    and sx <= win.mouse[0] <= sx + sw and sy <= win.mouse[1] <= sy + sh and contains(ipos, isz, win.mouse)):
                outline(win.p, ipos, isz, color_theme.tint_color, 0.0015 * H())
                self.mouse_over_item = i
            if ipos[1] + isz[1] >= sy and ipos[1] < sy + sh:
                spr.draw(win.p)
                folder.wrap_width = isz[0]
                folder.max_rows = 2
                fb = folder.bounds()
                folder.pos = fl(add(spr.pos, (spr.size[0] * 0.5 - fb[0] * 0.5, spr.size[1] + 0.005 * H())))
                folder.draw(win.p)
            fpos = add(fpos, (step, 0.0))
            if fpos[0] + fw > dp[0] + self.size[0]:
                fpos = (dp[0] + cpl, fpos[1] + cpb + spr.size[1] + cpt)
                if i == len(self.folders) - 1:
                    end_newline = True
        if not end_newline:
            fpos = (fpos[0], fpos[1] + cpb + fw)
        self.inner_size = (self.size[0], fpos[1] - dp[1])

    def set_current_directory(self, d):
        self.folders = []
        self.selected_item = self.mouse_over_item = -1
        entries = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".") or e.name.startswith("$"):
                        continue
                    try:
                        if e.is_dir():
                            entries.append((e.stat().st_mtime, e.name))
                    except OSError:
                        pass
        except OSError:
            pass
        entries.sort(reverse=True)
        self.folders = [Text(n, "body") for _, n in entries]

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)

    def get_inner_size(self):
        return self.inner_size if self.visible else (0.0, 0.0)


class FileChooser(Widget):
    def __init__(self, start_dir, size):
        super().__init__()
        self.size = size
        self.current = Text(start_dir, "body")
        self.up = Sprite("up_arrow")
        self.page = ScrollablePage(size)
        self.page.parent_widget = self
        self.body = FileChooserBody(self, size)
        self.page.add_widget(self.body)
        self.set_current_directory(start_dir)

    def on_event(self, ev, win, offset):
        if not self.visible:
            return True
        if ev.type == Event.PRESS and ev.button == 1 and contains(self.up.pos, self.up.size, (ev.x, ev.y)):
            self.open_parent_directory()
            return False
        return self.page.on_event(ev, win, offset)

    def _pad(self):
        v = 0.004629 * H()
        return (v * 2, v * 2)

    def draw(self, win, offset):
        if not self.visible:
            return
        start = add(self.position, offset)
        pad = self._pad()
        bg_h = float(int(self.current.bounds()[1] + pad[1]))
        dp = add(start, (0.0, bg_h + 0.015 * H()))
        body_size = fl((self.size[0], self.size[1] - (dp[1] - start[1])))
        self.page.set_size(body_size)
        self.body.size = self.page.get_inner_size()
        rect(win.p, fl(dp), fl(self.page.get_inner_size()), (0, 0, 0, 120))
        # navigation
        nav_size = fl((self.size[0], self.current.bounds()[1] + pad[1]))
        self.up.set_height(int(nav_size[1] * 0.8))
        self.up.pos = fl(add(start, (self.body.size[0] - self.up.size[0], nav_size[1] * 0.5 - self.up.size[1] * 0.5)))
        hover = contains(self.up.pos, self.up.size, win.mouse) and not self.has_parent_with_selected_child_widget()
        self.up.color = color_theme.tint_color if hover else (255, 255, 255, 255)
        self.up.draw(win.p)
        nav_w = self.body.size[0] - self.up.size[0] - 0.01 * H()
        rect(win.p, fl(start), fl((nav_w, nav_size[1])), (0, 0, 0, 120))
        self.current.color = color_theme.text_color
        self.current.pos = fl(add(start, (pad[0], nav_size[1] * 0.5 - self.current.bounds()[1] * 0.5)))
        win.push_scissor(start, (nav_w, nav_size[1]))
        self.current.draw(win.p)
        win.pop_scissor()
        self.page.position = (0.0, 0.0)
        self.page.draw(win, fl(dp))

    def get_size(self):
        return self.size if self.visible else (0.0, 0.0)

    def set_current_directory(self, d):
        self.current.set_string(d)
        self.body.set_current_directory(d)
        self.page.reset_scroll()

    def open_subdirectory(self, name):
        self.set_current_directory(os.path.join(self.current.s, name))

    def open_parent_directory(self):
        parent = os.path.dirname(self.current.s.rstrip("\\/")) or self.current.s
        if len(parent) == 2 and parent[1] == ":":
            parent += "\\"
        self.set_current_directory(parent)

    def get_current_directory(self):
        return self.current.s


class ColorSwatches(Widget):
    """Row of accent color squares; the selected one gets a white frame."""

    def __init__(self, colors):
        super().__init__()
        self.colors = colors            # [(id, rgba)]
        self.selected = None
        self.on_select = None

    def _cell(self):
        return float(int(0.03 * H())), float(int(0.008 * H()))

    def on_event(self, ev, win, offset):
        if not self.visible or ev.type != Event.PRESS or ev.button != 1:
            return True
        size, gap = self._cell()
        x, y = add(self.position, offset)
        for id_, _ in self.colors:
            if contains((x, y), (size, size), (ev.x, ev.y)):
                self.selected = id_
                if self.on_select:
                    self.on_select(id_)
                return False
            x += size + gap
        return True

    def draw(self, win, offset):
        if not self.visible:
            return
        size, gap = self._cell()
        x, y = fl(add(self.position, offset))
        b = max(2.0, float(int(0.0025 * H())))
        can_hover = not self.has_parent_with_selected_child_widget()
        for id_, color in self.colors:
            rect(win.p, (x, y), (size, size), color)
            if id_ == self.selected:
                outline(win.p, (x - b, y - b), (size + b * 2, size + b * 2), (255, 255, 255, 255), b)
            elif can_hover and contains((x, y), (size, size), win.mouse):
                outline(win.p, (x, y), (size, size), (255, 255, 255, 160), b)
            x += size + gap

    def get_size(self):
        if not self.visible:
            return (0.0, 0.0)
        size, gap = self._cell()
        return (len(self.colors) * (size + gap) - gap, size)
