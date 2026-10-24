"""On-screen system-info labels: layout building and live value formatting."""

# metric -> (title, unit, format)
METRICS = {
    "cpu-temp": ("CPU TEMP", "°C", "{:.0f}"),
    "cpu-load": ("CPU USAGE", "%", "{:.0f}"),
    "cpu-clock": ("CPU CLOCK", "MHz", "{:.0f}"),
    "gpu-temp": ("GPU TEMP", "°C", "{:.0f}"),
    "gpu-load": ("GPU USAGE", "%", "{:.0f}"),
    "gpu-clock": ("GPU CLOCK", "MHz", "{:.0f}"),
    "gpu-power": ("GPU POWER", "W", "{:.0f}"),
    "ram": ("RAM USAGE", "%", "{:.0f}"),
    "net-down": ("DOWNLOAD", "MB/s", "{:.1f}"),
    "net-up": ("UPLOAD", "MB/s", "{:.1f}"),
    "time": ("TIME", "", "{}"),
    "date": ("DATE", "", "{}"),
}

FONTS = ["roboto-regular", "RubikGlitch-Regular"]  # fonts the device firmware is known to have
NAMED_COLORS = {"white": 0xFFFFFF, "black": 0x000000, "grey": 0xDCDCDC, "gray": 0xDCDCDC,
                "red": 0xFF3B30, "orange": 0xFF9500, "yellow": 0xFFCC00, "green": 0x34C759,
                "cyan": 0x32D7FF, "blue": 0x0A84FF, "purple": 0xBF5AF2, "pink": 0xFF2D92}

FIRST_GROUP = 100   # group ids 100, 102, ...; label ids per group: title, value, unit


def parse_color(text):
    t = text.strip().lower()
    if t in NAMED_COLORS:
        return NAMED_COLORS[t]
    t = t.lstrip("#")
    if len(t) == 6:
        try:
            return int(t, 16)
        except ValueError:
            pass
    raise ValueError("color must be a name (%s) or #RRGGBB" % ", ".join(sorted(NAMED_COLORS)))


def label_ids(index):
    group = FIRST_GROUP + index * 2
    base = 101 + index * 6
    return group, base, base + 1, base + 2   # group, title, value, unit


def build_run_config(layout, screen_w=2240, screen_h=1080):
    """layout: {"metrics": [...], "side": "left"|"right"|"full", "color": int, "font": str,
    "value_size": int}. Returns a run_config body."""
    metrics = layout.get("metrics", [])
    if not metrics:
        return {"groups": []}
    color = layout.get("color", 0xFFFFFF)
    font = layout.get("font", FONTS[0])
    value_size = layout.get("value_size", 160)
    side = layout.get("side", "left")
    margin = 60
    x = margin + (screen_w // 2 if side == "right" else 0)
    width = (screen_w // 2 if side in ("left", "right") else screen_w) - 2 * margin
    slot = (screen_h - 2 * margin) // len(metrics)
    height = min(160, slot)
    top = margin + max(0, (slot - height) // 2)
    groups = []
    for i, metric in enumerate(metrics):
        title, unit, _ = METRICS[metric]
        gid, tid, vid, uid = label_ids(i)
        groups.append({
            "id": gid, "x": x, "y": top + i * slot, "width": width, "height": height,
            "align": "Left", "line_gap": -10,
            "labels": [
                {"id": tid, "line": 1, "gap_left": 13, "font": font, "size": 30, "color": color, "text": title},
                {"id": vid, "font": font, "size": value_size, "color": color, "text": "--"},
                {"id": uid, "font": font, "size": 36, "color": color, "text": unit},
            ],
        })
    return {"groups": groups}


def values_update(layout, readings):
    """Build {group_id: {value_label_id: text}} for the current readings."""
    out = {}
    for i, metric in enumerate(layout.get("metrics", [])):
        gid, _, vid, _ = label_ids(i)
        v = readings.get(metric)
        fmt = METRICS[metric][2]
        out[gid] = {vid: "--" if v is None else fmt.format(v)}
    return out
