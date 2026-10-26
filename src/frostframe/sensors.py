"""Linux system readings for on-screen labels: CPU/GPU temperature, load, clocks, RAM, network."""

import glob
import os
import shutil
import subprocess
import threading
import time


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _hwmons():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        yield _read(d + "/name", ""), d


def cpu_temp():
    prefs = {"k10temp": ["Tdie", "Tctl"], "zenpower": ["Tdie", "Tctl"], "coretemp": ["Package id 0"],
             "cpu_thermal": [None]}
    for name, d in _hwmons():
        if name not in prefs:
            continue
        labels = {_read(l, ""): l[:-len("_label")] + "_input" for l in glob.glob(d + "/temp*_label")}
        for want in prefs[name]:
            v = _read(labels.get(want) if want else d + "/temp1_input") if (want is None or want in labels) else None
            if v:
                return int(v) / 1000.0
        v = _read(d + "/temp1_input")
        if v:
            return int(v) / 1000.0
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        if _read(z + "/type") in ("x86_pkg_temp", "acpitz"):
            v = _read(z + "/temp")
            if v:
                return int(v) / 1000.0
    return None


def cpu_clock():
    vals = [int(v) for v in (_read(p) for p in glob.glob(
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if v]
    return sum(vals) / len(vals) / 1000.0 if vals else None


class _Rate:
    """Turns a monotonically increasing counter sample into a per-call rate."""

    def __init__(self, sample):
        self.sample = sample
        self.prev = sample()

    def get(self):
        cur = self.sample()
        prev, self.prev = self.prev, cur
        return cur, prev


def _cpu_times():
    with open("/proc/stat") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    return parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)


def _net_bytes():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            iface, data = line.split(":", 1)
            if iface.strip() == "lo" or iface.strip().startswith(("veth", "docker", "virbr", "br-")):
                continue
            cols = data.split()
            rx += int(cols[0])
            tx += int(cols[8])
    return rx, tx, time.monotonic()


def mem_used_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    total = info["MemTotal"]
    return 100.0 * (total - info.get("MemAvailable", info["MemFree"])) / total


class Nvidia:
    """Keeps one `nvidia-smi -lms` process running and remembers its latest reading.

    Readings older than STALE seconds are dropped (rather than shown frozen), and the process
    is restarted if it exits, e.g. after a driver update."""

    FIELDS = "temperature.gpu,utilization.gpu,clocks.gr,power.draw"
    STALE = 5.0
    RESTART_SECONDS = 30.0

    def __init__(self, interval_ms=1000):
        self.interval_ms = interval_ms
        self.latest = None
        self.at = 0.0
        self.proc = None
        self.started = 0.0
        self._start()

    def _start(self):
        self.started = time.monotonic()
        try:
            self.proc = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=" + self.FIELDS, "--format=csv,noheader,nounits",
                 "-lms", str(self.interval_ms)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self.proc = None
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            vals = []
            for x in line.split(",")[:4]:
                try:
                    vals.append(float(x))
                except ValueError:
                    vals.append(None)
            if len(vals) == 4:
                self.latest = dict(zip(("temp", "load", "clock", "power"), vals))
                self.at = time.monotonic()

    def get(self):
        now = time.monotonic()
        if (self.proc is None or self.proc.poll() is not None) and now - self.started > self.RESTART_SECONDS:
            self.close()
            self._start()
        return self.latest if self.latest and now - self.at < self.STALE else {}

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def amdgpu():
    best = None
    for name, d in _hwmons():
        if name != "amdgpu":
            continue
        dev = os.path.realpath(d + "/device")
        vram = int(_read(dev + "/mem_info_vram_total", "0") or 0)
        if not best or vram > best[2]:
            best = (d, dev, vram)
    if not best:
        return {}
    d, dev, _ = best
    temps = {_read(l, ""): l[:-len("_label")] + "_input" for l in glob.glob(d + "/temp*_label")}
    t = _read(temps.get("edge") or temps.get("junction") or d + "/temp1_input")
    p = _read(d + "/power1_average") or _read(d + "/power1_input")
    f = _read(d + "/freq1_input")
    busy = _read(dev + "/gpu_busy_percent")
    return {"temp": int(t) / 1000.0 if t else None, "load": float(busy) if busy else None,
            "clock": int(f) / 1e6 if f else None, "power": int(p) / 1e6 if p else None}


class Sensors:
    """Call read() about once a second; returns {metric: value or None}."""

    def __init__(self):
        self._cpu = _Rate(_cpu_times)
        self._net = _Rate(_net_bytes)
        self._nv = None           # started on first use
        self._has_nvidia = shutil.which("nvidia-smi") is not None

    def read(self):
        if self._nv is None and self._has_nvidia:
            self._nv = Nvidia()
        (idle, total), (pidle, ptotal) = self._cpu.get()
        load = 100.0 * (1 - (idle - pidle) / (total - ptotal)) if total > ptotal else None
        (rx, tx, t), (prx, ptx, pt) = self._net.get()
        dt = t - pt
        gpu = (self._nv.get() if self._nv else {}) or amdgpu()
        now = time.localtime()
        return {
            "cpu-temp": cpu_temp(), "cpu-load": load, "cpu-clock": cpu_clock(),
            "gpu-temp": gpu.get("temp"), "gpu-load": gpu.get("load"),
            "gpu-clock": gpu.get("clock"), "gpu-power": gpu.get("power"),
            "ram": mem_used_percent(),
            "net-down": (rx - prx) / dt / 1e6 if dt > 0 else None,
            "net-up": (tx - ptx) / dt / 1e6 if dt > 0 else None,
            "time": time.strftime("%H:%M", now), "date": time.strftime("%Y-%m-%d", now),
        }

    def close(self):
        """Stop the GPU reader (read() starts it again if needed)."""
        if self._nv:
            self._nv.close()
            self._nv = None
