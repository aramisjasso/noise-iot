"""Parsing and validation of the MQTT messages (contracts/mqtt.md).

Pure functions with no broker or database, so they can be tested alone.
"""

import json
import os
import re
import struct
import wave

import numpy as np
from jsonschema import Draft202012Validator, FormatChecker

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONTRACTS_DIR = os.path.join(REPO_DIR, "contracts", "mqtt")

CLIP_HEADER = struct.Struct("<16sHIIHH")
CLIP_HEADER_SIZE = CLIP_HEADER.size  # 30 bytes
ADC_MAX = 4095
WAV_GAIN = 16  # 12-bit values scaled to the 16-bit range of the .wav

_TOPIC = re.compile(r"^noise/([a-z0-9-]+)/([a-z0-9-]+)/(session/start|session/end|clip|climate)$")
_SESSION_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


class ProtocolError(ValueError):
    """The message does not follow the contract."""


def _validator(name):
    with open(os.path.join(CONTRACTS_DIR, name), encoding="utf-8") as f:
        return Draft202012Validator(json.load(f), format_checker=FormatChecker())


VALIDATORS = {
    "session/start": _validator("session_start.schema.json"),
    "session/end": _validator("session_end.schema.json"),
    "climate": _validator("climate.schema.json"),
}


def parse_topic(topic):
    """Split a topic into (site, node_id, kind).

    Raises:
        ProtocolError if the topic is not one of the four of the contract.
    """
    m = _TOPIC.match(topic)
    if not m:
        raise ProtocolError("unknown topic: %s" % topic)
    return m.group(1), m.group(2), m.group(3)


def parse_json(kind, node_id, payload):
    """Decode and validate a JSON message.

    Args:
        kind: "session/start", "session/end" or "climate".
        node_id: node from the topic; must match the body.
        payload: raw bytes of the message.

    Returns:
        The decoded dict.
    """
    try:
        msg = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("invalid JSON: %s" % exc) from exc
    errors = sorted(VALIDATORS[kind].iter_errors(msg), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "(root)"
        raise ProtocolError("%s: %s" % (where, e.message))
    if msg["node_id"] != node_id:
        raise ProtocolError("node_id %r does not match topic %r" % (msg["node_id"], node_id))
    return msg


def parse_clip(payload):
    """Decode a binary clip message.

    Returns:
        (header dict, samples as a numpy uint16 array).
    """
    if len(payload) < CLIP_HEADER_SIZE:
        raise ProtocolError("clip shorter than its header (%d bytes)" % len(payload))
    sid, idx, t_off, capture_us, n, late = CLIP_HEADER.unpack_from(payload, 0)
    try:
        session_id = sid.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("session_id is not ASCII") from exc
    if not _SESSION_ID.match(session_id):
        raise ProtocolError("bad session_id %r" % session_id)
    expected = CLIP_HEADER_SIZE + 2 * n
    if len(payload) != expected:
        raise ProtocolError("clip has %d bytes, header says %d" % (len(payload), expected))
    if n == 0 or capture_us == 0:
        raise ProtocolError("empty clip")
    samples = np.frombuffer(payload, dtype="<u2", count=n, offset=CLIP_HEADER_SIZE)
    if samples.max() > ADC_MAX:
        raise ProtocolError("sample above %d" % ADC_MAX)
    header = {
        "session_id": session_id,
        "clip_idx": idx,
        "t_offset_ms": t_off,
        "capture_us": capture_us,
        "n_samples": n,
        "late_samples": late,
        "fs_measured": n * 1e6 / capture_us,
    }
    return header, samples


def clip_stats(samples):
    """ADC statistics of a raw clip (DC offset and saturation)."""
    return {
        "adc_mean": float(samples.mean()),
        "adc_min": int(samples.min()),
        "adc_max": int(samples.max()),
        "clipped_samples": int(np.count_nonzero((samples == 0) | (samples == ADC_MAX))),
    }


def clip_paths(audio_dir, node_id, session_id, clip_idx):
    """Paths of the raw and wav files of a clip."""
    folder = os.path.join(audio_dir, node_id, session_id)
    stem = os.path.join(folder, "clip_%04d" % clip_idx)
    return folder, stem + ".u16", stem + ".wav"


def write_clip_files(audio_dir, node_id, session_id, clip_idx, samples, fs=8000):
    """Store a clip twice: exact raw values and a listenable wav.

    The .u16 file keeps the ADC values untouched (source of truth). The .wav
    is (raw - clip mean) * WAV_GAIN as 16-bit PCM, only for listening; the
    removed offset is stored in clips.adc_mean.

    Returns:
        (raw_path, wav_path).
    """
    folder, raw_path, wav_path = clip_paths(audio_dir, node_id, session_id, clip_idx)
    os.makedirs(folder, exist_ok=True)
    samples.astype("<u2").tofile(raw_path)
    centered = (samples.astype(np.float64) - samples.mean()) * WAV_GAIN
    pcm = np.clip(np.round(centered), -32768, 32767).astype("<i2")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(pcm.tobytes())
    return raw_path, wav_path
