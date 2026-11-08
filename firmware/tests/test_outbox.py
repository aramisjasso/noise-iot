"""Tests for lib/outbox.py on a temporary directory."""

import os
import tempfile
import unittest

import fakes  # noqa: F401  (sets sys.path)

import outbox


def batch(seq):
    return {"node_id": "test-01", "seq": seq, "levels_db": [50.0]}


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name + "/outbox"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load_round_trip(self):
        path = outbox.save(batch(7), self.dir)
        self.assertEqual(outbox.load(path), batch(7))
        self.assertEqual(os.listdir(self.dir), ["0000000007.json"])

    def test_pending_is_oldest_first(self):
        for seq in (15000, 9, 120):
            outbox.save(batch(seq), self.dir)
        seqs = [outbox.load(p)["seq"] for p in outbox.pending(self.dir)]
        self.assertEqual(seqs, [9, 120, 15000])

    def test_pending_without_directory_is_empty(self):
        self.assertEqual(outbox.pending(self.dir), [])

    def test_saving_twice_keeps_one_file(self):
        outbox.save(batch(1), self.dir)
        outbox.save(batch(1), self.dir)
        self.assertEqual(len(outbox.pending(self.dir)), 1)

    def test_flush_sends_in_order_and_removes(self):
        for seq in (3, 1, 2):
            outbox.save(batch(seq), self.dir)
        sent = []
        n = outbox.flush(lambda b: sent.append(b["seq"]) or True, directory=self.dir)
        self.assertEqual((n, sent), (3, [1, 2, 3]))
        self.assertEqual(outbox.pending(self.dir), [])

    def test_flush_stops_at_first_failure(self):
        for seq in (1, 2, 3):
            outbox.save(batch(seq), self.dir)
        n = outbox.flush(lambda b: b["seq"] == 1, directory=self.dir)
        self.assertEqual(n, 1)
        self.assertEqual([outbox.load(p)["seq"] for p in outbox.pending(self.dir)], [2, 3])

    def test_flush_respects_max_items(self):
        for seq in range(5):
            outbox.save(batch(seq), self.dir)
        self.assertEqual(outbox.flush(lambda b: True, max_items=2, directory=self.dir), 2)
        self.assertEqual(len(outbox.pending(self.dir)), 3)

    def test_corrupt_file_does_not_block_the_queue(self):
        outbox.save(batch(2), self.dir)
        with open(self.dir + "/0000000001.json", "w") as f:
            f.write("{truncated")
        sent = []
        n = outbox.flush(lambda b: sent.append(b["seq"]) or True, directory=self.dir)
        self.assertEqual((n, sent), (1, [2]))
        self.assertEqual(outbox.pending(self.dir), [])

    def test_full_queue_drops_the_oldest(self):
        original = outbox.MAX_PENDING
        outbox.MAX_PENDING = 3
        try:
            for seq in range(5):
                outbox.save(batch(seq), self.dir)
        finally:
            outbox.MAX_PENDING = original
        self.assertEqual([outbox.load(p)["seq"] for p in outbox.pending(self.dir)], [2, 3, 4])

    def test_no_temporary_files_left(self):
        outbox.save(batch(1), self.dir)
        self.assertFalse(any(n.endswith(".tmp") for n in os.listdir(self.dir)))


if __name__ == "__main__":
    unittest.main()
