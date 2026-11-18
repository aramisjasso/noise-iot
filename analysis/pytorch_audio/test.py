"""Evalúa por separado una MLP guardada usando solo el conjunto de prueba."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import torch

from model import AudioMLP, score


def main() -> None:
    parser = argparse.ArgumentParser(description="Prueba una MLP entrenada en sesiones reservadas.")
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="carpeta creada por train.py que contiene model.pt y test_data.npz")
    parser.add_argument("--batch-size", type=int, default=128)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    model_path = run_dir / "model.pt"
    test_data_path = run_dir / "test_data.npz"
    if not model_path.is_file() or not test_data_path.is_file():
        raise SystemExit("La carpeta indicada debe contener model.pt y test_data.npz")
    if args.batch_size < 1:
        raise SystemExit("--batch-size debe ser mayor que cero")

    # Lee el conjunto que train.py dejó apartado sin usarlo para ajustar el modelo.
    with np.load(test_data_path, allow_pickle=False) as saved:
        waveforms = saved["waveforms"].astype(np.float32)
        labels = saved["labels"].astype(np.int64)
        clip_ids = saved["clip_ids"].astype(str)
        session_groups = saved["session_groups"].astype(str)
    if waveforms.ndim != 2 or waveforms.shape[1] != 8_000:
        raise ValueError(f"Se esperaba una matriz de fragmentos de 8 000 muestras; recibida {waveforms.shape}")
    if not (len(waveforms) == len(labels) == len(clip_ids) == len(session_groups)):
        raise ValueError("El archivo de prueba tiene columnas con distinta cantidad de ejemplos")

    # Carga los pesos aprendidos y usa la misma definición de MLP que train.py.
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    checkpoint = torch.load(model_path, map_location=device)
    model = AudioMLP().to(device)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    probabilities: list[np.ndarray] = []
    # Solo se hacen predicciones aquí; no se actualizan los pesos del modelo.
    with torch.no_grad():
        for start in range(0, len(waveforms), args.batch_size):
            batch = torch.from_numpy(waveforms[start:start + args.batch_size, None, :]).to(device)
            logits = model(batch)
            probabilities.append(torch.softmax(logits, dim=1)[:, 1].cpu().numpy())
    prob_class_1 = np.concatenate(probabilities)
    predictions = (prob_class_1 >= 0.5).astype(np.int64)
    test_metrics = score(labels, predictions)

    # Guarda cada resultado para poder revisarlo por fragmento y por sesión.
    prediction_rows = [
        {
            "clip_id": clip_ids[index],
            "session_group": session_groups[index],
            "label": int(labels[index]),
            "probability_class_1": float(prob_class_1[index]),
            "prediction": int(predictions[index]),
        }
        for index in range(len(labels))
    ]
    with (run_dir / "test_predictions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(prediction_rows[0]))
        writer.writeheader()
        writer.writerows(prediction_rows)

    report = {
        "model": "basic_mlp_pytorch",
        "device": str(device),
        "sample_rate_hz": checkpoint["sample_rate_hz"],
        "samples_per_clip": checkpoint["samples_per_clip"],
        "label_source": checkpoint["label_source"],
        "pseudo_labels_are_for_pipeline_check_only": "marcas automáticas" in checkpoint["label_source"],
        "test_sessions": sorted(np.unique(session_groups).tolist()),
        "test_metrics": test_metrics,
        "warning": "Estas métricas solo describen las etiquetas usadas. No prueban molestia ni daño auditivo sin etiquetas revisadas para ese propósito.",
    }
    (run_dir / "test_metrics.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
