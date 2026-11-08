"""Tests for lib/uplink.py on the PC, with urequests replaced by a fake.

Run from the repo root:
    python -m unittest discover -s firmware/tests
"""

import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout

FIRMWARE_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(FIRMWARE_DIR, "lib"))

CONTRACT = os.path.join(FIRMWARE_DIR, "..", "contracts", "message.schema.json")
with open(CONTRACT, encoding="utf-8") as f:
    EXAMPLE = json.load(f)["examples"][0]


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


class FakeRequests(types.ModuleType):
    """Stands in for urequests: records the call, answers or raises."""

    def __init__(self):
        super().__init__("urequests")
        self.calls = []
        self.status_code = 200
        self.error = None
        self.last_response = None

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.error is not None:
            raise self.error
        self.last_response = FakeResponse(self.status_code)
        return self.last_response


fake = FakeRequests()
sys.modules["urequests"] = fake
import uplink  # noqa: E402

# Another test may have imported uplink first (with CPython's requests), so
# point the module at the fake explicitly.
uplink.requests = fake

URL = "https://example.supabase.co/functions/v1/ingest"


class PostBatchTest(unittest.TestCase):
    def setUp(self):
        fake.calls.clear()
        fake.status_code = 200
        fake.error = None
        fake.last_response = None

    def post(self):
        with redirect_stdout(io.StringIO()):
            return uplink.post_batch(EXAMPLE, URL, "secret", timeout_s=7)

    def test_sends_json_with_key(self):
        self.assertTrue(self.post())
        call = fake.calls[0]
        self.assertEqual(call["url"], URL)
        self.assertEqual(call["headers"]["x-ingest-key"], "secret")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["timeout"], 7)
        self.assertIsInstance(call["data"], bytes)
        self.assertEqual(json.loads(call["data"]), EXAMPLE)

    def test_any_2xx_confirms(self):
        for code in (200, 201, 204):
            fake.status_code = code
            self.assertTrue(self.post(), code)

    def test_other_codes_go_to_outbox(self):
        for code in (400, 401, 409, 422, 500, 503):
            fake.status_code = code
            self.assertFalse(self.post(), code)

    def test_response_is_always_closed(self):
        fake.status_code = 500
        self.post()
        self.assertTrue(fake.last_response.closed)

    def test_network_errors_do_not_raise(self):
        for err in (OSError(113, "ECONNABORTED"), MemoryError(), ValueError("tls")):
            fake.error = err
            self.assertFalse(self.post())


if __name__ == "__main__":
    unittest.main()
