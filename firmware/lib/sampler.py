"""Audio capture and level computation.

Captures 1-second chunks from the electret microphone front end through
ADC1 (12 bits) on GPIO34 at 8 kHz, i.e. 8000 samples per chunk, and reduces
each chunk to a single RMS level in dB. Only the levels leave the node; raw
audio is never sent.
"""

SAMPLE_RATE_HZ = 8000
CHUNK_SAMPLES = 8000  # 1 second at SAMPLE_RATE_HZ
ADC_PIN = 34  # ADC1 channel; ADC2 is unusable while Wi-Fi is on
ADC_BITS = 12


def init_adc(pin=ADC_PIN):
    """Configure the ADC for audio capture.

    Args:
        pin: GPIO number connected to the analog front end.

    Returns:
        Configured machine.ADC instance.
    """
    raise NotImplementedError


def capture_chunk(adc, buf):
    """Fill buf with CHUNK_SAMPLES raw readings taken at SAMPLE_RATE_HZ.

    Args:
        adc: ADC returned by init_adc().
        buf: preallocated array of CHUNK_SAMPLES unsigned 16-bit ints,
            reused across chunks to avoid heap fragmentation.

    Returns:
        None. buf is modified in place.
    """
    raise NotImplementedError


def rms_db(buf):
    """Compute the RMS level of one chunk in dB.

    The DC offset is removed before computing the RMS.

    Args:
        buf: raw samples from capture_chunk().

    Returns:
        Level in dB (float), or None if the chunk is unusable.
    """
    raise NotImplementedError
