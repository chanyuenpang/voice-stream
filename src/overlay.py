
#!/usr/bin/env python3
"""voice-overlay: mic indicator in the bottom-right corner.

green pulse  -> capturing speech
green solid  -> text was accepted and pasted (short flash)
grey         -> speech heard but rejected by the gate (short flash)
"""
import math, os, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell as LayerShell, GLib  # noqa

APP_DIR = os.path.expanduser(os.environ.get("VOICE_STREAM_HOME", "~/.local/share/voice-stream"))
STATE = os.path.expanduser(os.environ.get("VOICE_STREAM_STATE", APP_DIR + "/state"))
SIZE = 76
FLASH_S = 0.8

def read_state():
    try:
        with open(STATE) as f:
            s = f.read().strip()
    except Exception:
        return "idle"
    if s.startswith("ok"):
        return "ok"
    if s.startswith("listen"):
        return "listening"
    if s.startswith("accept"):
        return "accepted"
    if s.startswith("reject"):
        return "rejected"
    return "idle"

class Overlay(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.voice.Overlay")
        self.phase = 0.0
        self.mode = "idle"
        self.flash_until = 0.0
        self.last_state = "idle"

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_decorated(False)
        win.set_default_size(SIZE, SIZE)
        win.set_can_focus(False)

        LayerShell.init_for_window(win)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
        LayerShell.set_margin(win, LayerShell.Edge.BOTTOM, 30)
        LayerShell.set_margin(win, LayerShell.Edge.RIGHT, 30)
        LayerShell.set_namespace(win, "voice-indicator")
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)

        area = Gtk.DrawingArea()
        area.set_content_width(SIZE)
        area.set_content_height(SIZE)
        area.set_draw_func(self.draw)
        win.set_child(area)

        css = Gtk.CssProvider()
        css.load_from_data(b"window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        win.present()
        win.set_visible(False)
        self.win = win
        self.area = area
        GLib.timeout_add(60, self.tick)

    def tick(self):
        s = read_state()
        now = time.time()
        if s != self.last_state:
            if s in ("accepted", "rejected"):
                self.flash_until = now + FLASH_S
            self.last_state = s
        try:
            age = now - os.path.getmtime(STATE)
        except Exception:
            age = 999.0
        if s in ("listening", "ok") and age > 4.0:
            s = "idle"                      # stale state: service already stopped
        if s in ("listening", "ok"):
            self.mode = s
        elif s in ("accepted", "rejected") and now < self.flash_until:
            self.mode = s
        else:
            self.mode = "idle"

        visible = self.mode != "idle"
        if visible and not self.win.get_visible():
            self.win.set_visible(True)
        elif not visible and self.win.get_visible():
            self.win.set_visible(False)
        if visible:
            self.phase = (self.phase + 0.12) % (2 * math.pi)
            self.area.queue_draw()
        return True

    def draw(self, area, cr, w, h):
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        cx, cy = w / 2, h / 2

        if self.mode in ("accepted", "ok"):
            body, halo = (0.15, 0.63, 0.38, 0.92), (0.36, 0.86, 0.55)
        elif self.mode == "rejected":
            body, halo = (0.40, 0.41, 0.44, 0.92), None
        else:  # listening: neutral, not yet a success
            body, halo = (0.45, 0.51, 0.60, 0.95), (0.64, 0.70, 0.78)

        if halo:
            cr.set_line_width(2.5)
            cr.set_source_rgba(halo[0], halo[1], halo[2], 0.50 * (1.0 - pulse))
            cr.arc(cx, cy, SIZE / 2 - 6 + pulse * 9, 0, 2 * math.pi)
            cr.stroke()
        cr.set_source_rgba(*body)
        cr.arc(cx, cy, SIZE / 2 - 11, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.80 + 0.20 * pulse if self.mode == "listening" else 0.9)
        cr.set_line_width(2.4)

        bw, bh, br = 13, 24, 6.5
        x, y = cx - bw / 2, cy - 14
        cr.new_sub_path()
        cr.arc(x + bw - br, y + br, br, -math.pi / 2, 0)
        cr.arc(x + bw - br, y + bh - br, br, 0, math.pi / 2)
        cr.arc(x + br, y + bh - br, br, math.pi / 2, math.pi)
        cr.arc(x + br, y + br, br, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.fill()

        cr.arc(cx, cy - 2, 15, 0.15 * math.pi, 0.85 * math.pi)
        cr.stroke()
        cr.move_to(cx, cy + 13.5)
        cr.line_to(cx, cy + 21)
        cr.stroke()

def main():
    Overlay().run([])

if __name__ == "__main__":
    main()
