"""Tests for main.py (firmware v2): session ids, timing and run_session."""

import calendar
import json
import struct
import unittest
from unittest import mock

import jsonschema

from fakes import FakeADC, FakeDHT, FakePublisher, mqtt_schema

import main


def utc(text):
    """time.time() value of an ISO date on CPython (1970 epoch)."""
    return calendar.timegm(__import__("time").strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class TimeTest(unittest.TestCase):
    def test_seconds_until_next_window(self):
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:00:00Z")), 0)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:00:01Z")), 1799)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:29:59Z")), 1)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:47:00Z")), 780)

    def test_boundary_is_the_same_with_the_2000_epoch(self):
        offset_2000 = 946684800
        t = utc("2026-11-05T18:12:34Z")
        self.assertEqual(
            main.seconds_until_next_window(t), main.seconds_until_next_window(t - offset_2000)
        )

    def test_session_id_is_16_characters(self):
        sid = main.session_id(utc("2026-11-12T18:30:00Z"))
        self.assertEqual(sid, "20261112T183000Z")
        self.assertEqual(len(sid), 16)

    def test_topics(self):
        self.assertEqual(main.topic("clip"), "noise/tepic/test-01/clip")
        self.assertEqual(main.topic("session/start"), "noise/tepic/test-01/session/start")

    def test_clip_header_is_30_bytes(self):
        self.assertEqual(main.CLIP_HEADER_SIZE, 30)


class RunSessionTest(unittest.TestCase):
    """A 1 s session with 0.1 s clips (800 samples) and the DHT every 0.4 s."""

    SID = "20261112T180000Z"

    def run_session(self, pub, sensor=None):
        with mock.patch("builtins.print"):
            return main.run_session(
                self.SID, pub, FakeADC(amplitude=500), sensor or FakeDHT(),
                session_s=1, clip_samples=800, dht_every_ms=400,
                clock=lambda: utc("2026-11-12T18:00:01Z"),
            )

    def test_messages_follow_the_contract(self):
        pub = FakePublisher()
        end = self.run_session(pub)
        jsonschema.validate(json.loads(pub.of_kind("session/start")[0]),
                            mqtt_schema("session_start"))
        for body in pub.of_kind("climate"):
            jsonschema.validate(json.loads(body), mqtt_schema("climate"))
        jsonschema.validate(json.loads(pub.of_kind("session/end")[0]), mqtt_schema("session_end"))
        self.assertEqual(pub.messages[0][0], "noise/tepic/test-01/session/start")
        self.assertEqual(pub.messages[-1][0], "noise/tepic/test-01/session/end")
        self.assertGreaterEqual(end["clips_captured"], 8)
        self.assertEqual(end["clips_sent"], end["clips_captured"])
        self.assertGreaterEqual(end["dht_ok"], 2)

    def test_clips_are_numbered_and_timed(self):
        pub = FakePublisher()
        self.run_session(pub)
        clips = pub.of_kind("clip")
        previous = -1
        for i, payload in enumerate(clips):
            sid, idx, t_off, capture_us, n, late = struct.unpack_from(main.CLIP_HEADER, payload, 0)
            self.assertEqual(sid, self.SID.encode())
            self.assertEqual(idx, i)
            self.assertEqual(n, 800)
            self.assertEqual(len(payload), 30 + 2 * n)
            self.assertGreater(t_off, previous)
            self.assertGreater(capture_us, 90_000)
            previous = t_off
            samples = struct.unpack_from("<800H", payload, 30)
            self.assertEqual(max(samples), 2048 + 500)

    def test_failed_publishes_are_counted(self):
        end = self.run_session(FakePublisher(fail=True))
        self.assertGreater(end["clips_captured"], 0)
        self.assertEqual(end["clips_sent"], 0)
        self.assertEqual(end["clips_failed"], end["clips_captured"])

    def test_dht_failures_are_reported_with_nulls(self):
        pub = FakePublisher()
        end = self.run_session(pub, FakeDHT(fail_at=range(100)))
        self.assertEqual(end["dht_ok"], 0)
        readings = [json.loads(b) for b in pub.of_kind("climate")]
        self.assertTrue(readings)
        for r in readings:
            self.assertIsNone(r["temp_c"])
            self.assertEqual(r["attempts"], 2)
            self.assertIn("ETIMEDOUT", r["error"])


if __name__ == "__main__":
    unittest.main()
