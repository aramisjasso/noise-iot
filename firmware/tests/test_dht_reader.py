"""Tests for lib/dht_reader.py: schedule and one retry per failure."""

import unittest

from fakes import FakeDHT

import dht_reader


def run(sensor, chunks=600):
    rec = dht_reader.ClimateRecorder(sensor)
    for n in range(chunks):
        rec.tick(n)
    return rec.finish()


class ClimateRecorderTest(unittest.TestCase):
    def test_sixty_readings_every_ten_seconds(self):
        readings = run(FakeDHT())
        self.assertEqual(len(readings), 60)
        self.assertEqual([r["offset_s"] for r in readings], list(range(0, 600, 10)))
        self.assertEqual(readings[0], {"offset_s": 0, "temp_c": 24.0, "rh": 61.0})

    def test_failed_read_is_retried_one_chunk_later(self):
        sensor = FakeDHT(fail_at={3})  # 4th call: the read at chunk 30
        readings = run(sensor)
        self.assertEqual(len(readings), 60)
        self.assertEqual(readings[3], {"offset_s": 31, "temp_c": 24.0, "rh": 61.0})
        self.assertEqual(sensor.calls, 61)

    def test_failed_retry_gives_nulls_at_the_scheduled_time(self):
        readings = run(FakeDHT(fail_at={3, 4}))
        self.assertEqual(readings[3], {"offset_s": 30, "temp_c": None, "rh": None})
        self.assertEqual(readings[4]["offset_s"], 40)

    def test_dead_sensor_still_gives_sixty_entries(self):
        readings = run(FakeDHT(fail_at=range(1000)))
        self.assertEqual(len(readings), 60)
        self.assertTrue(all(r["temp_c"] is None and r["rh"] is None for r in readings))

    def test_pending_retry_at_window_end_counts_as_failed(self):
        rec = dht_reader.ClimateRecorder(FakeDHT(fail_at={0}))
        rec.tick(0)
        self.assertEqual(rec.finish(), [{"offset_s": 0, "temp_c": None, "rh": None}])

    def test_should_read(self):
        self.assertEqual([n for n in range(25) if dht_reader.should_read(n)], [0, 10, 20])


if __name__ == "__main__":
    unittest.main()
