"""Definición compartida de la MLP y de las métricas de clasificación."""
from __future__ import annotations

import numpy as np
import torch
from torch import nn


class AudioMLP(nn.Module):
    """MLP básica para un segundo de audio mono muestreado a 8 kHz."""

    def __init__(self) -> None:
        super().__init__()
        # Cada ejemplo tiene 8 000 muestras: 8 000 -> 32 neuronas -> 2 clases.
        self.layers = nn.Sequential(
            nn.Flatten(),
            nn.Linear(8_000, 32),
            nn.ReLU(),
            nn.Dropout(0.25),
            nn.Linear(32, 2),
        )

    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        # Entrega dos puntuaciones; softmax las convierte en probabilidades.
        return self.layers(waveform)


def score(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, object]:
    """Calcula exactitud, métricas de la clase 1 y matriz de confusión."""
    tn = int(np.sum((y_true == 0) & (y_pred == 0)))
    fp = int(np.sum((y_true == 0) & (y_pred == 1)))
    fn = int(np.sum((y_true == 1) & (y_pred == 0)))
    tp = int(np.sum((y_true == 1) & (y_pred == 1)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "n": int(len(y_true)),
        "accuracy": float(np.mean(y_true == y_pred)),
        "precision_class_1": precision,
        "recall_class_1": recall,
        "f1_class_1": f1,
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }
