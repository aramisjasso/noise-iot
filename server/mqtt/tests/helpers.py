"""Shared helpers for the subscriber tests: path setup and message builders."""

import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MQTT_DIR = os.path.dirname(HERE)
REPO_DIR = os.path.normpath(os.path.join(MQTT_DIR, "..", ".."))
for path in (MQTT_DIR, os.path.join(REPO_DIR, "analysis", "features")):
    if path not in sys.path:
        sys.path.insert(0, path)

SESSION = "20261112T180000Z"


def sine(n=8000, amplitude=400, freq=440, offset=1760, fs=8000):
    return [int(round(offset + amplitude * math.sin(2 * math.pi * freq * i / fs))) for i in range(n)]


def clip_payload(samples, session_id=SESSION, idx=0, t_offset_ms=0, capture_us=1000000, late=0):
    header = struct.pack("<16sHIIHH", session_id.encode(), idx, t_offset_ms, capture_us,
                         len(samples), late)
    return header + struct.pack("<%dH" % len(samples), *samples)


def start_msg(node="esp32-01", session_id=SESSION):
    return json.dumps({
        "node_id": node, "session_id": session_id, "started_at": "2026-11-12T18:00:01Z",
        "firmware_version": "0.2.0", "fs": 8000, "adc_bits": 12, "clip_samples": 8000,
        "session_s": 600, "mic_model": "KY-037", "gain_setting": None, "dht_model": "DHT11",
    }).encode()


def climate_msg(node="esp32-01", session_id=SESSION, t=6000, temp=27.0, rh=82.0, error=None):
    return json.dumps({
        "node_id": node, "session_id": session_id, "t_offset_ms": t, "temp_c": temp,
        "rh": rh, "attempts": 1, "error": error,
    }).encode()


def end_msg(node="esp32-01", session_id=SESSION):
    return json.dumps({
        "node_id": node, "session_id": session_id, "ended_at": "2026-11-12T18:10:01Z",
        "duration_ms": 600400, "clips_captured": 2, "clips_sent": 2, "clips_failed": 0,
        "late_samples": 0, "dht_ok": 1, "dht_failed": 0,
    }).encode()
