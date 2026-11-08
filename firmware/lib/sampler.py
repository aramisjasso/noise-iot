"""Audio capture and level computation.

Captures 1-second chunks from the microphone front end through ADC1
(12 bits) on GPIO34 at 8 kHz, i.e. 8000 samples per chunk, and reduces each
chunk to a single RMS level in dB. Only the levels leave the node; raw audio
is never sent.

Level reference: dB relative to an RMS of 1 ADC count (12 bits), plus
MIC_CAL_DB from config.py. Without calibration (MIC_CAL_DB = 0) a full-scale
sine reads about 63 dB and a silent input reads near 0 dB. To report dB SPL,
measure the same sound with a sound level meter and set MIC_CAL_DB to the
difference.

The same code runs on the ESP32 and in the PC tests: @micropython.native
makes the loops several times faster on the board and is a no-op here.
"""

import array
import math

try:
    import micropython
except ImportError:  # CPython, for the tests on the PC
    # The MicroPython compiler only honours the decorator when it is written
    # literally as @micropython.native, so it cannot be aliased; on the PC
    # this stand-in makes it a no-op.
    class micropython:
        @staticmethod
        def native(f):
            return f


try:
    from time import ticks_add, ticks_diff, ticks_us
except ImportError:  # CPython, for the tests on the PC
    import time as _time

    def ticks_us():
        return _time.perf_counter_ns() // 1000

    def ticks_add(ticks, delta):
        return ticks + delta

    def ticks_diff(a, b):
        return a - b


SAMPLE_RATE_HZ = 8000
CHUNK_SAMPLES = 8000  # 1 second at SAMPLE_RATE_HZ
SAMPLE_PERIOD_US = 1000000 // SAMPLE_RATE_HZ  # 125 us
ADC_PIN = 34  # ADC1 channel; ADC2 is unusable while Wi-Fi is on
ADC_BITS = 12

# Squares are added in blocks of this many samples so the partial sums stay
# below 2**30 (MicroPython small ints): 32 * 4095**2 < 2**30. Bigger sums
# would still be correct, just slower because they allocate.
_BLOCK = 32


def init_adc(pin=ADC_PIN):
    """Configure the ADC for audio capture.

    Args:
        pin: GPIO number connected to the analog front end.

    Returns:
        Configured machine.ADC instance (0-3.3 V input range).
    """
    from machine import ADC, Pin

    adc = ADC(Pin(pin))
    adc.atten(ADC.ATTN_11DB)
    return adc


def new_buffer(n=CHUNK_SAMPLES):
    """Allocate the sample buffer for capture_chunk() (2 bytes per sample).

    range() has a known length, so MicroPython allocates the array in one
    block; the initial values are overwritten by the first capture.
    """
    return array.array("H", range(n))


@micropython.native
def capture_chunk(adc, buf):
    """Fill buf with readings taken every SAMPLE_PERIOD_US microseconds.

    If the loop falls behind (e.g. a Wi-Fi interrupt), the next samples are
    taken back to back until it catches up, so a chunk still lasts about one
    second.

    Args:
        adc: ADC returned by init_adc().
        buf: buffer from new_buffer(), reused across chunks to avoid heap
            fragmentation. Samples are stored as 12-bit values (0-4095).

    Returns:
        Microseconds the capture took (about 1 000 000 for 8000 samples).
        Much more means the board cannot keep up with 8 kHz.
    """
    read = adc.read_u16
    period = SAMPLE_PERIOD_US
    n = len(buf)
    start = ticks_us()
    due = start
    for i in range(n):
        while ticks_diff(due, ticks_us()) > 0:
            pass
        buf[i] = read() >> 4
        due = ticks_add(due, period)
    return ticks_diff(ticks_us(), start)


@micropython.native
def _deviation_sums(buf, offset):
    """Return (sum of d, sum of d*d) with d = sample - offset."""
    n = len(buf)
    block = _BLOCK
    total = 0
    squares = 0.0
    i = 0
    while i < n:
        end = i + block
        if end > n:
            end = n
        part = 0
        while i < end:
            d = buf[i] - offset
            total += d
            part += d * d
            i += 1
        squares += part
    return total, squares


def rms_db(buf, cal_db=0.0):
    """Compute the RMS level of one chunk in dB.

    The DC offset (the bias of the front end, about half of the ADC range)
    is removed before computing the RMS.

    Args:
        buf: raw samples from capture_chunk().
        cal_db: calibration offset in dB (MIC_CAL_DB).

    Returns:
        Level in dB (float), or None if the chunk is unusable: empty, or a
        perfectly flat signal (input stuck at a rail or disconnected).
    """
    n = len(buf)
    if n == 0:
        return None
    # Any sample near the mean keeps the numbers small; the variance does
    # not depend on which one is used.
    total, squares = _deviation_sums(buf, buf[0])
    mean = total / n
    variance = squares / n - mean * mean
    if variance <= 0:
        return None
    return 10 * math.log10(variance) + cal_db
