"""Tests for analysis/climate_variants/train_variants.py on synthetic sessions."""

import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

import helpers
from test_classifier import Recorder

sys.path.insert(0, os.path.join(helpers.REPO_DIR, "analysis", "climate_variants"))
import train_variants as tv  # noqa: E402


def make_sessions(base):
    """2 nodes, one session per hour from Sat 21 00:00 to Mon 23 23:00, 8 s each,
    half the seconds loud. Climate differs by node and hour."""
    rec = Recorder(base)
    start = datetime(2026, 11, 21, 7, 0, tzinfo=timezone.utc)  # 00:00 in Tepic
    rng = np.random.default_rng(1)
    for h in range(72):
        t = start + timedelta(hours=h)
        sid = t.strftime("%Y%m%dT%H%M%SZ")
        for node, temp in (("esp32-01", 22.0), ("esp32-02", 27.0)):
            kinds = [k for k in rng.choice(["quiet", "loud"], 4) for _ in range(2)]
            rec.blocks(node, sid, kinds)
            for momento, dt in (("antes", 0.0), ("despues", 0.5)):
                rec.send(node, "dht", ('{"sesion":"%s","momento":"%s","temp":%.1f,"hum":%.1f,"intentos":1}'
                                       % (sid, momento, temp + dt + (h % 24) / 12, 50 + (h % 24))).encode())
            rec.fin(node, sid)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_sessions(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_interpolates_climate_and_labels(self):
        rows = tv.collect(self.tmp.name, -40.0, tv.local("2026-11-20 15:00"), 0, 1)
        self.assertEqual(len(rows), 72 * 2 * 4)
        self.assertEqual(rows[0]["x"].shape, (8000,))
        self.assertEqual({r["y"] for r in rows}, {0, 1})
        first = [r for r in rows if r["node"] == "esp32-01" and r["session"] == "20261121T070000Z"]
        temps = sorted(r["temp_c"] for r in first)
        self.assertAlmostEqual(temps[0], 22.0)
        self.assertLess(temps[-1], 22.5)

    def test_split_by_time(self):
        rows = tv.collect(self.tmp.name, -40.0, tv.local("2026-11-20 15:00"), 0, 1)
        idx = tv.split(rows, tv.local("2026-11-23 00:00"), tv.local("2026-11-23 12:00"))
        self.assertEqual([len(idx[p]) for p in ("train", "validation", "test")], [384, 96, 96])


@unittest.skipUnless(importlib.util.find_spec("torch"), "torch not installed")
class RunTest(unittest.TestCase):
    def test_four_variants_learn_the_rule(self):
        with tempfile.TemporaryDirectory() as base:
            make_sessions(base)
            rows = tv.collect(base, -40.0, tv.local("2026-11-20 15:00"), 0, 1)
            metrics, info = tv.run(rows, tv.local("2026-11-23 00:00"), tv.local("2026-11-23 12:00"),
                                   seeds=2, epochs=40)
        overall = {m["variant"]: m for m in metrics if m["node_id"] == "all"}
        self.assertEqual(set(overall), set(tv.VARIANTS))
        for m in overall.values():
            self.assertGreater(m["accuracy_mean"], 0.9)
        self.assertEqual(overall["sound"]["changed_vs_sound_pct"], 0.0)
        self.assertEqual({m["node_id"] for m in metrics}, {"all", "esp32-01", "esp32-02"})
        self.assertEqual(info["clips_per_split"]["test"], 96)


if __name__ == "__main__":
    unittest.main()
