"""frostframe command-line interface."""

import argparse
import json
import os
import shutil
import signal
import sys
import time

from . import __version__
from . import labels as L
from . import library
from . import media
from .device import Device, DeviceError, find_devices

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "frostframe")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


# ----------------------------------------------------------------- helpers ---

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def open_device(args):
    return Device(getattr(args, "device", None))


def jsonable(obj):
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items() if k != "_unknown"}
    if isinstance(obj, list):
        return [jsonable(v) for v in obj]
    return obj


def all_files(dev):
    fl = dev.file_list()
    return ([os.path.basename(f.get("path", "")) for f in fl.get("media", [])],
            [os.path.basename(f.get("path", "")) for f in fl.get("presets", [])])


def resolve_file(dev, name):
    """Accept an exact device file name or an unambiguous prefix of one."""
    user, presets = all_files(dev)
    everything = user + presets
    if name in everything:
        return name
    matches = [f for f in everything if f.startswith(name)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise DeviceError("no file on the screen matches %r (see `frostframe files`)" % name)
    raise DeviceError("%r matches several files: %s" % (name, ", ".join(matches)))


def update_user_config(dev, section, changes):
    cfg = dev.user_config()
    cfg.setdefault(section, {}).update(changes)
    dev.set_user_config(cfg)


def progress_bar(done, total):
    pct = done * 100 // total
    sys.stdout.write("\r  uploading %3d%% (%d/%d KiB)" % (pct, done // 1024, total // 1024))
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


# ---------------------------------------------------------------- commands ---

def cmd_info(args):
    with open_device(args) as dev:
        info = dev.hello()
        sysc = dev.sys_config()
        out = sysc.get("output", {})
        print("Product:        %s" % info.get("product_name", "?"))
        print("Firmware:       %s" % info.get("firmware_version", "?"))
        print("Screen app:     %s" % info.get("app_version", "?"))
        print("System:         %s %s" % (info.get("os_name", ""), info.get("os_version", "")))
        print("Serial number:  %s" % info.get("serial_number", "?"))
        print("Display:        %sx%s" % (out.get("width", "?"), out.get("height", "?")))
        try:
            st = dev.storage_status()
            print("Free storage:   %.0f MB%s" % (st.get("available_bytes", 0) / 1e6,
                                                 " (storage reports a problem!)" if st.get("abnormal") else ""))
        except DeviceError:
            pass
        print("Device node:    %s" % dev.node)


def cmd_files(args):
    with open_device(args) as dev:
        fl = dev.file_list()
        current = dev.user_config().get("work", {})
    mode = current.get("media_mode", "Single")
    if mode == "Dual":
        playing = {current.get("left_file"), current.get("right_file")}
    elif mode == "Kaleidoscope":
        playing = {current.get("kaleidoscope_file")}
    else:
        playing = {current.get("single_file")}
    on_device = {os.path.basename(f.get("path", "")) for k in ("media", "presets") for f in fl.get(k, [])}
    for missing in sorted(playing - on_device - {None, ""}):
        print("! The screen is set to play %s, which is no longer on the device (black screen).\n"
              "  Pick another file with `frostframe play NAME`." % missing)
    for title, key in (("Your files", "media"), ("Built-in presets", "presets")):
        items = fl.get(key, [])
        print("%s (%d):" % (title, len(items)))
        for f in items:
            name = os.path.basename(f.get("path", ""))
            print("  %s %-60s %7.1f MB" % ("▶" if name in playing else " ", name, f.get("size", 0) / 1e6))


def cmd_upload(args):
    if not os.path.exists(args.file):
        candidate = os.path.join(library.library_dir(load_config()), args.file)
        if os.path.exists(candidate):
            args.file = candidate
    with open_device(args) as dev:
        w, h = dev.screen_size()
    if args.half:
        w //= 2
    print("Converting %s to %dx%d (%s)..." % (args.file, w, h, args.fit))
    name, data = media.convert(args.file, w, h, mode=args.fit, max_seconds=args.seconds, quality=args.quality)
    print("  %s, %.1f MB, %d frames" % (name, len(data) / 1e6, media.count_frames(data)))
    with open_device(args) as dev:
        dev.upload(name, data, progress=progress_bar)
        print("Uploaded as %s" % name)
        if args.play:
            play(dev, name, side=args.side if args.half else None)
            print("Now playing.")


def play(dev, name, side=None, loop="Single"):
    work = dev.user_config().get("work", {})
    if side:
        work.update({"media_mode": "Dual", "%s_file" % side: name})
        other = "right" if side == "left" else "left"
        if not work.get("%s_file" % other):
            work["%s_file" % other] = name
    else:
        work.update({"media_mode": "Single", "single_file": name})
    work["loop_mode"] = loop
    update_user_config(dev, "work", work)


def cmd_play(args):
    loop = {"single": "Single", "all": "MediaAll", "random": "MediaRand",
            "presets": "PresetAll", "random-presets": "PresetRand"}[args.loop]
    with open_device(args) as dev:
        if args.left or args.right:
            work = dev.user_config().get("work", {})
            work["media_mode"] = "Dual"
            if args.left:
                work["left_file"] = resolve_file(dev, args.left)
            if args.right:
                work["right_file"] = resolve_file(dev, args.right)
            work["loop_mode"] = loop
            update_user_config(dev, "work", work)
            print("Playing %s | %s" % (work.get("left_file"), work.get("right_file")))
        elif args.name:
            name = resolve_file(dev, args.name)
            play(dev, name, loop=loop)
            print("Playing %s" % name)
        else:
            raise DeviceError("give a file name, or --left/--right")


def cmd_rm(args):
    with open_device(args) as dev:
        name = resolve_file(dev, args.name)
        user, _ = all_files(dev)
        if name not in user:
            raise DeviceError("%s is a built-in preset and can't be removed" % name)
        dev.remove_file(name)
        print("Removed %s" % name)


def save_to_library(dev, name, folder, progress=None):
    """Pull a file from the screen and save it into the library as a playable .mp4."""
    stem = name.split(".h264_")[0]
    stem = os.path.splitext(stem)[0] if stem.lower().endswith((".mp4", ".gif", ".png", ".jpg", ".jpeg")) else stem
    out = os.path.join(folder, stem + ".mp4")
    n = 2
    while os.path.exists(out):
        out = os.path.join(folder, "%s (%d).mp4" % (stem, n))
        n += 1
    return media.extract_mp4(dev.pull(name, progress=progress), out)


def cmd_pull(args):
    with open_device(args) as dev:
        names = [resolve_file(dev, n) for n in args.names]
        if args.presets:
            names += all_files(dev)[1]
        if not names:
            raise DeviceError("give file names, or --presets for all built-in clips")
        if args.library:
            folder = library.library_dir(load_config())
            for name in names:
                print("%s -> %s" % (name, save_to_library(dev, name, folder)))
            return
        if len(names) > 1 and args.output:
            raise DeviceError("-o only works with a single file")
        for name in names:
            data = dev.pull(name, progress=progress_bar if len(names) == 1 else None)
            out = args.output or name
            if args.video:
                out = media.extract_mp4(data, out if out.endswith(".mp4") else out + ".mp4")
            else:
                with open(out, "wb") as f:
                    f.write(data)
            print("Saved %s" % out)


def cmd_library(args):
    folder = library.library_dir(load_config())
    if args.action == "path":
        print(folder)
        return
    if args.action == "add":
        if not args.files:
            raise DeviceError("usage: frostframe library add FILE...")
        for f in args.files:
            print("Added %s" % library.add(folder, f))
        return
    files = library.list_files(folder)
    print("Library: %s (%d files)" % (folder, len(files)))
    for f in files:
        print("  %-60s %7.1f MB" % (os.path.basename(f), os.path.getsize(f) / 1e6))


def cmd_brightness(args):
    if not 0 <= args.percent <= 100:
        raise DeviceError("brightness must be 0-100")
    with open_device(args) as dev:
        update_user_config(dev, "display", {"brightness": args.percent, "backlight": True})
    print("Brightness %d%%" % args.percent)


def cmd_screen(args):
    with open_device(args) as dev:
        update_user_config(dev, "display", {"backlight": args.state == "on"})
    print("Screen %s" % args.state)


def cmd_labels(args):
    cfg = load_config()
    layout = cfg.get("layout", {})
    if args.action == "list":
        print("Metrics: " + ", ".join(L.METRICS))
        print("Fonts:   " + ", ".join(L.FONTS))
        print("Colors:  " + ", ".join(sorted(L.NAMED_COLORS)) + " or #RRGGBB")
        print("Current: %s" % (json.dumps(layout) if layout else "(none)"))
        return
    if args.action == "clear":
        layout = {"metrics": []}
    else:
        bad = [m for m in args.metrics if m not in L.METRICS]
        if bad:
            raise DeviceError("unknown metric(s): %s (see `frostframe labels list`)" % ", ".join(bad))
        if not 1 <= len(args.metrics) <= 4:
            raise DeviceError("choose 1 to 4 metrics")
        layout = {"metrics": args.metrics, "side": args.side, "font": args.font,
                  "color": L.parse_color(args.color), "value_size": args.size}
    cfg["layout"] = layout
    save_config(cfg)
    with open_device(args) as dev:
        w, h = dev.screen_size()
        dev.set_run_config(L.build_run_config(layout, w, h))
    if layout["metrics"]:
        print("Labels set: %s. Run `frostframe monitor` (or enable the service) to keep values live."
              % ", ".join(layout["metrics"]))
    else:
        print("Labels cleared.")


def cmd_monitor(args):
    from .sensors import Sensors
    sensors = Sensors()
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    cfg_mtime = None
    layout = {}
    sent_layout = False
    size = (2240, 1080)
    print("frostframe monitor running (Ctrl+C to stop)")
    try:
        while not stop:
            try:
                m = os.path.getmtime(CONFIG_FILE)
            except OSError:
                m = None
            if m != cfg_mtime:
                cfg_mtime, layout, sent_layout = m, load_config().get("layout", {}), False
            readings = sensors.read()
            if layout.get("metrics"):
                try:
                    with Device(args.device, timeout=3) as dev:
                        if not sent_layout:
                            size = dev.screen_size()
                            dev.set_run_config(L.build_run_config(layout, *size))
                            sent_layout = True
                        dev.update_labels(L.values_update(layout, readings))
                except DeviceError as e:
                    sent_layout = False  # re-send the layout once the device is back
                    if args.verbose:
                        print("  %s" % e, file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sensors.close()


def cmd_fan(args):
    if args.mode == "fixed":
        if args.percent is None or not 0 <= args.percent <= 100:
            raise DeviceError("usage: frostframe fan fixed <0-100>")
        cfg = {"mode": "Fixed", "fixed_power": args.percent}
    else:
        curve = []
        for point in (args.curve or "30:30,50:50,70:80,85:100").split(","):
            t, p = point.split(":")
            curve.append({"temperature": int(t), "power": int(p)})
        cfg = {"mode": "Smart", "curve": curve}
    with open_device(args) as dev:
        current = dev.cooler_config()
        current["fan"] = cfg
        dev.set_cooler_config(current)
    print("Fan set to %s" % (("fixed %d%%" % args.percent) if args.mode == "fixed" else "smart curve"))


def cmd_pump(args):
    with open_device(args) as dev:
        current = dev.cooler_config()
        current["pump"] = {"speed_percent": args.percent}
        dev.set_cooler_config(current)
    print("Pump set to %d%%" % args.percent)


def cmd_cooler(args):
    with open_device(args) as dev:
        cfg, st = dev.cooler_config(), dev.cooler_status()
    print("Config: %s" % json.dumps(jsonable(cfg)))
    print("Status: fan %d RPM, pump %d RPM" % (st.get("fan_rpm", 0), st.get("pump_rpm", 0)))
    if not st.get("fan_rpm") and not st.get("pump_rpm"):
        print("  (0 RPM usually means the fans/pump are connected to the motherboard, not the cooler)")


def cmd_dump(args):
    with open_device(args) as dev:
        out = {"device_info": dev.device_info(), "sys_config": dev.sys_config(),
               "user_config": dev.user_config(), "run_config": dev.run_config(),
               "cooler_config": dev.cooler_config(), "cooler_status": dev.cooler_status()}
    print(json.dumps(jsonable(out), indent=2, ensure_ascii=False))


def cmd_gui(args):
    try:
        from .gui import main as gui_main
    except (ImportError, ValueError) as e:
        raise DeviceError("the GUI needs GTK 3 for Python (python3-gi, gir1.2-gtk-3.0): %s" % e)
    return gui_main(args.page)


def cmd_doctor(args):
    ok = True
    devs = find_devices()
    if not devs:
        print("✘ no cooler screen found (USB vendor 391a printer interface)")
        ok = False
    for node, pid, name in devs:
        print("✔ %s (product %s) at %s" % (name, pid, node))
        if not (os.access(node, os.R_OK) and os.access(node, os.W_OK)):
            print("✘ no read/write access to %s: install the udev rule (see README), then re-plug or log out/in" % node)
            ok = False
    print(("✔ ffmpeg found" if shutil.which("ffmpeg") else "✘ ffmpeg not found (needed for uploads)"))
    print(("✔ nvidia-smi found (NVIDIA GPU readings)" if shutil.which("nvidia-smi")
           else "· nvidia-smi not found (fine without an NVIDIA GPU)"))
    if devs and ok:
        try:
            with open_device(args) as dev:
                info = dev.device_info()
                print("✔ talking to the screen: %s firmware %s" % (info.get("product_name"), info.get("firmware_version")))
                work = dev.user_config().get("work", {})
                fl = dev.file_list()
                on_device = {os.path.basename(f.get("path", "")) for k in ("media", "presets") for f in fl.get(k, [])}
                mode = work.get("media_mode", "Single")
                wanted = ([work.get("left_file"), work.get("right_file")] if mode == "Dual"
                          else [work.get("kaleidoscope_file")] if mode == "Kaleidoscope" else [work.get("single_file")])
                for w in wanted:
                    if w and w not in on_device:
                        print("✘ set to play %s, which isn't on the device (black screen): use `frostframe play`" % w)
                        ok = False
        except DeviceError as e:
            print("✘ %s" % e)
            ok = False
    return 0 if ok else 1


# --------------------------------------------------------------------- main ---

def main(argv=None):
    p = argparse.ArgumentParser(prog="frostframe", description="Control AIO cooler LCD screens on Linux.")
    p.add_argument("--version", action="version", version="frostframe " + __version__)
    p.add_argument("--device", help="device node (default: auto-detect)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("gui", help="open the graphical interface")
    s.add_argument("--page", choices=["media", "display", "cooler"], help="tab to open")
    s.set_defaults(func=cmd_gui)
    sub.add_parser("info", help="show device information").set_defaults(func=cmd_info)
    sub.add_parser("doctor", help="check setup and permissions").set_defaults(func=cmd_doctor)
    sub.add_parser("files", help="list media on the screen").set_defaults(func=cmd_files)

    s = sub.add_parser("upload", help="convert and upload an image, GIF or video")
    s.add_argument("file", help="a path, or a file name from your library")
    s.add_argument("--fit", choices=["fill", "fit", "stretch"], default="fill",
                   help="fill: crop to fill (default); fit: letterbox; stretch: distort")
    s.add_argument("--half", action="store_true", help="half-width clip for dual-screen mode")
    s.add_argument("--side", choices=["left", "right"], default="left", help="with --half --play: which side")
    s.add_argument("--seconds", type=float, help="limit video length")
    s.add_argument("--quality", type=int, default=23, help="x264 CRF, lower = better/larger (default 23)")
    s.add_argument("--play", action="store_true", help="start playing it after upload")
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("play", help="play a file (name or unique prefix)")
    s.add_argument("name", nargs="?")
    s.add_argument("--left", help="dual-screen mode: left file")
    s.add_argument("--right", help="dual-screen mode: right file")
    s.add_argument("--loop", choices=["single", "all", "random", "presets", "random-presets"], default="single")
    s.set_defaults(func=cmd_play)

    s = sub.add_parser("pull", help="download files from the screen")
    s.add_argument("names", nargs="*", metavar="NAME")
    s.add_argument("--presets", action="store_true", help="all built-in clips")
    s.add_argument("--library", action="store_true", help="save as .mp4 into your library")
    s.add_argument("-o", "--output", help="output file (single file only)")
    s.add_argument("--video", action="store_true", help="save as a playable .mp4 instead of the raw device file")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("library", help="your local media folder (default ~/Pictures/frostframe)")
    s.add_argument("action", nargs="?", choices=["list", "add", "path"], default="list")
    s.add_argument("files", nargs="*")
    s.set_defaults(func=cmd_library)

    s = sub.add_parser("rm", help="delete one of your files from the screen")
    s.add_argument("name")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("brightness", help="set screen brightness")
    s.add_argument("percent", type=int)
    s.set_defaults(func=cmd_brightness)

    s = sub.add_parser("screen", help="turn the screen on or off")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("labels", help="system-info text on the screen")
    s.add_argument("action", choices=["set", "clear", "list"])
    s.add_argument("metrics", nargs="*")
    s.add_argument("--side", choices=["left", "right", "full"], default="left")
    s.add_argument("--color", default="white")
    s.add_argument("--font", choices=L.FONTS, default=L.FONTS[0])
    s.add_argument("--size", type=int, default=160, help="value text size (default 160)")
    s.set_defaults(func=cmd_labels)

    s = sub.add_parser("monitor", help="keep on-screen labels updated with live readings")
    s.add_argument("--interval", type=float, default=1.0)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("fan", help="fan mode (only if fans are connected to the cooler)")
    s.add_argument("mode", choices=["smart", "fixed"])
    s.add_argument("percent", type=int, nargs="?")
    s.add_argument("--curve", help="smart mode curve, e.g. 30:30,50:50,70:80,85:100 (°C:%%)")
    s.set_defaults(func=cmd_fan)

    s = sub.add_parser("pump", help="pump speed (only if the pump is connected to the cooler)")
    s.add_argument("percent", type=int)
    s.set_defaults(func=cmd_pump)

    sub.add_parser("cooler", help="show fan/pump config and RPM").set_defaults(func=cmd_cooler)
    sub.add_parser("dump", help="print all device settings as JSON").set_defaults(func=cmd_dump)

    args = p.parse_args(argv)
    try:
        return args.func(args) or 0
    except (DeviceError, media.MediaError, ValueError) as e:
        print("frostframe: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
