"""One-second clips of an estacion/ session folder, shared by the labeler and the
server classifier so both cut, check and scale the audio the same way.

A session folder (written by server/mqtt/estacion.py) holds audio_crudo.u16 with
every 0.5 s block at position seq * block, bloques.csv with the blocks that
arrived and, once the session is closed, sesion.json.

Clip k is blocks 2k and 2k+1. The quality rules are the ones of Miguel's
src/audio/process_noise_data.py:

- missing_block: one of the two blocks is not in bloques.csv
- all_zero_block / constant_block: a block is all zeros or flat
- zero_run_ge_100: 100 or more consecutive zero samples

Preprocessing is the one of analysis/pytorch_audio/preprocess.py: remove the
clip mean and divide by the ADC full scale (4095 for 12 bits).

The labeling measure is the RMS of the mean-removed clip in dB relative to the
ADC full scale (dBFS): 20 * log10(rms / 4095). It is not calibrated sound
pressure, and nodes with different gain give different values for the same sound.
"""

import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone

import numpy as np

DEFAULT_FS = 8000
DEFAULT_BLOCK = 4000
DEFAULT_ADC_BITS = 12
CLIP_BLOCKS = 2
ZERO_RUN_LIMIT = 100


def session_params(folder):
    """fs, block samples and ADC bits from sesion.json, or the project defaults."""
    meta = {}
    path = os.path.join(folder, "sesion.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            meta = json.load(f)
    return (int(meta.get("fs") or DEFAULT_FS),
            int(meta.get("muestras_bloque") or DEFAULT_BLOCK),
            int(meta.get("bits_adc") or DEFAULT_ADC_BITS),
            meta)


def present_blocks(folder):
    """Block seq numbers listed in bloques.csv (rows that do not parse are skipped)."""
    path = os.path.join(folder, "bloques.csv")
    seqs = set()
    if not os.path.exists(path):
        return seqs
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                seqs.add(int(row["seq"]))
            except (KeyError, TypeError, ValueError):
                continue
    return seqs


def longest_zero_run(x):
    z = x == 0
    if not z.any():
        return 0
    cuts = np.r_[0, np.flatnonzero(z[1:] != z[:-1]) + 1, len(z)]
    lengths = np.diff(cuts)
    return int(lengths[z[cuts[:-1]]].max())


def check_clip(blocks):
    """Quality reasons for a clip given its blocks (list of uint16 arrays). Empty = usable."""
    reasons = []
    if any(np.all(b == 0) for b in blocks):
        reasons.append("all_zero_block")
    if any(np.ptp(b) == 0 for b in blocks):
        reasons.append("constant_block")
    if longest_zero_run(np.concatenate(blocks)) >= ZERO_RUN_LIMIT:
        reasons.append("zero_run_ge_100")
    return sorted(set(reasons))


def preprocess(samples, adc_bits=DEFAULT_ADC_BITS):
    """float32 network input: clip mean removed, divided by the ADC full scale."""
    x = samples.astype(np.float32)
    x -= x.mean()
    x /= float((1 << adc_bits) - 1)
    return x


def rms_dbfs(samples, adc_bits=DEFAULT_ADC_BITS):
    x = samples.astype(np.float64)
    x -= x.mean()
    rms = math.sqrt(float(np.mean(x * x)) + 1e-12)
    return 20.0 * math.log10(rms / ((1 << adc_bits) - 1))


def session_start(session_id):
    try:
        return datetime.strptime(session_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def clip_id(node_id, session_id, second_idx):
    return "%s_%s_%03d" % (node_id, session_id, second_idx)


def session_clips(folder, include_pending=False, skip=()):
    """Yield the one-second clips of a session folder.

    Each item: dict(second_idx, status "ok" | "excluded", reasons, samples (uint16
    array or None), fs, adc_bits).

    A clip with a block not received yet is yielded as excluded only once the
    session is closed (sesion.json exists), because a late block may still
    arrive; with include_pending=True it is yielded anyway. second_idx values in
    skip are not read.
    """
    fs, block, adc_bits, _ = session_params(folder)
    closed = os.path.exists(os.path.join(folder, "sesion.json"))
    present = present_blocks(folder)
    raw_path = os.path.join(folder, "audio_crudo.u16")
    raw = np.fromfile(raw_path, dtype="<u2") if os.path.exists(raw_path) else np.zeros(0, "<u2")
    slots = len(raw) // block
    last = max(present | {slots - 1}) if (present or slots) else -1
    for k in range((last + CLIP_BLOCKS) // CLIP_BLOCKS):
        if k in skip:
            continue
        seqs = [k * CLIP_BLOCKS + i for i in range(CLIP_BLOCKS)]
        item = {"second_idx": k, "fs": fs, "adc_bits": adc_bits, "samples": None}
        if any(q not in present or q >= slots for q in seqs):
            if closed or include_pending:
                item.update(status="excluded", reasons=["missing_block"])
                yield item
            continue
        blocks = [raw[q * block:(q + 1) * block] for q in seqs]
        reasons = check_clip(blocks)
        item.update(status="excluded" if reasons else "ok", reasons=reasons,
                    samples=np.concatenate(blocks))
        yield item


def clip_time(session_id, second_idx):
    start = session_start(session_id)
    return start + timedelta(seconds=second_idx) if start else None
