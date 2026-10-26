"""Offline tests for playlists and condition rules (fake clock, no device)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from frostframe import player  # noqa: E402

DUR = {"a": 2.0, "b": 3.0, "c": 4.0, "hot": 5.0, "cool": 5.0}
AVAILABLE = set(DUR)


def run(engine, cfg, seconds, readings=None, start=0.0, step=1.0):
    out, t = [], start
    while t < start + seconds:
        out.append(engine.tick(cfg, readings or {}, AVAILABLE, now=t))
        t += step
    return out


class PlaylistTests(unittest.TestCase):
    def test_sequence_with_repeats(self):
        cfg = {"playback": {"mode": "playlist", "playlist": "p"},
               "playlists": {"p": {"items": [{"file": "a", "repeats": 2}, {"file": "b", "repeats": 1}]}}}
        e = player.Engine(DUR.get)
        # a: 2 s x 2 = 4 s, then b: 3 s, then a again
        self.assertEqual(run(e, cfg, 9), ["a"] * 4 + ["b"] * 3 + ["a"] * 2)

    def test_shuffle_plays_every_item_once_per_cycle(self):
        cfg = {"playback": {"mode": "playlist", "playlist": "p"},
               "playlists": {"p": {"shuffle": True, "items": [{"file": f} for f in ("a", "b", "c")]}}}
        e = player.Engine(lambda f: 2.0)
        seq = run(e, cfg, 6)            # 3 items x 2 s
        self.assertEqual(sorted(set(seq)), ["a", "b", "c"])
        self.assertEqual(len(seq), 6)

    def test_missing_files_are_skipped(self):
        cfg = {"playback": {"mode": "playlist", "playlist": "p"},
               "playlists": {"p": {"items": [{"file": "gone"}, {"file": "b"}]}}}
        e = player.Engine(DUR.get)
        self.assertEqual(set(run(e, cfg, 5)), {"b"})

    def test_manual_mode_leaves_screen_alone(self):
        e = player.Engine(DUR.get)
        self.assertEqual(run(e, {"playback": {"mode": "manual"}}, 3), [None] * 3)


class RuleTests(unittest.TestCase):
    CFG = {"playback": {"mode": "rules"},
           "rules": {"conditions": [{"metric": "cpu-temp", "above": 75, "target": "file:hot"}],
                     "otherwise": "file:cool", "hold": 3, "hysteresis": 3}}

    def test_hold_then_switch_then_hysteresis(self):
        e = player.Engine(DUR.get)
        self.assertEqual(run(e, self.CFG, 2, {"cpu-temp": 50}), ["cool", "cool"])
        # hot for less than the hold time: no switch yet
        self.assertEqual(run(e, self.CFG, 3, {"cpu-temp": 80}, start=2), ["cool", "cool", "cool"])
        self.assertEqual(run(e, self.CFG, 1, {"cpu-temp": 80}, start=5), ["hot"])
        # 73 is within the 3-degree hysteresis of 75: stays hot
        self.assertEqual(run(e, self.CFG, 5, {"cpu-temp": 73}, start=6), ["hot"] * 5)
        # well below: back to cool after the hold time
        out = run(e, self.CFG, 5, {"cpu-temp": 60}, start=11)
        self.assertEqual(out[:3], ["hot"] * 3)
        self.assertEqual(out[-1], "cool")

    def test_brief_spike_is_ignored(self):
        e = player.Engine(DUR.get)
        run(e, self.CFG, 2, {"cpu-temp": 50})
        run(e, self.CFG, 1, {"cpu-temp": 90}, start=2)       # one-second spike
        self.assertEqual(run(e, self.CFG, 5, {"cpu-temp": 50}, start=3), ["cool"] * 5)

    def test_first_matching_rule_wins(self):
        cfg = {"playback": {"mode": "rules"},
               "rules": {"conditions": [{"metric": "cpu-temp", "above": 90, "target": "file:hot"},
                                        {"metric": "cpu-load", "above": 50, "target": "file:b"}],
                         "otherwise": "file:cool", "hold": 0}}
        e = player.Engine(DUR.get)
        self.assertEqual(run(e, cfg, 2, {"cpu-temp": 60, "cpu-load": 80})[-1], "b")
        self.assertEqual(run(e, cfg, 2, {"cpu-temp": 95, "cpu-load": 80}, start=2)[-1], "hot")


class DurationTests(unittest.TestCase):
    def test_headerless_slices(self):
        # two pictures: IDR slice + P slice, each with first_mb_in_slice == 0 (top bit set)
        s = b"\x00\x00\x00\x01\x67\x4d" + b"\x00\x00\x00\x01\x65\x88" + b"\x00\x00\x00\x01\x41\x9a"
        self.assertEqual(player.count_frames_any(s), 2)


if __name__ == "__main__":
    unittest.main()
