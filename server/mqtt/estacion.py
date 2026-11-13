"""Receiver for the estacion/ topics of Jesús's Arduino firmware.

firmware/ESP32/esp32_mqtt_5min.ino publishes in its own format (estacion/<device>/...),
not in contracts/mqtt.md. This module stores it the same way his TCP receiver
(firmware/experiments/tcp_v1/receptor_pc.py) did, so he can check that the data
arrives complete:

    <ESTACION_DIR>/<device>/<session>/
        audio_crudo.u16   ADC samples as received (uint16 little-endian, 8 kHz)
        audio.wav         same audio, mono 16-bit, DC removed (written at "fin")
        temp_hum.csv      DHT11 readings ("antes" and "despues")
        bloques.csv       one row per audio block: seq, padded samples, arrival time
        sesion.json       "inicio" metadata + "fin" summary + what the PC received

Nothing here goes to PostgreSQL. It is a check, not the campaign pipeline.

Messages (from the .ino):
    estacion/<d>/inicio   JSON  session metadata (may be missing: over 256 bytes,
                                PubSubClient drops it unless setBufferSize is raised)
    estacion/<d>/audio    BIN   session(16 bytes) + seq(uint32) + padded(uint16) + uint16 samples
    estacion/<d>/dht      JSON  {"sesion","momento","hora_utc","temp","hum","intentos"}
    estacion/<d>/fin      JSON  session summary
    estacion/<d>/estado   text  "en_linea" / "desconectado"
"""

import array
import csv
import json
import os
import struct
import sys
import time
import wave

AUDIO_HEADER = struct.Struct("<16sIH")  # session, seq, padded samples
DEFAULT_FS = 8000
DEFAULT_BLOCK = 4000


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def raw_to_wav(raw_path, wav_path, fs):
    """Convert the uint16 ADC file to 16-bit signed WAV, removing the DC level."""
    samples = array.array("H")
    with open(raw_path, "rb") as f:
        samples.frombytes(f.read())
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return
    mean = sum(samples) / len(samples)
    pcm = array.array("h", (max(-32768, min(32767, int(x - mean))) for x in samples))
    if sys.byteorder == "big":
        pcm.byteswap()
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(pcm.tobytes())


class StationSession:
    """Files of one recording session."""

    def __init__(self, base_dir, device, session):
        self.device = device
        self.session = session
        self.folder = os.path.join(base_dir, device, session)
        os.makedirs(self.folder, exist_ok=True)
        self.info = {}
        self.fs = DEFAULT_FS
        self.block = None  # samples per block, from "inicio" or the first block
        self.raw_path = os.path.join(self.folder, "audio_crudo.u16")
        self.f_audio = open(self.raw_path, "wb")
        self.f_dht = open(os.path.join(self.folder, "temp_hum.csv"), "w", newline="")
        self.dht = csv.writer(self.f_dht)
        self.dht.writerow(["hora_pc", "momento", "hora_utc", "temperatura_c", "humedad_pct", "intentos"])
        self.f_blocks = open(os.path.join(self.folder, "bloques.csv"), "w", newline="")
        self.blocks = csv.writer(self.f_blocks)
        self.blocks.writerow(["hora_pc", "seq", "muestras_rellenadas", "bytes"])
        self.received = 0
        self.padded = 0
        self.seqs = set()
        self.first_pc = _now()
        self.last_seen = time.time()
        self.closed = False

    def start(self, info):
        self.info = info
        self.fs = int(info.get("fs", DEFAULT_FS))
        self.block = int(info.get("muestras_bloque", self.block or DEFAULT_BLOCK))

    def audio(self, seq, padded, samples):
        n = len(samples) // 2
        if self.block is None:
            self.block = n
        # Each block goes to its own position: a missing block leaves zeros.
        self.f_audio.seek(seq * self.block * 2)
        self.f_audio.write(samples)
        self.blocks.writerow([_now(), seq, padded, len(samples)])
        self.received += 1
        self.padded += padded
        self.seqs.add(seq)
        self.last_seen = time.time()

    def reading(self, d):
        self.dht.writerow([_now(), d.get("momento"), d.get("hora_utc"), d.get("temp"),
                           d.get("hum"), d.get("intentos")])
        self.f_dht.flush()
        self.f_blocks.flush()
        self.last_seen = time.time()

    def missing_blocks(self):
        expected = int(self.info.get("bloques") or (max(self.seqs) + 1 if self.seqs else 0))
        return sorted(set(range(expected)) - self.seqs), expected

    def close(self, fin=None, complete=True):
        if self.closed:
            return None
        self.closed = True
        self.f_audio.close()
        self.f_dht.close()
        self.f_blocks.close()
        raw_to_wav(self.raw_path, os.path.join(self.folder, "audio.wav"), self.fs)
        missing, expected = self.missing_blocks()
        meta = dict(self.info)
        meta.update({
            "dispositivo": self.device,
            "sesion": self.session,
            "inicio_recibido": bool(self.info),
            "primer_mensaje_pc": self.first_pc,
            "fin_pc": _now(),
            "bloques_esperados": expected,
            "bloques_recibidos": self.received,
            "bloques_faltantes": missing,
            "muestras_rellenadas_recibidas": self.padded,
            "completa": complete,
            "fin": fin,
        })
        with open(os.path.join(self.folder, "sesion.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        return "%s: %d/%d bloques, %d faltantes, completa=%s" % (
            self.folder, self.received, expected, len(missing), complete)


class StationReceiver:
    """Routes estacion/<device>/<kind> messages to per-session files."""

    IDLE_S = 15 * 60  # a session with no messages for this long is closed as incomplete

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.sessions = {}  # (device, session) -> StationSession
        self.status = {}  # device -> last estado

    def _session(self, device, session):
        key = (device, session)
        if key not in self.sessions:
            # A new session of the same device closes the previous one.
            for (dev, sid), s in list(self.sessions.items()):
                if dev == device and sid != session:
                    s.close(complete=False)
                    del self.sessions[(dev, sid)]
            self.sessions[key] = StationSession(self.base_dir, device, session)
        return self.sessions[key]

    def close_idle(self):
        now = time.time()
        done = []
        for key, s in list(self.sessions.items()):
            if now - s.last_seen > self.IDLE_S:
                done.append(s.close(complete=False))
                del self.sessions[key]
        return done

    def handle(self, topic, payload):
        """Process one estacion/ message. Returns a line for the log."""
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "estacion":
            raise ValueError("unknown estacion topic: %s" % topic)
        _, device, kind = parts
        if kind == "estado":
            self.status[device] = payload.decode("utf-8", "replace")
            return "estacion %s estado=%s" % (device, self.status[device])
        if kind == "audio":
            if len(payload) <= AUDIO_HEADER.size:
                raise ValueError("audio block too short (%d bytes)" % len(payload))
            sid, seq, padded = AUDIO_HEADER.unpack_from(payload, 0)
            session = sid.decode("ascii", "replace").rstrip("\x00")
            self._session(device, session).audio(seq, padded, payload[AUDIO_HEADER.size:])
            return "estacion %s %s audio #%d rellenos=%d" % (device, session, seq, padded)
        msg = json.loads(payload)
        session = msg.get("sesion")
        if not session:
            raise ValueError("%s without sesion" % kind)
        s = self._session(device, session)
        if kind == "inicio":
            s.start(msg)
            return "estacion %s %s inicio" % (device, session)
        if kind == "dht":
            s.reading(msg)
            return "estacion %s %s dht %s T=%s H=%s" % (
                device, session, msg.get("momento"), msg.get("temp"), msg.get("hum"))
        if kind == "fin":
            line = s.close(fin=msg, complete=True)
            del self.sessions[(device, session)]
            return "estacion sesion cerrada " + line
        raise ValueError("unknown estacion kind: %s" % kind)
