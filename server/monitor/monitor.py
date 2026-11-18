"""Add-on monitor for noise-iot (no changes to the classifier or subscriber).

Run after applying schema.sql and registering monitored_nodes.
Dependencies: server/mqtt/requirements.txt (psycopg 3 and paho-mqtt 2).
"""
import json
import logging
import math
import os
import queue
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import psycopg

HERE = Path(__file__).resolve().parent
MQTT_DIR = HERE.parent / "mqtt"


def load_env(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(MQTT_DIR / ".env")
load_env(HERE / ".env")
DSN = os.environ.get("PG_DSN", "")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
HC_URL = os.environ.get("HEALTHCHECKS_PING_URL", "")
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
POLL_S = max(5, int(os.environ.get("MONITOR_POLL_S", "15")))
MQTT_DISCONNECT_MIN = int(os.environ.get("MQTT_DISCONNECT_ALERT_MIN", "20"))

log = logging.getLogger("noise-monitor")
now_utc = lambda: datetime.now(timezone.utc)


def seconds_since(instant):
    return (now_utc() - instant).total_seconds()


def send_discord(message, alert=True):
    text = message
    allowed = {"parse": []}
    if alert and ROLE_ID:
        text = f"<@&{ROLE_ID}> {text}"
        allowed = {"roles": [ROLE_ID]}
    body = json.dumps({"content": text, "allowed_mentions": allowed}).encode("utf-8")
    request = urllib.request.Request(
        WEBHOOK, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord HTTP {response.status}")


def set_alert(conn, target, kind, active, description):
    """One notification on incident start, one on recovery. Retry on HTTP failure."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT active FROM monitor_alert_state WHERE target=%s AND kind=%s",
            (target, kind),
        )
        row = cur.fetchone()
        if row and row[0] == active:
            return
        if not active and not row:
            return
        verb = "ALERTA" if active else "RECUPERADO"
        message = f"**{verb}** | {target} | {description}"
        try:
            send_discord(message, alert=active)
        except Exception:
            log.exception("Discord send failed; retry next cycle: %s/%s", target, kind)
            return
        cur.execute(
            """INSERT INTO monitor_alert_state(target,kind,active,updated_at)
               VALUES (%s,%s,%s,now()) ON CONFLICT(target,kind)
               DO UPDATE SET active=EXCLUDED.active,updated_at=now()""",
            (target, kind, active),
        )
        cur.execute(
            """INSERT INTO monitor_alert_history(target,kind,active,description)
               VALUES (%s,%s,%s,%s)""",
            (target, kind, active, description),
        )
        log.info("%s %s %s", verb, target, kind)


def invalid_dht(value):
    try:
        return value is None or not math.isfinite(float(value)) or float(value) == 0
    except (ValueError, TypeError):
        return True


def record_mqtt_event(conn, node_id, state):
    if state not in ("en_linea", "desconectado"):
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO mqtt_node_status
                (node_id,state,changed_at,observed_at,disconnected_since)
                VALUES (%s,%s,now(),now(),CASE WHEN %s='desconectado' THEN now() END)
                ON CONFLICT(node_id) DO UPDATE SET
                    state=EXCLUDED.state,
                    observed_at=now(),
                    changed_at=CASE WHEN mqtt_node_status.state=EXCLUDED.state
                                    THEN mqtt_node_status.changed_at ELSE now() END,
                    disconnected_since=CASE
                       WHEN EXCLUDED.state='en_linea' THEN NULL
                       WHEN mqtt_node_status.state='desconectado'
                            THEN mqtt_node_status.disconnected_since
                       ELSE now() END""",
            (node_id, state, state),
        )


def check_node(conn, node):
    node_id, pipeline, missing_min, min_span, min_rms, registered = node
    with conn.cursor() as cur:
        cur.execute("SELECT max(received_at) FROM clips WHERE node_id=%s", (node_id,))
        last_audio = cur.fetchone()[0]
        age_min = seconds_since(last_audio or registered) / 60
        no_audio = age_min > missing_min
        set_alert(conn, node_id, "audio_missing", no_audio,
                  f"{'sin datos' if no_audio else 'recibe audio'}; última recepción hace {age_min:.1f} min")

        # Evaluate the last 3 received blocks. The classifier offers a stronger
        # filtered-RMS test for estacion/; noise/ uses raw ADC range only.
        cur.execute(
            """SELECT adc_max - adc_min, received_at FROM clips
               WHERE node_id=%s ORDER BY received_at DESC LIMIT 3""",
            (node_id,),
        )
        blocks = cur.fetchall()
        if len(blocks) == 3 and seconds_since(blocks[0][1]) < 120:
            bad = [int(span) <= min_span for span, _ in blocks]
            source = f"ADC: span <= {min_span}"
            if pipeline == "estacion":
                cur.execute(
                    """SELECT status,reason,rms_dbfs,computed_at FROM (
                           SELECT DISTINCT ON (session_id,second_idx)
                                  session_id,second_idx,status,reason,rms_dbfs,computed_at
                           FROM predictions WHERE node_id=%s
                           ORDER BY session_id,second_idx,computed_at DESC
                       ) unique_seconds
                       ORDER BY computed_at DESC LIMIT 3""",
                    (node_id,),
                )
                predictions = cur.fetchall()
                if len(predictions) == 3 and seconds_since(predictions[0][3]) < 120:
                    bad = [
                        (status == "excluded" and any(tag in (reason or "") for tag in
                         ("all_zero_block", "constant_block", "zero_run_ge_100")))
                        or (rms is not None and math.isfinite(float(rms)) and float(rms) < min_rms)
                        for status, reason, rms, _ in predictions
                    ]
                    source = f"calidad/RMS (umbral {min_rms} dBFS)"
            if all(bad):
                set_alert(conn, node_id, "microphone", True,
                          f"micrófono sospechoso, 3 fragmentos seguidos ({source})")
            elif not any(bad):
                set_alert(conn, node_id, "microphone", False,
                          "3 fragmentos consecutivos con señal normal")

        cur.execute(
            """SELECT temp_c, rh FROM climate WHERE node_id=%s
               ORDER BY received_at DESC LIMIT 1""",
            (node_id,),
        )
        climate = cur.fetchone()
        if climate:
            bad_dht = invalid_dht(climate[0]) or invalid_dht(climate[1])
            set_alert(conn, node_id, "dht", bad_dht,
                      f"DHT: temperatura={climate[0]}, humedad={climate[1]}")

        # MQTT LWT is present only on Arduino estacion nodes in the current repo.
        if pipeline == "estacion":
            cur.execute(
                "SELECT state,disconnected_since FROM mqtt_node_status WHERE node_id=%s",
                (node_id,),
            )
            mqtt_row = cur.fetchone()
            if mqtt_row:
                state, disconnected_since = mqtt_row
                disconnected_too_long = (
                    state == "desconectado" and disconnected_since is not None
                    and seconds_since(disconnected_since) > MQTT_DISCONNECT_MIN * 60
                )
                if disconnected_too_long:
                    set_alert(conn, node_id, "mqtt", True,
                              f"MQTT desconectado hace más de {MQTT_DISCONNECT_MIN} min")
                elif state == "en_linea":
                    set_alert(conn, node_id, "mqtt", False,
                              "conexión MQTT restaurada")


def set_service(conn, service, state, detail=""):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO monitor_service_status(service,state,detail,checked_at)
               VALUES (%s,%s,%s,now()) ON CONFLICT(service)
               DO UPDATE SET state=EXCLUDED.state, detail=EXCLUDED.detail,
                             checked_at=now()""",
            (service, state, detail),
        )


def check_classifier(conn):
    """Only alert if station audio is arriving but classification lags behind."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT max(c.received_at) FROM clips c JOIN monitored_nodes n
               ON n.node_id=c.node_id WHERE n.pipeline='estacion' AND n.enabled"""
        )
        latest_clip = cur.fetchone()[0]
        if not latest_clip or seconds_since(latest_clip) > 120:
            set_service(conn, "classifier", "idle", "Sin audio reciente de estacion")
            return  # No fresh station audio: an idle classifier is not a fault.
        cur.execute(
            """SELECT max(p.computed_at) FROM predictions p JOIN monitored_nodes n
               ON n.node_id=p.node_id WHERE n.pipeline='estacion' AND n.enabled"""
        )
        latest_prediction = cur.fetchone()[0]
        stalled = latest_prediction is None or (latest_clip - latest_prediction).total_seconds() > 180
        set_service(conn, "classifier", "down" if stalled else "ok")
        set_alert(conn, "server", "classifier", stalled,
                  "clasificador atrasado con respecto al audio entrante" if stalled
                  else "clasificador al día")


def ping_healthchecks():
    if not HC_URL:
        return
    with urllib.request.urlopen(HC_URL, timeout=8) as response:
        if response.status >= 400:
            raise RuntimeError(f"Healthchecks HTTP {response.status}")


def main():
    if not DSN or not WEBHOOK.startswith("https://discord.com/api/webhooks/"):
        raise SystemExit("Configura PG_DSN y DISCORD_WEBHOOK_URL en los .env")
    event_queue = queue.Queue()
    broker = {"connected": False}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="noise-monitor-alerts")
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        broker["connected"] = not reason_code.is_failure
        if broker["connected"]:
            client.subscribe("estacion/+/estado", qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        broker["connected"] = False

    def on_message(client, userdata, message):
        parts = message.topic.split("/")
        if len(parts) == 3 and parts[2] == "estado":
            event_queue.put((parts[1], message.payload.decode("utf-8", "replace")))

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(os.environ.get("MQTT_HOST", "localhost"),
                         int(os.environ.get("MQTT_PORT", "1883")), keepalive=60)
    client.loop_start()
    last_hc = 0.0
    last_broker_good = time.monotonic()
    try:
        while True:
            try:
                with psycopg.connect(DSN, autocommit=True) as conn:
                    while True:
                        try:
                            node_id, state = event_queue.get_nowait()
                        except queue.Empty:
                            break
                        record_mqtt_event(conn, node_id, state)
                    if broker["connected"]:
                        last_broker_good = time.monotonic()
                    set_service(conn, "monitor", "ok", "Ciclo ejecutado")
                    set_service(conn, "database", "ok", "Consulta PostgreSQL correcta")
                    set_service(conn, "broker", "ok" if broker["connected"] else "down")
                    set_alert(conn, "server", "broker", 
                              time.monotonic() - last_broker_good > 120,
                              "broker MQTT sin conexión" if not broker["connected"]
                              else "broker MQTT accesible")
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT node_id,pipeline,missing_minutes,min_adc_span,
                               min_rms_dbfs,registered_at FROM monitored_nodes WHERE enabled"""
                        )
                        nodes = cur.fetchall()
                    for node in nodes:
                        try:
                            check_node(conn, node)
                        except Exception:
                            log.exception("Node check failed: %s", node[0])
                    check_classifier(conn)
                    if broker["connected"] and HC_URL and time.monotonic()-last_hc > 60:
                        ping_healthchecks()
                        last_hc = time.monotonic()
            except Exception:
                log.exception("Monitor loop failed (no external heartbeat sent)")
            time.sleep(POLL_S)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    main()
