"""Node entry point (firmware v2): 1-second audio clips over MQTT.

States:
    BOOT     Configure ADC1 and the DHT11, connect Wi-Fi and sync the clock
             via NTP, retrying until both work.
    WAIT     Sleep until the next session start (:00 or :30 UTC). Wake up
             PREPARE_S earlier to reconnect Wi-Fi, resync the clock and open
             the MQTT connection.
    SESSION  For SESSION_S seconds: capture a 1 s clip (nothing is sent while
             capturing), publish it, read the DHT11 when due and publish the
             reading; repeat. Publishes session/start before and session/end
             after (contracts/mqtt.md).

Why clips: the TCP prototype (firmware/experiments/tcp_v1) sent audio while
sampling, and 8-18 % of every session ended up as padding. Here the gaps are
between clips, with their time known (t_offset_ms), and every clip is clean.

Design choices:
    - session_id is the scheduled start in UTC (YYYYMMDDTHHMMSSZ); all nodes
      share the same ids, so sessions line up across sites.
    - If the broker is unreachable the session still runs (the clips are
      lost, not the schedule); the publisher retries the connection every
      few clips and session/end reports how many clips failed.
    - A hardware watchdog resets the node if anything hangs (WATCHDOG in
      config.py; turn it off while working at the REPL).
"""

import gc
import json
import struct
import time

try:
    from time import sleep_ms, ticks_add, ticks_diff, ticks_ms
except ImportError:  # CPython, for the tests on the PC
    def ticks_ms():
        return time.monotonic_ns() // 1000000

    def ticks_add(ticks, delta):
        return ticks + delta

    def ticks_diff(a, b):
        return a - b

    def sleep_ms(ms):
        time.sleep(ms / 1000)

import config
import dht_reader
import publisher
import sampler

STATE_BOOT = "BOOT"
STATE_WAIT = "WAIT"
STATE_SESSION = "SESSION"

CYCLE_S = 1800  # a session starts every 30 minutes
PREPARE_S = 45  # wake up this early to reconnect before the session
CLIP_SAMPLES = sampler.CHUNK_SAMPLES  # 1 s at 8 kHz
CLIP_HEADER = "<16sHIIHH"  # session_id, clip_idx, t_offset_ms, capture_us, n, late
CLIP_HEADER_SIZE = struct.calcsize(CLIP_HEADER)  # 30 bytes
DHT_EVERY_MS = 6000
WIFI_TIMEOUT_S = 20
BOOT_RETRY_S = 30
WATCHDOG_TIMEOUT_MS = 5 * 60 * 1000


# ---------------------------------------------------------------- time ---

def iso_utc(t):
    """Format t (time.time() value) as RFC 3339 UTC, e.g. 2026-11-05T18:00:00Z."""
    y, mo, d, h, mi, s = time.gmtime(t)[:6]
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (y, mo, d, h, mi, s)


def session_id(t):
    """Session id of a start time: YYYYMMDDTHHMMSSZ (16 characters, UTC)."""
    y, mo, d, h, mi, s = time.gmtime(t)[:6]
    return "%04d%02d%02dT%02d%02d%02dZ" % (y, mo, d, h, mi, s)


def seconds_until_next_window(now_s):
    """Seconds until the next :00 or :30 boundary; 0 exactly on a boundary.

    The 1970 and 2000 epochs differ by a whole number of 30-minute cycles,
    so the result is the same on every MicroPython port.
    """
    remainder = int(now_s) % CYCLE_S
    return 0 if remainder == 0 else CYCLE_S - remainder


def topic(kind):
    """MQTT topic for this node, e.g. noise/tepic/tepic-01/clip."""
    return "noise/%s/%s/%s" % (config.SITE, config.NODE_ID, kind)


def log(*args):
    print(iso_utc(time.time()), *args)


def sleep_s(seconds, feed=None):
    """Sleep in short slices so the watchdog keeps being fed."""
    deadline = ticks_add(ticks_ms(), int(seconds * 1000))
    while True:
        left = ticks_diff(deadline, ticks_ms())
        if left <= 0:
            return
        if feed:
            feed()
        sleep_ms(min(left, 10000))


# ------------------------------------------------------------- network ---

def connect_wifi(ssid, password, timeout_s=WIFI_TIMEOUT_S):
    """Connect to Wi-Fi in station mode.

    Returns:
        True if connected, False on timeout.
    """
    import network

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return True
    try:
        wlan.connect(ssid, password)
    except OSError as exc:
        log("wifi: connect failed:", repr(exc))
        return False
    deadline = ticks_add(ticks_ms(), timeout_s * 1000)
    while ticks_diff(deadline, ticks_ms()) > 0:
        if wlan.isconnected():
            log("wifi: connected", wlan.ifconfig()[0])
            return True
        sleep_ms(200)
    log("wifi: timeout")
    wlan.active(False)
    return False


def sync_time():
    """Set the RTC from NTP (UTC). Returns True on success."""
    import ntptime

    try:
        ntptime.settime()
    except Exception as exc:  # OSError, timeouts, bad packets
        log("ntp: failed:", repr(exc))
        return False
    log("ntp: clock set")
    return True


# ------------------------------------------------------------- session ---

def start_message(sid, started_at, session_s):
    """Body of session/start (contracts/mqtt/session_start.schema.json)."""
    return {
        "node_id": config.NODE_ID,
        "session_id": sid,
        "started_at": iso_utc(started_at),
        "firmware_version": config.FIRMWARE_VERSION,
        "fs": sampler.SAMPLE_RATE_HZ,
        "adc_bits": sampler.ADC_BITS,
        "clip_samples": CLIP_SAMPLES,
        "session_s": session_s,
        "mic_model": getattr(config, "MIC_MODEL", None) or None,
        "gain_setting": getattr(config, "GAIN_SETTING", None) or None,
        "dht_model": "DHT11",
    }


def run_session(sid, pub, adc, sensor, session_s, clip_samples=CLIP_SAMPLES,
                dht_every_ms=DHT_EVERY_MS, feed=None, clock=time.time):
    """SESSION state: capture and publish clips for session_s seconds.

    Args:
        sid: session id from session_id().
        pub: publisher.Publisher (or anything with publish(topic, payload)).
        adc: ADC returned by sampler.init_adc().
        sensor: DHT11 returned by dht_reader.init_sensor().
        session_s: session length in seconds.
        clip_samples: samples per clip (8000 = 1 s; smaller only in tests).
        dht_every_ms: DHT11 period.
        feed: optional callable to feed the watchdog once per clip.
        clock: wall clock (time.time), only for started_at and ended_at.

    Returns:
        The session/end message body (dict).
    """
    pub.publish(topic("session/start"), json.dumps(start_message(sid, clock(), session_s)))

    gc.collect()
    payload = sampler.new_clip_payload(CLIP_HEADER_SIZE, clip_samples)
    climate = dht_reader.PeriodicReader(sensor, dht_every_ms)
    sid_bytes = sid.encode()
    clip_topic = topic("clip")
    climate_topic = topic("climate")
    captured = sent = late_total = 0

    t0 = ticks_ms()
    while True:
        t_offset = ticks_diff(ticks_ms(), t0)
        if t_offset >= session_s * 1000:
            break
        capture_us, late = sampler.capture_into(adc, payload, CLIP_HEADER_SIZE, clip_samples)
        struct.pack_into(CLIP_HEADER, payload, 0, sid_bytes, captured, t_offset,
                         capture_us, clip_samples, late)
        late_total += late
        if pub.publish(clip_topic, payload):
            sent += 1
        captured += 1

        reading = climate.poll(ticks_diff(ticks_ms(), t0))
        if reading is not None:
            reading["node_id"] = config.NODE_ID
            reading["session_id"] = sid
            pub.publish(climate_topic, json.dumps(reading))
        if feed:
            feed()

    end = {
        "node_id": config.NODE_ID,
        "session_id": sid,
        "ended_at": iso_utc(clock()),
        "duration_ms": ticks_diff(ticks_ms(), t0),
        "clips_captured": captured,
        "clips_sent": sent,
        "clips_failed": captured - sent,
        "late_samples": late_total,
        "dht_ok": climate.ok,
        "dht_failed": climate.failed,
    }
    pub.publish(topic("session/end"), json.dumps(end))
    del payload
    gc.collect()
    return end


def main():
    """BOOT once, then WAIT -> SESSION forever."""
    feed = None
    if getattr(config, "WATCHDOG", True):
        from machine import WDT

        feed = WDT(timeout=WATCHDOG_TIMEOUT_MS).feed

    log(STATE_BOOT, config.NODE_ID, "firmware", config.FIRMWARE_VERSION)
    adc = sampler.init_adc()
    sensor = dht_reader.init_sensor(getattr(config, "DHT_PIN", dht_reader.DHT_PIN))
    session_s = getattr(config, "SESSION_S", 600)
    pub = publisher.Publisher(config.NODE_ID, config.MQTT_HOST, getattr(config, "MQTT_PORT", 1883))

    def online():
        return connect_wifi(config.WIFI_SSID, config.WIFI_PASSWORD)

    while not (online() and sync_time()):
        log(STATE_BOOT, "no network or no clock, retrying in", BOOT_RETRY_S, "s")
        sleep_s(BOOT_RETRY_S, feed)

    while True:
        now = time.time()
        wait = seconds_until_next_window(now)
        start = now + wait
        log(STATE_WAIT, wait, "s until", iso_utc(start))
        sleep_s(max(0, wait - PREPARE_S), feed)

        if online():
            sync_time()  # keep the RTC from drifting over the campaign
            if not pub.connected:
                pub.connect()
        sleep_s(max(0, start - time.time()), feed)

        sid = session_id(start)
        log(STATE_SESSION, sid, "free RAM", gc.mem_free())
        end = run_session(sid, pub, adc, sensor, session_s, feed=feed)
        log(STATE_SESSION, sid, "done: %d/%d clips sent, %d late samples, DHT %d ok %d failed"
            % (end["clips_sent"], end["clips_captured"], end["late_samples"],
               end["dht_ok"], end["dht_failed"]))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        # Last resort: log and reboot instead of sitting at the REPL.
        import sys

        sys.print_exception(exc)
        time.sleep(10)
        import machine

        machine.reset()
