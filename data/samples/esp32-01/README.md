# Sesiones de prueba de esp32-01 (prototipo TCP v1)

Cuatro sesiones de 30 s grabadas el 2026-11-09 (UTC) con
`firmware/experiments/tcp_v1/`. Cada carpeta tiene:

| Archivo | Contenido |
|---|---|
| `audio_crudo.u16` | 240 000 muestras `uint16` little-endian, 8 kHz, ADC de 12 bits (0-4095), sin encabezado |
| `sesion.json` | Metadatos de la sesión y conteo de muestras rellenadas |
| `temp_hum.csv` | Lecturas del DHT11 cada 6 s (celdas vacías = lectura fallida) |

El `.wav` no se sube: se regenera desde el `.u16`.

| Sesión | Relleno | Lecturas DHT11 válidas | Nota |
|---|---|---|---|
| 20261109T053954Z | 17.6 % | 0/5 | Tono dominante ~441 Hz |
| 20261109T054209Z | 17.8 % | 0/5 | Tono dominante ~884 Hz |
| 20261109T054447Z | 8.4 % | 5/5 | Tono dominante ~881 Hz |
| 20261109T055823Z | 10.7 % | 5/5 | 3 % de muestras saturadas (0 o 4095) |

El relleno son tramos de valores idénticos (hasta ~0.3 s) que no son audio
real; ver `firmware/experiments/tcp_v1/README.md`.
