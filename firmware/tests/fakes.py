"""Shared setup for the firmware tests on the PC.

Importing this module puts firmware/ and firmware/lib/ on sys.path (as they
are on the board) and installs stand-ins for the modules that only exist on
the ESP32 or on the node (config.py).
"""

import json
import math
import os
import sys
import types

FIRMWARE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
REPO_DIR = os.path.dirname(FIRMWARE_DIR)
for path in (FIRMWARE_DIR, os.path.join(FIRMWARE_DIR, "lib")):
    if path not in sys.path:
        sys.path.insert(0, path)

with open(os.path.join(REPO_DIR, "contracts", "message.schema.json"), encoding="utf-8") as f:
    CONTRACT = json.load(f)


def mqtt_schema(name):
    """Load contracts/mqtt/<name>.schema.json."""
    path = os.path.join(REPO_DIR, "contracts", "mqtt", name + ".schema.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)

if "config" not in sys.modules:
    config = types.ModuleType("config")
    config.WIFI_SSID = "test"
    config.WIFI_PASSWORD = "test"
    config.NODE_ID = "test-01"
    config.INGEST_URL = "http://localhost/ingest"
    config.INGEST_KEY = "test-key"
    config.FIRMWARE_VERSION = "0.0.0-test"
    config.MIC_CAL_DB = 0.0
    config.WATCHDOG = False
    config.SITE = "tepic"
    config.MQTT_HOST = "localhost"
    config.MQTT_PORT = 1883
    config.SESSION_S = 600
    config.MIC_MODEL = "KY-037"
    config.GAIN_SETTING = ""
    config.DHT_PIN = 4
    sys.modules["config"] = config


class FakeADC:
    """Returns a sine around mid-scale, like the biased front end.

    amplitude is in 12-bit counts; read_u16() scales to 16 bits as
    MicroPython does.
    """

    def __init__(self, amplitude=0, offset=2048, period=16):
        self.amplitude = amplitude
        self.offset = offset
        self.period = period
        self.i = 0

    def read_u16(self):
        v = self.offset + self.amplitude * math.sin(2 * math.pi * self.i / self.period)
        self.i += 1
        v = min(max(int(round(v)), 0), 4095)
        return v << 4


class FakeDHT:
    """DHT11 stand-in; fail_at holds the call numbers (0-based) that fail."""

    def __init__(self, fail_at=(), temp=24, rh=61):
        self.fail_at = set(fail_at)
        self.calls = 0
        self.temp = temp
        self.rh = rh
        self._ok = False

    def measure(self):
        call = self.calls
        self.calls += 1
        self._ok = call not in self.fail_at
        if not self._ok:
            raise OSError(116, "ETIMEDOUT")

    def temperature(self):
        return self.temp

    def humidity(self):
        return self.rh


class FakePublisher:
    """Records every publish; fail_topics makes publishes to those kinds fail."""

    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []
        self.connected = True

    def publish(self, topic, payload):
        self.messages.append((topic, bytes(payload) if isinstance(payload, bytearray) else payload))
        return not self.fail

    def of_kind(self, kind):
        return [p for t, p in self.messages if t.endswith("/" + kind)]


class FakeMQTTClient:
    """umqtt.simple stand-in: fail_connect / fail_publish simulate a dead broker."""

    instances = []

    def __init__(self, client_id, host, port, fail_connect=False, fail_publish=False):
        self.args = (client_id, host, port)
        self.fail_connect = fail_connect
        self.fail_publish = fail_publish
        self.published = []
        self.disconnected = False
        FakeMQTTClient.instances.append(self)

    def connect(self):
        if self.fail_connect:
            raise OSError(113, "EHOSTUNREACH")

    def publish(self, topic, msg, qos=0):
        if self.fail_publish:
            raise OSError(110, "ETIMEDOUT")
        self.published.append((topic, msg, qos))

    def disconnect(self):
        self.disconnected = True
