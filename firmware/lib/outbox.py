"""Persistent queue of unsent batches.

When a batch cannot be uploaded it is written to flash as a JSON file. At the
start of the next cycle every pending batch is resent; a batch is deleted
only after the server confirms it with a 2xx. Resending is safe because the
server deduplicates on (node_id, seq).
"""

OUTBOX_DIR = "/outbox"


def save(batch):
    """Store a batch in flash.

    Args:
        batch: dict matching contracts/message.schema.json.

    Returns:
        Path of the file written.
    """
    raise NotImplementedError


def pending():
    """List stored batches, oldest first.

    Returns:
        List of file paths inside OUTBOX_DIR.
    """
    raise NotImplementedError


def load(path):
    """Read one stored batch.

    Args:
        path: file path returned by pending().

    Returns:
        Batch dict.
    """
    raise NotImplementedError


def remove(path):
    """Delete a batch after it was confirmed by the server.

    Args:
        path: file path returned by pending().
    """
    raise NotImplementedError


def flush(send):
    """Resend every pending batch, oldest first.

    Args:
        send: callable taking a batch dict and returning True on 2xx
            (normally a wrapper around uplink.post_batch).

    Returns:
        Number of batches sent and removed. Stops at the first failure so
        the order is preserved.
    """
    raise NotImplementedError
