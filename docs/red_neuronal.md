# Red neuronal: clasificador de ruido

Documento de referencia para el equipo. Explica qué hace la red, cómo está construida, cómo se
entrenó, cómo funciona en el servidor y qué se concluyó al agregarle temperatura y humedad. Las
cifras son las de las corridas del 24 de noviembre de 2026.

## 1. Qué hace

Cada segundo de audio que llega de los nodos se clasifica como **ruido (1)** o **no ruido (0)**.
El resultado se guarda en PostgreSQL y lo muestra el dashboard de Node-RED.

"Ruido" tiene aquí una definición concreta y medible: **un segundo cuyo nivel RMS, después de
quitar el ruido eléctrico, es igual o mayor que −22 dBFS**. No distingue tipos de sonido (voces,
tráfico, música); solo si el segundo fue fuerte o no.

Las etiquetas con las que aprendió la red no las puso una persona escuchando: las calcula
automáticamente esa regla de umbral. Por eso la red **aprende a reproducir la regla** a partir del
audio. Es importante decirlo así en el reporte: el desempeño mide qué tan bien la red imita la regla,
no qué tan bien detecta molestia o daño auditivo.

## 2. Recorrido de los datos

```
ESP32 ──bloques de 0.5 s por MQTT──▶ Mosquitto ──▶ subscriber.py ──▶ audio_crudo.u16 por sesión
                                                                          │
                                         classifier.py (cada 15 s) ◀──────┘
                                                │  calidad → filtro eléctrico → envolvente → red
                                                ▼
                                    PostgreSQL: tabla predictions ──▶ Node-RED (dashboard)
```

1. El nodo graba a 8 kHz con el ADC de 12 bits y manda bloques de 0.5 s (4 000 muestras).
2. `server/mqtt/subscriber.py` guarda cada sesión en `C:\noise-data\estacion\<nodo>\<sesión>\`.
3. `server/mqtt/classifier.py` revisa las sesiones cada 15 s, arma fragmentos de 1 s con cada par de
   bloques (2k y 2k+1), los clasifica y escribe una fila por segundo en `predictions`.

## 3. Preparación de cada segundo

Todo esto vive en `analysis/labeling/clips.py`. Lo usan igual el entrenamiento y el clasificador
en vivo, para que la red vea en operación exactamente lo mismo que vio al entrenar.

### 3.1 Revisión de calidad

Se descarta el segundo (`status = excluded`) si:

- le falta uno de sus dos bloques (`missing_block`),
- un bloque está todo en ceros o plano (`all_zero_block`, `constant_block`),
- tiene 100 o más ceros seguidos (`zero_run_ge_100`).

Un segundo descartado no cuenta ni como ruido ni como silencio.

### 3.2 Quitar el ruido eléctrico

Antes de medir se eliminan, con la transformada de Fourier del segundo:

- la tendencia lineal (deriva lenta del amplificador),
- todo lo que está por debajo de 100 Hz (DC, deriva y los 60 Hz de la red eléctrica),
- una banda de ±2 Hz alrededor de cada armónico de 60 Hz hasta 1 020 Hz.

Se hizo porque, en las sesiones del 13 de noviembre, la mayor parte de la energía de los segundos
silenciosos era deriva por debajo de 10 Hz, y un nodo tenía picos claros en 60, 180 y 300 Hz. Sin
este filtro, el "ruido" medido sería en buena parte electrónica y no sonido.

### 3.3 La etiqueta

$$\text{rms\_dbfs} = 20 \log_{10}\left(\frac{\text{RMS del segundo filtrado}}{4095}\right), \qquad \text{etiqueta} = \begin{cases} 1 & \text{si } \text{rms\_dbfs} \ge -22 \\ 0 & \text{si no} \end{cases}$$

- dBFS significa decibeles respecto a la escala completa del ADC (4095). **No son dB de presión
  sonora**: el micrófono no está calibrado.
- El umbral es **absoluto** (igual para todas las sesiones) y no relativo a la mediana de cada
  sesión, porque la red ve un solo segundo y no podría saber cuál es el nivel habitual de su sesión.

### 3.4 Lo que entra a la red: envolvente de energía

La red no recibe la forma de onda, sino su **envolvente de energía**:

1. Se toma el segundo filtrado y se escala: x / 4095 × 16.
2. Se eleva al cuadrado cada muestra (energía instantánea).
3. Se promedia en ventanas móviles de 50 ms.

Resultado: 8 000 valores, todos positivos, que suben cuando hay sonido fuerte.

**Por qué no la forma de onda.** La primera versión recibía la forma de onda filtrada y en la prueba
acertaba 66 %, menos que contestar siempre "no ruido". La forma de onda sube y baja alrededor de
cero, y una capa lineal suma muestras positivas y negativas que se cancelan; además, el mismo sonido
desplazado unos milisegundos es una entrada distinta. La etiqueta depende del promedio de la señal
**al cuadrado**, así que con la envolvente la regla se vuelve algo que una capa lineal sí puede
aprender: con ese cambio, en las sesiones de prueba del 13 de noviembre la red pasó a 97.6 % de exactitud
(F1 de ruido 0.95).

## 4. Arquitectura

Es un perceptrón multicapa (MLP) sencillo (`analysis/pytorch_audio/model.py`):

```
entrada: 8 000 valores (envolvente de 1 s)
   │
   ▼  Flatten
Linear 8 000 → 32
   ▼  ReLU
Dropout (p = 0.25)
   ▼
Linear 32 → 2
   ▼  softmax
probabilidad de "no ruido" y de "ruido"
```

- **Parámetros:** 8 000 × 32 + 32 + 32 × 2 + 2 = **256 098**.
- **Capa oculta de 32 neuronas con ReLU:** aprende combinaciones de la energía a lo largo del segundo.
- **Dropout de 0.25:** durante el entrenamiento apaga al azar una cuarta parte de esas neuronas en
  cada paso, para que la red no memorice ejemplos.
- **Salida:** dos puntuaciones que softmax convierte en probabilidades. El clasificador guarda la
  probabilidad de ruido (`prob_noise`) y la etiqueta predicha (`predicted_label` = 1 si
  `prob_noise ≥ 0.5`).

## 5. Entrenamiento

### 5.1 Datos

`analysis/labeling/label_rms.py` arma el conjunto en el formato que espera `train.py`:

| Dato | Valor |
|---|---|
| Sesiones | 4 147 (del 13 al 24 de noviembre, todos los nodos) |
| Segundos por sesión | 4 elegidos al azar entre los que pasan la revisión de calidad |
| Fragmentos totales | 16 551 |
| Ruido / no ruido | 416 / 16 135 (2.5 % de ruido) |
| Segundos descartados por calidad | ~35 000 de ~630 000 (alrededor de 5 %), sobre todo por bloques perdidos |

Se toman 4 segundos por sesión para que el conjunto quepa en memoria (cada fragmento ocupa 32 KB).

### 5.2 Separación

`train.py` separa **por sesión**: los segundos de una misma grabación quedan todos en el mismo grupo,
para que la red no se evalúe con audio casi idéntico al que vio. 65 % de las sesiones para entrenar,
15 % para validar y 20 % para probar, al azar, exigiendo que los tres grupos tengan ejemplos de ambas
clases.

| Grupo | Sesiones | Fragmentos |
|---|---|---|
| Entrenamiento | 2 696 | 10 754 |
| Validación | 622 | 2 487 |
| Prueba | 829 | 3 310 |

### 5.3 Configuración

| Parámetro | Valor |
|---|---|
| Función de pérdida | entropía cruzada con pesos por clase: cada clase pesa `n / (2 · n_clase)`, para que los pocos ejemplos de ruido cuenten tanto como los muchos de no ruido |
| Optimizador | Adam, tasa de aprendizaje 1×10⁻³, weight decay 1×10⁻⁴ |
| Lotes | 64 fragmentos |
| Épocas | hasta 500, con paro temprano: se detiene si la pérdida de validación no mejora en 40 épocas y se queda con la mejor |
| Semilla | 17 |
| Mejor época | 245 (pérdida de validación 0.023) |

### 5.4 Resultados en la prueba

| Métrica | Valor |
|---|---|
| Exactitud | 0.989 |
| Precisión de ruido | 0.678 |
| Recall de ruido | 1.000 |
| **F1 de ruido** | **0.808** |
| Matriz de confusión | 3 192 VN · 38 FP · 0 FN · 80 VP |

Cómo leerlo:

- **La exactitud engaña con clases tan desbalanceadas.** Contestar siempre "no ruido" daría 97.6 %.
  La métrica que importa es el **F1 de ruido**.
- **No se le escapó ningún segundo con ruido** (recall 1.0); a cambio da algunas falsas alarmas
  (38 de 3 230 segundos sin ruido). Para un sistema de monitoreo es preferible así.

Modelo guardado: `data/noise-data/pytorch_training/run_20261124_141441/model.pt`.

## 6. En operación

- `server/mqtt/classifier.py` carga el modelo indicado en `MODEL_RUN_DIR` del `.env` y clasifica cada
  segundo nuevo unos segundos después de que llega.
- Tabla `predictions`: una fila por segundo y modelo, con `status`, `reason`, `rms_dbfs`,
  `rule_label` (la regla), `prob_noise` y `predicted_label` (la red). **Filtrar siempre por
  `model_version = 'run_20261124_141441'`**; las filas de `run_20261118_120409` son de un modelo viejo
  que no debe usarse.
- Vista `node_status`: por nodo y modelo, en la última hora, el % de ruido, el % de audio descartado y
  el último segundo clasificado. Sirve para las alertas.

### Volver a entrenar

```
python analysis/labeling/label_rms.py --estacion-dir C:\noise-data\estacion --threshold -22 --max-per-session 4
python analysis/pytorch_audio/train.py --data-dir data/noise-data/rms_-22 --epochs 500 --patience 40
python analysis/pytorch_audio/test.py --run-dir <output_dir que imprime train.py>
```

Después se pone el nuevo `output_dir` en `MODEL_RUN_DIR`, se reinicia `classifier.py` y se corre
`python server/mqtt/classifier.py --once --all` para reclasificar lo acumulado.

## 7. Experimento: sonido contra sonido con clima

Pregunta: ¿cambia el clasificador si además del sonido recibe temperatura, humedad o ambas?
Script: `analysis/climate_variants/train_variants.py`.

### 7.1 Diseño

| Variante | Entrada |
|---|---|
| Sonido | envolvente de 1 s (la red de la sección 4 tal cual) |
| Sonido + temperatura | lo mismo y la temperatura de ese segundo |
| Sonido + humedad | lo mismo y la humedad |
| Sonido + temperatura + humedad | lo mismo y las dos |

- **Misma etiqueta y misma preparación** que el clasificador en vivo.
- **Clima de cada segundo:** interpolado entre la lectura del DHT11 antes y después de la sesión.
- **Cómo entra el clima:** la red procesa el audio igual (8 000 → 32); la temperatura y la humedad,
  estandarizadas, se concatenan a las 32 salidas de la capa oculta y entran directo a la capa de
  salida (33 o 34 entradas, en lugar de 32).
- **Datos:** solo desde el viernes 20 de noviembre a las 15:00, con los nodos en su lugar final.
  14 327 segundos de 3 583 sesiones.
- **Separación por tiempo:** entrenamiento hasta el domingo 22, validación el lunes 23 de 00:00 a
  10:30 y prueba del lunes 23 a las 10:30 en adelante (9 017 / 1 646 / 3 664 segundos). Así la prueba
  usa días que la red nunca vio.
- **5 semillas por variante**, con promedio y desviación estándar.

### 7.2 Resultados (todos los nodos, prueba)

| Variante | Exactitud | F1 de ruido | Decisiones que cambian respecto a solo sonido |
|---|---|---|---|
| Sonido | 0.974 ± 0.005 | 0.784 ± 0.034 | — |
| Sonido + temperatura | 0.958 ± 0.023 | 0.576 ± 0.305 | 3.5 % |
| Sonido + humedad | 0.966 ± 0.018 | 0.618 ± 0.321 | 3.1 % |
| Sonido + temperatura + humedad | 0.957 ± 0.018 | 0.474 ± 0.348 | 4.0 % |

Resultados por nodo: tabla `classifier_variant_metrics`, `run_id = variants_20261124_143601`.

### 7.3 Interpretación

- **Agregar clima no mejoró en ningún nodo; empeoró en promedio** y volvió al clasificador inestable:
  la desviación del F1 entre semillas pasó de ±0.03 a ±0.3, es decir, con unas semillas funciona y
  con otras se cae.
- **Por qué:** la etiqueta sale del volumen del audio, así que la temperatura y la humedad no aportan
  información sobre ella. Pero sí se correlacionan con otras cosas: cada nodo tiene su propio clima
  (por ejemplo, esp32-03 vive entre 38 y 48 % de humedad y esp32-01 entre 56 y 75 %) y su propia
  proporción de ruido. En algunas semillas la red toma el clima como atajo ("este clima suele venir
  con ruido"); en los días de prueba el clima es distinto al del fin de semana y el atajo deja de
  funcionar.
- **Conclusión:** meter más entradas no hace mejor al clasificador. Si no tienen relación con lo que
  se predice, lo vuelven menos confiable.

### 7.4 Precauciones al reportarlo

- Hay pocos segundos con ruido en la prueba, sobre todo por nodo; los F1 por nodo son poco estables.
  La comparación sólida es la de todos los nodos.
- La prueba cae en lunes y martes, y el martes no hubo gente en CICESE; los datos con actividad
  normal son pocos.

## 8. Limitaciones

1. **Las etiquetas son automáticas.** La red aprende la regla del umbral; no hay etiquetas humanas ni
   de tipos de sonido.
2. **El umbral se eligió con datos del salón.** En las posiciones finales el edificio resultó mucho
   más silencioso: −22 dBFS marca solo el 2.5 % de los segundos como ruido. Con más tiempo convendría
   elegirlo de nuevo con los datos finales.
3. **No son decibeles reales.** El micrófono no está calibrado y cada nodo tiene distinta ganancia, así
   que el mismo sonido puede quedar de un lado u otro del umbral según el nodo.
4. **Pocos días de datos.** El periodo útil va del viernes 20 al martes 24, con un fin de semana de por
   medio.
5. **El modelo en vivo se entrenó también con sesiones del salón** (13 a 19 de noviembre). Para el
   clasificador no afecta, porque la regla es la misma en cualquier lugar; el experimento de la
   sección 7 sí usa solo datos de las posiciones finales.

## 9. Archivos

| Archivo | Qué hace |
|---|---|
| `analysis/labeling/clips.py` | Corta segundos, revisa calidad, quita ruido eléctrico, calcula `rms_dbfs` y la envolvente |
| `analysis/labeling/label_rms.py` | Etiqueta con el umbral y arma el conjunto de entrenamiento |
| `analysis/pytorch_audio/model.py` | La red (`AudioMLP`) |
| `analysis/pytorch_audio/train.py`, `test.py` | Entrenamiento y prueba |
| `server/mqtt/classifier.py` | Clasificador en vivo |
| `analysis/climate_variants/train_variants.py` | Experimento de las 4 variantes |
| `server/mqtt/schema.sql`, `analysis/climate_variants/schema.sql` | Tablas en PostgreSQL |
