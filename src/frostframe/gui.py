"""frostframe GTK 3 interface: `frostframe gui`."""

import os
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from . import cli  # noqa: E402
from . import labels as L  # noqa: E402
from . import media  # noqa: E402
from .device import Device, DeviceError, find_devices  # noqa: E402

SERVICE = "frostframe-monitor.service"
METRIC_NAMES = {"": "(none)", "cpu-temp": "CPU temperature", "cpu-load": "CPU usage",
                "cpu-clock": "CPU clock", "gpu-temp": "GPU temperature", "gpu-load": "GPU usage",
                "gpu-clock": "GPU clock", "gpu-power": "GPU power", "ram": "RAM usage",
                "net-down": "Download speed", "net-up": "Upload speed", "time": "Time", "date": "Date"}


class Worker:
    """Runs device operations one at a time off the GTK thread."""

    def __init__(self, window):
        self.window = window
        self.lock = threading.Lock()

    def run(self, fn, done=None, busy_text=None):
        if busy_text:
            self.window.set_busy(busy_text)

        def target():
            with self.lock:
                try:
                    result, error = fn(), None
                except (DeviceError, media.MediaError, ValueError, OSError) as e:
                    result, error = None, str(e)
            GLib.idle_add(self._finish, done, result, error)

        threading.Thread(target=target, daemon=True).start()

    def _finish(self, done, result, error):
        self.window.set_busy(None)
        if error:
            self.window.show_error(error)
        elif done:
            done(result)
        return False


def _combo(options, active=None):
    """options: [(id, label)]"""
    c = Gtk.ComboBoxText()
    for oid, label in options:
        c.append(oid, label)
    c.set_active_id(active if active is not None else options[0][0])
    return c


def _row(label_text, widget, hint=None):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    lab = Gtk.Label(label=label_text, xalign=0)
    lab.set_size_request(170, -1)
    box.pack_start(lab, False, False, 0)
    box.pack_start(widget, widget.get_halign() == Gtk.Align.FILL, True, 0)
    if hint:
        h = Gtk.Label(label=hint, xalign=0)
        h.get_style_context().add_class("dim-label")
        box.pack_start(h, False, False, 0)
    return box


def _section(title):
    lab = Gtk.Label(xalign=0)
    lab.set_markup("<b>%s</b>" % GLib.markup_escape_text(title))
    lab.set_margin_top(12)
    return lab


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, page=None):
        super().__init__(application=app, title="frostframe")
        self.set_default_size(900, 640)
        self.worker = Worker(self)
        self.files = {"media": [], "presets": []}
        self.work = {}
        self.screen = (2240, 1080)

        hb = Gtk.HeaderBar(show_close_button=True, title="frostframe")
        self.set_titlebar(hb)
        self.header = hb
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh from the screen")
        refresh.connect("clicked", lambda *_: self.refresh())
        hb.pack_end(refresh)
        self.spinner = Gtk.Spinner()
        hb.pack_end(self.spinner)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.ERROR, show_close_button=True)
        self.info_label = Gtk.Label(xalign=0, wrap=True)
        self.infobar.get_content_area().add(self.info_label)
        self.infobar.connect("response", lambda bar, _r: bar.hide())
        outer.pack_start(self.infobar, False, False, 0)

        stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.CENTER, margin=6)
        outer.pack_start(switcher, False, False, 0)
        outer.pack_start(stack, True, True, 0)
        stack.add_titled(self._media_page(), "media", "Media")
        stack.add_titled(self._display_page(), "display", "Display & readings")
        stack.add_titled(self._cooler_page(), "cooler", "Cooler")
        self.stack = stack

        self.status = Gtk.Label(xalign=0, margin=8)
        self.status.get_style_context().add_class("dim-label")
        outer.pack_end(self.status, False, False, 0)

        self.show_all()
        self.infobar.hide()
        self.progress.hide()
        if page:
            stack.set_visible_child_name(page)
        self.refresh()
        GLib.timeout_add_seconds(2, self._tick_readings)
        self._sensors = None

    # ------------------------------------------------------------ helpers --

    def set_busy(self, text):
        if text:
            self.spinner.start()
            self.status.set_text(text)
        else:
            self.spinner.stop()

    def show_error(self, text):
        self.info_label.set_text(text)
        self.infobar.show()
        self.status.set_text("")

    def run(self, fn, done=None, busy_text=None):
        self.worker.run(fn, done, busy_text)

    def refresh(self):
        def load():
            if not find_devices():
                raise DeviceError("No cooler screen found. Is it connected? (USB vendor 391a)")
            with Device() as d:
                info = d.device_info()
                return {"info": info, "files": d.file_list(), "user": d.user_config(),
                        "cooler": d.cooler_config(), "status": d.cooler_status(), "size": d.screen_size()}

        self.run(load, self._apply_state, "Reading the screen…")

    def _apply_state(self, s):
        self.infobar.hide()
        info = s["info"]
        self.header.set_subtitle("%s · firmware %s" % (info.get("product_name", "?"), info.get("firmware_version", "?")))
        self.header.set_tooltip_text("Serial number: %s\nScreen app: %s" % (info.get("serial_number", "?"),
                                                                         info.get("app_version", "?")))
        self.files = {"media": s["files"].get("media", []), "presets": s["files"].get("presets", [])}
        self.work = s["user"].get("work", {})
        self.screen = s["size"]
        disp = s["user"].get("display", {})
        self._loading = True
        self.brightness.set_value(disp.get("brightness", 80))
        self.screen_switch.set_active(disp.get("backlight", True))
        loop = {"Single": "single", "MediaAll": "all", "MediaRand": "random"}.get(self.work.get("loop_mode", "Single"), "single")
        self.loop_combo.set_active_id(loop)
        fan = s["cooler"].get("fan", {})
        self.fan_fixed.set_active(fan.get("mode", "Smart") == "Fixed")
        self.fan_smart.set_active(fan.get("mode", "Smart") != "Fixed")
        self.fan_power.set_value(fan.get("fixed_power", 60))
        self.pump.set_value(s["cooler"].get("pump", {}).get("speed_percent", 100))
        st = s["status"]
        self.rpm_label.set_text("Fan %d RPM · Pump %d RPM%s" % (
            st.get("fan_rpm", 0), st.get("pump_rpm", 0),
            "  —  0 RPM usually means the fans/pump are on motherboard headers, so these settings have no effect."
            if not st.get("fan_rpm") and not st.get("pump_rpm") else ""))
        self._loading = False
        self._fill_files()
        self.status.set_text("Connected.")

    # --------------------------------------------------------------- media --

    def _media_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.file_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(self.file_list)
        box.pack_start(scroll, True, True, 0)

        bar = Gtk.Box(spacing=8)
        self.loop_combo = _combo([("single", "Play one file"), ("all", "Loop your files"),
                                  ("random", "Shuffle your files")])
        self.loop_combo.connect("changed", self._loop_changed)
        bar.pack_start(Gtk.Label(label="Playback:"), False, False, 0)
        bar.pack_start(self.loop_combo, False, False, 0)
        box.pack_start(bar, False, False, 0)

        up = Gtk.Box(spacing=8)
        btn = Gtk.Button(label="Upload image, GIF or video…")
        btn.get_style_context().add_class("suggested-action")
        btn.connect("clicked", self._choose_upload)
        up.pack_start(btn, False, False, 0)
        self.fit_combo = _combo([("fill", "Fill (crop)"), ("fit", "Fit (letterbox)"), ("stretch", "Stretch")])
        up.pack_start(self.fit_combo, False, False, 0)
        self.half_check = Gtk.CheckButton(label="Half width (dual-screen)")
        up.pack_start(self.half_check, False, False, 0)
        self.play_check = Gtk.CheckButton(label="Play after upload", active=True)
        up.pack_start(self.play_check, False, False, 0)
        box.pack_start(up, False, False, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.pack_start(self.progress, False, False, 0)
        return box

    def _playing(self):
        mode = self.work.get("media_mode", "Single")
        if mode == "Dual":
            return {self.work.get("left_file"): "left", self.work.get("right_file"): "right"}
        return {self.work.get("single_file"): "playing"}

    def _fill_files(self):
        for child in self.file_list.get_children():
            self.file_list.remove(child)
        playing = self._playing()
        on_device = set()
        for kind, title in (("media", "Your files"), ("presets", "Built-in clips")):
            head = Gtk.Label(xalign=0, margin=6)
            head.set_markup("<b>%s</b>" % title)
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.add(head)
            self.file_list.add(row)
            if not self.files[kind]:
                empty = Gtk.Label(label="  (none yet: upload something below)", xalign=0, margin=6)
                empty.get_style_context().add_class("dim-label")
                r = Gtk.ListBoxRow(activatable=False)
                r.add(empty)
                self.file_list.add(r)
            for f in self.files[kind]:
                name = os.path.basename(f.get("path", ""))
                on_device.add(name)
                self.file_list.add(self._file_row(name, f.get("size", 0), kind == "media", playing))
        for missing in [n for n in playing if n and n not in on_device]:
            self.show_error("The screen is set to play %s, which is no longer on the device, so the "
                            "screen is black. Pick a file to play." % missing)
        self.file_list.show_all()

    def _file_row(self, name, size, deletable, playing):
        row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(spacing=8, margin=4)
        state = playing.get(name)
        mark = Gtk.Label(label={"playing": "▶", "left": "◧", "right": "◨"}.get(state, " "))
        mark.set_size_request(20, -1)
        box.pack_start(mark, False, False, 0)
        lab = Gtk.Label(label=name, xalign=0, ellipsize=3)
        lab.set_tooltip_text(name)
        box.pack_start(lab, True, True, 0)
        sz = Gtk.Label(label="%.1f MB" % (size / 1e6))
        sz.get_style_context().add_class("dim-label")
        box.pack_start(sz, False, False, 0)
        for text, tip, cb in (("Play", "Play full screen", lambda *_: self._play(name)),
                              ("Left", "Dual-screen: play on the left half", lambda *_: self._play_side(name, "left")),
                              ("Right", "Dual-screen: play on the right half", lambda *_: self._play_side(name, "right"))):
            b = Gtk.Button(label=text)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            box.pack_start(b, False, False, 0)
        d = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
        if deletable:
            d.set_tooltip_text("Delete from the screen")
            d.connect("clicked", lambda *_: self._delete(name))
        else:
            d.set_opacity(0)          # keeps the button columns aligned
            d.set_sensitive(False)
        box.pack_start(d, False, False, 0)
        row.add(box)
        return row

    def _loop_mode(self):
        return {"single": "Single", "all": "MediaAll", "random": "MediaRand"}[self.loop_combo.get_active_id()]

    def _play(self, name):
        loop = self._loop_mode()
        self.run(lambda: self._with_dev(lambda d: cli.play(d, name, loop=loop)),
                 lambda _r: self.refresh(), "Switching to %s…" % name)

    def _play_side(self, name, side):
        loop = self._loop_mode()

        def op(d):
            work = d.user_config().get("work", {})
            work["media_mode"] = "Dual"
            work["%s_file" % side] = name
            other = "right" if side == "left" else "left"
            if not work.get("%s_file" % other):
                work["%s_file" % other] = name
            work["loop_mode"] = loop
            cli.update_user_config(d, "work", work)

        self.run(lambda: self._with_dev(op), lambda _r: self.refresh(), "Dual-screen: %s on the %s…" % (name, side))

    def _loop_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        loop = self._loop_mode()
        self.run(lambda: self._with_dev(lambda d: cli.update_user_config(d, "work", {"loop_mode": loop})),
                 None, "Setting playback mode…")

    def _delete(self, name):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text="Delete %s from the screen?" % name)
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self.run(lambda: self._with_dev(lambda d: d.remove_file(name)), lambda _r: self.refresh(),
                     "Deleting %s…" % name)

    def _choose_upload(self, *_):
        dlg = Gtk.FileChooserDialog(title="Choose an image, GIF or video", transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Upload", Gtk.ResponseType.ACCEPT)
        flt = Gtk.FileFilter()
        flt.set_name("Images, GIFs and videos")
        for pat in ("*.gif", "*.png", "*.jpg", "*.jpeg", "*.mp4", "*.webm", "*.mov", "*.mkv",
                    "*.GIF", "*.PNG", "*.JPG", "*.JPEG", "*.MP4"):
            flt.add_pattern(pat)
        dlg.add_filter(flt)
        resp = dlg.run()
        path = dlg.get_filename()
        dlg.destroy()
        if resp == Gtk.ResponseType.ACCEPT and path:
            self._upload(path)

    def _upload(self, path):
        w, h = self.screen
        half = self.half_check.get_active()
        if half:
            w //= 2
        fit = self.fit_combo.get_active_id()
        play_after = self.play_check.get_active()
        self.progress.set_fraction(0)
        self.progress.set_text("Converting…")
        self.progress.show()

        def progress(done, total):
            GLib.idle_add(self._set_progress, done / total, "Uploading %d%%" % (done * 100 // total))

        def op():
            name, data = media.convert(path, w, h, mode=fit)
            with Device() as d:
                d.upload(name, data, progress=progress)
                if play_after:
                    cli.play(d, name, side="left" if half else None)
            return name

        def done(name):
            self.progress.set_text("Uploaded %s" % name)
            GLib.timeout_add_seconds(3, lambda: self.progress.hide() or False)
            self.refresh()

        self.run(op, done, "Converting and uploading %s…" % os.path.basename(path))

    def _set_progress(self, frac, text):
        self.progress.set_fraction(frac)
        self.progress.set_text(text)
        return False

    @staticmethod
    def _with_dev(fn):
        with Device() as d:
            return fn(d)

    # ------------------------------------------------------------- display --

    def _display_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
        box.pack_start(_section("Screen"), False, False, 0)
        self.brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.brightness.set_value_pos(Gtk.PositionType.RIGHT)
        self.brightness.connect("value-changed", self._brightness_changed)
        box.pack_start(_row("Brightness", self.brightness), False, False, 0)
        self.screen_switch = Gtk.Switch(halign=Gtk.Align.START)
        self.screen_switch.connect("notify::active", self._screen_toggled)
        box.pack_start(_row("Screen on", self.screen_switch), False, False, 0)

        box.pack_start(_section("On-screen readings"), False, False, 0)
        layout = cli.load_config().get("layout", {})
        current = layout.get("metrics", ["cpu-temp", "cpu-load", "gpu-temp"]) + ["", "", "", ""]
        opts = list(METRIC_NAMES.items())
        self.metric_combos = []
        self.metric_values = []
        for i in range(4):
            c = _combo(opts, current[i])
            v = Gtk.Label(label="", xalign=0)
            v.set_size_request(120, -1)
            row = Gtk.Box(spacing=12)
            row.pack_start(c, True, True, 0)
            row.pack_start(v, False, False, 0)
            box.pack_start(_row("Reading %d" % (i + 1), row), False, False, 0)
            self.metric_combos.append(c)
            self.metric_values.append(v)
        self.color_btn = Gtk.ColorButton(halign=Gtk.Align.START)
        self.color_btn.set_size_request(90, -1)
        rgb = layout.get("color", 0xFFFFFF)
        from gi.repository import Gdk
        self.color_btn.set_rgba(Gdk.RGBA((rgb >> 16 & 255) / 255, (rgb >> 8 & 255) / 255, (rgb & 255) / 255, 1))
        box.pack_start(_row("Text color", self.color_btn, "any color"), False, False, 0)
        self.font_combo = _combo([(f, f) for f in L.FONTS], layout.get("font", L.FONTS[0]))
        box.pack_start(_row("Font", self.font_combo), False, False, 0)
        self.side_combo = _combo([("left", "Left half"), ("right", "Right half"), ("full", "Full width")],
                                 layout.get("side", "left"))
        box.pack_start(_row("Position", self.side_combo), False, False, 0)
        self.size_spin = Gtk.SpinButton.new_with_range(60, 240, 10)
        self.size_spin.set_value(layout.get("value_size", 160))
        box.pack_start(_row("Number size", self.size_spin), False, False, 0)

        actions = Gtk.Box(spacing=8)
        apply_btn = Gtk.Button(label="Apply readings")
        apply_btn.get_style_context().add_class("suggested-action")
        apply_btn.connect("clicked", self._apply_labels)
        actions.pack_start(apply_btn, False, False, 0)
        clear_btn = Gtk.Button(label="Remove readings")
        clear_btn.connect("clicked", self._clear_labels)
        actions.pack_start(clear_btn, False, False, 0)
        box.pack_start(actions, False, False, 0)

        self.service_switch = Gtk.Switch(halign=Gtk.Align.START, active=self._service_active())
        self.service_switch.connect("notify::active", self._service_toggled)
        box.pack_start(_row("Keep readings live", self.service_switch,
                            "background service, starts at login"), False, False, 0)
        return box

    def _brightness_changed(self, scale):
        if getattr(self, "_loading", False):
            return
        if getattr(self, "_bright_timer", None):
            GLib.source_remove(self._bright_timer)
        self._bright_timer = GLib.timeout_add(400, self._send_brightness)

    def _send_brightness(self):
        self._bright_timer = None
        value = int(self.brightness.get_value())
        self.run(lambda: self._with_dev(lambda d: cli.update_user_config(
            d, "display", {"brightness": value, "backlight": True})), None, "Brightness %d%%…" % value)
        return False

    def _screen_toggled(self, sw, _p):
        if getattr(self, "_loading", False):
            return
        on = sw.get_active()
        self.run(lambda: self._with_dev(lambda d: cli.update_user_config(d, "display", {"backlight": on})),
                 None, "Screen %s…" % ("on" if on else "off"))

    def _layout_from_ui(self):
        metrics = [c.get_active_id() for c in self.metric_combos if c.get_active_id()]
        rgba = self.color_btn.get_rgba()
        color = (int(round(rgba.red * 255)) << 16) | (int(round(rgba.green * 255)) << 8) | int(round(rgba.blue * 255))
        return {"metrics": list(dict.fromkeys(metrics)), "side": self.side_combo.get_active_id(),
                "font": self.font_combo.get_active_id(), "color": color,
                "value_size": int(self.size_spin.get_value())}

    def _apply_labels(self, *_):
        layout = self._layout_from_ui()
        self._save_layout(layout)
        w, h = self.screen
        self.run(lambda: self._with_dev(lambda d: d.set_run_config(L.build_run_config(layout, w, h))),
                 lambda _r: self.status.set_text("Readings applied."), "Applying readings…")

    def _clear_labels(self, *_):
        layout = {"metrics": []}
        self._save_layout(layout)
        self.run(lambda: self._with_dev(lambda d: d.set_run_config({"groups": []})),
                 lambda _r: self.status.set_text("Readings removed."), "Removing readings…")

    @staticmethod
    def _save_layout(layout):
        cfg = cli.load_config()
        cfg["layout"] = layout
        cli.save_config(cfg)

    @staticmethod
    def _service_active():
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", SERVICE]).returncode == 0

    def _service_toggled(self, sw, _p):
        on = sw.get_active()
        if on == self._service_active():
            return
        cmd = ["systemctl", "--user", "enable" if on else "disable", "--now", SERVICE]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            self.show_error("Couldn't %s the background service: %s\nInstall it with ./install.sh --service."
                            % ("start" if on else "stop", r.stderr.strip()))
            sw.set_active(not on)

    def _tick_readings(self):
        """Show the current values next to each reading picker (no device access)."""
        if self._sensors is None:
            from .sensors import Sensors
            self._sensors = Sensors()
        r = self._sensors.read()
        for combo, lab in zip(self.metric_combos, self.metric_values):
            m = combo.get_active_id()
            if m and m in L.METRICS:
                v = r.get(m)
                title, unit, fmt = L.METRICS[m]
                lab.set_text("--" if v is None else (fmt.format(v) + " " + unit).strip())
            else:
                lab.set_text("")
        return True

    # -------------------------------------------------------------- cooler --

    def _cooler_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
        box.pack_start(_section("Status"), False, False, 0)
        self.rpm_label = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(self.rpm_label, False, False, 0)
        box.pack_start(_section("Fans"), False, False, 0)
        self.fan_smart = Gtk.RadioButton.new_with_label(None, "Smart (follows temperature)")
        self.fan_fixed = Gtk.RadioButton.new_with_label_from_widget(self.fan_smart, "Fixed speed")
        box.pack_start(self.fan_smart, False, False, 0)
        box.pack_start(self.fan_fixed, False, False, 0)
        self.fan_power = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.fan_power.set_value_pos(Gtk.PositionType.RIGHT)
        box.pack_start(_row("Fixed fan power %", self.fan_power), False, False, 0)
        box.pack_start(_section("Pump"), False, False, 0)
        self.pump = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.pump.set_value_pos(Gtk.PositionType.RIGHT)
        box.pack_start(_row("Pump speed %", self.pump), False, False, 0)
        b = Gtk.Button(label="Apply cooler settings", halign=Gtk.Align.START)
        b.get_style_context().add_class("suggested-action")
        b.connect("clicked", self._apply_cooler)
        box.pack_start(b, False, False, 0)
        return box

    def _apply_cooler(self, *_):
        if self.fan_fixed.get_active():
            fan = {"mode": "Fixed", "fixed_power": int(self.fan_power.get_value())}
        else:
            fan = {"mode": "Smart", "curve": [{"temperature": t, "power": p}
                                              for t, p in ((30, 30), (50, 50), (70, 80), (85, 100))]}
        pump = {"speed_percent": int(self.pump.get_value())}

        def op(d):
            cfg = d.cooler_config()
            cfg["fan"], cfg["pump"] = fan, pump
            d.set_cooler_config(cfg)

        self.run(lambda: self._with_dev(op), lambda _r: self.refresh(), "Applying cooler settings…")


class App(Gtk.Application):
    def __init__(self, page=None):
        super().__init__(application_id="io.github.nateindahood.frostframe")
        self.page = page

    def do_activate(self):
        win = self.get_active_window() or Window(self, self.page)
        if self.page:
            win.stack.set_visible_child_name(self.page)
        win.present()


def main(page=None):
    GLib.set_prgname("frostframe")
    GLib.set_application_name("frostframe %s" % __version__)
    return App(page).run([])
