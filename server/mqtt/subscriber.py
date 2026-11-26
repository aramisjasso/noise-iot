"""MQTT subscriber: stores what the nodes publish on estacion/#.

The firmware (firmware/ESP32) publishes each session on estacion/<device>/...
(inicio, audio, dht, fin, estado). Every session is stored as files in
ESTACION_DIR and its blocks, DHT readings and summary go to PostgreSQL (see
estacion.py). ESTACION_SITE says which site the nodes belong to.

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
sys.path.insert(0, os.path.join(REPO_DIR, "analysis", "features"))  # mfcc, used by estacion.py

from estacion import StationReceiver  # noqa: E402

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
    # ESTACION_DIR is the folder of the sessions; without it, it falls back to a
    # sibling "estacion" folder of AUDIO_DIR, as in earlier versions of the .env.
    audio_dir = os.environ.get("AUDIO_DIR", os.path.join(REPO_DIR, "data", "raw", "audio"))
    estacion_dir = os.environ.get("ESTACION_DIR", os.path.join(os.path.dirname(audio_dir), "estacion"))
    store = PgStore(os.environ["PG_DSN"])
    station = StationReceiver(estacion_dir, store, os.environ.get("ESTACION_SITE", "tepic"))

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=os.environ.get("MQTT_CLIENT_ID", "noise-subscriber"),
        clean_session=False,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("connected to %s:%d (%s)", host, port, reason_code)
        client.subscribe([("estacion/#", 1)])

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning("disconnected (%s), reconnecting", reason_code)

    def on_message(client, userdata, message):
        try:
            log.info(station.handle(message.topic, message.payload))
            for line in station.close_idle():
                log.warning("estacion sesion sin fin, cerrada: %s", line)
        except Exception as exc:
            log.warning("estacion rejected %s: %r", message.topic, exc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    log.info("estacion sessions in %s", estacion_dir)
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
