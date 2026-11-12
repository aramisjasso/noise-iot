"""Clip features for the noise classifier, with numpy only.

Implements the feature set proposed for the 1-second clips:

- 13 MFCC (coefficients 1 to 13; coefficient 0 is dropped), summarized by
  their mean and standard deviation over the clip.
- Frames of 25 ms (200 samples) with a 10 ms hop (80 samples), Hamming
  window, 256-point FFT and 26 Mel filters.
- 4 extra features: mean and max frame RMS, absolute peak and zero-crossing
  rate.

Total: 13 * 2 + 4 = 30 features per clip. Input is the raw ADC clip; the DC
offset is removed per clip and nothing is normalized per clip, so amplitude
differences between clips are preserved.
"""

import numpy as np

FEATURE_VERSION = "mfcc13-v1"
FS = 8000
FRAME = 200  # 25 ms
HOP = 80  # 10 ms
NFFT = 256
N_MELS = 26
N_MFCC = 13  # coefficients 1..13
_EPS = 1e-10


def _hz_to_mel(hz):
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel):
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def mel_filterbank(fs=FS, nfft=NFFT, n_mels=N_MELS):
    """Triangular Mel filters (HTK scale) from 0 Hz to fs/2.

    Returns:
        Array of shape (n_mels, nfft // 2 + 1).
    """
    mels = np.linspace(_hz_to_mel(0.0), _hz_to_mel(fs / 2.0), n_mels + 2)
    bins = np.floor((nfft + 1) * _mel_to_hz(mels) / fs).astype(int)
    fb = np.zeros((n_mels, nfft // 2 + 1))
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        for k in range(left, center):
            fb[m - 1, k] = (k - left) / max(center - left, 1)
        for k in range(center, right):
            fb[m - 1, k] = (right - k) / max(right - center, 1)
    return fb


def dct_matrix(n_out, n_in):
    """Orthonormal DCT-II matrix of shape (n_out, n_in)."""
    k = np.arange(n_out)[:, None]
    n = np.arange(n_in)[None, :]
    m = np.cos(np.pi * k * (2 * n + 1) / (2 * n_in)) * np.sqrt(2.0 / n_in)
    m[0] /= np.sqrt(2.0)
    return m


_FB = mel_filterbank()
_DCT = dct_matrix(N_MFCC + 1, N_MELS)
_WINDOW = np.hamming(FRAME)


def frames(x, frame=FRAME, hop=HOP):
    """Split a 1-D signal into overlapping frames (no padding).

    Returns:
        Array of shape (n_frames, frame).
    """
    n = 1 + (len(x) - frame) // hop
    if n <= 0:
        raise ValueError("clip shorter than one frame")
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def mfcc(x):
    """MFCC 1..13 for every frame of a DC-free signal.

    Returns:
        Array of shape (n_frames, 13).
    """
    f = frames(x) * _WINDOW
    power = np.abs(np.fft.rfft(f, NFFT)) ** 2 / NFFT
    log_mel = np.log(power @ _FB.T + _EPS)
    return (log_mel @ _DCT.T)[:, 1:]


def clip_features(raw):
    """Compute the 30 features of one clip.

    Args:
        raw: raw ADC samples (any integer or float array, 0-4095).

    Returns:
        dict with rms_mean, rms_max, peak, zcr (floats) and mfcc_mean,
        mfcc_std (lists of 13 floats). Amplitudes are in ADC counts.
    """
    x = np.asarray(raw, dtype=np.float64)
    x = x - x.mean()
    frame_rms = np.sqrt(np.mean(frames(x) ** 2, axis=1))
    signs = np.signbit(x)
    zcr = float(np.count_nonzero(signs[1:] != signs[:-1])) / (len(x) - 1)
    c = mfcc(x)
    return {
        "rms_mean": float(frame_rms.mean()),
        "rms_max": float(frame_rms.max()),
        "peak": float(np.abs(x).max()),
        "zcr": zcr,
        "mfcc_mean": [float(v) for v in c.mean(axis=0)],
        "mfcc_std": [float(v) for v in c.std(axis=0)],
    }
