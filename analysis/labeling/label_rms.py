"""Label one-second clips as noise (1) or not noise (0) with an RMS threshold.

The rule: label = 1 when the RMS of the mean-removed clip is at or above the
threshold, in dB relative to the ADC full scale (dBFS, see clips.py). The
threshold is absolute, not relative to the session, because the network only
sees one clip and could not learn a rule that depends on the rest of the session.

Two modes:

    # Compare thresholds: how many clips each one marks as noise, per node.
    python analysis/labeling/label_rms.py --estacion-dir C:\\noise-data\\estacion --sweep -30 -26 -22 -18 -14

    # Build the dataset for analysis/pytorch_audio/train.py with one threshold.
    python analysis/labeling/label_rms.py --estacion-dir C:\\noise-data\\estacion --threshold -20 --examples 10

The dataset folder (default data/noise-data/rms_<threshold>) has the format of
Miguel's preprocess.py, so his train.py and test.py run on it unchanged:

    clips.npz       waveforms (float32, n x 8000), clip_ids, sample_rate
    metadata.csv    clip_id, station, session_id, session_group, start_s, ..., rms_dbfs
    labels.csv      clip_id, label (0/1)
    labeling.json   threshold, rule, counts per class and node, clips skipped
    examples/       with --examples N: WAVs of the N clips closest to the threshold
                    on each side, to listen to what the threshold separates

Only clips that pass the quality check are used. --max-per-session (random
sample per session) keeps the dataset in memory: each clip is 32 KB.
"""

import argparse
import csv
import json
import os
import random
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clips  # noqa: E402

REPO_DIR = Path(__file__).resolve().parents[2]
RULE = "rms_dbfs >= threshold (clip without DC, <100 Hz and 60 Hz harmonics; dB re ADC full scale)"


def iter_sessions(estacion_dir, nodes=None):
    for node in sorted(os.listdir(estacion_dir)):
        node_dir = os.path.join(estacion_dir, node)
        if not os.path.isdir(node_dir) or (nodes and node not in nodes):
            continue
        for session in sorted(os.listdir(node_dir)):
            folder = os.path.join(node_dir, session)
            if os.path.isdir(folder) and clips.session_start(session) is not None:
                yield node, session, folder


def collect(estacion_dir, max_per_session, seed, nodes=None, keep_samples=True):
    """Usable clips of every session: list of dicts with node, session, second_idx,
    rms_dbfs and (if keep_samples) the preprocessed waveform."""
    rng = random.Random(seed)
    rows, excluded = [], {}
    for node, session, folder in iter_sessions(estacion_dir, nodes):
        usable = []
        for c in clips.session_clips(folder):
            if c["status"] != "ok":
                for r in c["reasons"]:
                    excluded[r] = excluded.get(r, 0) + 1
                continue
            usable.append(c)
        if max_per_session and len(usable) > max_per_session:
            usable = sorted(rng.sample(usable, max_per_session), key=lambda c: c["second_idx"])
        for c in usable:
            row = {"node": node, "session": session, "second_idx": c["second_idx"],
                   "fs": c["fs"], "rms_dbfs": clips.rms_dbfs(c["samples"], c["adc_bits"], c["fs"])}
            if keep_samples:
                row["waveform"] = clips.preprocess(c["samples"], c["adc_bits"], c["fs"])
                row["raw"] = c["samples"]
            rows.append(row)
    return rows, excluded


def sweep(rows, thresholds):
    nodes = sorted({r["node"] for r in rows})
    levels = np.array([r["rms_dbfs"] for r in rows])
    print("%d usable clips. RMS dBFS: min %.1f, p10 %.1f, median %.1f, p90 %.1f, max %.1f"
          % (len(rows), levels.min(), *np.percentile(levels, [10, 50, 90]), levels.max()))
    print()
    header = "umbral dBFS | ruido total | " + " | ".join(nodes)
    print(header)
    print("-" * len(header))
    for t in thresholds:
        cells = []
        for node in nodes:
            lv = np.array([r["rms_dbfs"] for r in rows if r["node"] == node])
            cells.append("%5.1f %%" % (100.0 * np.mean(lv >= t)))
        print("%11.1f | %9.1f %% | %s" % (t, 100.0 * np.mean(levels >= t), " | ".join(cells)))


def write_wav(path, raw, fs):
    pcm = np.clip((raw.astype(np.int32) - int(raw.mean())) * 16, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(pcm.tobytes())


def build(rows, excluded, threshold, out_dir, examples):
    if not rows:
        raise SystemExit("No usable clips found.")
    fs = rows[0]["fs"]
    if any(r["fs"] != 8000 or r["waveform"].shape != (8000,) for r in rows):
        raise SystemExit("train.py expects 1 s clips at 8 kHz (8000 samples).")
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = [clips.clip_id(r["node"], r["session"], r["second_idx"]) for r in rows]
    labels = [int(r["rms_dbfs"] >= threshold) for r in rows]
    np.savez_compressed(out_dir / "clips.npz",
                        waveforms=np.stack([r["waveform"] for r in rows]).astype(np.float32),
                        clip_ids=np.asarray(ids, dtype="U96"),
                        sample_rate=np.asarray(fs, dtype=np.int32))
    with (out_dir / "metadata.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["clip_id", "station", "session_id", "session_group", "start_s", "end_s",
                    "block_start", "block_end", "quality_status", "rms_dbfs", "rule_label"])
        for cid, r, y in zip(ids, rows, labels):
            k = r["second_idx"]
            w.writerow([cid, r["node"], r["session"], "%s/%s" % (r["node"], r["session"]),
                        k, k + 1, 2 * k, 2 * k + 1, "ok", "%.2f" % r["rms_dbfs"], y])
    with (out_dir / "labels.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["clip_id", "label"])
        w.writerows(zip(ids, labels))

    per_node = {}
    for r, y in zip(rows, labels):
        n = per_node.setdefault(r["node"], {"noise": 0, "not_noise": 0})
        n["noise" if y else "not_noise"] += 1
    summary = {
        "threshold_dbfs": threshold,
        "rule": RULE,
        "label_1": "ruido",
        "label_0": "no ruido",
        "clips": len(rows),
        "noise": sum(labels),
        "not_noise": len(rows) - sum(labels),
        "sessions": len({(r["node"], r["session"]) for r in rows}),
        "per_node": per_node,
        "excluded_clips_by_reason": excluded,
    }
    (out_dir / "labeling.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    if examples:
        order = sorted(range(len(rows)), key=lambda i: rows[i]["rms_dbfs"])
        above = [i for i in order if labels[i] == 1][:examples]
        below = [i for i in order if labels[i] == 0][-examples:]
        for name, chosen in (("ruido", above), ("no_ruido", below)):
            d = out_dir / "examples" / name
            d.mkdir(parents=True, exist_ok=True)
            for i in chosen:
                write_wav(d / ("%s_%.1fdB.wav" % (ids[i], rows[i]["rms_dbfs"])), rows[i]["raw"], fs)
    print(json.dumps({"output_dir": str(out_dir), **summary}, indent=2, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--estacion-dir", required=True, type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sweep", nargs="+", type=float, metavar="DBFS",
                      help="thresholds to compare; prints the share of noise per node")
    mode.add_argument("--threshold", type=float, metavar="DBFS",
                      help="build the training dataset with this threshold")
    p.add_argument("--out", type=Path, help="dataset folder (default data/noise-data/rms_<threshold>)")
    p.add_argument("--max-per-session", type=int, default=60,
                   help="random sample of usable clips per session (0 = all)")
    p.add_argument("--nodes", nargs="+", help="only these nodes (default all)")
    p.add_argument("--examples", type=int, default=0, help="WAVs to export on each side of the threshold")
    p.add_argument("--seed", type=int, default=17)
    args = p.parse_args(argv)

    rows, excluded = collect(args.estacion_dir, args.max_per_session, args.seed, args.nodes,
                             keep_samples=args.threshold is not None)
    if not rows:
        raise SystemExit("No usable clips in %s" % args.estacion_dir)
    if args.sweep:
        sweep(rows, args.sweep)
        return
    out = args.out or REPO_DIR / "data" / "noise-data" / ("rms_%g" % args.threshold)
    build(rows, excluded, args.threshold, out, args.examples)


if __name__ == "__main__":
    main()
