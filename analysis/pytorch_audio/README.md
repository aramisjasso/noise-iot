# Red neuronal (PyTorch)

Modelo, entrenamiento y prueba del clasificador de ruido. La explicación completa (qué hace,
arquitectura, entrenamiento y resultados) está en [`docs/red_neuronal.md`](../../docs/red_neuronal.md).

| Archivo | Qué hace |
|---|---|
| `model.py` | La red `AudioMLP`: 8 000 → 32 (ReLU, dropout 0.25) → 2, y las métricas. |
| `train.py` | Entrena con el conjunto que arma `analysis/labeling/label_rms.py`, separando por sesión (65 / 15 / 20 %) y con paro temprano. Guarda el modelo y el conjunto de prueba en `data/noise-data/pytorch_training/run_<fecha>/`. |
| `test.py` | Carga un modelo guardado y calcula las métricas de prueba (`test_metrics.json`, `test_predictions.csv`). |
| `requirements.txt` | NumPy y PyTorch. |

## Uso

Desde la raíz del repo:

```
python analysis/labeling/label_rms.py --estacion-dir C:\noise-data\estacion --threshold -22 --max-per-session 4
python analysis/pytorch_audio/train.py --data-dir data/noise-data/rms_-22 --epochs 500 --patience 40
python analysis/pytorch_audio/test.py --run-dir <output_dir que imprime train.py>
```

La entrada de la red es la envolvente de energía de 1 s (8 000 valores) que prepara
`analysis/labeling/clips.py`; el mismo preprocesamiento lo usa el clasificador en vivo
(`server/mqtt/classifier.py`).

Las etiquetas salen de una regla de umbral RMS, no de una revisión humana: la red aprende a
reproducir esa regla. Para clasificar tipos de sonido o molestia haría falta etiquetar fragmentos
a mano con un criterio acordado.
