"""Tests for protocol.py: topics, JSON validation and the binary clip."""

import os
import tempfile
import unittest
import wave

import numpy as np

from helpers import SESSION, clip_payload, climate_msg, end_msg, sine, start_msg

import protocol


class TopicTest(unittest.TestCase):
    def test_valid_topics(self):
        self.assertEqual(protocol.parse_topic("noise/tepic/esp32-01/clip"),
                         ("tepic", "esp32-01", "clip"))
        self.assertEqual(protocol.parse_topic("noise/monterrey/mty-01/session/start")[2],
                         "session/start")

    def test_unknown_topic(self):
        for t in ("noise/tepic/esp32-01/batch", "other/tepic/x/clip", "noise/Tepic/x/clip"):
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_topic(t)


class JsonTest(unittest.TestCase):
    def test_valid_messages(self):
        self.assertEqual(protocol.parse_json("session/start", "esp32-01", start_msg())["fs"], 8000)
        self.assertEqual(protocol.parse_json("session/end", "esp32-01", end_msg())["clips_sent"], 2)
        msg = protocol.parse_json("climate", "esp32-01", climate_msg(temp=None, rh=None, error="x"))
        self.assertIsNone(msg["temp_c"])

    def test_node_must_match_topic(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_json("climate", "other-01", climate_msg())

    def test_schema_errors_are_reported(self):
        with self.assertRaisesRegex(protocol.ProtocolError, "rh"):
            protocol.parse_json("climate", "esp32-01", climate_msg(rh=140))
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_json("climate", "esp32-01", b"{not json")


class ClipTest(unittest.TestCase):
    def test_round_trip(self):
        samples = sine()
        header, x = protocol.parse_clip(clip_payload(samples, idx=7, t_offset_ms=8123,
                                                     capture_us=1000250, late=3))
        self.assertEqual(header["session_id"], SESSION)
        self.assertEqual(header["clip_idx"], 7)
        self.assertEqual(header["t_offset_ms"], 8123)
        self.assertEqual(header["late_samples"], 3)
        self.assertAlmostEqual(header["fs_measured"], 7998.0, places=0)
        self.assertEqual(x.tolist(), samples)

    def test_header_is_30_bytes(self):
        self.assertEqual(protocol.CLIP_HEADER_SIZE, 30)
        self.assertEqual(len(clip_payload([0] * 8000)), 16030)

    def test_rejects_bad_clips(self):
        good = clip_payload(sine(100))
        for bad in (good[:20], good[:-2], clip_payload([5000] * 10),
                    clip_payload([1] * 10, session_id="2026-11-12 18:00")):
            with self.assertRaises(protocol.ProtocolError):
                protocol.parse_clip(bad)

    def test_stats_count_saturation(self):
        s = protocol.clip_stats(np.array([0, 4095, 2000, 2001], dtype=np.uint16))
        self.assertEqual(s["clipped_samples"], 2)
        self.assertEqual((s["adc_min"], s["adc_max"]), (0, 4095))

    def test_files_keep_raw_values(self):
        samples = np.array(sine(), dtype=np.uint16)
        with tempfile.TemporaryDirectory() as d:
            raw, wav_path = protocol.write_clip_files(d, "esp32-01", SESSION, 3, samples)
            self.assertTrue(raw.endswith(os.path.join("esp32-01", SESSION, "clip_0003.u16")))
            np.testing.assert_array_equal(np.fromfile(raw, dtype="<u2"), samples)
            with wave.open(wav_path) as w:
                self.assertEqual((w.getframerate(), w.getnframes(), w.getsampwidth()),
                                 (8000, 8000, 2))
                pcm = np.frombuffer(w.readframes(8000), dtype="<i2")
            self.assertLess(abs(pcm.mean()), 20)  # DC removed


if __name__ == "__main__":
    unittest.main()
