# Servidor MQTT: suscriptor, clasificador y base de datos local

Corre en la laptop servidor de CICESE, junto a Mosquitto y PostgreSQL.

```
Nodo ESP32 ──MQTT QoS 1──▶ Mosquitto ──▶ subscriber.py ──▶ PostgreSQL + ESTACION_DIR/<nodo>/<sesión>/
                                                                  │
                                              classifier.py ◀─────┘  ──▶ tabla predictions
```

| Archivo | Qué hace |
|---|---|
| `schema.sql` | Tablas `sessions`, `clips`, `climate`, `clip_features`, `labels`, `predictions` y sus vistas. Se puede correr las veces que sea. |
| `subscriber.py` | Escucha `estacion/#` y pasa cada mensaje a `estacion.py`. |
| `estacion.py` | Guarda cada sesión como archivos y la mete a PostgreSQL. Calcula los rasgos MFCC de cada bloque con `analysis/features/mfcc.py`. |
| `store.py` | Escribe en PostgreSQL; los duplicados de QoS 1 se ignoran por llave. |
| `classifier.py` | Clasifica cada segundo de audio que llega (regla RMS y, si hay modelo, la red neuronal) y lo guarda en `predictions`. |
| `.env.example` | Plantilla de la configuración (broker, base de datos, carpetas, clasificador). |

## Sesiones de `estacion/`

El firmware de Arduino (`firmware/ESP32/`) publica en su propio formato
(`estacion/<dispositivo>/...`). El suscriptor se adapta a él: guarda cada sesión en
`ESTACION_DIR` y además la mete a PostgreSQL.

| Mensaje | Tabla | Cómo |
|---|---|---|
| `inicio` | `sessions` | `clip_samples` = `muestras_bloque`, `session_s` = `duracion_s`; el mensaje completo en `meta->'inicio'` |
| `audio` | `clips` + `clip_features` | Una fila por bloque de 0.5 s: `clip_idx` = `seq`, `late_samples` = muestras rellenadas, `raw_path` = el `audio_crudo.u16` de la sesión y `sample_offset` = dónde empieza el bloque dentro de él. Los rasgos se calculan sobre ese bloque. |
| `dht` | `climate` | `t_offset_ms` = 0 para "antes" y el final del audio para "después" |
| `fin` | `sessions` | Duración, bloques enviados y fallidos; el mensaje completo en `meta->'fin'` y lo que recibió el servidor en `meta->'servidor'` |

La fila de la sesión se crea con el primer mensaje que llegue, con `started_at` tomado del id de
la sesión, así que si se pierde `inicio` los bloques se guardan igual. `ESTACION_SITE` en el
`.env` dice a qué sitio pertenecen estos nodos (por defecto `tepic`).

Archivos por sesión:

```
ESTACION_DIR/<dispositivo>/<sesion>/
    audio_crudo.u16   muestras del ADC tal como llegaron (uint16 LE, 8 kHz)
    audio.wav         el mismo audio sin componente DC (se escribe al llegar "fin")
    temp_hum.csv      lecturas del DHT11 ("antes" y "despues")
    bloques.csv       un renglón por bloque: seq, muestras rellenadas, hora de llegada
    sesion.json       "inicio" + resumen "fin" + bloques recibidos y faltantes
```

`sesion.json` dice cuántos bloques llegaron, cuáles faltan (`bloques_faltantes`) y si llegó el
mensaje `inicio` (`inicio_recibido`; PubSubClient lo descarta si pasa de 256 bytes y no se
llamó `mqtt.setBufferSize(1024)`). Un bloque que no llegó queda en ceros en su posición del
`.u16`. Si nunca llega `fin`, la sesión se cierra como incompleta al empezar la siguiente del
mismo dispositivo o tras 15 minutos sin mensajes.

## Clasificador (`classifier.py`)

Corre aparte del suscriptor. Cada 15 s revisa las sesiones de `ESTACION_DIR` que recibieron
bloques en las últimas 24 h y, por cada fragmento de 1 s nuevo (bloques 2k y 2k+1):

1. Revisa su calidad (`analysis/labeling/clips.py`).
2. Calcula `rms_dbfs` y la etiqueta de la regla con `NOISE_THRESHOLD_DBFS`.
3. Si `MODEL_RUN_DIR` apunta a una carpeta de `analysis/pytorch_audio/train.py`, corre la red
   y guarda su probabilidad y su etiqueta. Si está vacío, `model_version = rule-only`.

Un fragmento al que le falta un bloque espera; cuando la sesión cierra se guarda como
`excluded` con `reason = missing_block`. El suscriptor escribe cada bloque a disco al
recibirlo, así que el resultado aparece segundos después de que llega el audio.

| Tabla o vista | Para qué |
|---|---|
| `predictions` | Una fila por fragmento y modelo: estado de calidad, `rms_dbfs`, `rule_label`, `prob_noise`, `predicted_label`, `captured_at`. |
| `predictions_timed` | Lo mismo con el sitio, para graficar. |
| `node_status` | Última hora por nodo: hora del último fragmento, % excluido (audio en blanco, bloques perdidos), % de ruido y RMS promedio. Sirve para las alertas. |

```
python server/mqtt/classifier.py --once        # una pasada, para probar
python server/mqtt/classifier.py --once --all  # también las sesiones viejas
python server/mqtt/classifier.py               # se queda corriendo
```

Requiere `torch` solo si hay modelo. Cómo se etiqueta y se entrena: `analysis/labeling/README.md`.

## Instalación en la laptop (Windows, una sola vez)

1. **Python 3.10 o más reciente** desde python.org (marca "Add python.exe to PATH").
2. **Dependencias**, desde la raíz del repo:

   ```
   pip install -r server/mqtt/requirements.txt
   ```

3. **Tablas**, con el usuario de la aplicación:

   ```
   psql -U noise_app -d noise_iot -f server/mqtt/schema.sql
   ```

4. **Configuración**: copia `server/mqtt/.env.example` a `server/mqtt/.env` y pon
   la contraseña de `noise_app`. Crea la carpeta de `ESTACION_DIR` (por ejemplo
   `C:\noise-data\estacion`). El `.env` no se sube al repo.

5. **Mosquitto con persistencia**: en `C:\Program Files\mosquitto\mosquitto.conf`
   deben estar `persistence true` y `persistence_location`. Así, si el suscriptor
   se cae, Mosquitto guarda los mensajes y se los entrega al volver.

## Correrlo

A mano, para probar:

```
python server/mqtt/subscriber.py
```

Cada mensaje deja una línea en la consola (y en `LOG_FILE` si lo configuraste):

```
INFO estacion esp32-01 20261124T180000Z inicio
INFO estacion esp32-01 20261124T180000Z audio #0 rellenos=0
INFO estacion esp32-01 20261124T180000Z dht antes T=24.1 H=58.0
```

Un mensaje que no se puede procesar aparece como `WARNING estacion rejected ...` con la
razón, y el suscriptor sigue.

### Que arranque solo con la laptop

Para que no dependa de que alguien tenga sesión abierta, regístralo como tarea
programada que corre al encender, como `SYSTEM`. En PowerShell como
administrador (ajusta la ruta de `python.exe`, que ves con `where python`):

```
schtasks /Create /TN "noise-subscriber" /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "\"C:\Program Files\Python312\python.exe\" C:\learn\mctai\noise-iot\server\mqtt\subscriber.py"
```

Arrancarla sin reiniciar: `schtasks /Run /TN "noise-subscriber"`. Detenerla:
`schtasks /End /TN "noise-subscriber"`. Con `LOG_FILE` en el `.env` puedes ver
lo que hace aunque no tenga consola.

## Revisar los datos

```sql
-- ¿Cuántos clips llegaron por sesión, contra cuántos dice el nodo que mandó?
select * from session_summary order by started_at desc;

-- Clips con hora absoluta
select node_id, clip_idx, captured_at, late_samples, clipped_samples
from clips_timed order by captured_at desc limit 20;
```

## Pruebas

En la PC, sin broker ni base de datos (requieren `numpy`; las de la red, también `torch`):

```
python -m unittest discover -s server/mqtt/tests
```
