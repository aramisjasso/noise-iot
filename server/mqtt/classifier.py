"""Classifier: labels each second of audio as it arrives and stores it in PostgreSQL.

Runs next to subscriber.py. Every CLASSIFIER_POLL_S seconds it looks at the
sessions in ESTACION_DIR touched in the last CLASSIFIER_LOOKBACK_H hours and, for
every one-second clip (blocks 2k and 2k+1) not stored yet:

1. Checks its quality (analysis/labeling/clips.py, Miguel's rules).
2. Computes the RMS in dBFS and the rule label with NOISE_THRESHOLD_DBFS.
3. If MODEL_RUN_DIR points to a run of analysis/pytorch_audio/train.py, runs the
   network (Miguel's AudioMLP) and stores its probability and label.

Results go to the predictions table (schema.sql); Node-RED reads it or the
predictions_timed and node_status views. A clip with a block that has not
arrived is left for later, and stored as excluded once the session is closed.

Configuration: the same .env as subscriber.py (PG_DSN, ESTACION_DIR) plus
MODEL_RUN_DIR, NOISE_THRESHOLD_DBFS, CLASSIFIER_POLL_S, CLASSIFIER_LOOKBACK_H.

Usage:
    python server/mqtt/classifier.py           # keeps running
    python server/mqtt/classifier.py --once    # one pass and exit (to test)
    python server/mqtt/classifier.py --once --all   # also old sessions (backfill)
"""

import argparse
import logging
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO_DIR, "analysis", "labeling"))

import clips  # noqa: E402

log = logging.getLogger("classifier")
RULE_ONLY = "rule-only"


def load_env(path=os.path.join(HERE, ".env")):
    """Load KEY=VALUE lines from .env without overriding real variables."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


class Network:
    """Miguel's AudioMLP loaded from a train.py run folder (model.pt)."""

    def __init__(self, run_dir):
        sys.path.insert(0, os.path.join(REPO_DIR, "analysis", "pytorch_audio"))
        import torch
        from model import AudioMLP

        self.torch = torch
        checkpoint = torch.load(os.path.join(run_dir, "model.pt"), map_location="cpu")
        if checkpoint.get("samples_per_clip", 8000) != 8000:
            raise ValueError("the model expects %s samples per clip" % checkpoint["samples_per_clip"])
        self.model = AudioMLP()
        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.model.eval()
        self.version = os.path.basename(os.path.normpath(run_dir))

    def predict(self, waveforms):
        """Probability of class 1 (ruido) for an n x 8000 float32 array."""
        with self.torch.no_grad():
            x = self.torch.from_numpy(np.ascontiguousarray(waveforms[:, None, :]))
            return self.torch.softmax(self.model(x), dim=1)[:, 1].numpy()


class Classifier:
    """Independent of the loop and the database driver, for the tests."""

    def __init__(self, store, estacion_dir, threshold_dbfs, network=None):
        self.store = store
        self.estacion_dir = estacion_dir
        self.threshold = threshold_dbfs
        self.network = network
        self.version = network.version if network else RULE_ONLY
        self.done = {}  # (node, session) -> second_idx already stored
        self.seen = {}  # (node, session) -> (bloques.csv mtime, closed) at the last pass

    def sessions(self, lookback_s=None):
        now = time.time()
        for node in sorted(os.listdir(self.estacion_dir)):
            node_dir = os.path.join(self.estacion_dir, node)
            if not os.path.isdir(node_dir):
                continue
            for session in sorted(os.listdir(node_dir)):
                folder = os.path.join(node_dir, session)
                if not os.path.isdir(folder) or clips.session_start(session) is None:
                    continue
                blocks_csv = os.path.join(folder, "bloques.csv")
                mtime = os.path.getmtime(blocks_csv) if os.path.exists(blocks_csv) else 0
                if lookback_s is None or now - mtime <= lookback_s:
                    closed = os.path.exists(os.path.join(folder, "sesion.json"))
                    yield node, session, folder, (mtime, closed)

    def session(self, node, session, folder):
        """Classify the new clips of one session. Returns how many were stored."""
        key = (node, session)
        if key not in self.done:
            self.done[key] = set(self.store.prediction_indexes(node, session, self.version))
        rows, ok_rows, waveforms = [], [], []
        for c in clips.session_clips(folder, skip=self.done[key]):
            row = {
                "node_id": node,
                "session_id": session,
                "second_idx": c["second_idx"],
                "model_version": self.version,
                "captured_at": clips.clip_time(session, c["second_idx"]),
                "status": c["status"],
                "reason": "|".join(c["reasons"]) or None,
                "rms_dbfs": None,
                "threshold_dbfs": self.threshold,
                "rule_label": None,
                "prob_noise": None,
                "predicted_label": None,
            }
            if c["samples"] is not None:
                row["rms_dbfs"] = clips.rms_dbfs(c["samples"], c["adc_bits"])
            if c["status"] == "ok":
                row["rule_label"] = int(row["rms_dbfs"] >= self.threshold)
                if self.network is not None:
                    ok_rows.append(row)
                    waveforms.append(clips.preprocess(c["samples"], c["adc_bits"]))
            rows.append(row)
        if waveforms:
            probs = self.network.predict(np.stack(waveforms))
            for row, p in zip(ok_rows, probs):
                row["prob_noise"] = float(p)
                row["predicted_label"] = int(p >= 0.5)
        self.store.predictions(rows)
        self.done[key].update(r["second_idx"] for r in rows)
        return len(rows)

    def run_once(self, lookback_s=None):
        total = 0
        for node, session, folder, signature in self.sessions(lookback_s):
            # Nothing new arrived since the last pass: do not read the audio again.
            if self.seen.get((node, session)) == signature:
                continue
            try:
                n = self.session(node, session, folder)
                self.seen[(node, session)] = signature
            except Exception:
                log.exception("failed %s/%s", node, session)
                continue
            if n:
                log.info("%s %s: %d clips", node, session, n)
            total += n
        return total


def main(argv=None):
    p = argparse.ArgumentParser(description="Classify estacion/ audio into the predictions table.")
    p.add_argument("--once", action="store_true", help="one pass and exit")
    p.add_argument("--all", action="store_true", help="every session, not only recent ones")
    args = p.parse_args(argv)

    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()]
        + ([logging.FileHandler(os.environ["CLASSIFIER_LOG_FILE"], encoding="utf-8")]
           if os.environ.get("CLASSIFIER_LOG_FILE") else []),
    )
    from store import PgStore

    estacion_dir = os.environ["ESTACION_DIR"]
    threshold = float(os.environ.get("NOISE_THRESHOLD_DBFS", "-20"))
    run_dir = os.environ.get("MODEL_RUN_DIR", "").strip()
    network = Network(run_dir) if run_dir else None
    poll = float(os.environ.get("CLASSIFIER_POLL_S", "15"))
    lookback = None if args.all else float(os.environ.get("CLASSIFIER_LOOKBACK_H", "24")) * 3600

    store = PgStore(os.environ["PG_DSN"])
    classifier = Classifier(store, estacion_dir, threshold, network)
    log.info("estacion %s, threshold %.1f dBFS, model %s", estacion_dir, threshold, classifier.version)
    while True:
        try:
            classifier.run_once(lookback)
        except Exception:  # database down, folder missing...: log and try again
            log.exception("pass failed")
            try:
                store.close()
            except Exception:
                pass
        if args.once:
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
