# Prototipo TCP v1 (Jesús)

Primer prototipo funcional del nodo. Se conserva como referencia; el firmware
de la campaña está en `firmware/main.py`.

| Archivo | Qué hace |
|---|---|
| `main.py` | Firmware MicroPython: captura audio a 8 kHz (ADC de 12 bits, KY-037 en GPIO34) y lo envía crudo por TCP a una PC en bloques de 0.5 s; lee el DHT11 cada 6 s. |
| `receptor_pc.py` | Receptor en la PC: guarda por sesión `audio_crudo.u16`, `audio.wav`, `temp_hum.csv` y `sesion.json`. |
| `observar_audio.py` | Grafica un `audio_crudo.u16` (señal, detalle y espectro). |

Las credenciales de Wi-Fi y la IP de la PC se retiraron al subirlo al repo.

## Hallazgos que llevaron a la v2

Con las sesiones de `data/samples/esp32-01/`:

- Entre 8 % y 18 % de las muestras de cada sesión son relleno: mientras envía un
  bloque por Wi-Fi o lee el DHT11, el ESP32 deja de muestrear y repite el último
  valor para conservar la alineación de tiempo. Por eso los 60 bloques llegan
  "completos" aunque tengan relleno.
- No se guarda dónde quedó el relleno, así que no se puede excluir después.
- La v2 captura clips de 1 s sin transmitir y los envía entre clip y clip: los
  huecos quedan entre clips, con su hora, y no dentro de la señal.
