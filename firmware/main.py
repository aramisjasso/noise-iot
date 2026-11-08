"""Node entry point: measurement state machine.

States (diagrama de estados, Notion):
    BOOT             Configure ADC1 and the DHT11, connect Wi-Fi and sync the
                     clock via NTP, retrying until both work; then resend any
                     batches left in the outbox.
    WAIT             Sleep until the next window start (:00 or :30 UTC).
    SAMPLING_WINDOW  Record a 10-minute window: 600 one-second chunks, one
                     dB level per chunk and a DHT11 reading every 10 chunks.
    SEND_BATCH       POST the batch over HTTPS; on 2xx resend what is queued
                     and go back to WAIT.
    QUEUED           Sending failed: store the batch in the outbox and go back
                     to WAIT; it is retried after the next successful send.

Windows last 10 minutes and start aligned to minute :00 and :30 of every
hour, so all nodes measure the same time span.

Design choices not in the diagram:
    - seq is the number of 30-minute windows since 2026-01-01 UTC. It needs
      no file in flash, grows by exactly 1 per cycle (gaps = lost windows)
      and survives a flash erase, so it never collides with old batches.
    - Wi-Fi is turned off during WAIT and SAMPLING_WINDOW: radio bursts add
      noise to ADC1 and interrupt the 8 kHz loop.
    - A hardware watchdog resets the node if anything hangs (WATCHDOG in
      config.py; turn it off while working at the REPL).
    - Saving raw audio for training windows (SD card) is still undecided
      and not implemented.
"""

import gc
import time

import config
import dht_reader
import outbox
import sampler
import uplink

STATE_BOOT = "BOOT"
STATE_WAIT = "WAIT"
STATE_SAMPLING_WINDOW = "SAMPLING_WINDOW"
STATE_SEND_BATCH = "SEND_BATCH"
STATE_QUEUED = "QUEUED"

WINDOW_LENGTH_S = 600
WINDOW_CHUNKS = 600  # one chunk per second
CYCLE_S = 1800  # a window starts every 30 minutes
WINDOW_START_MINUTES = (0, 30)

SEQ_EPOCH = (2026, 1, 1)  # seq 0 is the window at 2026-01-01T00:00:00Z
WIFI_TIMEOUT_S = 20
BOOT_RETRY_S = 30
MAX_FLUSH_PER_CYCLE = 20  # each resend takes a few seconds
WATCHDOG_TIMEOUT_MS = 5 * 60 * 1000
WIFI_OFF_WHILE_SAMPLING = True


# ---------------------------------------------------------------- time ---

def _days_from_civil(y, m, d):
    """Days since 1970-01-01 of a Gregorian date (proleptic, UTC)."""
    if m <= 2:
        y -= 1
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _unix_seconds(t):
    """Unix time of t, whatever epoch time.time() uses on this port.

    MicroPython ports differ (1970 or 2000); going through gmtime() fields
    makes the result the same everywhere.
    """
    y, mo, d, h, mi, s = time.gmtime(t)[:6]
    return _days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s


def iso_utc(t):
    """Format t (time.time() value) as RFC 3339 UTC, e.g. 2026-11-05T18:00:00Z."""
    y, mo, d, h, mi, s = time.gmtime(t)[:6]
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (y, mo, d, h, mi, s)


def seconds_until_next_window(now_s):
    """Compute how long to wait until the next :00 or :30 boundary.

    Args:
        now_s: current UTC time in seconds since the epoch.

    Returns:
        Seconds (int) until the next window start; 0 exactly on a boundary.
    """
    remainder = int(now_s) % CYCLE_S
    return 0 if remainder == 0 else CYCLE_S - remainder


def window_seq(window_start_s):
    """Sequence number of the window that starts at window_start_s.

    Returns:
        Non-negative int: windows since SEQ_EPOCH, +1 every 30 minutes.
    """
    since = _unix_seconds(window_start_s) - _days_from_civil(*SEQ_EPOCH) * 86400
    return since // CYCLE_S


def log(*args):
    print(iso_utc(time.time()), *args)


def sleep_s(seconds, feed=None):
    """Sleep in short slices so the watchdog keeps being fed."""
    deadline = time.ticks_add(time.ticks_ms(), int(seconds * 1000))
    while True:
        left = time.ticks_diff(deadline, time.ticks_ms())
        if left <= 0:
            return
        if feed:
            feed()
        time.sleep_ms(min(left, 10000))


# ------------------------------------------------------------- network ---

def connect_wifi(ssid, password, timeout_s=WIFI_TIMEOUT_S):
    """Connect to Wi-Fi in station mode.

    Args:
        ssid: network name.
        password: network password.
        timeout_s: give up after this many seconds.

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
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if wlan.isconnected():
            log("wifi: connected", wlan.ifconfig()[0])
            return True
        time.sleep_ms(200)
    log("wifi: timeout")
    wlan.active(False)
    return False


def wifi_off():
    """Turn the radio off (quieter ADC, steadier sampling loop)."""
    import network

    network.WLAN(network.STA_IF).active(False)


def sync_time():
    """Set the RTC from NTP (UTC).

    Returns:
        True if the clock was synchronized, False otherwise.
    """
    import ntptime

    try:
        ntptime.settime()
    except Exception as exc:  # OSError, timeouts, bad packets
        log("ntp: failed:", repr(exc))
        return False
    log("ntp: clock set")
    return True


# ------------------------------------------------------------ sampling ---

def run_window(window_start_s, seq, adc, sensor, cal_db=0.0, feed=None):
    """Record one full sampling window and build the batch.

    Args:
        window_start_s: UTC start of the window (time.time() value).
        seq: batch sequence number, see window_seq().
        adc: ADC returned by sampler.init_adc().
        sensor: DHT11 returned by dht_reader.init_sensor().
        cal_db: microphone calibration offset (MIC_CAL_DB).
        feed: optional callable to feed the watchdog once per chunk.

    Returns:
        Batch dict matching contracts/message.schema.json.
    """
    buf = sampler.new_buffer()
    climate = dht_reader.ClimateRecorder(sensor)
    levels = []
    capture_us = 0
    slowest_us = 0
    for n in range(WINDOW_CHUNKS):
        try:
            took = sampler.capture_chunk(adc, buf)
            capture_us += took
            slowest_us = max(slowest_us, took)
            level = sampler.rms_db(buf, cal_db)
        except Exception as exc:  # a bad chunk must not lose the window
            log("chunk", n, "failed:", repr(exc))
            level = None
        levels.append(None if level is None else round(level, 1))
        climate.tick(n)
        if feed:
            feed()

    # The buffer is not needed while sending; free it before TLS needs RAM.
    del buf
    gc.collect()
    log(
        "window done: avg chunk %d ms, slowest %d ms, %d null levels"
        % (capture_us // WINDOW_CHUNKS // 1000, slowest_us // 1000, levels.count(None))
    )
    return {
        "node_id": config.NODE_ID,
        "seq": seq,
        "window_start": iso_utc(window_start_s),
        "firmware_version": config.FIRMWARE_VERSION,
        "sample_rate_hz": sampler.SAMPLE_RATE_HZ,
        "levels_db": levels,
        "climate": climate.finish(),
    }


# ------------------------------------------------------------ delivery ---

def deliver(batch, send):
    """SEND_BATCH and QUEUED: upload one batch or keep it for later.

    Args:
        batch: dict from run_window().
        send: callable returning True when the server confirmed (2xx).

    Returns:
        True if the batch was confirmed, False if it went to the outbox.
    """
    if send(batch):
        log(STATE_SEND_BATCH, "seq", batch["seq"], "confirmed")
        resent = outbox.flush(send, MAX_FLUSH_PER_CYCLE)
        if resent:
            log("outbox: resent", resent, "batches")
        return True
    path = outbox.save(batch)
    log(STATE_QUEUED, "seq", batch["seq"], "saved to", path)
    return False


def main():
    """Run the state machine forever: BOOT -> WAIT -> SAMPLING_WINDOW ->
    SEND_BATCH (on success) or QUEUED (on failure) -> WAIT ...
    """
    feed = None
    if getattr(config, "WATCHDOG", True):
        from machine import WDT

        feed = WDT(timeout=WATCHDOG_TIMEOUT_MS).feed

    log(STATE_BOOT, config.NODE_ID, "firmware", config.FIRMWARE_VERSION)
    adc = sampler.init_adc()
    sensor = dht_reader.init_sensor()
    cal_db = getattr(config, "MIC_CAL_DB", 0.0)

    def send(batch):
        if feed:
            feed()
        return uplink.post_batch(batch, config.INGEST_URL, config.INGEST_KEY)

    def online():
        return connect_wifi(config.WIFI_SSID, config.WIFI_PASSWORD)

    while not (online() and sync_time()):
        log(STATE_BOOT, "no network or no clock, retrying in", BOOT_RETRY_S, "s")
        wifi_off()
        sleep_s(BOOT_RETRY_S, feed)

    resent = outbox.flush(send, MAX_FLUSH_PER_CYCLE)
    if resent:
        log("outbox: resent", resent, "batches")

    while True:
        if WIFI_OFF_WHILE_SAMPLING:
            wifi_off()
        now = time.time()
        wait = seconds_until_next_window(now)
        start = now + wait
        seq = window_seq(start)
        log(STATE_WAIT, wait, "s until", iso_utc(start), "seq", seq)
        sleep_s(wait, feed)

        log(STATE_SAMPLING_WINDOW, "seq", seq)
        gc.collect()
        batch = run_window(start, seq, adc, sensor, cal_db, feed)

        if online():
            log("free RAM before sending:", gc.mem_free())
            deliver(batch, send)
            sync_time()  # keep the RTC from drifting over the campaign
        else:
            path = outbox.save(batch)
            log(STATE_QUEUED, "seq", seq, "no Wi-Fi, saved to", path)


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
