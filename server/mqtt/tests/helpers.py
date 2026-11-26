"""Shared helpers for the server tests: path setup and a test signal."""

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MQTT_DIR = os.path.dirname(HERE)
REPO_DIR = os.path.normpath(os.path.join(MQTT_DIR, "..", ".."))
for path in (MQTT_DIR, os.path.join(REPO_DIR, "analysis", "features")):
    if path not in sys.path:
        sys.path.insert(0, path)


def sine(n=8000, amplitude=400, freq=440, offset=1760, fs=8000):
    return [int(round(offset + amplitude * math.sin(2 * math.pi * freq * i / fs))) for i in range(n)]
