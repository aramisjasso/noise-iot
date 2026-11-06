"""Node entry point: measurement state machine.

States:
    BOOT             Load config, connect Wi-Fi, sync the clock via NTP and
                     resend any batches left in the outbox.
    WAIT             Sleep until the next window start (:00 or :30 UTC).
    SAMPLING_WINDOW  Record a 10-minute window: 600 one-second chunks, one
                     dB level per chunk and a DHT11 reading every 10 chunks.
    SEND_BATCH       POST the batch over HTTPS; on 2xx go back to WAIT.
    QUEUED           Sending failed: store the batch in the outbox and go back
                     to WAIT; it is retried at the start of the next cycle.

Windows last 10 minutes and start aligned to minute :00 and :30 of every
hour, so all nodes measure the same time span.
"""

STATE_BOOT = "BOOT"
STATE_WAIT = "WAIT"
STATE_SAMPLING_WINDOW = "SAMPLING_WINDOW"
STATE_SEND_BATCH = "SEND_BATCH"
STATE_QUEUED = "QUEUED"

WINDOW_LENGTH_S = 600
WINDOW_START_MINUTES = (0, 30)


def connect_wifi(ssid, password, timeout_s=20):
    """Connect to Wi-Fi in station mode.

    Args:
        ssid: network name.
        password: network password.
        timeout_s: give up after this many seconds.

    Returns:
        True if connected, False on timeout.
    """
    raise NotImplementedError


def sync_time():
    """Set the RTC from NTP (UTC).

    Returns:
        True if the clock was synchronized, False otherwise.
    """
    raise NotImplementedError


def seconds_until_next_window(now_s):
    """Compute how long to wait until the next :00 or :30 boundary.

    Args:
        now_s: current UTC time in seconds since the epoch.

    Returns:
        Seconds (int) until the next window start.
    """
    raise NotImplementedError


def run_window(window_start_s, seq):
    """Record one full sampling window and build the batch.

    Args:
        window_start_s: UTC start of the window in seconds since the epoch.
        seq: batch sequence number for this node.

    Returns:
        Batch dict matching contracts/message.schema.json.
    """
    raise NotImplementedError


def next_seq():
    """Return the next batch sequence number, persisted in flash.

    Returns:
        Non-negative int, strictly increasing across reboots.
    """
    raise NotImplementedError


def main():
    """Run the state machine forever: BOOT -> WAIT -> SAMPLING_WINDOW ->
    SEND_BATCH (on success) or QUEUED (on failure) -> WAIT ...
    """
    raise NotImplementedError


if __name__ == "__main__":
    main()
