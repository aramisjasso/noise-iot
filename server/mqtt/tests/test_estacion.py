"""Tests for estacion.py with messages built as firmware/ESP32/esp32_mqtt_5min.ino does."""

import csv
import json
import os
import struct
import tempfile
import unittest
import wave

import helpers  # noqa: F401 (path setup)

from estacion import StationReceiver

SID = "20261113T183000Z"


def audio(seq, value, padded=0, n=4000):
    return struct.pack("<16sIH", SID.encode(), seq, padded) + struct.pack("<%dH" % n, *([value] * n))


def inicio(bloques=3):
    return json.dumps({"dispositivo": "esp32-01", "sesion": SID, "fs": 8000, "duracion_s": 1,
                       "muestras_bloque": 4000, "bloques": bloques, "bits_adc": 12}).encode()


def dht(momento, temp=24.0):
    return json.dumps({"sesion": SID, "momento": momento, "hora_utc": SID, "temp": temp,
                       "hum": 61.0, "intentos": 1}).encode()


def fin():
    return json.dumps({"sesion": SID, "duracion_real_ms": 1500, "muestras_rellenadas": 0,
                       "bloques_perdidos_buffer": 0, "bloques_no_enviados": 0,
                       "bloques_enviados": 3, "buffers": 16, "buffers_libres_min": 15}).encode()


class StationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rx = StationReceiver(self.tmp.name)
        self.folder = os.path.join(self.tmp.name, "esp32-01", SID)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, kind, payload):
        return self.rx.handle("estacion/esp32-01/" + kind, payload)

    def test_full_session_writes_all_files(self):
        self.send("estado", b"en_linea")
        self.send("inicio", inicio())
        self.send("dht", dht("antes"))
        for seq in range(3):
            self.send("audio", audio(seq, 1000 + seq))
        self.send("dht", dht("despues", 25.0))
        self.assertIn("3/3", self.send("fin", fin()))
        for name in ("audio_crudo.u16", "audio.wav", "temp_hum.csv", "bloques.csv", "sesion.json"):
            self.assertTrue(os.path.exists(os.path.join(self.folder, name)), name)
        self.assertEqual(os.path.getsize(os.path.join(self.folder, "audio_crudo.u16")), 3 * 8000)
        with wave.open(os.path.join(self.folder, "audio.wav")) as w:
            self.assertEqual((w.getnframes(), w.getframerate()), (12000, 8000))
        with open(os.path.join(self.folder, "temp_hum.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["momento"] for r in rows], ["antes", "despues"])
        meta = json.load(open(os.path.join(self.folder, "sesion.json"), encoding="utf-8"))
        self.assertEqual(meta["bloques_recibidos"], 3)
        self.assertEqual(meta["bloques_faltantes"], [])
        self.assertTrue(meta["inicio_recibido"])
        self.assertEqual(meta["fin"]["bloques_enviados"], 3)

    def test_missing_inicio_and_lost_block(self):
        # inicio dropped by PubSubClient (>256 bytes) and block 1 never arrived
        self.send("audio", audio(0, 1000))
        self.send("audio", audio(2, 1002))
        self.send("fin", fin())
        meta = json.load(open(os.path.join(self.folder, "sesion.json"), encoding="utf-8"))
        self.assertFalse(meta["inicio_recibido"])
        self.assertEqual(meta["bloques_faltantes"], [1])
        raw = open(os.path.join(self.folder, "audio_crudo.u16"), "rb").read()
        self.assertEqual(len(raw), 3 * 8000)  # block 2 in its place, block 1 as zeros
        self.assertEqual(raw[8000:16000], bytes(8000))

    def test_bad_messages_raise(self):
        with self.assertRaises(ValueError):
            self.send("audio", b"short")
        with self.assertRaises(ValueError):
            self.rx.handle("estacion/esp32-01/otro/nivel", b"x")


class RecordingStore:
    """Records the PgStore calls made by StationReceiver."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))

    def named(self, name):
        return [args for n, args in self.calls if n == name]


class StationDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RecordingStore()
        self.rx = StationReceiver(self.tmp.name, self.store, "tepic")

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, kind, payload):
        return self.rx.handle("estacion/esp32-01/" + kind, payload)

    def test_session_goes_to_the_tables(self):
        self.send("inicio", inicio())
        self.send("dht", dht("antes"))
        for seq in range(3):
            self.send("audio", audio(seq, 1000 + seq, padded=seq))
        self.send("dht", json.dumps({"sesion": SID, "momento": "despues", "temp": None,
                                     "hum": None, "intentos": 3}).encode())
        self.send("fin", fin())

        site, node, sid, started = self.store.named("estacion_session")[0]
        self.assertEqual((site, node, sid), ("tepic", "esp32-01", SID))
        self.assertEqual(started.isoformat(), "2026-11-13T18:30:00+00:00")
        fields = self.store.named("estacion_start")[0][2]
        self.assertEqual((fields["clip_samples"], fields["session_s"]), (4000, 1))

        clips = self.store.named("clip")
        self.assertEqual(len(clips), 3)
        _, _, row, features, version = clips[2]
        self.assertEqual((row["clip_idx"], row["t_offset_ms"], row["sample_offset"]), (2, 1000, 8000))
        self.assertEqual((row["late_samples"], row["n_samples"]), (2, 4000))
        self.assertEqual(len(features["mfcc_mean"]), 13)
        self.assertEqual(version, "mfcc13-v1")

        climate = [args[1] for args in self.store.named("climate")]
        self.assertEqual([c["t_offset_ms"] for c in climate], [0, 1000])
        self.assertIsNone(climate[1]["temp_c"])
        self.assertEqual(climate[1]["error"], "sin lectura valida del DHT11")

        _, _, end, meta = self.store.named("estacion_end")[0]
        self.assertEqual((end["clips_sent"], end["clips_failed"]), (3, 0))
        self.assertEqual(meta["servidor"]["bloques_faltantes"], [])

    def test_blocks_without_inicio_still_stored(self):
        self.send("audio", audio(0, 1000))
        self.assertEqual(len(self.store.named("estacion_session")), 1)
        self.assertEqual(len(self.store.named("clip")), 1)


if __name__ == "__main__":
    unittest.main()
