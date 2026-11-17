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

With a store (PgStore), every message also goes to PostgreSQL, adapted to the
tables of schema.sql:

    inicio  -> sessions (firmware, fs, adc_bits, clip_samples = muestras_bloque,
               session_s = duracion_s; the whole message in sessions.meta.inicio)
    audio   -> clips, one row per 0.5 s block (clip_idx = seq, late_samples = padded
               samples, raw_path = the session's audio_crudo.u16 at sample_offset),
               and clip_features computed on that block
    dht     -> climate (t_offset_ms 0 for "antes", end of the audio for "despues")
    fin     -> sessions (duration, blocks sent and lost; whole message in meta.fin)

The session row is created on the first message of any kind, with started_at
taken from the session id, so a lost "inicio" does not lose the blocks.

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
from datetime import datetime, timezone

AUDIO_HEADER = struct.Struct("<16sIH")  # session, seq, padded samples
ADC_MAX = 4095
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


def session_start_time(session):
    """UTC datetime of a YYYYMMDDTHHMMSSZ session id, or None if it does not parse."""
    try:
        return datetime.strptime(session, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class StationReceiver:
    """Routes estacion/<device>/<kind> messages to per-session files and, with a
    store, to PostgreSQL."""

    IDLE_S = 15 * 60  # a session with no messages for this long is closed as incomplete

    def __init__(self, base_dir, store=None, site="tepic"):
        self.base_dir = base_dir
        self.store = store
        self.site = site
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
            if self.store is not None:
                self.store.estacion_session(self.site, device, session, session_start_time(session))
        return self.sessions[key]

    # ------------------------------------------------------------ database ---

    def _db_block(self, device, s, seq, padded, raw):
        import numpy as np
        import mfcc

        samples = np.frombuffer(raw, dtype="<u2")
        if samples.size == 0 or samples.max() > ADC_MAX:
            raise ValueError("block %d has samples out of range" % seq)
        block = s.block or samples.size
        row = {
            "session_id": s.session,
            "clip_idx": seq,
            "t_offset_ms": seq * block * 1000 // s.fs,
            "n_samples": int(samples.size),
            # Hardware timer on the node: the server records the nominal timing.
            "capture_us": samples.size * 1000000 // s.fs,
            "fs_measured": float(s.fs),
            "late_samples": padded,
            "adc_mean": float(samples.mean()),
            "adc_min": int(samples.min()),
            "adc_max": int(samples.max()),
            "clipped_samples": int(np.count_nonzero((samples == 0) | (samples == ADC_MAX))),
            "raw_path": s.raw_path,
            "wav_path": os.path.join(s.folder, "audio.wav"),
            "sample_offset": seq * block,
        }
        self.store.clip(self.site, device, row, mfcc.clip_features(samples), mfcc.FEATURE_VERSION)

    def _db_climate(self, device, s, msg):
        if msg.get("momento") == "antes":
            t_offset = 0
        elif s.info.get("duracion_s"):
            t_offset = int(s.info["duracion_s"]) * 1000
        else:
            last = max(s.seqs) + 1 if s.seqs else 0
            t_offset = last * (s.block or DEFAULT_BLOCK) * 1000 // s.fs
        ok = msg.get("temp") is not None and msg.get("hum") is not None
        self.store.climate(self.site, {
            "node_id": device,
            "session_id": s.session,
            "t_offset_ms": t_offset,
            "temp_c": msg.get("temp"),
            "rh": msg.get("hum"),
            "attempts": int(msg.get("intentos") or 1),
            "error": None if ok else "sin lectura valida del DHT11",
        })

    def _db_end(self, device, s, fin):
        missing, expected = s.missing_blocks()
        sent = int(fin.get("bloques_enviados", s.received))
        lost = int(fin.get("bloques_perdidos_buffer", 0))
        self.store.estacion_end(device, s.session, {
            "duration_ms": int(fin.get("duracion_real_ms", 0)),
            "clips_captured": expected - lost,
            "clips_sent": sent,
            "clips_failed": expected - sent,
            "late_samples": int(fin.get("muestras_rellenadas", s.padded)),
        }, {
            "fin": fin,
            "servidor": {"bloques_recibidos": s.received, "bloques_esperados": expected,
                         "bloques_faltantes": missing},
        })

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
            s = self._session(device, session)
            raw = payload[AUDIO_HEADER.size:]
            s.audio(seq, padded, raw)
            if self.store is not None:
                self._db_block(device, s, seq, padded, raw)
            return "estacion %s %s audio #%d rellenos=%d" % (device, session, seq, padded)
        msg = json.loads(payload)
        session = msg.get("sesion")
        if not session:
            raise ValueError("%s without sesion" % kind)
        s = self._session(device, session)
        if kind == "inicio":
            s.start(msg)
            if self.store is not None:
                self.store.estacion_start(device, session, {
                    "firmware_version": msg.get("firmware"),
                    "fs": s.fs,
                    "adc_bits": msg.get("bits_adc"),
                    "clip_samples": s.block,
                    "session_s": msg.get("duracion_s"),
                }, {"inicio": msg})
            return "estacion %s %s inicio" % (device, session)
        if kind == "dht":
            s.reading(msg)
            if self.store is not None:
                self._db_climate(device, s, msg)
            return "estacion %s %s dht %s T=%s H=%s" % (
                device, session, msg.get("momento"), msg.get("temp"), msg.get("hum"))
        if kind == "fin":
            if self.store is not None:
                self._db_end(device, s, msg)
            line = s.close(fin=msg, complete=True)
            del self.sessions[(device, session)]
            return "estacion sesion cerrada " + line
        raise ValueError("unknown estacion kind: %s" % kind)
