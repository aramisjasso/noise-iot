# Datos

El audio no va en git: pesa demasiado y puede tener voces de personas.

- **Sesiones de los nodos:** las guarda el suscriptor en la laptop servidor, en `ESTACION_DIR`
  (por ejemplo `C:\noise-data\estacion\<nodo>\<sesión>\`). Ver `server/mqtt/README.md`.
- **Salidas de entrenamiento:** `analysis/labeling/label_rms.py`, `analysis/pytorch_audio/train.py`
  y `analysis/climate_variants/train_variants.py` escriben en `data/noise-data/`, que está en el
  `.gitignore`.
