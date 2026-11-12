"""Tests for analysis/features/mfcc.py."""

import unittest

import numpy as np

from helpers import sine

import mfcc


class FeaturesTest(unittest.TestCase):
    def test_shape_and_names(self):
        f = mfcc.clip_features(sine())
        self.assertEqual(len(f["mfcc_mean"]), 13)
        self.assertEqual(len(f["mfcc_std"]), 13)
        self.assertEqual(len(f["mfcc_mean"]) * 2 + 4, 30)

    def test_frames_follow_the_parameters(self):
        # 1 s at 8 kHz, 200-sample frames every 80 samples
        self.assertEqual(mfcc.frames(np.zeros(8000)).shape, (98, 200))

    def test_dc_offset_does_not_change_features(self):
        a = mfcc.clip_features(sine(offset=1500))
        b = mfcc.clip_features(sine(offset=2500))
        self.assertAlmostEqual(a["rms_mean"], b["rms_mean"], places=6)
        np.testing.assert_allclose(a["mfcc_mean"], b["mfcc_mean"], atol=1e-6)

    def test_amplitude_and_frequency(self):
        quiet = mfcc.clip_features(sine(amplitude=100))
        loud = mfcc.clip_features(sine(amplitude=400))
        self.assertAlmostEqual(loud["rms_mean"] / quiet["rms_mean"], 4.0, places=2)
        self.assertAlmostEqual(loud["rms_mean"], 400 / np.sqrt(2), delta=3)
        self.assertAlmostEqual(loud["peak"], 400, delta=1)
        # a 440 Hz sine crosses zero 880 times per second
        self.assertAlmostEqual(loud["zcr"] * 7999, 880, delta=2)
        low = mfcc.clip_features(sine(freq=200))
        self.assertGreater(low["zcr"], 0)
        self.assertNotEqual(low["mfcc_mean"], loud["mfcc_mean"])

    def test_mel_filters_cover_the_band(self):
        fb = mfcc.mel_filterbank()
        self.assertEqual(fb.shape, (26, 129))
        self.assertTrue((fb.sum(axis=1) > 0).all())


if __name__ == "__main__":
    unittest.main()
