"""frostframe GTK 3 interface: `frostframe gui`."""

import os
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from . import __version__  # noqa: E402
from . import cli  # noqa: E402
from . import fonts  # noqa: E402
from . import labels as L  # noqa: E402
from . import library  # noqa: E402
from . import player  # noqa: E402
from . import media  # noqa: E402
from .device import Device, DeviceError, find_devices  # noqa: E402

SERVICE = "frostframe-monitor.service"
SCREEN_PREVIEW_W = 448
PREVIEW_W, PREVIEW_H = 380, 240
PREVIEW_PULL_BYTES = 4 * 1024 * 1024   # the first few seconds of a clip are enough for a preview
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


def _narrow(combo, chars=16):
    """Keep a combo box from growing to fit its longest entry."""
    for cell in combo.get_cells():
        cell.set_property("ellipsize", 3)
        cell.set_property("width-chars", chars)
    return combo


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


def _heading(title):
    lab = Gtk.Label(xalign=0)
    lab.set_markup("<b>%s</b>" % GLib.markup_escape_text(title))
    return lab


def _section(title):
    lab = Gtk.Label(xalign=0)
    lab.set_markup("<b>%s</b>" % GLib.markup_escape_text(title))
    lab.set_margin_top(12)
    return lab


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, page=None):
        super().__init__(application=app, title="frostframe")
        self.set_default_size(1180, 720)
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
        stack.add_titled(self._playlists_page(), "playlists", "Playlists & automation")
        stack.add_titled(self._display_page(), "display", "Display & readings")
        stack.add_titled(self._cooler_page(), "cooler", "Cooler")
        self.stack = stack

        self.status = Gtk.Label(xalign=0, margin=8)
        self.status.get_style_context().add_class("dim-label")
        outer.pack_end(self.status, False, False, 0)

        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)

        self.show_all()
        self.infobar.hide()
        self.progress.hide()
        self._watch_library()
        if page:
            stack.set_visible_child_name(page)
        self.refresh()
        GLib.timeout_add_seconds(2, self._tick_readings)
        GLib.timeout_add_seconds(2, self._tick_now_playing)
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
        self._refresh_play_page()
        self.status.set_text("Connected.")

    # --------------------------------------------------------------- media --

    def _media_page(self):
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, margin=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        left.pack_start(_heading("On your screen"), False, False, 0)
        self.file_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.file_list.connect("row-selected", self._screen_row_selected)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.add(self.file_list)
        left.pack_start(sc, True, True, 0)

        bar = Gtk.Box(spacing=8)
        self.loop_combo = _combo([("single", "Play one file"), ("all", "Loop your files"),
                                  ("random", "Shuffle your files")])
        self.loop_combo.connect("changed", self._loop_changed)
        bar.pack_start(Gtk.Label(label="Playback:"), False, False, 0)
        bar.pack_start(self.loop_combo, False, False, 0)
        left.pack_start(bar, False, False, 0)

        head = Gtk.Box(spacing=6)
        self.lib_title = _heading("Your library")
        head.pack_start(self.lib_title, True, True, 0)
        for label, tip, cb in (("Add files…", "Copy images, GIFs or videos into your library", self._choose_add),
                               ("Open folder", "Open the library folder in your file manager", self._open_library),
                               ("Change folder…", "Use a different folder as your library", self._change_library)):
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            head.pack_start(b, False, False, 0)
        left.pack_start(head, False, False, 0)
        self.lib_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.lib_list.connect("row-selected", self._library_row_selected)
        hint = Gtk.Label(label="Drop GIFs, videos or images anywhere on this window\n"
                               "(or copy them into the library folder) to keep them here.", justify=Gtk.Justification.CENTER)
        hint.get_style_context().add_class("dim-label")
        hint.show()
        self.lib_list.set_placeholder(hint)
        sc2 = Gtk.ScrolledWindow(vexpand=True)
        sc2.add(self.lib_list)
        left.pack_start(sc2, True, True, 0)

        up = Gtk.Box(spacing=8)
        up.pack_start(Gtk.Label(label="Send as:"), False, False, 0)
        self.fit_combo = _combo([("fill", "Fill (crop)"), ("fit", "Fit (letterbox)"), ("stretch", "Stretch")])
        up.pack_start(self.fit_combo, False, False, 0)
        self.half_check = Gtk.CheckButton(label="Half width (dual-screen)")
        up.pack_start(self.half_check, False, False, 0)
        self.play_check = Gtk.CheckButton(label="Play after sending", active=True)
        up.pack_start(self.play_check, False, False, 0)
        left.pack_start(up, False, False, 0)
        self.progress = Gtk.ProgressBar(show_text=True)
        left.pack_start(self.progress, False, False, 0)
        paned.pack1(left, True, False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12)
        right.pack_start(_heading("Preview"), False, False, 0)
        frame = Gtk.Frame()
        self.preview_img = Gtk.Image()
        self.preview_img.set_size_request(PREVIEW_W, PREVIEW_H)
        frame.add(self.preview_img)
        right.pack_start(frame, False, False, 0)
        self.preview_spinner = Gtk.Spinner()
        right.pack_start(self.preview_spinner, False, False, 0)
        self.preview_caption = Gtk.Label(label="Select a file to preview it.", xalign=0, wrap=True,
                                         max_width_chars=40)
        self.preview_caption.get_style_context().add_class("dim-label")
        right.pack_start(self.preview_caption, False, False, 0)
        paned.pack2(right, False, False)
        return paned

    # ----- library

    def _library_path(self):
        return library.library_dir(cli.load_config())

    def _watch_library(self):
        folder = self._library_path()
        if getattr(self, "_lib_monitor", None):
            self._lib_monitor.cancel()
        self._lib_monitor = Gio.File.new_for_path(folder).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self._lib_monitor.connect("changed", lambda *_: self._schedule_library_refresh())
        self.refresh_library()

    def _schedule_library_refresh(self):
        if getattr(self, "_lib_timer", None):
            GLib.source_remove(self._lib_timer)
        self._lib_timer = GLib.timeout_add(500, self._library_timer_fired)

    def _library_timer_fired(self):
        self._lib_timer = None
        self.refresh_library()
        return False

    def refresh_library(self):
        folder = self._library_path()
        self.lib_title.set_markup("<b>Your library</b>  <small>%s</small>" % GLib.markup_escape_text(
            folder.replace(os.path.expanduser("~"), "~", 1)))
        for child in self.lib_list.get_children():
            self.lib_list.remove(child)
        for path in library.list_files(folder):
            self.lib_list.add(self._library_row(path))
        self.lib_list.show_all()

    def _library_row(self, path):
        row = Gtk.ListBoxRow()
        row.path = path
        box = Gtk.Box(spacing=8, margin=4)
        lab = Gtk.Label(label=os.path.basename(path), xalign=0, ellipsize=3)
        lab.set_tooltip_text(path)
        box.pack_start(lab, True, True, 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        sz = Gtk.Label(label="%.1f MB" % (size / 1e6))
        sz.get_style_context().add_class("dim-label")
        box.pack_start(sz, False, False, 0)
        send = Gtk.Button(label="Send to screen")
        send.set_tooltip_text("Convert and upload to the cooler screen")
        send.connect("clicked", lambda *_: self._upload(path))
        box.pack_start(send, False, False, 0)
        rm = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
        rm.set_tooltip_text("Move to the trash (removes it from your library only)")
        rm.connect("clicked", lambda *_: self._trash_library_file(path))
        box.pack_start(rm, False, False, 0)
        row.add(box)
        return row

    def _add_to_library(self, paths):
        folder = self._library_path()
        added, skipped = [], []
        for p in paths:
            try:
                added.append(library.add(folder, p))
            except (media.MediaError, OSError) as e:
                skipped.append("%s (%s)" % (os.path.basename(p), e))
        self.refresh_library()
        if added:
            self.status.set_text("Added %d file(s) to your library." % len(added))
        if skipped:
            self.show_error("Not added: " + "; ".join(skipped))

    def _on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        paths = []
        for uri in data.get_uris() or []:
            try:
                path, _host = GLib.filename_from_uri(uri)
            except GLib.Error:
                continue
            if os.path.isdir(path):
                paths += [os.path.join(path, n) for n in sorted(os.listdir(path)) if library.is_media(n)]
            else:
                paths.append(path)
        if paths:
            self._add_to_library(paths)

    def _choose_add(self, *_):
        dlg = Gtk.FileChooserDialog(title="Add images, GIFs or videos to your library", transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN, select_multiple=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.ACCEPT)
        flt = Gtk.FileFilter()
        flt.set_name("Images, GIFs and videos")
        for ext in library.EXTENSIONS:
            flt.add_pattern("*" + ext)
            flt.add_pattern("*" + ext.upper())
        dlg.add_filter(flt)
        resp = dlg.run()
        paths = dlg.get_filenames()
        dlg.destroy()
        if resp == Gtk.ResponseType.ACCEPT and paths:
            self._add_to_library(paths)

    def _open_library(self, *_):
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(self._library_path()).get_uri(), None)

    def _change_library(self, *_):
        dlg = Gtk.FileChooserDialog(title="Choose your library folder", transient_for=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Use this folder", Gtk.ResponseType.ACCEPT)
        dlg.set_current_folder(self._library_path())
        resp = dlg.run()
        folder = dlg.get_filename()
        dlg.destroy()
        if resp == Gtk.ResponseType.ACCEPT and folder:
            cfg = cli.load_config()
            cfg["library_dir"] = folder
            cli.save_config(cfg)
            self._watch_library()

    def _trash_library_file(self, path):
        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error as e:
            self.show_error("Couldn't move %s to the trash: %s" % (os.path.basename(path), e.message))
        self.refresh_library()

    # ----- preview

    def _library_row_selected(self, _box, row):
        if row is not None and hasattr(row, "path"):
            self.file_list.unselect_all()
            self._show_preview("local", row.path, os.path.basename(row.path))

    def _screen_row_selected(self, _box, row):
        if row is not None and hasattr(row, "device_name"):
            self.lib_list.unselect_all()
            self._show_preview("screen", (row.device_name, row.device_size), row.device_name)

    def _show_preview(self, kind, key, title):
        self._preview_gen = getattr(self, "_preview_gen", 0) + 1
        gen = self._preview_gen
        self.preview_img.clear()
        self.preview_caption.set_text("%s\nLoading preview…" % title)
        self.preview_spinner.start()

        def job():
            if kind == "local":
                return library.preview_for_file(key)
            name, size = key

            def fetch():
                with self.worker.lock:
                    with Device() as d:
                        return d.pull(name, limit=PREVIEW_PULL_BYTES)
            return library.preview_for_device_file(name, size, fetch)

        def target():
            try:
                path, err = job(), None
            except (DeviceError, media.MediaError, OSError) as e:
                path, err = None, str(e)
            GLib.idle_add(self._preview_ready, gen, title, path, err)

        threading.Thread(target=target, daemon=True).start()

    def _preview_ready(self, gen, title, path, err):
        if gen != self._preview_gen:
            return False
        self.preview_spinner.stop()
        if err:
            self.preview_caption.set_text("%s\nNo preview: %s" % (title, err))
            return False
        try:
            if path.lower().endswith(".gif"):
                self.preview_img.set_from_animation(GdkPixbuf.PixbufAnimation.new_from_file(path))
            else:
                self.preview_img.set_from_pixbuf(
                    GdkPixbuf.Pixbuf.new_from_file_at_scale(path, PREVIEW_W, PREVIEW_H, True))
            self.preview_caption.set_text(title)
        except GLib.Error as e:
            self.preview_caption.set_text("%s\nNo preview: %s" % (title, e.message))
        return False

    # ----- files on the screen

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
        for kind, title in (("media", "Uploaded"), ("presets", "Built-in clips")):
            head = Gtk.Label(xalign=0, margin=4)
            head.set_markup("<small><b>%s</b></small>" % title)
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            row.add(head)
            self.file_list.add(row)
            if not self.files[kind]:
                empty = Gtk.Label(label="  (nothing yet: send something from your library)", xalign=0, margin=4)
                empty.get_style_context().add_class("dim-label")
                r = Gtk.ListBoxRow(activatable=False, selectable=False)
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
        row = Gtk.ListBoxRow()
        row.device_name, row.device_size = name, size
        box = Gtk.Box(spacing=6, margin=3)
        state = playing.get(name)
        mark = Gtk.Label(label={"playing": "▶", "left": "◧", "right": "◨"}.get(state, " "))
        mark.set_size_request(18, -1)
        box.pack_start(mark, False, False, 0)
        lab = Gtk.Label(label=name, xalign=0, ellipsize=3)
        lab.set_tooltip_text("%s\n%.1f MB" % (name, size / 1e6))
        box.pack_start(lab, True, True, 0)
        for text, tip, cb in (("Play", "Play full screen", lambda *_: self._play(name)),
                              ("L", "Dual-screen: play on the left half", lambda *_: self._play_side(name, "left")),
                              ("R", "Dual-screen: play on the right half", lambda *_: self._play_side(name, "right"))):
            b = Gtk.Button(label=text)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            box.pack_start(b, False, False, 0)
        save = Gtk.Button.new_from_icon_name("document-save-symbolic", Gtk.IconSize.BUTTON)
        save.set_tooltip_text("Save a copy to your library (as .mp4)")
        save.connect("clicked", lambda *_: self._save_to_library(name))
        box.pack_start(save, False, False, 0)
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

    def _save_to_library(self, name):
        folder = self._library_path()
        self.run(lambda: self._with_dev(lambda d: cli.save_to_library(d, name, folder)),
                 lambda out: (self.status.set_text("Saved %s to your library." % os.path.basename(out)),
                              self.refresh_library()), "Saving %s to your library…" % name)

    def _loop_mode(self):
        return {"single": "Single", "all": "MediaAll", "random": "MediaRand"}[self.loop_combo.get_active_id()]

    def _play(self, name):
        cli.set_playback("manual")
        loop = self._loop_mode()
        self.run(lambda: self._with_dev(lambda d: cli.play(d, name, loop=loop)),
                 lambda _r: self.refresh(), "Switching to %s…" % name)

    def _play_side(self, name, side):
        cli.set_playback("manual")
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
                    cli.set_playback("manual")
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
        page = Gtk.Box(spacing=24, margin=16)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.pack_start(box, True, True, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.pack_start(_section("How it will look"), False, False, 0)
        self.screen_preview = Gtk.DrawingArea()
        self.screen_preview.set_size_request(SCREEN_PREVIEW_W, SCREEN_PREVIEW_W * 1080 // 2240)
        self.screen_preview.connect("draw", self._draw_screen_preview)
        frame = Gtk.Frame()
        frame.add(self.screen_preview)
        right.pack_start(frame, False, False, 0)
        note = Gtk.Label(label="Approximate: the cooler draws the text itself,\nusing the same fonts.",
                         xalign=0)
        note.get_style_context().add_class("dim-label")
        right.pack_start(note, False, False, 0)
        page.pack_start(right, False, False, 0)
        self._last_readings = {}
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
            c.connect("changed", lambda *_: self.screen_preview.queue_draw())
        self.color_btn = Gtk.ColorButton(halign=Gtk.Align.START)
        self.color_btn.set_size_request(90, -1)
        rgb = layout.get("color", 0xFFFFFF)
        self.color_btn.set_rgba(Gdk.RGBA((rgb >> 16 & 255) / 255, (rgb >> 8 & 255) / 255, (rgb & 255) / 255, 1))
        self.color_btn.connect("color-set", lambda *_: self.screen_preview.queue_draw())
        box.pack_start(_row("Text color", self.color_btn, "any color"), False, False, 0)
        self.font_combo = self._font_combo(layout.get("font") if layout.get("font") in L.FONTS else L.FONTS[0])
        self.font_combo.connect("changed", lambda *_: self.screen_preview.queue_draw())
        box.pack_start(_row("Font", self.font_combo), False, False, 0)
        self.side_combo = _combo([("left", "Left half"), ("right", "Right half"), ("full", "Full width")],
                                 layout.get("side", "left"))
        self.side_combo.connect("changed", lambda *_: self.screen_preview.queue_draw())
        box.pack_start(_row("Position", self.side_combo), False, False, 0)
        self.size_spin = Gtk.SpinButton.new_with_range(60, 240, 10)
        self.size_spin.set_value(layout.get("value_size", 160))
        self.size_spin.connect("value-changed", lambda *_: self.screen_preview.queue_draw())
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
        return page

    @staticmethod
    def _font_combo(active):
        """Font picker that shows each font name in that font."""
        store = Gtk.ListStore(str, str, str)   # device font id, display name, Pango family
        for fid in L.FONTS:
            store.append([fid, L.FONT_NAMES[fid], fonts.family(fid)])
        combo = Gtk.ComboBox(model=store, id_column=0)
        cell = Gtk.CellRendererText(size_points=15)
        combo.pack_start(cell, True)
        combo.add_attribute(cell, "text", 1)
        combo.add_attribute(cell, "family", 2)
        combo.set_active_id(active)
        return combo

    def _draw_screen_preview(self, area, cr):
        """Miniature of the cooler screen showing the readings layout as it will be sent."""
        w = area.get_allocated_width()
        h = area.get_allocated_height()
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        layout = self._layout_from_ui()
        scale = w / 2240.0
        rgb = layout["color"]
        cr.set_source_rgb((rgb >> 16 & 255) / 255, (rgb >> 8 & 255) / 255, (rgb & 255) / 255)
        family = fonts.family(layout["font"])
        run = L.build_run_config(layout, 2240, 1080)
        values = L.values_update(layout, self._last_readings)

        def text(t, size_px, x, y):
            pl = PangoCairo.create_layout(cr)
            fd = Pango.FontDescription.from_string(family)
            fd.set_absolute_size(max(1.0, size_px * scale) * Pango.SCALE)
            pl.set_font_description(fd)
            pl.set_text(t, -1)
            cr.move_to(x, y)
            PangoCairo.show_layout(cr, pl)
            _ink, logical = pl.get_pixel_extents()
            return logical.width, logical.height

        for g in run["groups"]:
            title, value, unit = g["labels"]
            gx, gy = g["x"] * scale, g["y"] * scale
            _tw, th = text(title["text"], title["size"], gx + title.get("gap_left", 0) * scale, gy)
            vtext = values.get(g["id"], {}).get(value["id"], "--")
            vw, vh = text(vtext, value["size"], gx, gy + th * 0.9)
            text(unit["text"], unit["size"], gx + vw + 6 * scale, gy + th * 0.9 + vh - unit["size"] * scale * 1.6)
        if not run["groups"]:
            cr.set_source_rgb(0.5, 0.5, 0.5)
            text("No readings selected", 60, 20, h / 2 - 20)
        return False

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
        self._last_readings = r
        self.screen_preview.queue_draw()
        for combo, lab in zip(self.metric_combos, self.metric_values):
            m = combo.get_active_id()
            if m and m in L.METRICS:
                v = r.get(m)
                title, unit, fmt = L.METRICS[m]
                lab.set_text("--" if v is None else (fmt.format(v) + " " + unit).strip())
            else:
                lab.set_text("")
        return True

    # ------------------------------------------------- playlists & rules --

    def _playlists_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        top = Gtk.Box(spacing=10)
        top.pack_start(_heading("What controls the screen:"), False, False, 0)
        self.mode_combo = _combo([("manual", "Manual (I pick what plays)"), ("playlist", "A playlist"),
                                  ("rules", "Automation (by temperature / usage)")])
        self.mode_combo.connect("changed", self._mode_changed)
        top.pack_start(self.mode_combo, False, False, 0)
        self.now_label = Gtk.Label(xalign=0, ellipsize=3)
        self.now_label.get_style_context().add_class("dim-label")
        top.pack_start(self.now_label, True, True, 0)
        outer.pack_start(top, False, False, 0)

        cols = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=560, wide_handle=True)
        outer.pack_start(cols, True, True, 0)

        # --- playlists
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.pack_start(_section("Playlists"), False, False, 0)
        bar = Gtk.Box(spacing=6)
        self.pl_combo = _narrow(Gtk.ComboBoxText(), 12)
        self.pl_combo.connect("changed", lambda *_: self._fill_playlist_items())
        bar.pack_start(self.pl_combo, True, True, 0)
        for label, cb in (("New…", self._pl_new), ("Rename…", self._pl_rename), ("Delete", self._pl_delete)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            bar.pack_start(b, False, False, 0)
        left.pack_start(bar, False, False, 0)
        self.pl_items = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        ph = Gtk.Label(label="No clips yet. Use “Add clips…” below.")
        ph.get_style_context().add_class("dim-label")
        ph.show()
        self.pl_items.set_placeholder(ph)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.add(self.pl_items)
        left.pack_start(sc, True, True, 0)
        bar2 = Gtk.Box(spacing=6)
        add = Gtk.Button(label="Add clips…")
        add.connect("clicked", self._pl_add_dialog)
        bar2.pack_start(add, False, False, 0)
        self.pl_order = _combo([("seq", "Play in order"), ("shuffle", "Shuffle")])
        self.pl_order.connect("changed", self._pl_order_changed)
        bar2.pack_start(self.pl_order, False, False, 0)
        playb = Gtk.Button(label="Play this playlist")
        playb.get_style_context().add_class("suggested-action")
        playb.connect("clicked", self._pl_play)
        bar2.pack_end(playb, False, False, 0)
        left.pack_start(bar2, False, False, 0)
        hint = Gtk.Label(label="“× N” plays a clip N times before moving to the next.", xalign=0)
        hint.get_style_context().add_class("dim-label")
        left.pack_start(hint, False, False, 0)
        left.set_margin_end(12)
        cols.pack1(left, True, False)

        # --- automation
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.pack_start(_section("Automation"), False, False, 0)
        expl = Gtk.Label(label="Rules are checked top to bottom; the first one that matches decides what plays.",
                         xalign=0, wrap=True)
        expl.get_style_context().add_class("dim-label")
        right.pack_start(expl, False, False, 0)
        self.rule_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.pack_start(self.rule_box, False, False, 0)
        addr = Gtk.Button(label="Add rule", halign=Gtk.Align.START)
        addr.connect("clicked", lambda *_: self._add_rule_row({}))
        right.pack_start(addr, False, False, 0)
        ow = Gtk.Box(spacing=8)
        ow.pack_start(Gtk.Label(label="Otherwise play"), False, False, 0)
        self.otherwise_combo = _narrow(Gtk.ComboBoxText(), 14)
        ow.pack_start(self.otherwise_combo, True, True, 0)
        right.pack_start(ow, False, False, 0)
        self.hold_spin = Gtk.SpinButton.new_with_range(3, 600, 1)
        self.hold_spin.set_halign(Gtk.Align.START)
        self.hold_spin.set_tooltip_text("How many seconds a condition must last before the clip changes")
        right.pack_start(_row("Wait before switching (s)", self.hold_spin), False, False, 0)
        self.hyst_spin = Gtk.SpinButton.new_with_range(0, 30, 1)
        self.hyst_spin.set_halign(Gtk.Align.START)
        self.hyst_spin.set_tooltip_text("Once a rule is on, it stays on until the value drops this far "
                                        "past its limit. Stops flicker near the limit.")
        right.pack_start(_row("Switch-back margin", self.hyst_spin), False, False, 0)
        btns = Gtk.Box(spacing=8)
        save = Gtk.Button(label="Save rules")
        save.connect("clicked", lambda *_: self._save_rules(False))
        btns.pack_start(save, False, False, 0)
        on = Gtk.Button(label="Save and turn automation on")
        on.get_style_context().add_class("suggested-action")
        on.connect("clicked", lambda *_: self._save_rules(True))
        btns.pack_start(on, False, False, 0)
        right.pack_start(btns, False, False, 0)
        right.set_margin_start(12)
        scr = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scr.add(right)
        cols.pack2(scr, True, True)
        self._rule_rows = []
        return outer

    # ----- shared helpers

    def _screen_files(self):
        return [os.path.basename(f.get("path", "")) for k in ("media", "presets") for f in self.files.get(k, [])]

    def _targets(self):
        cfg = cli.load_config()
        return ([("playlist:" + n, "Playlist: " + n) for n in cfg.get("playlists", {})] +
                [("file:" + f, f.split(".h264_")[0]) for f in self._screen_files()])

    def _fill_target_combo(self, combo, active):
        combo.remove_all()
        for tid, label in self._targets():
            combo.append(tid, label)
        if active:
            combo.set_active_id(active)

    def _refresh_play_page(self):
        cfg = cli.load_config()
        self._loading_play = True
        self.mode_combo.set_active_id(cfg.get("playback", {}).get("mode", "manual"))
        cur = self.pl_combo.get_active_text() or cfg.get("playback", {}).get("playlist")
        self.pl_combo.remove_all()
        for n in cfg.get("playlists", {}):
            self.pl_combo.append(n, n)
        if cur in cfg.get("playlists", {}):
            self.pl_combo.set_active_id(cur)
        elif cfg.get("playlists"):
            self.pl_combo.set_active(0)
        self._fill_playlist_items()
        rules = cfg.get("rules", {})
        for row in self._rule_rows:
            self.rule_box.remove(row["box"])
        self._rule_rows = []
        for r in rules.get("conditions", []):
            self._add_rule_row(r)
        self._fill_target_combo(self.otherwise_combo, rules.get("otherwise"))
        self.hold_spin.set_value(rules.get("hold", 10))
        self.hyst_spin.set_value(rules.get("hysteresis", 3))
        self._loading_play = False

    def _mode_changed(self, combo):
        if getattr(self, "_loading_play", False):
            return
        mode = combo.get_active_id()
        if mode == "playlist":
            name = self.pl_combo.get_active_text()
            if not name:
                self.show_error("Create a playlist first.")
                self._refresh_play_page()
                return
            cli.set_playback("playlist", name)
        else:
            cli.set_playback(mode)
        if mode != "manual":
            self._ensure_service()

    def _ensure_service(self):
        if not self._service_active():
            r = subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE], capture_output=True, text=True)
            if r.returncode != 0:
                self.show_error("Playlists and automation need the background service: %s\\n"
                                "Install it with ./install.sh --service." % r.stderr.strip())
                return
            self.service_switch.set_active(True)

    def _tick_now_playing(self):
        st = cli.read_state()
        cfg = cli.load_config()
        mode = cfg.get("playback", {}).get("mode", "manual")
        if mode == "manual":
            text = "You choose what plays (Media tab)."
        elif not st:
            text = "Background service not running: turn on “Keep readings live” on the Display tab."
        elif st.get("file"):
            text = "Now: " + st["file"].split(".h264_")[0]
            if st.get("items"):
                text += "  ·  clip %d of %d, play %d of %d" % (st["item"], st["items"], st.get("repeat", 1),
                                                               st.get("repeats", 1))
            if mode == "rules":
                text += "  ·  rule: %s" % ("#%d" % (st["rule"] + 1) if st.get("rule") is not None else "otherwise")
        else:
            text = st.get("note") or st.get("error") or ""
        self.now_label.set_text(text)
        return True

    # ----- playlists

    def _save_playlists(self, pls):
        cfg = cli.load_config()
        cfg["playlists"] = pls
        cli.save_config(cfg)

    def _current_playlist(self):
        name = self.pl_combo.get_active_text()
        pls = cli.load_config().get("playlists", {})
        return name, pls

    def _fill_playlist_items(self):
        for c in self.pl_items.get_children():
            self.pl_items.remove(c)
        name, pls = self._current_playlist()
        pl = pls.get(name, {}) if name else {}
        self._loading_play_order = True
        self.pl_order.set_active_id("shuffle" if pl.get("shuffle") else "seq")
        self._loading_play_order = False
        on_screen = set(self._screen_files())
        for i, it in enumerate(pl.get("items", [])):
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=6, margin=3)
            lab = Gtk.Label(label=it["file"].split(".h264_")[0], xalign=0, ellipsize=3)
            lab.set_tooltip_text(it["file"] + ("" if it["file"] in on_screen or not on_screen
                                               else "\\n(not on the screen any more: skipped)"))
            if on_screen and it["file"] not in on_screen:
                lab.get_style_context().add_class("dim-label")
            box.pack_start(lab, True, True, 0)
            box.pack_start(Gtk.Label(label="×"), False, False, 0)
            spin = Gtk.SpinButton.new_with_range(1, 99, 1)
            spin.set_width_chars(2)
            spin.set_value(it.get("repeats", 1))
            spin.connect("value-changed", lambda w, i=i: self._pl_edit(i, repeats=int(w.get_value())))
            box.pack_start(spin, False, False, 0)
            for icon, tip, delta in (("go-up-symbolic", "Move up", -1), ("go-down-symbolic", "Move down", 1)):
                b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
                b.set_tooltip_text(tip)
                b.connect("clicked", lambda _b, i=i, d=delta: self._pl_move(i, d))
                box.pack_start(b, False, False, 0)
            rm = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
            rm.set_tooltip_text("Remove from playlist")
            rm.connect("clicked", lambda _b, i=i: self._pl_edit(i, remove=True))
            box.pack_start(rm, False, False, 0)
            row.add(box)
            self.pl_items.add(row)
        self.pl_items.show_all()

    def _pl_edit(self, i, repeats=None, remove=False):
        name, pls = self._current_playlist()
        items = pls.get(name, {}).get("items", [])
        if not 0 <= i < len(items):
            return
        if remove:
            items.pop(i)
        elif repeats is not None:
            items[i]["repeats"] = repeats
        self._save_playlists(pls)
        if remove:
            self._fill_playlist_items()

    def _pl_move(self, i, delta):
        name, pls = self._current_playlist()
        items = pls.get(name, {}).get("items", [])
        j = i + delta
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
            self._save_playlists(pls)
            self._fill_playlist_items()

    def _pl_order_changed(self, combo):
        if getattr(self, "_loading_play_order", False):
            return
        name, pls = self._current_playlist()
        if name in pls:
            pls[name]["shuffle"] = combo.get_active_id() == "shuffle"
            self._save_playlists(pls)

    def _ask_name(self, title, initial=""):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_OK", Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=initial, activates_default=True, margin=12)
        dlg.get_content_area().add(entry)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.show_all()
        resp = dlg.run()
        text = entry.get_text().strip()
        dlg.destroy()
        return text if resp == Gtk.ResponseType.OK and text else None

    def _pl_new(self, *_):
        name = self._ask_name("New playlist")
        if not name:
            return
        _cur, pls = self._current_playlist()
        pls.setdefault(name, {"items": [], "shuffle": False})
        self._save_playlists(pls)
        self.pl_combo.append(name, name)
        self.pl_combo.set_active_id(name)
        self._refresh_targets()

    def _pl_rename(self, *_):
        old, pls = self._current_playlist()
        if not old:
            return
        new = self._ask_name("Rename playlist", old)
        if not new or new == old:
            return
        pls[new] = pls.pop(old)
        cfg = cli.load_config()
        cfg["playlists"] = pls
        if cfg.get("playback", {}).get("playlist") == old:
            cfg["playback"]["playlist"] = new
        for r in cfg.get("rules", {}).get("conditions", []):
            if r.get("target") == "playlist:" + old:
                r["target"] = "playlist:" + new
        if cfg.get("rules", {}).get("otherwise") == "playlist:" + old:
            cfg["rules"]["otherwise"] = "playlist:" + new
        cli.save_config(cfg)
        self._refresh_play_page()
        self.pl_combo.set_active_id(new)

    def _pl_delete(self, *_):
        name, pls = self._current_playlist()
        if not name:
            return
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text="Delete the playlist “%s”?" % name)
        dlg.format_secondary_text("The clips stay on the screen and in your library.")
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            pls.pop(name, None)
            self._save_playlists(pls)
            self.pl_combo.set_active(-1)
            self._refresh_play_page()

    def _pl_add_dialog(self, *_):
        name, pls = self._current_playlist()
        if not name:
            name = self._ask_name("New playlist")
            if not name:
                return
            pls.setdefault(name, {"items": [], "shuffle": False})
            self._save_playlists(pls)
            self._refresh_play_page()
            self.pl_combo.set_active_id(name)
        dlg = Gtk.Dialog(title="Add clips to “%s”" % name, transient_for=self, modal=True)
        dlg.set_default_size(520, 520)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Add", Gtk.ResponseType.OK)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        checks = []
        for title, entries in (("On your screen", [("screen", f) for f in self._screen_files()]),
                               ("In your library (uploaded when added)",
                                [("library", p) for p in library.list_files(self._library_path())])):
            h = Gtk.Label(xalign=0, margin=6)
            h.set_markup("<b>%s</b>" % title)
            r = Gtk.ListBoxRow(activatable=False)
            r.add(h)
            lb.add(r)
            for kind, val in entries:
                cb = Gtk.CheckButton(label=os.path.basename(val).split(".h264_")[0], margin_start=12)
                r = Gtk.ListBoxRow()
                r.add(cb)
                lb.add(r)
                checks.append((cb, kind, val))
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.add(lb)
        dlg.get_content_area().pack_start(sc, True, True, 0)
        dlg.show_all()
        resp = dlg.run()
        chosen = [(k, v) for cb, k, v in checks if cb.get_active()]
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not chosen:
            return
        w, h = self.screen

        def op():
            names = []
            uploads = [v for k, v in chosen if k == "library"]
            with Device() as d:
                existing = set(os.path.basename(f.get("path", "")) for k in ("media", "presets")
                               for f in d.file_list().get(k, []))
                for k, v in chosen:
                    if k == "screen":
                        names.append(v)
                        continue
                    dev_name = media.device_name(v, w, h)
                    if dev_name not in existing:
                        dev_name, data = media.convert(v, w, h)
                        d.upload(dev_name, data)
                    names.append(dev_name)
            return names, len(uploads)

        def done(res):
            names, uploaded = res
            cfg = cli.load_config()
            pl = cfg.setdefault("playlists", {}).setdefault(name, {"items": [], "shuffle": False})
            pl["items"] += [{"file": n, "repeats": 1} for n in names]
            cli.save_config(cfg)
            self.status.set_text("Added %d clip(s) to %s%s." % (len(names), name,
                                 (", uploaded %d from your library" % uploaded) if uploaded else ""))
            self.refresh()

        self.run(op, done, "Adding clips to %s…" % name)

    def _pl_play(self, *_):
        name = self.pl_combo.get_active_text()
        if not name:
            self.show_error("Create a playlist first.")
            return
        cli.set_playback("playlist", name)
        self._ensure_service()
        self._refresh_play_page()
        self.status.set_text("Playing playlist %s." % name)

    def _refresh_targets(self):
        for row in self._rule_rows:
            cur = row["target"].get_active_id()
            self._fill_target_combo(row["target"], cur)
        self._fill_target_combo(self.otherwise_combo, self.otherwise_combo.get_active_id())

    # ----- rules

    def _add_rule_row(self, rule):
        box = Gtk.Box(spacing=6)
        box.pack_start(Gtk.Label(label="When"), False, False, 0)
        metric = _narrow(_combo(list(player.RULE_METRICS.items()), rule.get("metric", "cpu-temp")), 11)
        box.pack_start(metric, False, False, 0)
        op = _combo([("above", "is above"), ("below", "is below")], "below" if "below" in rule else "above")
        box.pack_start(op, False, False, 0)
        val = Gtk.SpinButton.new_with_range(0, 120, 1)
        val.set_width_chars(3)
        val.set_value(rule.get("above", rule.get("below", 75)))
        box.pack_start(val, False, False, 0)
        box.pack_start(Gtk.Label(label="→ play"), False, False, 0)
        target = _narrow(Gtk.ComboBoxText(), 14)
        self._fill_target_combo(target, rule.get("target"))
        box.pack_start(target, True, True, 0)
        rm = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
        row = {"box": box, "metric": metric, "op": op, "value": val, "target": target}
        rm.connect("clicked", lambda *_: self._remove_rule_row(row))
        box.pack_start(rm, False, False, 0)
        self.rule_box.pack_start(box, False, False, 0)
        box.show_all()
        self._rule_rows.append(row)

    def _remove_rule_row(self, row):
        self.rule_box.remove(row["box"])
        self._rule_rows.remove(row)

    def _save_rules(self, turn_on):
        conds = []
        for row in self._rule_rows:
            t = row["target"].get_active_id()
            if not t:
                self.show_error("Every rule needs something to play.")
                return
            conds.append({"metric": row["metric"].get_active_id(), row["op"].get_active_id(): row["value"].get_value(),
                          "target": t})
        cfg = cli.load_config()
        cfg["rules"] = {"conditions": conds, "otherwise": self.otherwise_combo.get_active_id(),
                        "hold": self.hold_spin.get_value(), "hysteresis": self.hyst_spin.get_value()}
        cli.save_config(cfg)
        if turn_on:
            cli.set_playback("rules")
            self._ensure_service()
            self._refresh_play_page()
        self.status.set_text("Rules saved%s." % (" and automation turned on" if turn_on else ""))

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
    fonts.register()   # before any text is laid out, so previews can use the screen's fonts
    GLib.set_prgname("frostframe")
    GLib.set_application_name("frostframe %s" % __version__)
    return App(page).run([])
