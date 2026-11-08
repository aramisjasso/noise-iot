"""Persistent queue of unsent batches.

When a batch cannot be uploaded it is written to flash as a JSON file named
after its seq, so sorting the names gives the oldest first. Every pending
batch is resent after the next successful upload; a batch is deleted only
after the server confirms it with a 2xx. Resending is safe because the
server deduplicates on (node_id, seq).

At most MAX_PENDING batches are kept (about 5 KB each); when the queue is
full the oldest one is dropped, so a long outage cannot fill the flash.
"""

import json
import os

OUTBOX_DIR = "/outbox"
MAX_PENDING = 150  # ~75 hours of windows, ~750 KB of flash


def _dir(directory):
    return directory if directory is not None else OUTBOX_DIR


def save(batch, directory=None):
    """Store a batch in flash.

    The file is written under a temporary name and renamed, so a reset in
    the middle never leaves a half-written batch in the queue.

    Args:
        batch: dict matching contracts/message.schema.json.
        directory: queue directory (OUTBOX_DIR on the node).

    Returns:
        Path of the file written.
    """
    d = _dir(directory)
    try:
        os.mkdir(d)
    except OSError:
        pass  # already exists
    path = "%s/%010d.json" % (d, batch["seq"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(batch, f)
    remove(path)  # rename does not overwrite on every filesystem
    os.rename(tmp, path)
    for old in pending(d)[:-MAX_PENDING]:
        print("outbox: queue full, dropping", old)
        remove(old)
    return path


def pending(directory=None):
    """List stored batches, oldest first.

    Returns:
        List of file paths inside the queue directory.
    """
    d = _dir(directory)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return ["%s/%s" % (d, n) for n in sorted(names) if n.endswith(".json")]


def load(path):
    """Read one stored batch.

    Args:
        path: file path returned by pending().

    Returns:
        Batch dict.
    """
    with open(path) as f:
        return json.load(f)


def remove(path):
    """Delete a stored batch; a missing file is not an error.

    Args:
        path: file path returned by pending().
    """
    try:
        os.remove(path)
    except OSError:
        pass


def flush(send, max_items=None, directory=None):
    """Resend pending batches, oldest first.

    Args:
        send: callable taking a batch dict and returning True on 2xx
            (normally a wrapper around uplink.post_batch).
        max_items: send at most this many, so a long queue does not eat
            into the next window; the rest goes in later cycles.
        directory: queue directory (OUTBOX_DIR on the node).

    Returns:
        Number of batches sent and removed. Stops at the first failure so
        the order is preserved. Unreadable files are dropped, otherwise one
        corrupt file would block the queue forever.
    """
    sent = 0
    for path in pending(directory):
        if max_items is not None and sent >= max_items:
            break
        try:
            batch = load(path)
        except (OSError, ValueError) as exc:
            print("outbox: dropping unreadable", path, repr(exc))
            remove(path)
            continue
        if not send(batch):
            break
        remove(path)
        sent += 1
    return sent
