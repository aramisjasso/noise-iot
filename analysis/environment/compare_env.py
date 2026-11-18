"""Does temperature or humidity help predict how much noise a session has?

Unit: one session (5 min of audio, DHT11 read before and after). Target: the
share of usable seconds labeled as noise in that session (the predictions table
written by server/mqtt/classifier.py). Four variants of a small MLP:

    base      hour of day, weekend or not, node
    temp      base + temperature
    hum       base + humidity
    both      base + temperature + humidity

The base includes hour and node on purpose: temperature also tells the time of
day and the place, so without them a "temperature effect" could just be the
afternoon or one noisy node. A variant only shows something if it beats the base
on days it never saw.

Split by days, in order: the last --test-days for test, the --val-days before
them for early stopping, the rest for training. With fewer than 3 days it falls
back to a split of sessions in order and marks the run as a technical check
only. Each variant is trained with several seeds and the metrics are averaged.

Results go to PostgreSQL (schema.sql):

    environment_predictions  one row per run, variant and session: node, split,
                             real and predicted % of noise
    environment_metrics      one row per run, variant and node (and 'all'): MAE in
                             percentage points on the test sessions, MAE of the
                             base and gain over the base

Usage (from the repo root, with the .env of server/mqtt):
    python analysis/environment/compare_env.py --since "2026-11-19 09:00"
    python analysis/environment/compare_env.py --no-db     # only prints the report
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

REPO_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
LOCAL_TZ = timezone(timedelta(hours=-7))  # Tepic (America/Mazatlan), no DST
VARIANTS = {"base": (), "temp": ("temp_c",), "hum": ("rh",), "both": ("temp_c", "rh")}

SESSIONS_SQL = """
with noise as (
    select node_id, session_id,
           count(*) filter (where status = 'ok') as ok_clips,
           avg(coalesce(predicted_label, rule_label)) filter (where status = 'ok') as noise_frac
    from predictions
    where model_version = %(model_version)s
    group by node_id, session_id
), env as (
    select node_id, session_id, avg(temp_c) as temp_c, avg(rh) as rh
    from climate
    where temp_c is not null and rh is not null and temp_c <> 0 and rh <> 0
    group by node_id, session_id
)
select s.node_id, s.session_id, s.started_at, n.ok_clips, n.noise_frac, e.temp_c, e.rh
from sessions s
join noise n using (node_id, session_id)
join env e using (node_id, session_id)
where s.started_at >= %(since)s and n.ok_clips >= %(min_clips)s
order by s.started_at
"""


def load_env_file(path=os.path.join(REPO_DIR, "server", "mqtt", ".env")):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ------------------------------------------------------------------- data ---

def features(rows, nodes, columns, stats=None):
    """Feature matrix: hour as sin/cos, weekend flag, node one-hot, then the
    standardized climate columns. stats (mean, std per column) come from train."""
    x = []
    for r in rows:
        t = r["started_at"].astimezone(LOCAL_TZ)
        hour = t.hour + t.minute / 60.0
        # Weekend as a flag, not the weekday: with two weeks of data each weekday
        # appears once or twice, and the test days would be weekdays barely seen.
        f = [math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
             1.0 if t.weekday() >= 5 else 0.0]
        f += [1.0 if r["node_id"] == n else 0.0 for n in nodes]
        for c in columns:
            mean, std = stats[c]
            f.append((r[c] - mean) / std)
        x.append(f)
    return np.asarray(x, dtype=np.float32)


def split(rows, val_days, test_days):
    """Indexes of train, validation and test, and whether the split is by days."""
    days = sorted({r["started_at"].astimezone(LOCAL_TZ).date() for r in rows})
    if len(days) >= 3:
        test_days = min(test_days, len(days) - 2)
        val_days = min(val_days, len(days) - 1 - test_days)
        test = set(days[-test_days:])
        val = set(days[-test_days - val_days:-test_days])
        part = ["test" if d in test else "validation" if d in val else "train"
                for d in (r["started_at"].astimezone(LOCAL_TZ).date() for r in rows)]
        by_days = True
    else:
        n = len(rows)
        a, b = int(n * 0.7), int(n * 0.85)
        part = ["train"] * a + ["validation"] * (b - a) + ["test"] * (n - b)
        by_days = False
    idx = {p: [i for i, q in enumerate(part) if q == p] for p in ("train", "validation", "test")}
    return idx, part, by_days


# ------------------------------------------------------------------ model ---

def train_mlp(x_train, y_train, x_val, y_val, seed, epochs=400, patience=30):
    import torch
    from torch import nn

    torch.manual_seed(seed)
    model = nn.Sequential(nn.Linear(x_train.shape[1], 16), nn.ReLU(), nn.Linear(16, 1), nn.Sigmoid())
    opt = torch.optim.Adam(model.parameters(), lr=1e-2, weight_decay=1e-4)
    xt, yt = torch.from_numpy(x_train), torch.from_numpy(y_train)[:, None]
    xv, yv = torch.from_numpy(x_val), torch.from_numpy(y_val)[:, None]
    best, best_state, wait = float("inf"), None, 0
    for _ in range(epochs):
        model.train()
        opt.zero_grad()
        loss = nn.functional.mse_loss(model(xt), yt)
        loss.backward()
        opt.step()
        model.eval()
        with torch.no_grad():
            v = float(nn.functional.mse_loss(model(xv), yv))
        if v < best - 1e-6:
            best, wait = v, 0
            best_state = {k: t.clone() for k, t in model.state_dict().items()}
        else:
            wait += 1
            if wait >= patience:
                break
    model.load_state_dict(best_state)
    model.eval()
    return lambda x: model(torch.from_numpy(x)).detach().numpy()[:, 0]


def compare(rows, val_days=2, test_days=3, seeds=5):
    """Train the four variants. Returns (predictions, metrics, info)."""
    if len(rows) < 10:
        raise SystemExit("Only %d sessions with audio and climate; at least 10 are needed." % len(rows))
    nodes = sorted({r["node_id"] for r in rows})
    idx, part, by_days = split(rows, val_days, test_days)
    if not idx["test"] or not idx["validation"]:
        raise SystemExit("Not enough sessions for validation and test.")
    y = np.asarray([r["noise_frac"] for r in rows], dtype=np.float32)
    train_rows = [rows[i] for i in idx["train"]]
    stats = {c: (float(np.mean([r[c] for r in train_rows])),
                 float(np.std([r[c] for r in train_rows])) or 1.0) for c in ("temp_c", "rh")}

    predicted = {}
    for name, columns in VARIANTS.items():
        x = features(rows, nodes, columns, stats)
        runs = [train_mlp(x[idx["train"]], y[idx["train"]], x[idx["validation"]], y[idx["validation"]],
                          seed)(x) for seed in range(seeds)]
        predicted[name] = np.mean(runs, axis=0)

    test = np.asarray(idx["test"])
    predictions, metrics = [], []
    for name in VARIANTS:
        for i, r in enumerate(rows):
            predictions.append({"variant": name, "node_id": r["node_id"], "session_id": r["session_id"],
                                "started_at": r["started_at"], "split": part[i],
                                "noise_pct_real": 100.0 * float(y[i]),
                                "noise_pct_pred": 100.0 * float(predicted[name][i])})
        for node in ["all"] + nodes:
            sel = test if node == "all" else np.asarray([i for i in test if rows[i]["node_id"] == node])
            if sel.size == 0:
                continue
            mae = 100.0 * float(np.mean(np.abs(predicted[name][sel] - y[sel])))
            mae_base = 100.0 * float(np.mean(np.abs(predicted["base"][sel] - y[sel])))
            metrics.append({"variant": name, "node_id": node, "n_sessions": int(sel.size),
                            "mae_pp": mae, "mae_base_pp": mae_base, "gain_pp": mae_base - mae})
    days = sorted({str(r["started_at"].astimezone(LOCAL_TZ).date()) for r in rows})
    info = {"sessions": len(rows), "nodes": nodes, "days": days, "split_by_days": by_days,
            "sessions_per_split": {k: len(v) for k, v in idx.items()}, "seeds": seeds,
            "technical_check_only": not by_days,
            "temp_range_c": [min(r["temp_c"] for r in rows), max(r["temp_c"] for r in rows)],
            "rh_range_pct": [min(r["rh"] for r in rows), max(r["rh"] for r in rows)]}
    return predictions, metrics, info


def report(metrics, info):
    print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
    if info["technical_check_only"]:
        print("\nAVISO: menos de 3 días de datos; la separación no es por días y el resultado "
              "solo sirve para comprobar que el código corre.")
    print("\nError absoluto medio en las sesiones de prueba (puntos porcentuales de ruido).")
    print("Ganancia > 0: la variante predice mejor que la base (hora, día y nodo).\n")
    print("%-6s %-10s %5s %9s %9s %9s" % ("var", "nodo", "n", "MAE", "MAE base", "ganancia"))
    for m in metrics:
        print("%-6s %-10s %5d %9.2f %9.2f %+9.2f" % (m["variant"], m["node_id"], m["n_sessions"],
                                                   m["mae_pp"], m["mae_base_pp"], m["gain_pp"]))


# --------------------------------------------------------------- database ---

def save(dsn, run_id, predictions, metrics, info, args):
    import psycopg

    with psycopg.connect(dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("insert into environment_runs (run_id, model_version, since, info) "
                    "values (%s, %s, %s, %s)",
                    (run_id, args.model_version, args.since, json.dumps(info, default=str)))
        cur.executemany(
            "insert into environment_predictions (run_id, variant, node_id, session_id, started_at, "
            "split, noise_pct_real, noise_pct_pred) values (%(run_id)s, %(variant)s, %(node_id)s, "
            "%(session_id)s, %(started_at)s, %(split)s, %(noise_pct_real)s, %(noise_pct_pred)s)",
            [dict(p, run_id=run_id) for p in predictions])
        cur.executemany(
            "insert into environment_metrics (run_id, variant, node_id, n_sessions, mae_pp, "
            "mae_base_pp, gain_pp) values (%(run_id)s, %(variant)s, %(node_id)s, %(n_sessions)s, "
            "%(mae_pp)s, %(mae_base_pp)s, %(gain_pp)s)",
            [dict(m, run_id=run_id) for m in metrics])


def load_sessions(dsn, model_version, since, min_clips):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        if model_version is None:
            cur.execute("select model_version from predictions group by model_version "
                        "order by max(computed_at) desc limit 1")
            row = cur.fetchone()
            if row is None:
                raise SystemExit("The predictions table is empty: run server/mqtt/classifier.py first.")
            model_version = row["model_version"]
        cur.execute(SESSIONS_SQL, {"model_version": model_version, "since": since, "min_clips": min_clips})
        rows = cur.fetchall()
    for r in rows:
        r["noise_frac"], r["temp_c"], r["rh"] = float(r["noise_frac"]), float(r["temp_c"]), float(r["rh"])
    return rows, model_version


def main(argv=None):
    p = argparse.ArgumentParser(description="Compare noise prediction with and without temperature and humidity.")
    p.add_argument("--since", default="1970-01-01 00:00",
                   help='only sessions from this local time on, e.g. "2026-11-19 09:00" (nodes installed)')
    p.add_argument("--model-version", help="predictions.model_version to use (default: the latest)")
    p.add_argument("--min-clips", type=int, default=60, help="usable seconds a session needs")
    p.add_argument("--val-days", type=int, default=2)
    p.add_argument("--test-days", type=int, default=3)
    p.add_argument("--seeds", type=int, default=5)
    p.add_argument("--no-db", action="store_true", help="print the report without saving it")
    args = p.parse_args(argv)

    load_env_file()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
    args.since = since
    rows, args.model_version = load_sessions(os.environ["PG_DSN"], args.model_version, since, args.min_clips)
    predictions, metrics, info = compare(rows, args.val_days, args.test_days, args.seeds)
    info["model_version"] = args.model_version
    report(metrics, info)
    if not args.no_db:
        run_id = datetime.now(LOCAL_TZ).strftime("env_%Y%m%d_%H%M%S")
        save(os.environ["PG_DSN"], run_id, predictions, metrics, info, args)
        print("\nGuardado como run_id = %s" % run_id)


if __name__ == "__main__":
    main()
