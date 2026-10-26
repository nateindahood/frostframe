"""Offline tests (no device needed): python3 -m unittest discover -s tests"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from frostframe import cli, library  # noqa: E402
from frostframe import labels as L  # noqa: E402
from frostframe import media, protocol as P, wire  # noqa: E402
from frostframe.device import MAX_FRAME, Device, DeviceError, crc32c  # noqa: E402


class WireTests(unittest.TestCase):
    def test_roundtrip_nested_repeated_enum_sint(self):
        body = {"groups": [{"id": 100, "x": 60, "align": "Left", "line_gap": -10,
                            "labels": [{"id": 101, "gap_left": -3, "text": "CPU °C", "color": 0xDCDCDC}]}]}
        data = wire.encode(P.RUN_CONFIG, body)
        self.assertEqual(wire.decode(P.RUN_CONFIG, data), body)

    def test_known_bytes(self):
        # get_device_info request as the device expects it: header{version 1} + field 100 {dummy "NA"}
        spec = {1: ("header", "msg", P.HEADER), 100: ("get_device_info", "msg", P.DUMMY)}
        data = wire.encode(spec, {"header": {"version": 1}, "get_device_info": {"dummy": "NA"}})
        self.assertEqual(data.hex(), "0a020801a206040a024e41")

    def test_unknown_fields_roundtrip(self):
        raw = wire.encode({1: ("a", "uint", None), 9: ("b", "string", None)}, {"a": 5, "b": "x"})
        partial = wire.decode({1: ("a", "uint", None)}, raw)
        self.assertIn("_unknown", partial)
        self.assertEqual(wire.encode({1: ("a", "uint", None)}, partial), raw)

    def test_response_error(self):
        rsp = wire.encode(P.RESPONSE_SPEC, {"header": {"version": 1},
                                            "error": {"code": "BodyCaseNotSupported", "why": "x"}})
        self.assertEqual(wire.decode(P.RESPONSE_SPEC, rsp)["error"]["code"], "BodyCaseNotSupported")


class Crc32cTests(unittest.TestCase):
    def test_check_value(self):
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)


class LabelTests(unittest.TestCase):
    def test_layout_and_update(self):
        layout = {"metrics": ["cpu-temp", "gpu-load"], "color": 0xFFFFFF, "side": "left"}
        rc = L.build_run_config(layout)
        self.assertEqual(len(rc["groups"]), 2)
        self.assertEqual(rc["groups"][0]["labels"][0]["text"], "CPU TEMP")
        upd = L.values_update(layout, {"cpu-temp": 41.6, "gpu-load": None})
        self.assertEqual(upd, {100: {102: "42"}, 102: {108: "--"}})
        wire.encode(P.RUN_CONFIG, rc)  # encodable

    def test_colors(self):
        self.assertEqual(L.parse_color("#00ff80"), 0x00FF80)
        self.assertEqual(L.parse_color("white"), 0xFFFFFF)
        with self.assertRaises(ValueError):
            L.parse_color("nope")


class MediaTests(unittest.TestCase):
    def test_count_frames(self):
        aud, idr = b"\x00\x00\x00\x01\x09\x10", b"\x00\x00\x00\x01\x65\x88"
        self.assertEqual(media.count_frames((aud + idr) * 3), 3)

    def test_device_name(self):
        self.assertEqual(media.device_name("/x/My Cat (1).GIF", 1120, 1080), "my_cat_1.gif.h264_1120x1080")

    def test_header_roundtrip(self):
        hdr = wire.encode(P.MEDIA_HEADER, {"magic": P.MEDIA_MAGIC, "payload_format": "H264Raw",
                                           "width": 2240, "height": 1080, "frame_count": 3})
        blob = struct.pack("<I", len(hdr)) + hdr + b"\x00\x00\x00\x01\x09\x10"
        self.assertEqual(media.read_header(blob)["width"], 2240)


class SafetyTests(unittest.TestCase):
    def test_no_system_commands(self):
        # request 450 includes "Reboot", which leaves the screen in flashing mode
        self.assertNotIn(450, [num for num, _spec in P.REQUESTS.values()])

    def test_device_names_are_safe_local_names(self):
        for bad in (".bashrc", "../../etc/x", "-y", "..", ""):
            safe = library.safe_name(bad)
            self.assertFalse(safe.startswith((".", "-")) or "/" in safe or not safe, (bad, safe))

    def test_oversized_reply_is_refused(self):
        r, w = os.pipe()
        dev = Device.__new__(Device)
        dev.fd, dev._buf, dev.timeout = r, b"", 1
        os.write(w, P.FRAME_MAGIC + struct.pack("<I", MAX_FRAME + 1))
        with self.assertRaises(DeviceError):
            dev._read_frame(1)
        os.close(r)
        os.close(w)

    def test_kaleidoscope_counts_as_playing(self):
        work = {"media_mode": "Kaleidoscope", "kaleidoscope_file": "k", "single_file": "s"}
        self.assertEqual(list(cli.playing_files(work)), ["k"])


class CliValidationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = cli.CONFIG_DIR, cli.CONFIG_FILE
        cli.CONFIG_DIR = self.tmp.name
        cli.CONFIG_FILE = os.path.join(self.tmp.name, "config.json")
        cli.save_config({"rules": {"conditions": [{"metric": "ram", "above": 90, "target": "file:x"}]}})

    def tearDown(self):
        cli.CONFIG_DIR, cli.CONFIG_FILE = self.saved
        self.tmp.cleanup()

    def quiet(self, argv):
        import contextlib
        import io
        self.err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(self.err):
            return cli.main(argv)

    def test_auto_remove_zero_keeps_rules(self):
        self.assertEqual(self.quiet(["auto", "remove", "0"]), 1)
        self.assertEqual(len(cli.load_config()["rules"]["conditions"]), 1)
        self.assertEqual(self.quiet(["auto", "remove", "1"]), 0)
        self.assertEqual(cli.load_config()["rules"]["conditions"], [])

    def test_unsafe_cooler_values_refused_before_touching_the_device(self):
        for argv in (["pump", "150"], ["pump", "-1"], ["pump", "10"], ["fan", "fixed", "5"],
                     ["fan", "smart", "--curve", "30:-5"], ["fan", "smart", "--curve", "30:10"]):
            self.assertEqual(self.quiet(["--device", "/nonexistent"] + argv), 1, argv)
            self.assertNotIn("nonexistent", self.err.getvalue(), argv)   # refused before opening


if __name__ == "__main__":
    unittest.main()
