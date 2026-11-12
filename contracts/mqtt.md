# Contrato MQTT (v2: clips de audio)

Cada nodo graba **sesiones**: a las :00 y :30 (UTC) captura clips de 1 s de audio
durante `SESSION_S` segundos (600 en la campaña). Entre clip y clip publica el
clip y, cada 6 s, una lectura del DHT11. **No se transmite mientras se captura**,
así que dentro de un clip no hay relleno; los huecos quedan entre clips y su
hora se conoce por `t_offset_ms`.

El servidor guarda el audio en disco, los metadatos en PostgreSQL y calcula los
rasgos de cada clip (MFCC, RMS, pico, cruces por cero).

## Broker

| Parámetro | Valor |
|---|---|
| Host | `158.97.122.248` (laptop servidor en CICESE) |
| Puerto | `1883` (sin TLS) |
| QoS | **1** en todos los mensajes |
| `retain` | `false` |
| `client_id` del nodo | su `NODE_ID` |

## Tópicos

```
noise/<site>/<node_id>/session/start   JSON     al iniciar la sesión
noise/<site>/<node_id>/clip            binario  un clip de 1 s
noise/<site>/<node_id>/climate         JSON     una lectura del DHT11
noise/<site>/<node_id>/session/end     JSON     al terminar la sesión
```

- `site`: `tepic` o `monterrey`.
- `node_id`: el `NODE_ID` del nodo, por ejemplo `tepic-01` o `esp32-01`. Solo letras
  minúsculas, dígitos y guiones.
- El suscriptor escucha `noise/+/+/#`.

`session_id` es la hora programada de inicio de la sesión en UTC con el formato
`YYYYMMDDTHHMMSSZ` (16 caracteres), por ejemplo `20261112T180000Z`. La llave de
cada sesión es `(node_id, session_id)`.

## `session/start` (JSON)

Esquema: [`mqtt/session_start.schema.json`](mqtt/session_start.schema.json).

```json
{
  "node_id": "esp32-01",
  "session_id": "20261112T180000Z",
  "started_at": "2026-11-12T18:00:01Z",
  "firmware_version": "0.2.0",
  "fs": 8000,
  "adc_bits": 12,
  "clip_samples": 8000,
  "session_s": 600,
  "mic_model": "KY-037",
  "gain_setting": null,
  "dht_model": "DHT11"
}
```

`started_at` es la hora real del reloj del nodo al empezar (puede ser unos
segundos después de `session_id`).

## `clip` (binario)

Un mensaje por clip: **encabezado de 30 bytes + muestras**, todo little-endian.

| Offset | Tipo | Campo | Significado |
|---|---|---|---|
| 0 | `char[16]` | `session_id` | ASCII, igual que en `session/start` |
| 16 | `uint16` | `clip_idx` | Número de clip dentro de la sesión, desde 0 |
| 18 | `uint32` | `t_offset_ms` | Inicio del clip, en ms desde el inicio de la sesión (reloj monotónico del nodo) |
| 22 | `uint32` | `capture_us` | Duración real de la captura en µs; frecuencia real = `n / capture_us × 10⁶` |
| 26 | `uint16` | `n` | Número de muestras (8000) |
| 28 | `uint16` | `late` | Muestras tomadas más de un periodo (125 µs) tarde; no se rellenan, solo se cuentan |
| 30 | `uint16[n]` | muestras | Valores crudos del ADC, 0 a 4095 |

En Python: `struct.unpack_from("<16sHIIHH", payload, 0)`. Tamaño total con
8000 muestras: 16 030 bytes.

## `climate` (JSON)

Esquema: [`mqtt/climate.schema.json`](mqtt/climate.schema.json).

```json
{
  "node_id": "esp32-01",
  "session_id": "20261112T180000Z",
  "t_offset_ms": 6210,
  "temp_c": 27.0,
  "rh": 82.0,
  "attempts": 1,
  "error": null
}
```

Si la lectura falla después de los reintentos, `temp_c` y `rh` van en `null` y
`error` dice por qué (por ejemplo `"OSError: [Errno 116] ETIMEDOUT"`). Nunca hay
celdas vacías.

## `session/end` (JSON)

Esquema: [`mqtt/session_end.schema.json`](mqtt/session_end.schema.json).

```json
{
  "node_id": "esp32-01",
  "session_id": "20261112T180000Z",
  "ended_at": "2026-11-12T18:10:01Z",
  "duration_ms": 600412,
  "clips_captured": 498,
  "clips_sent": 498,
  "clips_failed": 0,
  "late_samples": 12,
  "dht_ok": 83,
  "dht_failed": 1
}
```

Si el nodo pierde la red a media sesión, puede faltar este mensaje; el servidor
reconstruye la sesión con los clips que sí llegaron.

## Idempotencia

QoS 1 garantiza que cada mensaje llega **al menos una vez**, así que puede llegar
repetido. El servidor ignora duplicados por llave:

| Mensaje | Llave |
|---|---|
| `session/start`, `session/end` | `(node_id, session_id)` |
| `clip` | `(node_id, session_id, clip_idx)` |
| `climate` | `(node_id, session_id, t_offset_ms)` |
