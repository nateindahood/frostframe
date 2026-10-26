"""Talking to the cooler screen over its USB printer-class interface (Linux usblp)."""

import errno
import glob
import os
import select
import struct
import time

from . import protocol as P
from . import wire

VENDOR_ID = "391a"
CHUNK_SIZE = 256 * 1024
MAX_FRAME = 16 * 1024 * 1024    # largest reply we accept; real ones are well under 1 MiB


class DeviceError(Exception):
    pass


def find_devices():
    """Return [(device_node, product_id, product_name)] for connected screens."""
    found = []
    for lp in sorted(glob.glob("/sys/class/usbmisc/lp*")):
        usb = os.path.realpath(os.path.join(lp, "device", ".."))
        try:
            vid, pid, name = (_read_attr(usb, a) for a in ("idVendor", "idProduct", "product"))
        except OSError:
            continue
        if vid == VENDOR_ID:
            found.append(("/dev/usb/" + os.path.basename(lp), pid, name))
    return found


def _read_attr(usb, attr):
    with open(os.path.join(usb, attr)) as f:
        return f.read().strip()


def crc32c(data):
    """CRC-32C (Castagnoli), as used for file transfers."""
    table = _crc32c_table()
    crc = 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ table[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


def descramble(data, start=0):
    """Undo the device's read obfuscation: byte i is XOR-ed with ((start + i) & 0xFF)."""
    if not data:
        return data
    key = bytes((start + i) & 0xFF for i in range(256))
    key = (key * (len(data) // 256 + 2))[: len(data)]
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


_TABLE = None


def _crc32c_table():
    global _TABLE
    if _TABLE is None:
        _TABLE = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ (0x82F63B78 if c & 1 else 0)
            _TABLE.append(c)
    return _TABLE


class Device:
    """One open connection. Use as a context manager."""

    def __init__(self, node=None, timeout=5.0):
        if node is None:
            devs = find_devices()
            if not devs:
                raise DeviceError("No cooler screen found (USB vendor %s). Is it connected? "
                                  "Run `frostframe doctor`." % VENDOR_ID)
            node = devs[0][0]
        self.node = node
        self.timeout = timeout
        self._buf = b""
        self._track = int(time.time()) & 0xFFFFF
        self.fd = None
        deadline = time.monotonic() + timeout
        while True:  # the kernel's usblp driver allows one opener at a time; wait our turn
            try:
                self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
                break
            except OSError as e:
                if e.errno == errno.EBUSY and time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                self._open_error(node, e)

    @staticmethod
    def _open_error(node, e):
        try:
            raise e
        except PermissionError:
            raise DeviceError("No permission to open %s. Install the udev rule (see README) and "
                              "re-plug the device or log out and back in." % node)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise DeviceError("%s is busy: another program (e.g. the vendor app) is using the "
                                  "screen. Close it first." % node)
            raise DeviceError("Cannot open %s: %s" % (node, e.strerror))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ framing --

    def _write_frame(self, payload):
        frame = P.FRAME_MAGIC + struct.pack("<I", len(payload)) + payload
        view = memoryview(frame)
        deadline = time.monotonic() + self.timeout
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise DeviceError("write timed out")
                select.select([], [self.fd], [], 0.1)
            except OSError as e:     # unplugged, USB reset, ...
                raise DeviceError("lost connection to the screen: %s" % e.strerror)

    def _read_frame(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if len(self._buf) >= 8:
                if self._buf[:4] != P.FRAME_MAGIC:
                    i = self._buf.find(P.FRAME_MAGIC, 1)
                    self._buf = self._buf[i:] if i > 0 else b""
                    continue
                n = struct.unpack("<I", self._buf[4:8])[0]
                if n > MAX_FRAME:
                    self._buf = b""
                    raise DeviceError("the screen sent an oversized reply (%d bytes)" % n)
                if len(self._buf) >= 8 + n:
                    payload, self._buf = self._buf[8:8 + n], self._buf[8 + n:]
                    return payload
            left = deadline - time.monotonic()
            if left <= 0:
                raise DeviceError("timed out waiting for the device")
            r, _, _ = select.select([self.fd], [], [], min(left, 0.5))
            if r:
                try:
                    chunk = os.read(self.fd, 1 << 20)
                except BlockingIOError:
                    continue
                except OSError as e:
                    raise DeviceError("lost connection to the screen: %s" % e.strerror)
                if not chunk:
                    time.sleep(0.01)
                self._buf += chunk

    def _drain(self):
        """Drop anything already waiting, e.g. a late reply to a request that timed out,
        so it can't be mistaken for the answer to the next one."""
        self._buf = b""
        while select.select([self.fd], [], [], 0)[0]:
            try:
                if not os.read(self.fd, 1 << 20):
                    break
            except BlockingIOError:
                break
            except OSError as e:
                raise DeviceError("lost connection to the screen: %s" % e.strerror)

    # ----------------------------------------------------------- requests --

    def request(self, name, body=None, track_id=None, timeout=None):
        """Send one request and return (response_name, response_dict)."""
        num, spec = P.REQUESTS[name]
        header = {"version": 1}
        if track_id is not None:
            header["track_id"] = track_id
        payload = wire.encode({1: ("header", "msg", P.HEADER), num: (name, "msg", spec)},
                              {"header": header, name: body or {}})
        self._drain()
        self._write_frame(payload)
        while True:
            rsp = wire.decode(P.RESPONSE_SPEC, self._read_frame(timeout or self.timeout))
            kind = next((k for k in rsp if k not in ("header", "error", "_unknown")), None)
            if kind == "app_exception":  # unsolicited notification; keep waiting
                continue
            err = rsp.get("error", {})
            if err.get("code", "Success") not in ("Success", 0):
                raise DeviceError("device refused %s: %s" % (name, err.get("why") or err.get("code")))
            return kind, rsp.get(kind, {})

    def _get(self, name, expect):
        kind, body = self.request(name, {"dummy": "NA"} if name != "get_device_auth" else {"key": 1})
        if kind != expect:
            raise DeviceError("unexpected reply %r to %s" % (kind, name))
        return body

    # ---------------------------------------------------------------- API --

    def hello(self):
        """The vendor app's start-up sequence; returns device info."""
        info = self._get("get_device_info", "device_info")
        self._get("get_sys_config", "sys_config")
        self._get("get_device_auth", "device_auth")
        return info

    def device_info(self):
        return self._get("get_device_info", "device_info")

    def sys_config(self):
        return self._get("get_sys_config", "sys_config")

    def screen_size(self):
        out = self.sys_config().get("output", {})
        return out.get("width", 2240), out.get("height", 1080)

    def file_list(self):
        return self._get("get_file_list", "file_list")

    def user_config(self):
        return self._get("get_user_config", "user_config")

    def run_config(self):
        return self._get("get_run_config", "run_config")

    def cooler_config(self):
        return self._get("get_cooler_config", "cooler_config")

    def cooler_status(self):
        return self._get("get_cooler_status", "cooler_status")

    def storage_status(self):
        return self._get("get_storage_status", "storage_status")

    def set_user_config(self, cfg):
        self.request("user_config", cfg)

    def set_run_config(self, cfg):
        self.request("run_config", cfg)

    def set_cooler_config(self, cfg):
        self.request("cooler_config", cfg)

    def update_labels(self, groups):
        """groups: {group_id: {label_id: text}}"""
        body = {"groups": [{"id": g, "labels": [{"id": l, "text": t} for l, t in labels.items()]}
                           for g, labels in groups.items()]}
        self.request("label_update", body)

    def pull(self, name, progress=None, limit=None):
        """Download a file from the screen (returns the original bytes; at most `limit` bytes).

        The device sends file contents XOR-ed with the byte position (i & 0xFF); undone here."""
        session = (self._track + 1) & 0xFFFFFFFF
        buf = bytearray()
        size = None
        while size is None or len(buf) < size:
            kind, body = self.request("file_pull", {"name": name.encode(), "session_id": session,
                                                    "offset": len(buf)}, timeout=15)
            if kind != "file_pull_result" or body.get("status", "OK") not in ("OK", 0):
                raise DeviceError("could not read %s from the screen" % name)
            size = body.get("file_size", 0)
            chunk = body.get("data", b"")
            if not chunk:
                break
            buf += chunk
            if progress:
                progress(len(buf), size)
            if limit and len(buf) >= limit:
                return descramble(bytes(buf))
        if len(buf) != size:
            raise DeviceError("incomplete read of %s (%d of %d bytes)" % (name, len(buf), size or 0))
        return descramble(bytes(buf))

    def remove_file(self, name, file_type="media"):
        self.request("file_remove", {"name": name, "file_type": file_type})

    def upload(self, name, data, file_type="media", progress=None):
        """Upload a prepared file (header + payload). Raises DeviceError on failure."""
        try:
            free = self.storage_status().get("available_bytes")
        except DeviceError:
            free = None
        if free is not None and len(data) > free:
            raise DeviceError("not enough space on the screen: %s needs %.1f MB, %.1f MB free. Delete "
                              "something first, or make the clip shorter (--seconds)."
                              % (name, len(data) / 1e6, free / 1e6))
        track = self._track = (self._track + 1) & 0xFFFFFFFF
        self._expect_status(self.request("file_begin", {"name": name.encode(), "size": len(data)},
                                         track_id=track), "file_begin_status")
        sent = 0
        while sent < len(data):
            chunk = data[sent:sent + CHUNK_SIZE]
            self._expect_status(self.request("file_data", {"data": chunk}, track_id=track, timeout=30),
                                "file_data_status")
            sent += len(chunk)
            if progress:
                progress(sent, len(data))
        self._expect_status(self.request("file_end", {"file_type": file_type.encode(), "crc": crc32c(data)},
                                         track_id=track, timeout=60), "file_end_status")

    @staticmethod
    def _expect_status(reply, expect):
        kind, body = reply
        status = body.get("status", "OK")
        if kind != expect or status not in ("OK", 0):
            raise DeviceError("upload failed at %s: %s" % (expect, status))
