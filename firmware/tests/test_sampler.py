"""Tests for lib/sampler.py: capture pacing and RMS level."""

import array
import math
import unittest

from fakes import FakeADC

import sampler


def filled(values):
    return array.array("H", values)


def tone(amplitude, n, offset=2048):
    """n 12-bit samples of a sine from the fake ADC."""
    adc = FakeADC(amplitude=amplitude, offset=offset)
    return filled(adc.read_u16() >> 4 for _ in range(n))


class RmsDbTest(unittest.TestCase):
    def test_sine_level(self):
        # A sine of amplitude A has RMS A/sqrt(2): 20*log10(1000/sqrt(2)) = 56.99 dB
        buf = tone(1000, 8000)
        self.assertAlmostEqual(sampler.rms_db(buf), 20 * math.log10(1000 / math.sqrt(2)), places=1)

    def test_dc_offset_is_removed(self):
        low = tone(300, 1600, offset=1000)
        high = tone(300, 1600, offset=3000)
        self.assertAlmostEqual(sampler.rms_db(low), sampler.rms_db(high), places=6)

    def test_louder_is_higher(self):
        quiet = tone(50, 800)
        loud = tone(500, 800)
        self.assertAlmostEqual(sampler.rms_db(loud) - sampler.rms_db(quiet), 20.0, places=1)

    def test_calibration_offset(self):
        buf = tone(100, 800)
        self.assertAlmostEqual(sampler.rms_db(buf, cal_db=30.0) - sampler.rms_db(buf), 30.0)

    def test_full_scale_square_wave(self):
        # Worst case for the integer sums: every sample at a rail.
        buf = filled([0, 4095] * 4000)
        self.assertAlmostEqual(sampler.rms_db(buf), 20 * math.log10(2047.5), places=6)

    def test_flat_or_empty_chunk_is_none(self):
        self.assertIsNone(sampler.rms_db(filled([4095] * 8000)))
        self.assertIsNone(sampler.rms_db(filled([])))


class CaptureChunkTest(unittest.TestCase):
    def test_fills_buffer_with_12_bit_samples(self):
        buf = sampler.new_buffer(400)
        adc = FakeADC(amplitude=2000)
        sampler.capture_chunk(adc, buf)
        self.assertEqual(adc.i, 400)
        self.assertTrue(all(0 <= v <= 4095 for v in buf))
        self.assertEqual(max(buf), 2048 + 2000)

    def test_paces_at_8_khz(self):
        buf = sampler.new_buffer(800)  # 0.1 s at 8 kHz
        took_us = sampler.capture_chunk(FakeADC(), buf)
        self.assertGreaterEqual(took_us, 99_000)
        self.assertLess(took_us, 150_000)

    def test_buffer_size(self):
        buf = sampler.new_buffer()
        self.assertEqual(len(buf), sampler.CHUNK_SAMPLES)
        self.assertEqual(buf.itemsize, 2)


if __name__ == "__main__":
    unittest.main()
