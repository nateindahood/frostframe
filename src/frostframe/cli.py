"""frostframe command-line interface."""

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time

from . import __version__
from . import labels as L
from . import library
from . import media
from . import player
from .device import Device, DeviceError, find_devices

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "frostframe")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# Rewritten every second by the monitor, so it lives in the per-user tmpfs, not on disk.
STATE_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or
                          os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))),
                          "frostframe", "state.json")

# Below these the AIO may not cool the CPU properly; going lower needs --force (CLI) or a
# confirmation (GUI).
PUMP_MIN_SAFE = 50
FAN_MIN_SAFE = 20


# ----------------------------------------------------------------- helpers ---

def config_problem():
    """None if the config file is missing or valid, else a message saying what's wrong.

    A broken file is never overwritten (that would throw away the user's playlists and rules);
    a copy is kept as config.json.bad so the user can fix it."""
    try:
        with open(CONFIG_FILE) as f:
            text = f.read()
    except OSError:
        return None
    try:
        json.loads(text)
        return None
    except ValueError as e:
        bad = CONFIG_FILE + ".bad"
        try:
            with open(bad) as f:
                saved = f.read() == text
        except OSError:
            saved = False
        if not saved:
            try:
                with open(bad, "w") as f:
                    f.write(text)
            except OSError:
                pass
        return ("%s has an error (%s), so frostframe won't change your settings until it's fixed. "
                "Fix or delete the file (a copy is in %s)." % (CONFIG_FILE, e, os.path.basename(bad)))


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    problem = config_problem()
    if problem:
        raise ValueError(problem)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        os.unlink(tmp)
        raise


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


def playing_files(work):
    """{file name: "playing" | "left" | "right"} for a user_config "work" section."""
    mode = work.get("media_mode", "Single")
    if mode == "Dual":
        return {work.get("left_file"): "left", work.get("right_file"): "right"}
    if mode == "Kaleidoscope":
        return {work.get("kaleidoscope_file"): "playing"}
    return {work.get("single_file"): "playing"}


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
        playing = set(playing_files(dev.user_config().get("work", {})))
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
    frames = media.read_header(data).get("frame_count", 0)
    print("  %s, %.1f MB, %d frames" % (name, len(data) / 1e6, frames))
    if args.seconds and frames >= args.seconds * media.FPS:
        print("  (cut to the first %g s; use --seconds N for more, or --seconds 0 for no limit)" % args.seconds)
    with open_device(args) as dev:
        dev.upload(name, data, progress=progress_bar)
        print("Uploaded as %s" % name)
        if args.play:
            set_playback("manual")
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
    set_playback("manual")
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


def unused_path(folder, stem, ext):
    """folder/stem.ext, or folder/stem (2).ext etc. if that exists: never overwrite."""
    out = os.path.join(folder, stem + ext)
    n = 2
    while os.path.exists(out):
        out = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
        n += 1
    return out


def save_to_library(dev, name, folder, progress=None):
    """Pull a file from the screen and save it into the library as a playable .mp4."""
    stem = name.split(".h264_")[0]
    stem = os.path.splitext(stem)[0] if stem.lower().endswith((".mp4", ".gif", ".png", ".jpg", ".jpeg")) else stem
    out = unused_path(folder, library.safe_name(stem), ".mp4")
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
            if args.output:      # the user's own choice of name: may overwrite
                out = args.output
                if args.video and not out.endswith(".mp4"):
                    out += ".mp4"
            else:                # a name supplied by the device: sanitized, never overwrites
                out = unused_path(".", library.safe_name(name), ".mp4" if args.video else "")
            if args.video:
                out = media.extract_mp4(data, out)
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
        if not 60 <= args.size <= 240:
            raise DeviceError("--size must be 60-240")
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


def set_playback(mode, playlist=None):
    """mode: manual (you pick files), playlist, or rules. The monitor service follows this."""
    cfg = load_config()
    pb = cfg.setdefault("playback", {})
    pb["mode"] = mode
    if playlist is not None:
        pb["playlist"] = playlist
    save_config(cfg)


def service_running():
    import subprocess
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", "frostframe-monitor.service"]).returncode == 0


def cmd_monitor(args):
    from .sensors import Sensors
    sensors = Sensors()
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    cfg_mtime = None
    cfg = {}
    sent_layout = False
    size = (2240, 1080)
    sizes = {}                 # device file name -> size, refreshed periodically
    files_at = 0.0
    last_sent = None           # file we last told the screen to play
    dev_holder = {}
    durations = player.Durations(lambda name, limit=None: dev_holder["dev"].pull(name, limit=limit))
    engine = player.Engine(lambda f: durations.get(f, sizes.get(f, 0)))
    print("frostframe monitor running (Ctrl+C to stop)")
    try:
        while not stop:
            try:
                m = os.path.getmtime(CONFIG_FILE)
            except OSError:
                m = None
            if m != cfg_mtime:
                cfg_mtime, cfg, sent_layout = m, load_config(), False
            layout = cfg.get("layout", {})
            mode = cfg.get("playback", {}).get("mode", "manual")
            state = {"mode": mode}
            if not (layout.get("metrics") or mode != "manual"):
                sensors.close()            # nothing to show or decide: no GPU polling either
                readings = {}
                engine.tick(cfg, readings, set())
                last_sent = None
            else:
                readings = sensors.read()
                try:
                    with Device(args.device, timeout=3) as dev:
                        dev_holder["dev"] = dev
                        if mode != "manual":
                            if time.monotonic() - files_at > 30 or not sizes:
                                fl = dev.file_list()
                                sizes = {os.path.basename(f.get("path", "")): f.get("size", 0)
                                         for k in ("media", "presets") for f in fl.get(k, [])}
                                files_at = time.monotonic()
                                if last_sent is None:
                                    last_sent = dev.user_config().get("work", {}).get("single_file")
                            want = engine.tick(cfg, readings, set(sizes))
                            state = dict(engine.state)
                            if want and want != last_sent and want in sizes:
                                play(dev, want)
                                last_sent = want
                                if args.verbose:
                                    print("  now playing %s" % want)
                        else:
                            engine.tick(cfg, readings, set())
                            last_sent = None
                        if layout.get("metrics"):
                            if not sent_layout:
                                size = dev.screen_size()
                                dev.set_run_config(L.build_run_config(layout, *size))
                                sent_layout = True
                            dev.update_labels(L.values_update(layout, readings))
                except Exception as e:     # keep running: device unplugged, busy, odd reply, ...
                    sent_layout = False  # re-send the layout once the device is back
                    state["error"] = str(e) if isinstance(e, DeviceError) else "%s: %s" % (type(e).__name__, e)
                    if args.verbose or not isinstance(e, DeviceError):
                        print("  %s" % state["error"], file=sys.stderr)
                finally:
                    dev_holder.pop("dev", None)
            _write_state(state, readings)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sensors.close()


def _write_state(state, readings):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        out = dict(state, time=time.time(), readings=readings)
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(out, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError:
        pass


def read_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        return st if time.time() - st.get("time", 0) < 10 else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------- playlists & rules ---

def parse_target(dev, cfg, text):
    """'playlist:NAME', 'file:NAME', or a bare playlist name / file name or prefix."""
    if text.startswith("playlist:"):
        if text[9:] not in cfg.get("playlists", {}):
            raise DeviceError("no playlist named %r" % text[9:])
        return text
    if text.startswith("file:"):
        return "file:" + resolve_file(dev, text[5:])
    if text in cfg.get("playlists", {}):
        return "playlist:" + text
    return "file:" + resolve_file(dev, text)


def playlist_users(cfg, name):
    """Which automation rules use playlist `name`, as text ('' if none)."""
    rules = cfg.get("rules", {})
    used = ["rule %d" % i for i, r in enumerate(rules.get("conditions", []), 1) if r.get("target") == "playlist:" + name]
    if rules.get("otherwise") == "playlist:" + name:
        used.append("'otherwise'")
    return ", ".join(used)


def describe_target(t):
    return ("playlist %s" % t[9:]) if t.startswith("playlist:") else t[5:] if t.startswith("file:") else str(t)


def _parse_items(args, specs):
    """NAME[:repeats] -> playlist items. A NAME that isn't on the screen but is a file (or a
    library file) is converted and uploaded. Conversion happens with the device closed, so the
    background service can keep using it meanwhile."""
    parsed = []
    for spec in specs:
        name, _, reps = spec.rpartition(":") if ":" in spec and spec.rsplit(":", 1)[1].isdigit() else (spec, "", "1")
        parsed.append([name, max(1, int(reps or 1))])
    uploads = {}
    with open_device(args) as dev:
        w, h = dev.screen_size()
        for item in parsed:
            try:
                item[0] = resolve_file(dev, item[0])
            except DeviceError:
                path = item[0] if os.path.isfile(item[0]) else os.path.join(library.library_dir(load_config()), item[0])
                if not os.path.isfile(path):
                    raise
                uploads[item[0]] = path
    converted = {}
    for key, path in uploads.items():
        print("  converting %s from your library..." % os.path.basename(path))
        converted[key] = media.convert(path, w, h)
    if converted:
        with open_device(args) as dev:
            for name, data in converted.values():
                print("  uploading %s..." % name)
                dev.upload(name, data)
    return [{"file": converted[n][0] if n in converted else n, "repeats": r} for n, r in parsed]


def cmd_playlist(args):
    cfg = load_config()
    pls = cfg.setdefault("playlists", {})
    if args.action == "list":
        if not pls:
            print("No playlists yet. Create one: frostframe playlist create NAME FILE[:repeats]...")
        for name, pl in pls.items():
            print("%s (%d items, %s)" % (name, len(pl.get("items", [])), "shuffle" if pl.get("shuffle") else "in order"))
        return
    if not args.name:
        raise DeviceError("give a playlist name")
    if args.action in ("create", "add"):
        if not args.items:
            raise DeviceError("give one or more files: NAME or NAME:repeats (from the screen or your library)")
        items = _parse_items(args, args.items)
        cfg = load_config()
        pls = cfg.setdefault("playlists", {})
        if args.action == "create":
            if args.name in pls:
                print("(replacing the existing playlist %s)" % args.name)
            pls[args.name] = {"items": items, "shuffle": args.shuffle}
        else:
            pls.setdefault(args.name, {"items": [], "shuffle": args.shuffle})["items"] += items
        save_config(cfg)
        print("Playlist %s: %d items" % (args.name, len(pls[args.name]["items"])))
    elif args.action == "show":
        pl = pls.get(args.name) or {}
        print("%s (%s):" % (args.name, "shuffle" if pl.get("shuffle") else "in order"))
        for i, it in enumerate(pl.get("items", []), 1):
            print("  %d. %s  x%d" % (i, it["file"], it.get("repeats", 1)))
    elif args.action == "delete":
        if args.name not in pls:
            raise DeviceError("no playlist named %r" % args.name)
        pls.pop(args.name)
        pb = cfg.get("playback", {})
        if pb.get("mode") == "playlist" and pb.get("playlist") == args.name:
            pb["mode"] = "manual"
            print("It was playing: switched to manual (the screen keeps the current clip).")
        users = playlist_users(cfg, args.name)
        if users:
            print("Note: automation still uses it (%s); that will be skipped until you change it." % users)
        save_config(cfg)
        print("Deleted playlist %s" % args.name)
    elif args.action == "play":
        if args.name not in pls:
            raise DeviceError("no playlist named %r" % args.name)
        if args.shuffle:
            pls[args.name]["shuffle"] = True
            save_config(cfg)
        set_playback("playlist", args.name)
        print("Playing playlist %s." % args.name)
        if not service_running():
            print("Note: playlists are run by the background service, which isn't running. Start it with "
                  "`systemctl --user enable --now frostframe-monitor` (or keep `frostframe monitor` open).")


def cmd_auto(args):
    cfg = load_config()
    rules = cfg.setdefault("rules", {"conditions": [], "hold": 10, "hysteresis": 3})
    a = args.action
    if a == "list":
        mode = cfg.get("playback", {}).get("mode", "manual")
        print("Automation is %s." % ("ON" if mode == "rules" else "off"))
        for i, r in enumerate(rules.get("conditions", []), 1):
            op = "above" if "above" in r else "below"
            print("  %d. when %s is %s %s -> %s" % (i, r["metric"], op, r[op], describe_target(r["target"])))
        print("  otherwise -> %s" % (describe_target(rules["otherwise"]) if rules.get("otherwise") else "(leave as is)"))
        print("  hold %ss, hysteresis %s" % (rules.get("hold", 10), rules.get("hysteresis", 3)))
        return
    if a in ("on", "off"):
        set_playback("rules" if a == "on" else "manual")
        print("Automation %s." % a)
        if a == "on" and not service_running():
            print("Note: start the background service: systemctl --user enable --now frostframe-monitor")
        return
    if a == "clear":
        rules["conditions"] = []
    elif a == "add":
        if len(args.values) != 4 or args.values[1] not in ("above", "below"):
            raise DeviceError("usage: frostframe auto add METRIC above|below NUMBER TARGET   (e.g. cpu-temp above 75 Hot)")
        metric, op, num, target = args.values
        if metric not in player.RULE_METRICS:
            raise DeviceError("metric must be one of: %s" % ", ".join(player.RULE_METRICS))
        with open_device(args) as dev:
            t = parse_target(dev, cfg, target)
        rules.setdefault("conditions", []).append({"metric": metric, op: float(num), "target": t})
    elif a == "otherwise":
        if len(args.values) != 1:
            raise DeviceError("usage: frostframe auto otherwise TARGET")
        with open_device(args) as dev:
            rules["otherwise"] = parse_target(dev, cfg, args.values[0])
    elif a == "remove":
        conds = rules.setdefault("conditions", [])
        if len(args.values) != 1 or not args.values[0].isdigit() or not 1 <= int(args.values[0]) <= len(conds):
            raise DeviceError("usage: frostframe auto remove N   (N = rule number from `frostframe auto list`, 1-%d)"
                              % len(conds) if conds else "there are no rules to remove")
        del conds[int(args.values[0]) - 1]
    elif a in ("hold", "hysteresis"):
        if len(args.values) != 1:
            raise DeviceError("usage: frostframe auto %s NUMBER" % a)
        rules[a] = max(3 if a == "hold" else 0, float(args.values[0]))
    save_config(cfg)
    args.action = "list"
    cmd_auto(args)


def cmd_stop(args):
    set_playback("manual")
    print("Playlists/automation stopped; the screen keeps playing the current file.")


def cmd_status(args):
    st = read_state()
    cfg = load_config()
    mode = cfg.get("playback", {}).get("mode", "manual")
    print("Mode: %s" % {"manual": "manual (you pick what plays)", "playlist": "playlist %s" % cfg.get("playback", {}).get("playlist"),
                        "rules": "automation"}.get(mode, mode))
    if not st:
        print("Background service: not running" + ("  <- needed for playlists/automation" if mode != "manual" else ""))
        return
    if st.get("file"):
        extra = ""
        if st.get("items"):
            extra = " (item %d/%d, play %d/%d)" % (st["item"], st["items"], st.get("repeat", 1), st.get("repeats", 1))
        print("Playing: %s%s" % (st["file"], extra))
    if st.get("source"):
        print("From: %s" % describe_target(st["source"]))
    if mode == "rules":
        print("Rule in effect: %s" % ("#%d" % (st["rule"] + 1) if st.get("rule") is not None else "otherwise"))
    if st.get("error"):
        print("Last error: %s" % st["error"])


def _check_low(what, percent, minimum, force):
    if percent < minimum and not force:
        raise DeviceError("%s below %d%% may not cool your CPU enough. If you're sure, add --force."
                          % (what, minimum))


def cmd_fan(args):
    if args.mode == "fixed":
        if args.percent is None or not 0 <= args.percent <= 100:
            raise DeviceError("usage: frostframe fan fixed <0-100>")
        _check_low("Fan speed", args.percent, FAN_MIN_SAFE, args.force)
        cfg = {"mode": "Fixed", "fixed_power": args.percent}
    else:
        curve = []
        for point in (args.curve or "30:30,50:50,70:80,85:100").split(","):
            try:
                t, p = (int(x) for x in point.split(":"))
            except ValueError:
                raise DeviceError("curve points are TEMP:PERCENT, e.g. 30:30,50:50,70:80,85:100")
            if not (0 <= t <= 110 and 0 <= p <= 100):
                raise DeviceError("curve point %s: temperature must be 0-110 °C and speed 0-100%%" % point)
            _check_low("Fan speed", p, FAN_MIN_SAFE, args.force)
            curve.append({"temperature": t, "power": p})
        cfg = {"mode": "Smart", "curve": curve}
    with open_device(args) as dev:
        current = dev.cooler_config()
        current["fan"] = cfg
        dev.set_cooler_config(current)
    print("Fan set to %s" % (("fixed %d%%" % args.percent) if args.mode == "fixed" else "smart curve"))


def cmd_pump(args):
    if not 0 <= args.percent <= 100:
        raise DeviceError("pump speed must be 0-100")
    _check_low("Pump speed", args.percent, PUMP_MIN_SAFE, args.force)
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
                wanted = playing_files(dev.user_config().get("work", {}))
                fl = dev.file_list()
                on_device = {os.path.basename(f.get("path", "")) for k in ("media", "presets") for f in fl.get(k, [])}
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
    s.add_argument("--page", choices=["media", "playlists", "display", "cooler"], help="tab to open")
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
    s.add_argument("--seconds", type=float, default=media.DEFAULT_MAX_SECONDS,
                   help="limit video length (default %d; 0 = no limit)" % media.DEFAULT_MAX_SECONDS)
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
    s.add_argument("--size", type=int, default=160, help="value text size, 60-240 (default 160)")
    s.set_defaults(func=cmd_labels)

    s = sub.add_parser("monitor", help="background loop: live readings, playlists, automation")
    s.add_argument("--interval", type=float, default=1.0)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_monitor)

    s = sub.add_parser("fan", help="fan mode (only if fans are connected to the cooler)")
    s.add_argument("mode", choices=["smart", "fixed"])
    s.add_argument("percent", type=int, nargs="?")
    s.add_argument("--curve", help="smart mode curve, e.g. 30:30,50:50,70:80,85:100 (°C:%%)")
    s.add_argument("--force", action="store_true", help="allow speeds below %d%%" % FAN_MIN_SAFE)
    s.set_defaults(func=cmd_fan)

    s = sub.add_parser("pump", help="pump speed (only if the pump is connected to the cooler)")
    s.add_argument("percent", type=int)
    s.add_argument("--force", action="store_true", help="allow speeds below %d%%" % PUMP_MIN_SAFE)
    s.set_defaults(func=cmd_pump)

    s = sub.add_parser("playlist", help="playlists of clips (played by the background service)")
    s.add_argument("action", choices=["list", "create", "add", "show", "play", "delete"])
    s.add_argument("name", nargs="?")
    s.add_argument("items", nargs="*", help="FILE or FILE:repeats (on the screen, or in your library)")
    s.add_argument("--shuffle", action="store_true")
    s.set_defaults(func=cmd_playlist)

    s = sub.add_parser("auto", help="switch media by CPU/GPU temperature or usage")
    s.add_argument("action", choices=["list", "add", "remove", "otherwise", "on", "off", "clear", "hold", "hysteresis"])
    s.add_argument("values", nargs="*")
    s.set_defaults(func=cmd_auto)

    sub.add_parser("status", help="what's playing and why").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="stop playlists/automation (manual mode)").set_defaults(func=cmd_stop)

    sub.add_parser("cooler", help="show fan/pump config and RPM").set_defaults(func=cmd_cooler)
    sub.add_parser("dump", help="print all device settings as JSON").set_defaults(func=cmd_dump)

    args = p.parse_args(argv)
    try:
        return args.func(args) or 0
    except (DeviceError, media.MediaError, ValueError, OSError) as e:
        print("frostframe: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
