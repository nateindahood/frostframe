"""Playlists and condition-based media switching.

The screen itself can only play one file (or loop all / shuffle all files). frostframe's
background service (`frostframe monitor`) uses this engine to decide, every second, which file
the screen should be playing, and switches it when that changes.

Config (in ~/.config/frostframe/config.json):

    "playlists": {"Chill": {"items": [{"file": "cat.gif.h264_2240x1080", "repeats": 2}, ...],
                            "shuffle": false}},
    "playback":  {"mode": "manual" | "playlist" | "rules", "playlist": "Chill"},
    "rules":     {"conditions": [{"metric": "cpu-temp", "above": 75, "target": "playlist:Hot"}, ...],
                  "otherwise": "playlist:Chill", "hold": 10, "hysteresis": 3}

A target is "playlist:<name>" or "file:<device file name>".
"""

import json
import os
import random
import struct
import time

from . import media

RULE_METRICS = {"cpu-temp": "CPU temperature", "cpu-load": "CPU usage",
                "gpu-temp": "GPU temperature", "gpu-load": "GPU usage", "ram": "RAM usage"}
MIN_ITEM_SECONDS = 2.0       # never switch faster than this
DEFAULT_SECONDS = 10.0       # if a clip's length can't be determined
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "frostframe",
                     "durations.json")


# ------------------------------------------------------------- durations ---

def duration_from_file(data):
    """Seconds of a device media file (with or without frostframe's media header)."""
    fps = 30
    if len(data) > 8 and not data.startswith(b"\x00\x00"):
        try:
            hdr = media.read_header(data)
            fps = hdr.get("frame_rate") or 30
            if hdr.get("frame_count"):
                return hdr["frame_count"] / float(fps)
            data = data[4 + struct.unpack("<I", data[:4])[0]:]
        except Exception:
            pass
    frames = media.count_frames(data)
    return frames / float(fps) if frames else DEFAULT_SECONDS


class Durations:
    """Clip lengths by device file name, cached on disk (name + size identify a file)."""

    RETRY_SECONDS = 60

    def __init__(self, fetch):
        self.fetch = fetch        # fetch(name, limit=None) -> bytes from the device
        self.failed = {}          # key -> time of the last failed lookup
        try:
            with open(CACHE) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def get(self, name, size):
        key = "%s|%d" % (name, size)
        if key not in self.cache:
            if time.monotonic() - self.failed.get(key, -1e9) < self.RETRY_SECONDS:
                return DEFAULT_SECONDS
            try:
                data = self.fetch(name, limit=65536)          # header is enough for our uploads
                if data.startswith(b"\x00\x00"):               # headerless (built-in): count frames
                    data = self.fetch(name)
            except Exception:       # e.g. the file was just deleted: guess, retry later
                self.failed[key] = time.monotonic()
                return DEFAULT_SECONDS
            self.cache[key] = round(duration_from_file(data), 3)
            try:
                os.makedirs(os.path.dirname(CACHE), exist_ok=True)
                with open(CACHE + ".tmp", "w") as f:
                    json.dump(self.cache, f)
                os.replace(CACHE + ".tmp", CACHE)
            except OSError:
                pass
        return self.cache[key]


# ----------------------------------------------------------------- engine ---

class PlaylistRun:
    """Walks one playlist: each item plays `repeats` times, then the next (wrapping around)."""

    def __init__(self, playlist, available, duration, now):
        self.items = [it for it in playlist.get("items", []) if it.get("file") in available]
        self.shuffle = bool(playlist.get("shuffle"))
        self.duration = duration
        self.order = []
        self.pos = -1
        self.started = now
        self._next(now)

    def update(self, playlist, available, now):
        """The playlist was edited or files came and went: carry on with the current clip
        if it's still there (and still counting its plays), else start the next one."""
        cur = self.current()
        self.items = [it for it in playlist.get("items", []) if it.get("file") in available]
        self.shuffle = bool(playlist.get("shuffle"))
        self.order = list(range(len(self.items)))
        if self.shuffle:
            random.shuffle(self.order)
        same = [i for i, it in enumerate(self.items) if cur and it.get("file") == cur.get("file")]
        if same:
            self.pos = self.order.index(same[0])
        else:
            self.pos = -1
            self._next(now)

    def _next(self, now):
        if not self.items:
            return
        self.pos += 1
        if self.pos >= len(self.order):
            self.order = list(range(len(self.items)))
            if self.shuffle and len(self.order) > 1:
                last = self.order[self.pos - 1] if self.pos > 0 and self.pos - 1 < len(self.order) else None
                random.shuffle(self.order)
                if last is not None and self.order[0] == last:   # avoid repeating across cycles
                    self.order.append(self.order.pop(0))
            self.pos = 0
        self.started = now

    def current(self):
        return self.items[self.order[self.pos]] if self.items else None

    def tick(self, now):
        item = self.current()
        if item is None:
            return None
        length = max(MIN_ITEM_SECONDS, self.duration(item["file"]) * max(1, int(item.get("repeats", 1))))
        if len(self.items) > 1 and now - self.started >= length:
            self._next(now)
        cur = self.current()
        return cur["file"] if cur else None

    def status(self, now):
        item = self.current()
        if item is None:
            return {}
        d = self.duration(item["file"])
        reps = max(1, int(item.get("repeats", 1)))
        elapsed = now - self.started
        return {"item": self.pos + 1, "items": len(self.items), "file": item["file"],
                "repeat": min(reps, int(elapsed // d) + 1) if d else 1, "repeats": reps}


class Engine:
    """Decides what the screen should play. Call tick() about once a second."""

    def __init__(self, duration):
        self.duration = duration        # duration(file) -> seconds
        self.source = None              # current target string
        self.run = None
        self.active_rule = None         # index of the rule in effect (None = "otherwise")
        self.pending = None             # (rule index, since) waiting out the hold time
        self.state = {}
        self._playlist_sig = None       # what the running playlist was built from

    def _rule_matches(self, rule, readings, active):
        v = readings.get(rule.get("metric"))
        if v is None:
            return False
        hyst = float(active and self._hyst or 0)
        if "above" in rule:
            return v >= float(rule["above"]) - hyst
        if "below" in rule:
            return v <= float(rule["below"]) + hyst
        return False

    def _choose_rule(self, rules, readings, now):
        conds = rules.get("conditions", [])
        self._hyst = rules.get("hysteresis", 3)
        hold = float(rules.get("hold", 10))
        want = None
        for i, r in enumerate(conds):
            if self._rule_matches(r, readings, i == self.active_rule):
                want = i
                break
        if want == self.active_rule:
            self.pending = None
        elif self.pending is None or self.pending[0] != want:
            self.pending = (want, now)
        elif now - self.pending[1] >= hold:
            self.active_rule, self.pending = want, None
        if self.active_rule is not None and self.active_rule < len(conds):
            return conds[self.active_rule].get("target")
        self.active_rule = None
        return rules.get("otherwise")

    def tick(self, cfg, readings, available, now=None):
        """Return the device file name the screen should play now, or None to leave it alone."""
        now = time.monotonic() if now is None else now
        playback = cfg.get("playback", {})
        mode = playback.get("mode", "manual")
        if mode == "rules":
            target = self._choose_rule(cfg.get("rules", {}), readings, now)
        elif mode == "playlist":
            target = "playlist:%s" % playback.get("playlist", "")
        else:
            self.source, self.run, self.state = None, None, {"mode": "manual"}
            return None
        if not target:
            self.state = {"mode": mode, "note": "nothing to play"}
            return None
        pl = cfg.get("playlists", {}).get(target[9:]) if target.startswith("playlist:") else None
        sig = (json.dumps(pl, sort_keys=True), frozenset(available)) if pl else None
        if target != self.source or sig != self._playlist_sig:
            if target == self.source and pl and self.run:
                self.run.update(pl, available, now)
            else:
                self.run = PlaylistRun(pl, available, self.duration, now) if pl else None
            self.source = target
        self._playlist_sig = sig
        if target.startswith("file:"):
            f = target[5:]
            self.state = {"mode": mode, "source": target, "file": f, "rule": self.active_rule}
            return f if f in available else None
        if self.run is None:
            self.state = {"mode": mode, "source": target, "note": "playlist not found or empty"}
            return None
        f = self.run.tick(now)
        self.state = dict(self.run.status(now), mode=mode, source=target, rule=self.active_rule)
        return f
