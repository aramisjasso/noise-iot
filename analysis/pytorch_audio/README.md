# Flujo PyTorch para audio

Este directorio contiene el preprocesamiento de fragmentos de audio y el código para entrenar una MLP básica con PyTorch. La red recibe la forma de onda directamente; por ahora no calcula Mel ni MFCC.

## Importante antes de entrenar

Las marcas RMS actuales indican que un fragmento supera un umbral de amplitud. Como se calculan a partir del propio nivel de audio, sirven para revisar que el código corre, pero no para demostrar que la red reconoce sonidos molestos o dañinos. Para una evaluación útil, el equipo debe revisar y etiquetar fragmentos con un criterio independiente y acordado.

La red recibe fragmentos mono de un segundo a 8 kHz (8 000 muestras). El preprocesamiento elimina el desplazamiento constante de cada fragmento y escala las muestras del ADC a números pequeños. No usa temperatura ni humedad como entradas; esas mediciones se mantienen separadas y asociadas con cada sesión.

## Preparar los datos

Instala dependencias desde la carpeta principal:

```powershell
python -m pip install -r src/pytorch_audio/requirements.txt
```

Primero genera la revisión de calidad existente y después prepara las formas de onda:

```powershell
python src/audio/process_noise_data.py
python src/pytorch_audio/preprocess.py
```

Los resultados quedan en `data/noise-data/pytorch_dataset/`:

- `clips.npz`: formas de onda válidas, una por fragmento.
- `metadata.csv`: equipo, sesión, tiempo y resultado de calidad.
- `labels_template.csv`: plantilla con un renglón por `clip_id`.
- `preprocessing_summary.json`: conteos y elementos que se omitieron.

Los fragmentos con partes faltantes, señal plana o defectos detectados no se incluyen. Si el sistema de adquisición cambia su frecuencia o tamaño de bloque, revisa primero `preprocess.py`.

## Crear etiquetas revisadas

1. Copia `labels_template.csv` como `labels.csv` en `data/noise-data/pytorch_dataset/`.
2. Escucha los fragmentos y llena `label` con `0` o `1` usando el criterio definido por el equipo.
3. Mantén la columna vacía si el fragmento no se puede decidir. El entrenamiento omitirá esos renglones.

Por ejemplo, para una tarea de dos clases, el equipo puede acordar qué significa “fondo” y qué significa “sonido objetivo”. Si el objetivo es molestia, las etiquetas deben venir de valoraciones humanas con instrucciones iguales. El valor RMS automático no debe usarse como sustituto de esa revisión.

## Entrenar

```powershell
python src/pytorch_audio/train.py
```

El programa usa `labels.csv`, separa las sesiones completas entre aprendizaje, validación y prueba, y detiene el entrenamiento cuando deja de mejorar la validación. La MLP aplana las 8 000 muestras de cada fragmento, las pasa por una capa oculta de 32 neuronas y entrega dos clases. `train.py` no calcula resultados sobre el grupo de prueba; guarda el modelo y ese grupo en una carpeta nueva bajo `data/noise-data/pytorch_training/`.

Al terminar, la consola muestra la ruta de la ejecución. Usa esa ruta para ejecutar por separado la evaluación:

```powershell
python src/pytorch_audio/test.py --run-dir data/noise-data/pytorch_training/run_YYYYMMDD_HHMMSS
```

Reemplaza `run_YYYYMMDD_HHMMSS` por el nombre real que imprimió `train.py`. El programa de prueba escribe `test_metrics.json` y `test_predictions.csv` en esa misma carpeta. No vuelve a entrenar ni modifica los pesos.

Esta MLP es intencionalmente sencilla. Al recibir la forma de onda sin filtros aprendidos por convolución, puede necesitar más ejemplos etiquetados y ser sensible a diferencias de fase o alineación entre fragmentos. Úsala como punto de comparación, no como detector validado.

Cada grupo debe contener ejemplos revisados de las dos clases. Si faltan sesiones o una clase no aparece en alguno de los grupos, el programa se detendrá y pedirá más datos; no mezclará fragmentos de una misma sesión entre grupos.

## Prueba técnica con marcas RMS

Se puede comprobar el recorrido completo sin etiquetas humanas:

```powershell
python src/pytorch_audio/train.py --pseudo-label pseudo_label_6db
python src/pytorch_audio/test.py --run-dir data/noise-data/pytorch_training/run_YYYYMMDD_HHMMSS
```

Primero reemplaza la ruta de ejemplo por la que imprima el entrenamiento. También se puede usar `pseudo_label_2db`. Estas opciones se señalan en el reporte como pruebas técnicas solamente. Una métrica alta en este modo no significa que el modelo detecte molestia, daño ni una clase real de sonido.

## Archivos de este directorio

- `preprocess.py`: usa la lista de calidad actual y prepara los fragmentos a partir de `audio_crudo.u16`.
- `model.py`: define la MLP y las métricas compartidas por ambos pasos.
- `train.py`: entrena la red y guarda el conjunto de prueba sin evaluarlo.
- `test.py`: carga el modelo guardado y calcula por separado las métricas de prueba.
- `requirements.txt`: NumPy y PyTorch.
