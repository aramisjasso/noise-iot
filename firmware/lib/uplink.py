"""Batch upload to the ingest function.

Serializes a batch to JSON (contracts/message.schema.json) and sends it with
an HTTPS POST to INGEST_URL, authenticated with INGEST_KEY in the
x-ingest-key header. Any 2xx response means the server stored the batch (or
already had it); anything else, or a network error, means the batch must go
to the outbox.
"""

import gc

try:
    import ujson as json
except ImportError:  # CPython, for the tests on the PC
    import json

try:
    import urequests as requests
except ImportError:
    import requests

DEFAULT_TIMEOUT_S = 15


def post_batch(batch, url, key, timeout_s=DEFAULT_TIMEOUT_S):
    """POST one batch as JSON over HTTPS.

    Args:
        batch: dict matching contracts/message.schema.json.
        url: ingest endpoint (INGEST_URL).
        key: ingest key (INGEST_KEY), sent in the x-ingest-key header.
        timeout_s: request timeout in seconds.

    Returns:
        True if the server answered 2xx (batch confirmed), False otherwise.
        Never raises on network errors.
    """
    resp = None
    try:
        body = json.dumps(batch).encode()
        # TLS needs a large contiguous block of RAM; free what we can first.
        gc.collect()
        resp = requests.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-ingest-key": key,
            },
            timeout=timeout_s,
        )
        return 200 <= resp.status_code < 300
    except Exception as exc:  # OSError, timeouts, TLS errors, MemoryError
        print("uplink: send failed:", repr(exc))
        return False
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
