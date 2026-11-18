"""Prepara formas de onda de un segundo que pasaron la revisión de calidad."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data" / "noise-data"
SESSION_ROOT = DATA_ROOT / "estacion"
FEATURE_ROOT = DATA_ROOT / "processed_features"
OUTPUT_ROOT = DATA_ROOT / "pytorch_dataset"
SAMPLE_RATE_FALLBACK = 8_000
CLIP_SECONDS = 1


def read_csv(path: Path) -> list[dict[str, str]]:
    # utf-8-sig también acepta los CSV que incluyen marca BOM de Excel.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    # Escribe tablas fáciles de revisar o completar con una hoja de cálculo.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    # Estos dos archivos los genera el flujo de auditoría RMS que ya existe.
    quality_path = FEATURE_ROOT / "clip_quality.csv"
    features_path = FEATURE_ROOT / "rms_features_pseudolabeled.csv"
    if not quality_path.is_file() or not features_path.is_file():
        raise SystemExit(
            "Faltan los resultados de calidad. Ejecuta primero "
            "python src/audio/process_noise_data.py"
        )

    # clip_quality decide qué fragmentos cumplen la máscara automática.
    quality_rows = read_csv(quality_path)
    feature_rows = read_csv(features_path)
    pseudo_by_id = {row["clip_id"]: row for row in feature_rows}
    usable = [row for row in quality_rows if row["status"] == "usable_for_feature_exploration"]

    waveforms: list[np.ndarray] = []
    metadata: list[dict[str, object]] = []
    skipped: list[str] = []
    assumptions: list[str] = []
    rejected_sessions: set[tuple[str, str]] = set()
    session_cache: dict[tuple[str, str], tuple[np.ndarray, int, int, int]] = {}

    # Recorre solo fragmentos utilizables y carga cada archivo de sesión una vez.
    for row in usable:
        station, session_id = row["station"], row["session_id"]
        key = (station, session_id)
        if key in rejected_sessions:
            continue
        if key not in session_cache:
            session_dir = SESSION_ROOT / station / session_id
            raw_path = session_dir / "audio_crudo.u16"
            meta_path = session_dir / "sesion.json"
            if not raw_path.is_file():
                skipped.append(f"{station}/{session_id}: falta audio_crudo.u16")
                rejected_sessions.add(key)
                continue
            if meta_path.is_file():
                session_meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            else:
                # Si falta el JSON, se usan los valores del sistema actual:
                # 8 kHz, ADC de 12 bits y bloques de 4 000 muestras.
                session_meta = {}
                assumptions.append(f"{station}/{session_id}: se usaron valores del proyecto porque falta sesion.json")
            sample_rate = int(session_meta.get("fs", SAMPLE_RATE_FALLBACK))
            adc_bits = int(session_meta.get("bits_adc", 12))
            block_samples = int(session_meta.get("muestras_bloque", 4_000))
            if sample_rate <= 0 or not 1 <= adc_bits <= 16 or block_samples <= 0:
                skipped.append(f"{station}/{session_id}: metadatos de captura inválidos")
                rejected_sessions.add(key)
                continue
            raw = np.fromfile(raw_path, dtype="<u2")
            session_cache[key] = (raw, sample_rate, adc_bits, block_samples)

        if key not in session_cache:
            continue

        raw, sample_rate, adc_bits, block_samples = session_cache[key]
        if sample_rate != SAMPLE_RATE_FALLBACK:
            skipped.append(f"{station}/{session_id}: frecuencia {sample_rate} Hz; se esperaba 8000 Hz")
            rejected_sessions.add(key)
            continue
        if sample_rate * CLIP_SECONDS != 2 * block_samples:
            skipped.append(f"{station}/{session_id}: el tamaño de bloque no forma fragmentos de 1 s")
            rejected_sessions.add(key)
            continue

        first_block = int(row["block_start"])
        start = first_block * block_samples
        stop = start + sample_rate * CLIP_SECONDS
        if start < 0 or stop > len(raw):
            skipped.append(f"{row['clip_id']}: el fragmento queda fuera del archivo crudo")
            continue

        # Toma exactamente dos bloques contiguos para formar un segundo de audio.
        samples = raw[start:stop].astype(np.float32)
        # Quita el valor constante del ADC y escala la señal sin borrar su amplitud.
        samples -= samples.mean()
        samples /= float((1 << adc_bits) - 1)
        waveforms.append(samples)

        pseudo = pseudo_by_id.get(row["clip_id"], {})
        metadata.append({
            "clip_id": row["clip_id"],
            "station": station,
            "session_id": session_id,
            "session_group": f"{station}/{session_id}",
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "block_start": row["block_start"],
            "block_end": row["block_end"],
            "quality_status": row["status"],
            "quality_reason": row["reason"],
            "zero_samples": row["zero_samples"],
            "longest_zero_run": row["longest_zero_run"],
            "pseudo_label_6db": pseudo.get("pseudo_label_6db", ""),
            "pseudo_label_2db": pseudo.get("pseudo_label_2db", ""),
            "label_review": pseudo.get("label_review", "pending_human_listening"),
        })

    if not waveforms:
        detail = "\n".join(skipped[:10])
        raise SystemExit("No se pudo preparar ningún fragmento válido.\n" + detail)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    waveform_array = np.stack(waveforms).astype(np.float32)
    clip_ids = np.asarray([str(row["clip_id"]) for row in metadata], dtype="U96")
    # Guarda todas las señales en una matriz: una fila por fragmento.
    np.savez_compressed(
        OUTPUT_ROOT / "clips.npz",
        waveforms=waveform_array,
        clip_ids=clip_ids,
        sample_rate=np.asarray(SAMPLE_RATE_FALLBACK, dtype=np.int32),
    )

    fields = list(metadata[0].keys())
    write_csv(OUTPUT_ROOT / "metadata.csv", metadata, fields)
    template_path = OUTPUT_ROOT / "labels_template.csv"
    # No reemplaza una plantilla previa. Las etiquetas humanas se agregan aparte.
    if not template_path.exists():
        write_csv(template_path, [{"clip_id": clip_id, "label": ""} for clip_id in clip_ids], ["clip_id", "label"])

    # Registra cómo se prepararon los datos y qué sesiones o fragmentos se omitieron.
    summary = {
        "source_quality_file": str(quality_path.relative_to(PROJECT_ROOT)),
        "clips_saved": len(metadata),
        "waveform_shape": list(waveform_array.shape),
        "sample_rate_hz": SAMPLE_RATE_FALLBACK,
        "samples_per_clip": SAMPLE_RATE_FALLBACK * CLIP_SECONDS,
        "normalization": "per-clip mean removed; centered ADC samples divided by ADC full scale",
        "labels_used_for_training": False,
        "assumptions": assumptions,
        "skipped_items": skipped,
        "note": "The RMS pseudo-label columns are included only for explicit pipeline checks. They are not human-reviewed labels.",
    }
    (OUTPUT_ROOT / "preprocessing_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(OUTPUT_ROOT), **summary}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
