# Etiquetas por umbral RMS

`label_rms.py` etiqueta cada fragmento de 1 s como **ruido (1)** o **no ruido (0)** según su
nivel RMS, y deja el conjunto en el formato que leen `analysis/pytorch_audio/train.py` y `test.py`.

`clips.py` corta, revisa y escala el audio. Lo usan este script y el clasificador del server
(`server/mqtt/classifier.py`), para que el entrenamiento y la operación traten el audio igual.

## La regla

- Un fragmento son dos bloques seguidos de 0.5 s de una sesión de `estacion/`.
- Se descarta si le falta un bloque, si un bloque está en ceros o plano, o si tiene 100 o más
  ceros seguidos.
- Antes de medir se quita el ruido eléctrico: la tendencia lineal, todo lo que está debajo de
  100 Hz (DC, deriva del amplificador) y una banda de ±2 Hz en cada armónico de 60 Hz hasta
  1020 Hz.
- La red recibe la envolvente de energía del fragmento filtrado: la señal al cuadrado,
  promediada en ventanas de 50 ms (8 000 valores, el tamaño de entrada de `model.py`). Con la
  forma de onda cruda la red no
  aprendía la regla: en las sesiones del 13 de noviembre acertaba 66 %, menos que responder
  siempre "no ruido". Con la envolvente acierta 97.6 % (F1 de ruido 0.95) con el umbral −22.
- Su nivel es el RMS del fragmento filtrado, en dB respecto a la escala completa del ADC:
  `rms_dbfs = 20·log10(rms / 4095)`. No es un nivel calibrado en dB SPL.
- `label = 1` si `rms_dbfs ≥ umbral`.

El umbral es absoluto, no relativo a la mediana de la sesión, porque la red ve un solo
fragmento y no sabría cuál es el nivel habitual de su sesión.

## Uso

Desde la raíz del repo:

```
# 1. Comparar umbrales: qué porcentaje queda como ruido en cada nodo
python analysis/labeling/label_rms.py --estacion-dir C:\noise-data\estacion --sweep -30 -26 -22 -18 -14

# 2. Armar el conjunto con el umbral elegido y exportar WAV de ejemplo para escucharlos
python analysis/labeling/label_rms.py --estacion-dir C:\noise-data\estacion --threshold -22 --max-per-session 4 --examples 10

# 3. Entrenar y probar
python analysis/pytorch_audio/train.py --data-dir data/noise-data/rms_-22 --epochs 500 --patience 40
python analysis/pytorch_audio/test.py --run-dir <carpeta que imprime train.py>
```

`--max-per-session` (60 por omisión) toma una muestra al azar por sesión para que el conjunto
quepa en memoria: cada fragmento ocupa 32 KB. Con `0` se usan todos.

## Cómo escoger el umbral

La exactitud de la red no sirve para escogerlo: la etiqueta sale del mismo RMS, así que la red
solo aprende a reproducir la regla y daría cifras altas con casi cualquier umbral. Se escoge con:

1. El reparto de `--sweep`: que ninguna clase quede casi vacía.
2. Los WAV de `examples/ruido` y `examples/no_ruido`: son los fragmentos más cercanos al umbral
   de cada lado. Si al escucharlos la frontera no tiene sentido, se mueve el umbral.

Con las 23 sesiones de prueba del 13 de noviembre, el nivel de fondo cambia mucho entre nodos
(mediana cerca de −27 dBFS en esp32-01, −21 en esp32-02 y −41 en esp32-04). Con un mismo umbral,
un nodo con más ganancia sale como "ruido" más seguido aunque el lugar no sea más ruidoso.
