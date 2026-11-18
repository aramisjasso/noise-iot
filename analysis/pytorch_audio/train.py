"""Entrena una MLP sencilla con etiquetas revisadas para fragmentos de audio."""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from model import AudioMLP


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = PROJECT_ROOT / "data" / "noise-data" / "pytorch_dataset"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "noise-data" / "pytorch_training"


def load_metadata(path: Path) -> list[dict[str, str]]:
    # Lee la información que el preprocesador guardó para cada fragmento.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def load_targets(metadata: list[dict[str, str]], labels_path: Path, pseudo_column: str | None) -> tuple[list[int], list[int], str]:
    # Usa etiquetas revisadas por el equipo, salvo que se solicite una prueba
    # técnica explícita con alguna de las marcas RMS automáticas.
    if pseudo_column:
        if pseudo_column not in {"pseudo_label_6db", "pseudo_label_2db"}:
            raise ValueError("--pseudo-label debe ser pseudo_label_6db o pseudo_label_2db")
        lookup = {row["clip_id"]: row.get(pseudo_column, "") for row in metadata}
        source = f"marcas automáticas {pseudo_column} (solo prueba técnica; no evalúa el sonido real)"
    else:
        if not labels_path.is_file():
            raise FileNotFoundError(
                f"No existe {labels_path}. Copia labels_template.csv a labels.csv y llena la columna label."
            )
        with labels_path.open(encoding="utf-8-sig", newline="") as stream:
            label_rows = list(csv.DictReader(stream))
        if not label_rows or not {"clip_id", "label"}.issubset(label_rows[0]):
            raise ValueError("El archivo de etiquetas debe tener columnas clip_id,label")
        lookup = {row["clip_id"]: row["label"].strip() for row in label_rows}
        source = str(labels_path)

    indices: list[int] = []
    targets: list[int] = []
    for index, row in enumerate(metadata):
        value = lookup.get(row["clip_id"], "")
        if value == "":
            continue
        if value not in {"0", "1"}:
            raise ValueError(f"Etiqueta inválida para {row['clip_id']}: {value!r}. Usa 0 o 1.")
        indices.append(index)
        targets.append(int(value))
    if not indices:
        raise ValueError("No hay etiquetas 0/1 disponibles para entrenar.")
    return indices, targets, source


def group_split(groups: np.ndarray, labels: np.ndarray, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # Separa sesiones completas para evitar que fragmentos de una misma
    # grabación aparezcan a la vez en entrenamiento y en evaluación.
    unique_groups = np.unique(groups)
    if len(unique_groups) < 3:
        raise ValueError("Se requieren al menos tres sesiones etiquetadas para separar aprendizaje, validación y prueba.")
    n_test = max(1, round(len(unique_groups) * 0.20))
    n_val = max(1, round(len(unique_groups) * 0.15))
    if n_test + n_val >= len(unique_groups):
        n_test, n_val = 1, 1

    rng = np.random.default_rng(seed)
    for _ in range(2_000):
        shuffled = rng.permutation(unique_groups)
        test_groups = set(shuffled[:n_test])
        val_groups = set(shuffled[n_test:n_test + n_val])
        train_groups = set(shuffled[n_test + n_val:])
        train_idx = np.flatnonzero(np.isin(groups, list(train_groups)))
        val_idx = np.flatnonzero(np.isin(groups, list(val_groups)))
        test_idx = np.flatnonzero(np.isin(groups, list(test_groups)))
        if all(len(np.unique(labels[idx])) == 2 for idx in (train_idx, val_idx, test_idx)):
            return train_idx, val_idx, test_idx
    raise ValueError(
        "No se encontró una separación por sesión con las dos clases en los tres grupos. "
        "Reúne más sesiones y ejemplos revisados de ambas clases."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrena una MLP PyTorch básica con fragmentos de audio de un segundo.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--labels", type=Path, default=None, help="CSV revisado con columnas clip_id,label")
    parser.add_argument("--pseudo-label", choices=["pseudo_label_6db", "pseudo_label_2db"],
                        help="Solo para comprobar el flujo. No mide detección real.")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=12)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    dataset_path = data_dir / "clips.npz"
    metadata_path = data_dir / "metadata.csv"
    labels_path = (args.labels or data_dir / "labels.csv").resolve()
    if not dataset_path.is_file() or not metadata_path.is_file():
        raise SystemExit(f"No encuentro {dataset_path}. Ejecuta primero src/pytorch_audio/preprocess.py")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # Carga las formas de onda y comprueba que correspondan con el CSV de datos.
    metadata = load_metadata(metadata_path)
    with np.load(dataset_path, allow_pickle=False) as saved:
        waveforms = saved["waveforms"].astype(np.float32)
        clip_ids = saved["clip_ids"].astype(str)
        sample_rate = int(saved["sample_rate"])
    if sample_rate != 8_000 or waveforms.ndim != 2 or waveforms.shape[1] != 8_000:
        raise ValueError(f"Se esperaba audio mono de 1 s a 8 kHz; forma recibida: {waveforms.shape}")
    if len(metadata) != len(waveforms) or len(clip_ids) != len(waveforms):
        raise ValueError("clips.npz y metadata.csv no tienen el mismo número de fragmentos")
    if not np.array_equal(clip_ids, np.asarray([row["clip_id"] for row in metadata], dtype=str)):
        raise ValueError("El orden de clip_ids en clips.npz no coincide con metadata.csv")
    if args.epochs < 1 or args.patience < 1:
        raise ValueError("--epochs y --patience deben ser números positivos")

    # Conserva únicamente los fragmentos que ya tienen una etiqueta 0 o 1.
    indices, target_values, label_source = load_targets(metadata, labels_path, args.pseudo_label)
    labels = np.asarray(target_values, dtype=np.int64)
    selected_audio = waveforms[indices]
    selected_metadata = [metadata[index] for index in indices]
    groups = np.asarray([row["session_group"] for row in selected_metadata])
    # La partición se hace por sesión, no mezclando clips al azar.
    train_idx, val_idx, test_idx = group_split(groups, labels, args.seed)

    # Usa GPU si PyTorch encuentra una; de lo contrario, continúa en CPU.
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = AudioMLP().to(device)
    counts = np.bincount(labels[train_idx], minlength=2)
    if np.any(counts == 0):
        raise ValueError("El grupo de aprendizaje necesita ejemplos de las dos clases.")
    weights = len(train_idx) / (2.0 * counts.astype(np.float32))
    # Los pesos compensan diferencias grandes en la cantidad de ejemplos por clase.
    criterion = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32, device=device))
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-4)

    def make_loader(ix: np.ndarray, shuffle: bool) -> DataLoader:
        # PyTorch espera la forma [ejemplos, canales, muestras] para la entrada;
        # la MLP la aplana antes de la primera capa lineal.
        x = torch.from_numpy(selected_audio[ix, None, :])
        y = torch.from_numpy(labels[ix])
        return DataLoader(TensorDataset(x, y), batch_size=64, shuffle=shuffle)

    train_loader = make_loader(train_idx, True)
    val_loader = make_loader(val_idx, False)
    best_loss = float("inf")
    best_state: dict[str, torch.Tensor] | None = None
    best_epoch = 0
    wait = 0
    history: list[dict[str, float | int]] = []

    # Entrena por lotes y conserva el modelo que mejor resultado logra en validación.
    for epoch in range(1, args.epochs + 1):
        model.train()
        running_loss = 0.0
        seen = 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(x), y)
            loss.backward()
            optimizer.step()
            running_loss += float(loss.detach()) * len(y)
            seen += len(y)

        model.eval()
        val_loss_sum = 0.0
        val_seen = 0
        with torch.no_grad():
            for x, y in val_loader:
                x, y = x.to(device), y.to(device)
                loss = criterion(model(x), y)
                val_loss_sum += float(loss) * len(y)
                val_seen += len(y)
        train_loss = running_loss / max(1, seen)
        val_loss = val_loss_sum / max(1, val_seen)
        history.append({"epoch": epoch, "train_loss": train_loss, "validation_loss": val_loss})
        if val_loss < best_loss:
            best_loss = val_loss
            best_epoch = epoch
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            wait = 0
        else:
            wait += 1
            if wait >= args.patience:
                break

    if best_state is None:
        raise RuntimeError("El entrenamiento no produjo un modelo válido")
    # Cada ejecución recibe una carpeta nueva para no sobrescribir resultados.
    run_name = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_dir = DEFAULT_OUTPUT / run_name
    out_dir.mkdir(parents=True, exist_ok=False)
    torch.save({
        "model_state_dict": best_state,
        "sample_rate_hz": sample_rate,
        "samples_per_clip": int(waveforms.shape[1]),
        "input": "normalized_raw_waveform",
        "classes": {"0": "clase negativa acordada con el equipo", "1": "clase positiva acordada con el equipo"},
        "label_source": label_source,
        "seed": args.seed,
    }, out_dir / "model.pt")

    # Guarda aparte los ejemplos reservados. train.py no calcula sus métricas;
    # eso queda para test.py, que se ejecuta después con el modelo ya guardado.
    test_ids = np.asarray([selected_metadata[index]["clip_id"] for index in test_idx], dtype="U96")
    test_groups = np.asarray([groups[index] for index in test_idx], dtype="U128")
    np.savez_compressed(
        out_dir / "test_data.npz",
        waveforms=selected_audio[test_idx].astype(np.float32),
        labels=labels[test_idx].astype(np.int64),
        clip_ids=test_ids,
        session_groups=test_groups,
    )

    # Deja un registro de qué ejemplos quedaron en cada parte de la separación.
    split_rows = []
    split_by_index = {int(index): name for name, indices_part in (
        ("train", train_idx), ("validation", val_idx), ("test", test_idx)
    ) for index in indices_part}
    for index, row in enumerate(selected_metadata):
        split_rows.append({
            "clip_id": row["clip_id"],
            "session_group": row["session_group"],
            "label": int(labels[index]),
            "split": split_by_index[index],
        })
    with (out_dir / "split_manifest.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["clip_id", "session_group", "label", "split"])
        writer.writeheader()
        writer.writerows(split_rows)

    results = {
        "model": "basic_mlp_pytorch",
        "device": str(device),
        "sample_rate_hz": sample_rate,
        "samples_per_clip": int(waveforms.shape[1]),
        "input": "normalized raw waveform; no Mel/MFCC features",
        "label_source": label_source,
        "pseudo_labels_are_for_pipeline_check_only": bool(args.pseudo_label),
        "warning": "Metrics do not validate annoyance or harmful exposure unless labels were independently reviewed for that purpose.",
        "seed": args.seed,
        "best_epoch": best_epoch,
        "best_validation_loss": best_loss,
        "session_counts": {
            "train": int(len(np.unique(groups[train_idx]))),
            "validation": int(len(np.unique(groups[val_idx]))),
            "test": int(len(np.unique(groups[test_idx]))),
        },
        "clip_counts": {"train": int(len(train_idx)), "validation": int(len(val_idx)), "test": int(len(test_idx))},
        "training_history": history,
        "note": "El conjunto de prueba se guardó por separado; ejecuta test.py para calcular métricas finales.",
    }
    (out_dir / "training_summary.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"output_dir": str(out_dir), **{k: v for k, v in results.items() if k != "training_history"}}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
