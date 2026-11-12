"""Tests for the Ingestor routing, with an in-memory store."""

import tempfile
import unittest

from helpers import SESSION, clip_payload, climate_msg, end_msg, sine, start_msg

import protocol
from subscriber import Ingestor


class FakeStore:
    def __init__(self):
        self.sessions = {}
        self.clips = {}
        self.climate_rows = []

    def session_start(self, site, msg):
        self.sessions[(msg["node_id"], msg["session_id"])] = dict(msg, site=site)

    def session_end(self, site, msg):
        self.sessions.setdefault((msg["node_id"], msg["session_id"]), {}).update(msg)

    def climate(self, site, msg):
        self.climate_rows.append(msg)

    def clip_exists(self, node, sid, idx):
        return (node, sid, idx) in self.clips

    def clip(self, site, node, row, features, version):
        self.clips[(node, row["session_id"], row["clip_idx"])] = (row, features, version)


class IngestorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FakeStore()
        self.ing = Ingestor(self.store, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_session(self):
        base = "noise/tepic/esp32-01/"
        self.ing.handle(base + "session/start", start_msg())
        self.ing.handle(base + "clip", clip_payload(sine(), idx=0))
        self.ing.handle(base + "climate", climate_msg())
        self.ing.handle(base + "clip", clip_payload(sine(), idx=1, t_offset_ms=1200))
        self.ing.handle(base + "session/end", end_msg())
        self.assertEqual(self.store.sessions[("esp32-01", SESSION)]["site"], "tepic")
        self.assertEqual(len(self.store.clips), 2)
        row, features, version = self.store.clips[("esp32-01", SESSION, 1)]
        self.assertEqual(row["t_offset_ms"], 1200)
        self.assertEqual(len(features["mfcc_mean"]), 13)
        self.assertEqual(version, "mfcc13-v1")
        self.assertEqual(len(self.store.climate_rows), 1)

    def test_duplicate_clip_is_skipped(self):
        topic = "noise/tepic/esp32-01/clip"
        self.ing.handle(topic, clip_payload(sine()))
        self.assertIn("duplicate", self.ing.handle(topic, clip_payload(sine())))
        self.assertEqual(len(self.store.clips), 1)

    def test_bad_message_raises_protocol_error(self):
        with self.assertRaises(protocol.ProtocolError):
            self.ing.handle("noise/tepic/esp32-01/clip", b"short")


if __name__ == "__main__":
    unittest.main()
