"""Batch upload to the ingest function.

Serializes a batch to JSON (contracts/message.schema.json) and sends it with
an HTTPS POST to INGEST_URL, authenticated with INGEST_KEY. Any 2xx response
means the server stored the batch; anything else, or a network error, means
the batch must go to the outbox.
"""

DEFAULT_TIMEOUT_S = 15


def post_batch(batch, url, key, timeout_s=DEFAULT_TIMEOUT_S):
    """POST one batch as JSON over HTTPS.

    Args:
        batch: dict matching contracts/message.schema.json.
        url: ingest endpoint (INGEST_URL).
        key: ingest key (INGEST_KEY), sent in the request headers.
        timeout_s: request timeout in seconds.

    Returns:
        True if the server answered 2xx (batch confirmed), False otherwise.
        Never raises on network errors.
    """
    raise NotImplementedError
