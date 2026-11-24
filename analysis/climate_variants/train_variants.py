"""Same noise classifier, four inputs: sound, sound + temperature, sound + humidity,
sound + both. Shows how the classifier changes with what it is fed.

Each example is one second of audio of an estacion/ session (analysis/labeling/
clips.py): the same quality check, the same input (energy envelope, 8000 values)
and the same label (RMS >= threshold) as the live classifier. The climate of that
second is interpolated between the DHT11 reading before the session and the one
after it (temp_hum.csv).

Model: Miguel's AudioMLP (8000 -> 32 -> 2). The climate variants add the
standardized temperature and/or humidity to the 32 hidden values, just before the
output layer, so with sound only it is exactly his network.

Split by time (local time of Tepic): train before --val-from, validation from
--val-from to --test-from (early stopping), test from --test-from on. Every
variant uses the same clips and the same split, and is trained with --seeds seeds;
metrics are the mean and standard deviation over the seeds.

Results in data/noise-data/climate_variants/<run_id>/:
    metrics.json   info of the run and metrics per variant, total and per node
    metrics.csv    the same as a table
and, with --save-db, in the tables of analysis/climate_variants/schema.sql.

Usage (repo root, on the server):
    python analysis/climate_variants/train_variants.py --estacion-dir C:\\noise-data\\estacion --save-db
"""

import argparse
import csv
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO_DIR = HERE.parents[1]
sys.path.insert(0, str(REPO_DIR / "analysis" / "labeling"))
import clips  # noqa: E402

LOCAL_TZ = timezone(timedelta(hours=-7))  # Tepic (America/Mazatlan), no DST
VARIANTS = {
    "sound": (),
    "sound_temp": ("temp_c",),
    "sound_hum": ("rh",),
    "sound_temp_hum": ("temp_c", "rh"),
}
VARIANT_NAMES = {
    "sound": "Sonido",
    "sound_temp": "Sonido + temperatura",
    "sound_hum": "Sonido + humedad",
    "sound_temp_hum": "Sonido + temperatura + humedad",
}


# ------------------------------------------------------------------- data ---

def read_climate(folder):
    """(temp_before, rh_before, temp_after, rh_after) from temp_hum.csv, or None
    if either reading is missing, zero or not a number."""
    path = os.path.join(folder, "temp_hum.csv")
    if not os.path.exists(path):
        return None
    readings = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                t, h = float(row["temperatura_c"]), float(row["humedad_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            if t != 0 and h != 0 and t == t and h == h:
                readings[row.get("momento")] = (t, h)
    if "antes" not in readings or "despues" not in readings:
        return None
    return readings["antes"] + readings["despues"]


def collect(estacion_dir, threshold, since, per_session, seed, nodes=None):
    """Usable seconds of every session with both DHT readings, from `since` on.
    Returns a list of dicts: node, session, start (UTC), second, x (float16
    envelope), y (rule label), temp_c, rh."""
    rng = random.Random(seed)
    rows = []
    for node in sorted(os.listdir(estacion_dir)):
        node_dir = os.path.join(estacion_dir, node)
        if not os.path.isdir(node_dir) or (nodes and node not in nodes):
            continue
        for session in sorted(os.listdir(node_dir)):
            start = clips.session_start(session)
            folder = os.path.join(node_dir, session)
            if start is None or start < since or not os.path.isdir(folder):
                continue
            climate = read_climate(folder)
            if climate is None:
                continue
            every = list(clips.session_clips(folder, include_pending=True))  # reads the audio once
            usable = [c for c in every if c["status"] == "ok"]
            if not usable:
                continue
            if per_session and len(usable) > per_session:
                usable = rng.sample(usable, per_session)
            t0, h0, t1, h1 = climate
            span = max(1, len(every))  # seconds of the session, for the interpolation
            for c in usable:
                frac = c["second_idx"] / span
                rows.append({
                    "node": node, "session": session, "start": start, "second": c["second_idx"],
                    "x": clips.preprocess(c["samples"], c["adc_bits"], c["fs"]).astype(np.float16),
                    "y": int(clips.rms_dbfs(c["samples"], c["adc_bits"], c["fs"]) >= threshold),
                    "temp_c": t0 + (t1 - t0) * frac,
                    "rh": h0 + (h1 - h0) * frac,
                })
    return rows


def split(rows, val_from, test_from):
    part = []
    for r in rows:
        t = r["start"] + timedelta(seconds=r["second"])
        part.append("test" if t >= test_from else "validation" if t >= val_from else "train")
    return {p: np.asarray([i for i, q in enumerate(part) if q == p]) for p in ("train", "validation", "test")}


# ------------------------------------------------------------------ model ---

def build_model(n_extra):
    import torch
    from torch import nn

    class AudioClimateMLP(nn.Module):
        """Miguel's AudioMLP; the climate joins the 32 hidden values."""

        def __init__(self):
            super().__init__()
            self.audio = nn.Sequential(nn.Flatten(), nn.Linear(8000, 32), nn.ReLU(), nn.Dropout(0.25))
            self.out = nn.Linear(32 + n_extra, 2)

        def forward(self, x, extra):
            h = self.audio(x)
            return self.out(torch.cat([h, extra], dim=1) if n_extra else h)

    return AudioClimateMLP()


def train_one(data, idx, columns, seed, epochs, patience):
    """Train one variant with one seed. Returns test predictions (0/1)."""
    import torch
    from torch import nn

    torch.manual_seed(seed)
    np.random.seed(seed)
    x, y, extra = data["x"], data["y"], data["extra"][:, [data["columns"].index(c) for c in columns]]
    model = build_model(len(columns))
    tr = idx["train"]
    counts = np.bincount(y[tr], minlength=2).astype(np.float64)
    weights = torch.tensor(counts.sum() / np.maximum(counts, 1) / 2, dtype=torch.float32)
    criterion = nn.CrossEntropyLoss(weight=weights)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)

    def batches(sel, shuffle):
        order = np.random.permutation(sel) if shuffle else sel
        for k in range(0, len(order), 64):
            b = order[k:k + 64]
            yield (torch.from_numpy(x[b].astype(np.float32)), torch.from_numpy(extra[b]),
                   torch.from_numpy(y[b]))

    def evaluate(sel):
        model.eval()
        loss, preds = 0.0, []
        with torch.no_grad():
            for xb, eb, yb in batches(sel, False):
                logits = model(xb, eb)
                loss += float(criterion(logits, yb)) * len(yb)
                preds.append(logits.argmax(1).numpy())
        return loss / max(1, len(sel)), np.concatenate(preds) if preds else np.zeros(0, int)

    best, best_state, wait = float("inf"), None, 0
    for _ in range(epochs):
        model.train()
        for xb, eb, yb in batches(tr, True):
            optimizer.zero_grad()
            criterion(model(xb, eb), yb).backward()
            optimizer.step()
        val_loss, _ = evaluate(idx["validation"])
        if val_loss < best - 1e-5:
            best, wait = val_loss, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            wait += 1
            if wait >= patience:
                break
    model.load_state_dict(best_state)
    return evaluate(idx["test"])[1]


def score(y_true, y_pred):
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"accuracy": float(np.mean(y_true == y_pred)),
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def run(rows, val_from, test_from, seeds=5, epochs=60, patience=8):
    """Train the four variants. Returns (metrics rows, info)."""
    idx = split(rows, val_from, test_from)
    for p, sel in idx.items():
        if len(sel) == 0:
            raise SystemExit("No clips in %s: check --val-from and --test-from." % p)
    tr = idx["train"]
    columns = ["temp_c", "rh"]
    raw = np.asarray([[r[c] for c in columns] for r in rows], dtype=np.float64)
    mean, std = raw[tr].mean(axis=0), raw[tr].std(axis=0)
    data = {"x": np.stack([r["x"] for r in rows]), "y": np.asarray([r["y"] for r in rows], dtype=np.int64),
            "extra": ((raw - mean) / np.where(std > 0, std, 1)).astype(np.float32), "columns": columns}

    test = idx["test"]
    y_test = data["y"][test]
    nodes = [rows[i]["node"] for i in test]
    preds = {name: [train_one(data, idx, cols, seed, epochs, patience) for seed in range(seeds)]
             for name, cols in VARIANTS.items()}

    metrics = []
    for name in VARIANTS:
        for node in ["all"] + sorted(set(nodes)):
            sel = np.arange(len(test)) if node == "all" else np.asarray([k for k, n in enumerate(nodes) if n == node])
            scores = [score(y_test[sel], p[sel]) for p in preds[name]]
            changed = [100.0 * float(np.mean(p[sel] != s[sel])) for p, s in zip(preds[name], preds["sound"])]
            metrics.append({
                "variant": name, "variant_name": VARIANT_NAMES[name], "node_id": node,
                "n_test": int(len(sel)), "noise_pct_test": 100.0 * float(np.mean(y_test[sel])),
                "accuracy_mean": float(np.mean([s["accuracy"] for s in scores])),
                "accuracy_std": float(np.std([s["accuracy"] for s in scores])),
                "f1_mean": float(np.mean([s["f1"] for s in scores])),
                "f1_std": float(np.std([s["f1"] for s in scores])),
                "changed_vs_sound_pct": float(np.mean(changed)),
            })
    info = {
        "clips": len(rows),
        "sessions": len({(r["node"], r["session"]) for r in rows}),
        "clips_per_split": {k: int(len(v)) for k, v in idx.items()},
        "val_from": val_from.isoformat(), "test_from": test_from.isoformat(), "seeds": seeds,
        "temp_c_train_mean_std": [float(mean[0]), float(std[0])],
        "rh_train_mean_std": [float(mean[1]), float(std[1])],
    }
    return metrics, info


# ----------------------------------------------------------------- output ---

def report(metrics, info):
    print(json.dumps(info, indent=2, ensure_ascii=False))
    print("\nPrueba (promedio ± desviación de las semillas). 'cambia' = % de segundos en que la")
    print("variante decide distinto que la de solo sonido con la misma semilla.\n")
    print("%-31s %-9s %6s %16s %16s %8s" % ("variante", "nodo", "n", "exactitud", "F1 ruido", "cambia"))
    for m in metrics:
        print("%-31s %-9s %6d %8.3f ± %.3f %8.3f ± %.3f %7.1f%%" % (
            m["variant_name"], m["node_id"], m["n_test"], m["accuracy_mean"], m["accuracy_std"],
            m["f1_mean"], m["f1_std"], m["changed_vs_sound_pct"]))


def save_files(out_dir, metrics, info):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps({"info": info, "metrics": metrics}, indent=2,
                                                     ensure_ascii=False), encoding="utf-8")
    with (out_dir / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(metrics[0]))
        w.writeheader()
        w.writerows(metrics)


def save_db(dsn, run_id, metrics, info, threshold):
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("insert into classifier_variant_runs (run_id, threshold_dbfs, info) values (%s, %s, %s)",
                    (run_id, threshold, json.dumps(info)))
        cur.executemany(
            "insert into classifier_variant_metrics (run_id, variant, variant_name, node_id, n_test, "
            "noise_pct_test, accuracy_mean, accuracy_std, f1_mean, f1_std, changed_vs_sound_pct) values "
            "(%(run_id)s, %(variant)s, %(variant_name)s, %(node_id)s, %(n_test)s, %(noise_pct_test)s, "
            "%(accuracy_mean)s, %(accuracy_std)s, %(f1_mean)s, %(f1_std)s, %(changed_vs_sound_pct)s)",
            [dict(m, run_id=run_id) for m in metrics])


def load_env_file(path=REPO_DIR / "server" / "mqtt" / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def local(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)


def main(argv=None):
    p = argparse.ArgumentParser(description="Train the noise classifier with four inputs and compare.")
    p.add_argument("--estacion-dir", required=True)
    p.add_argument("--since", default="2026-11-20 15:00", help="first session used (local time)")
    p.add_argument("--val-from", default="2026-11-23 00:00", help="validation from here (local time)")
    p.add_argument("--test-from", default="2026-11-23 10:30", help="test from here (local time)")
    p.add_argument("--threshold", type=float, default=-22.0, help="noise label threshold in dBFS")
    p.add_argument("--per-session", type=int, default=4, help="random usable seconds per session")
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--epochs", type=int, default=60)
    p.add_argument("--nodes", nargs="+")
    p.add_argument("--save-db", action="store_true", help="also save in PostgreSQL (PG_DSN of server/mqtt/.env)")
    args = p.parse_args(argv)

    rows = collect(args.estacion_dir, args.threshold, local(args.since), args.per_session, 17, args.nodes)
    if not rows:
        raise SystemExit("No usable clips with DHT readings since %s." % args.since)
    metrics, info = run(rows, local(args.val_from), local(args.test_from), args.seeds, args.epochs)
    info.update(since=args.since, threshold_dbfs=args.threshold, per_session=args.per_session)
    run_id = datetime.now(LOCAL_TZ).strftime("variants_%Y%m%d_%H%M%S")
    report(metrics, info)
    out_dir = REPO_DIR / "data" / "noise-data" / "climate_variants" / run_id
    save_files(out_dir, metrics, info)
    print("\nResultados en %s" % out_dir)
    if args.save_db:
        load_env_file()
        save_db(os.environ["PG_DSN"], run_id, metrics, info, args.threshold)
        print("Guardado en PostgreSQL como run_id = %s" % run_id)


if __name__ == "__main__":
    main()
