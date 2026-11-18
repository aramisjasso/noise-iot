"""Tests for analysis/environment/compare_env.py without a database."""

import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

import helpers

sys.path.insert(0, os.path.join(helpers.REPO_DIR, "analysis", "environment"))
import compare_env  # noqa: E402

HAS_TORCH = importlib.util.find_spec("torch") is not None


def sessions(days, temp_effect):
    """Sessions every 10 min, 8:00-18:00, 2 nodes. Noise depends on the hour and
    the node and, if temp_effect, also on a temperature that varies by day."""
    rng = np.random.default_rng(0)
    rows = []
    start = datetime(2026, 11, 19, 8, 0, tzinfo=compare_env.LOCAL_TZ)
    for d in range(days):
        day_temp = 24 + 6 * rng.random()
        for k in range(60):
            t = start + timedelta(days=d, minutes=10 * k)
            for node in ("esp32-01", "esp32-02"):
                hour = t.hour + t.minute / 60
                temp = day_temp + 3 * np.sin((hour - 8) / 10 * np.pi) + rng.normal(0, 0.3)
                noise = 0.2 + 0.2 * (node == "esp32-02") + 0.1 * np.sin((hour - 8) / 10 * np.pi)
                if temp_effect:
                    noise += 0.04 * (temp - 27)
                rows.append({"node_id": node, "session_id": t.strftime("%Y%m%dT%H%M%SZ"),
                             "started_at": t.astimezone(timezone.utc), "ok_clips": 300,
                             "noise_frac": float(np.clip(noise + rng.normal(0, 0.02), 0, 1)),
                             "temp_c": float(temp), "rh": float(55 + rng.normal(0, 5))})
    return rows


class SplitTest(unittest.TestCase):
    def test_split_by_days(self):
        idx, part, by_days = compare_env.split(sessions(8, False), val_days=2, test_days=3)
        self.assertTrue(by_days)
        self.assertEqual({k: len(v) for k, v in idx.items()}, {"train": 360, "validation": 240, "test": 360})

    def test_one_day_is_a_technical_check(self):
        _, _, by_days = compare_env.split(sessions(1, False), 2, 3)
        self.assertFalse(by_days)


@unittest.skipUnless(HAS_TORCH, "torch not installed")
class CompareTest(unittest.TestCase):
    def gain(self, metrics, variant):
        return next(m["gain_pp"] for m in metrics if m["variant"] == variant and m["node_id"] == "all")

    def test_finds_temperature_effect(self):
        preds, metrics, info = compare_env.compare(sessions(8, True), seeds=2)
        self.assertGreater(self.gain(metrics, "temp"), 0.8)
        self.assertGreater(self.gain(metrics, "both"), 0.8)
        self.assertEqual(len(preds), 4 * 960)
        self.assertEqual({m["node_id"] for m in metrics}, {"all", "esp32-01", "esp32-02"})
        self.assertTrue(info["split_by_days"])

    def test_no_effect_gives_no_gain(self):
        _, metrics, _ = compare_env.compare(sessions(8, False), seeds=2)
        self.assertLess(abs(self.gain(metrics, "hum")), 0.6)


if __name__ == "__main__":
    unittest.main()
