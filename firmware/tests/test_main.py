"""Tests for main.py: window timing, seq, batch building and delivery."""

import calendar
import tempfile
import unittest
from unittest import mock

import jsonschema

from fakes import CONTRACT, FakeADC, FakeDHT

import main
import outbox
import sampler


def utc(text):
    """time.time() value of an ISO date on CPython (1970 epoch)."""
    return calendar.timegm(__import__("time").strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


class TimeTest(unittest.TestCase):
    def test_seconds_until_next_window(self):
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:00:00Z")), 0)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:00:01Z")), 1799)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:29:59Z")), 1)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:30:00Z")), 0)
        self.assertEqual(main.seconds_until_next_window(utc("2026-11-05T18:47:00Z")), 780)

    def test_boundary_is_the_same_with_the_2000_epoch(self):
        # Some MicroPython ports count from 2000-01-01; the offset is a whole
        # number of 30-minute cycles, so the boundaries do not move.
        offset_2000 = 946684800
        t = utc("2026-11-05T18:12:34Z")
        self.assertEqual(
            main.seconds_until_next_window(t), main.seconds_until_next_window(t - offset_2000)
        )

    def test_iso_utc(self):
        self.assertEqual(main.iso_utc(utc("2026-11-05T18:30:00Z")), "2026-11-05T18:30:00Z")

    def test_seq_counts_windows_since_2026(self):
        self.assertEqual(main.window_seq(utc("2026-01-01T00:00:00Z")), 0)
        self.assertEqual(main.window_seq(utc("2026-01-01T00:30:00Z")), 1)
        self.assertEqual(main.window_seq(utc("2026-01-02T00:00:00Z")), 48)
        a = main.window_seq(utc("2026-11-05T18:00:00Z"))
        b = main.window_seq(utc("2026-11-05T18:30:00Z"))
        self.assertEqual(b - a, 1)
        self.assertEqual(main.window_seq(utc("2027-01-01T00:00:00Z")), 365 * 48)

    def test_days_from_civil(self):
        self.assertEqual(main._days_from_civil(1970, 1, 1), 0)
        self.assertEqual(main._days_from_civil(2000, 3, 1), 11017)
        self.assertEqual(main._days_from_civil(2028, 2, 29) + 1, main._days_from_civil(2028, 3, 1))


def fast_window(levels=None, dht_fail_at=()):
    """run_window with capture replaced so the test does not take 10 min."""
    levels = iter(levels) if levels is not None else None

    def fake_capture(adc, buf):
        for i in range(len(buf)):
            buf[i] = adc.read_u16() >> 4
        return 1_000_000

    def fake_rms(buf, cal_db=0.0):
        return next(levels) if levels is not None else real_rms(buf, cal_db)

    real_rms = sampler.rms_db
    with mock.patch.object(sampler, "CHUNK_SAMPLES", 160), \
         mock.patch.object(sampler, "new_buffer", lambda n=160: sampler.array.array("H", range(160))), \
         mock.patch.object(sampler, "capture_chunk", fake_capture), \
         mock.patch.object(sampler, "rms_db", fake_rms):
        return main.run_window(
            utc("2026-11-05T18:00:00Z"), 15024, FakeADC(amplitude=300), FakeDHT(fail_at=dht_fail_at)
        )


class RunWindowTest(unittest.TestCase):
    def test_batch_matches_the_contract(self):
        batch = fast_window()
        jsonschema.validate(batch, CONTRACT, format_checker=jsonschema.FormatChecker())
        self.assertEqual(batch["node_id"], "test-01")
        self.assertEqual(batch["seq"], 15024)
        self.assertEqual(batch["window_start"], "2026-11-05T18:00:00Z")
        self.assertEqual(batch["firmware_version"], "0.0.0-test")
        self.assertEqual(len(batch["levels_db"]), 600)
        self.assertEqual(len(batch["climate"]), 60)

    def test_levels_rounded_and_failed_chunks_null(self):
        values = [52.123, None] + [40.0] * 598
        batch = fast_window(levels=values)
        self.assertEqual(batch["levels_db"][:3], [52.1, None, 40.0])
        jsonschema.validate(batch, CONTRACT)

    def test_exception_in_a_chunk_does_not_lose_the_window(self):
        def boom():
            yield 50.0
            raise ValueError("adc")

        values = boom()

        def flaky(buf, cal_db=0.0):
            return next(values, 45.0)

        with mock.patch.object(sampler, "rms_db", flaky), \
             mock.patch.object(sampler, "capture_chunk", lambda adc, buf: 1_000_000), \
             mock.patch.object(sampler, "new_buffer", lambda n=0: []), \
             mock.patch("builtins.print"):
            batch = main.run_window(utc("2026-11-05T18:00:00Z"), 1, None, FakeDHT())
        self.assertEqual(batch["levels_db"][:3], [50.0, None, 45.0])
        jsonschema.validate(batch, CONTRACT)

    def test_dht_failures_keep_the_batch_valid(self):
        batch = fast_window(dht_fail_at=range(0, 200))
        jsonschema.validate(batch, CONTRACT)
        self.assertTrue(all(c["temp_c"] is None for c in batch["climate"]))


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(outbox, "OUTBOX_DIR", self.tmp.name + "/outbox")
        self.patch.start()
        self.quiet = mock.patch("builtins.print")
        self.quiet.start()

    def tearDown(self):
        self.quiet.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_failed_send_goes_to_outbox(self):
        self.assertFalse(main.deliver({"seq": 1}, lambda b: False))
        self.assertEqual(len(outbox.pending()), 1)

    def test_success_resends_the_queue(self):
        main.deliver({"seq": 1}, lambda b: False)
        main.deliver({"seq": 2}, lambda b: False)
        sent = []
        self.assertTrue(main.deliver({"seq": 3}, lambda b: sent.append(b["seq"]) or True))
        self.assertEqual(sent, [3, 1, 2])
        self.assertEqual(outbox.pending(), [])

    def test_queue_resend_is_limited_per_cycle(self):
        for seq in range(main.MAX_FLUSH_PER_CYCLE + 5):
            outbox.save({"seq": seq})
        main.deliver({"seq": 999}, lambda b: True)
        self.assertEqual(len(outbox.pending()), 5)


if __name__ == "__main__":
    unittest.main()
