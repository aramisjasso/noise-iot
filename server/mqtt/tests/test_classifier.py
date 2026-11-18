"""Tests for analysis/labeling (clips.py, label_rms.py) and classifier.py.

Sessions are written with StationReceiver, as the subscriber does, so the
classifier reads exactly the files it will find on the server.
"""

import csv
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import numpy as np

import helpers  # noqa: F401 (path setup)

sys.path.insert(0, os.path.join(helpers.REPO_DIR, "analysis", "labeling"))

import clips  # noqa: E402
import label_rms  # noqa: E402
from classifier import Classifier  # noqa: E402
from estacion import StationReceiver  # noqa: E402

FS, BLOCK = 8000, 4000
QUIET_DB = -58.0  # gaussian noise, sd 5 ADC counts
LOUD_DB = -23.2   # sine, amplitude 400 ADC counts


def block(kind, seed):
    rng = np.random.default_rng(seed)
    t = np.arange(BLOCK) / FS
    if kind == "loud":
        x = 1760 + 400 * np.sin(2 * np.pi * (310 + 40 * (seed % 7)) * t) + rng.normal(0, 5, BLOCK)
    elif kind == "zero":
        x = np.zeros(BLOCK)
    else:
        x = 1760 + rng.normal(0, 5, BLOCK)
    return np.clip(np.round(x), 0, 4095).astype("<u2")


class FakeStore:
    def __init__(self):
        self.rows = {}

    def prediction_indexes(self, node_id, session_id, model_version):
        return {k[2] for k in self.rows if k[:2] == (node_id, session_id) and k[3] == model_version}

    def predictions(self, rows):
        for r in rows:
            self.rows.setdefault((r["node_id"], r["session_id"], r["second_idx"], r["model_version"]), r)


class Recorder:
    """Writes estacion/ sessions on disk through StationReceiver."""

    def __init__(self, base):
        self.rx = StationReceiver(base)

    def send(self, node, kind, payload):
        self.rx.handle("estacion/%s/%s" % (node, kind), payload)

    def blocks(self, node, sid, kinds, skip=()):
        self.send(node, "inicio", json.dumps({"sesion": sid, "fs": FS, "muestras_bloque": BLOCK,
                                              "bloques": len(kinds), "bits_adc": 12}).encode())
        for seq, kind in enumerate(kinds):
            if seq in skip:
                continue
            payload = struct.pack("<16sIH", sid.encode(), seq, 0) + block(kind, seq).tobytes()
            self.send(node, "audio", payload)

    def fin(self, node, sid):
        self.send(node, "fin", json.dumps({"sesion": sid, "bloques_enviados": 0}).encode())


class ClipsTest(unittest.TestCase):
    def test_rms_dbfs_levels(self):
        self.assertAlmostEqual(clips.rms_dbfs(np.concatenate([block("quiet", 1), block("quiet", 2)])),
                               QUIET_DB, delta=0.5)
        self.assertAlmostEqual(clips.rms_dbfs(np.concatenate([block("loud", 1), block("loud", 2)])),
                               LOUD_DB, delta=0.5)

    def test_quality_rules(self):
        q = block("quiet", 1)
        self.assertEqual(clips.check_clip([q, q]), [])
        self.assertIn("all_zero_block", clips.check_clip([q, block("zero", 0)]))
        holed = q.copy()
        holed[100:250] = 0
        self.assertEqual(clips.check_clip([holed, q]), ["zero_run_ge_100"])

    def test_electrical_noise_is_ignored(self):
        # 60 Hz hum, its 3rd harmonic and a slow drift add nothing to the level
        t = np.arange(2 * BLOCK) / FS
        q = np.concatenate([block("quiet", 1), block("quiet", 2)]).astype(float)
        hum = q + 300 * np.sin(2 * np.pi * 60 * t) + 200 * np.sin(2 * np.pi * 180 * t) + 300 * t
        hum = np.clip(np.round(hum), 0, 4095).astype("<u2")
        self.assertAlmostEqual(clips.rms_dbfs(hum), clips.rms_dbfs(q.astype("<u2")), delta=0.5)

    def test_preprocess_is_energy_envelope_of_filtered_clip(self):
        x = np.concatenate([block("loud", 3), block("loud", 3)])
        y = clips.preprocess(x)
        self.assertEqual((y.dtype, y.shape), (np.float32, (8000,)))
        self.assertTrue((y >= 0).all())
        # mean of the envelope is the squared RMS of the filtered clip, times the gain squared
        rms = 10 ** (clips.rms_dbfs(x) / 20)
        expected = (rms * clips.INPUT_GAIN) ** 2
        self.assertAlmostEqual(float(y.mean()), expected, delta=0.02 * expected)  # window edges


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = Recorder(self.base)
        self.sid = "20261118T180000Z"
        self.folder = os.path.join(self.base, "esp32-01", self.sid)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_session_waits_for_missing_block(self):
        # seconds: 0 quiet, 1 loud, 2 missing its second block (not closed yet)
        self.rec.blocks("esp32-01", self.sid, ["quiet", "quiet", "loud", "loud", "quiet", "quiet"],
                        skip={5})
        live = list(clips.session_clips(self.folder))
        self.assertEqual([c["second_idx"] for c in live], [0, 1])
        self.rec.fin("esp32-01", self.sid)
        closed = list(clips.session_clips(self.folder))
        self.assertEqual([(c["second_idx"], c["status"]) for c in closed],
                         [(0, "ok"), (1, "ok"), (2, "excluded")])
        self.assertEqual(closed[2]["reasons"], ["missing_block"])

    def test_classifier_rule_only_is_incremental(self):
        store = FakeStore()
        clf = Classifier(store, self.base, threshold_dbfs=-40.0)
        self.rec.blocks("esp32-01", self.sid, ["quiet", "quiet", "loud", "loud"])
        self.assertEqual(clf.run_once(), 2)
        self.assertEqual(clf.run_once(), 0)  # nothing new
        # two more blocks arrive, then the session ends with one block lost
        self.rec.send("esp32-01", "audio", struct.pack("<16sIH", self.sid.encode(), 4, 0)
                      + block("loud", 4).tobytes())
        self.assertEqual(clf.run_once(), 0)  # second 2 still waits for block 5
        self.rec.fin("esp32-01", self.sid)
        self.assertEqual(clf.run_once(), 1)
        rows = {k[2]: r for k, r in store.rows.items()}
        self.assertEqual([rows[i]["rule_label"] for i in (0, 1)], [0, 1])
        self.assertEqual(rows[2]["status"], "excluded")
        self.assertIsNone(rows[1]["predicted_label"])
        self.assertEqual(rows[1]["model_version"], "rule-only")
        self.assertEqual(rows[1]["captured_at"].isoformat(), "2026-11-18T18:00:01+00:00")

    def test_classifier_resumes_from_database(self):
        self.rec.blocks("esp32-01", self.sid, ["quiet"] * 4)
        self.rec.fin("esp32-01", self.sid)
        store = FakeStore()
        Classifier(store, self.base, -40.0).run_once()
        self.assertEqual(Classifier(store, self.base, -40.0).run_once(), 0)


class LabelRmsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "estacion")
        rec = Recorder(self.base)
        # 4 sessions x 6 s, each with quiet and loud seconds, plus one zero second
        for i in range(4):
            sid = "20261118T18%02d00Z" % i
            rec.blocks("esp32-0%d" % (1 + i % 2), sid,
                       ["quiet", "quiet", "loud", "loud", "quiet", "quiet",
                        "loud", "loud", "zero", "zero", "quiet", "quiet"])
            rec.fin("esp32-0%d" % (1 + i % 2), sid)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sweep_prints_share_per_node(self):
        out = io.StringIO()
        with redirect_stdout(out):
            label_rms.main(["--estacion-dir", self.base, "--sweep", "-50", "-10"])
        text = out.getvalue()
        self.assertIn("20 usable clips", text)
        self.assertIn("40.0 %", text)  # 2 loud of 5 usable seconds per session
        self.assertIn("0.0 %", text)

    def test_dataset_has_miguel_format(self):
        out_dir = os.path.join(self.tmp.name, "ds")
        with redirect_stdout(io.StringIO()):
            label_rms.main(["--estacion-dir", self.base, "--threshold", "-40", "--out", out_dir,
                            "--examples", "2"])
        with np.load(os.path.join(out_dir, "clips.npz")) as d:
            self.assertEqual(d["waveforms"].shape, (20, 8000))
            self.assertEqual(int(d["sample_rate"]), 8000)
            ids = list(d["clip_ids"].astype(str))
        with open(os.path.join(out_dir, "metadata.csv"), encoding="utf-8-sig") as f:
            meta = list(csv.DictReader(f))
        with open(os.path.join(out_dir, "labels.csv"), encoding="utf-8-sig") as f:
            labels = list(csv.DictReader(f))
        self.assertEqual([m["clip_id"] for m in meta], ids)
        self.assertEqual([r["clip_id"] for r in labels], ids)
        self.assertEqual(sum(int(r["label"]) for r in labels), 8)
        self.assertEqual(meta[0]["session_group"], "esp32-01/20261118T180000Z")
        summary = json.load(open(os.path.join(out_dir, "labeling.json"), encoding="utf-8"))
        self.assertEqual(summary["excluded_clips_by_reason"]["all_zero_block"], 4)
        self.assertEqual(len(os.listdir(os.path.join(out_dir, "examples", "ruido"))), 2)

    @unittest.skipUnless(__import__("importlib").util.find_spec("torch"), "torch not installed")
    def test_train_and_classify_with_network(self):
        # Miguel's train.py, unchanged, on the labeler output; then the classifier loads it.
        out_dir = os.path.join(self.tmp.name, "ds")
        with redirect_stdout(io.StringIO()):
            label_rms.main(["--estacion-dir", self.base, "--threshold", "-40", "--out", out_dir])
        train = os.path.join(helpers.REPO_DIR, "analysis", "pytorch_audio", "train.py")
        res = subprocess.run([sys.executable, train, "--data-dir", out_dir, "--epochs", "30"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        run_dir = json.loads(res.stdout[res.stdout.index("{"):])["output_dir"]
        try:
            from classifier import Network
            store = FakeStore()
            Classifier(store, self.base, -40.0, Network(run_dir)).run_once()
            ok = [r for r in store.rows.values() if r["status"] == "ok"]
            self.assertEqual(len(ok), 20)
            self.assertTrue(all(0.0 <= r["prob_noise"] <= 1.0 for r in ok))
        finally:
            import shutil
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
