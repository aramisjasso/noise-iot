"""MQTT subscriber: stores what the nodes publish (contracts/mqtt.md).

Listens to noise/+/+/# on the broker and, for every message:

- session/start, session/end, climate: validates the JSON and writes the row.
- clip: saves the audio (.u16 raw and .wav), computes the 30 features of
  analysis/features/mfcc.py and writes the clip and its features.

A bad message is logged and skipped; it never stops the subscriber. The
MQTT session is persistent (fixed client id, clean_session=False, QoS 1), so
messages published while the subscriber is down are delivered when it
comes back, as long as Mosquitto keeps them (persistence true).

Configuration comes from environment variables or from a .env file next to
this script (see .env.example).

Usage:
    python server/mqtt/subscriber.py
"""

import logging
import os
import sys
import time

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO_DIR, "analysis", "features"))

import mfcc  # noqa: E402
import protocol  # noqa: E402

log = logging.getLogger("subscriber")


def load_env(path=os.path.join(HERE, ".env")):
    """Load KEY=VALUE lines from .env without overriding real variables."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


class Ingestor:
    """Routes each message to the store. Independent of paho, for the tests."""

    def __init__(self, store, audio_dir):
        self.store = store
        self.audio_dir = audio_dir

    def handle(self, topic, payload):
        """Process one message.

        Returns:
            Short description of what was done (for the log).

        Raises:
            protocol.ProtocolError if the message breaks the contract.
        """
        site, node_id, kind = protocol.parse_topic(topic)
        if kind == "clip":
            return self._clip(site, node_id, payload)
        msg = protocol.parse_json(kind, node_id, payload)
        if kind == "session/start":
            self.store.session_start(site, msg)
        elif kind == "session/end":
            self.store.session_end(site, msg)
        else:
            self.store.climate(site, msg)
        return "%s %s %s" % (kind, node_id, msg["session_id"])

    def _clip(self, site, node_id, payload):
        header, samples = protocol.parse_clip(payload)
        sid, idx = header["session_id"], header["clip_idx"]
        if self.store.clip_exists(node_id, sid, idx):
            return "clip %s %s #%d duplicate" % (node_id, sid, idx)
        raw_path, wav_path = protocol.write_clip_files(self.audio_dir, node_id, sid, idx, samples)
        row = dict(header, **protocol.clip_stats(samples))
        row.update(raw_path=raw_path, wav_path=wav_path)
        features = mfcc.clip_features(samples)
        self.store.clip(site, node_id, row, features, mfcc.FEATURE_VERSION)
        return "clip %s %s #%d late=%d clipped=%d" % (
            node_id, sid, idx, header["late_samples"], row["clipped_samples"])


def main():
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()]
        + ([logging.FileHandler(os.environ["LOG_FILE"], encoding="utf-8")]
           if os.environ.get("LOG_FILE") else []),
    )
    from store import PgStore

    host = os.environ.get("MQTT_HOST", "localhost")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    audio_dir = os.environ.get("AUDIO_DIR", os.path.join(REPO_DIR, "data", "raw", "audio"))
    store = PgStore(os.environ["PG_DSN"])
    ingestor = Ingestor(store, audio_dir)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=os.environ.get("MQTT_CLIENT_ID", "noise-subscriber"),
        clean_session=False,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("connected to %s:%d (%s)", host, port, reason_code)
        client.subscribe("noise/+/+/#", qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning("disconnected (%s), reconnecting", reason_code)

    def on_message(client, userdata, message):
        try:
            log.info(ingestor.handle(message.topic, message.payload))
        except protocol.ProtocolError as exc:
            log.warning("rejected %s: %s", message.topic, exc)
        except Exception:  # database down, disk full...: log and keep going
            log.exception("failed %s", message.topic)
            try:
                store.close()
            except Exception:
                pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log.info("audio in %s", audio_dir)
    while True:
        try:
            client.connect(host, port, keepalive=60)
            break
        except OSError as exc:
            log.warning("broker not reachable (%s), retrying in 10 s", exc)
            time.sleep(10)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
