# Firmware del nodo (ESP32 + MicroPython)

Sigue el diagrama de estados del proyecto:

```
BOOT ──▶ WAIT ──▶ SAMPLING_WINDOW ──▶ SEND_BATCH ──[confirmado]──▶ WAIT
 │        ▲        600 chunks de 1 s        │
 │        │        DHT11 cada 10 s          └──[sin red]──▶ QUEUED ──▶ WAIT
 └─ Wi-Fi + NTP (reintenta hasta lograrlo)
```

| Archivo | Qué hace |
|---|---|
| `main.py` | Máquina de estados: espera a las :00 y :30 (UTC), mide la ventana de 10 min y manda el lote. |
| `lib/sampler.py` | Muestrea el micrófono por ADC1 (GPIO34) a 8 kHz y saca un nivel RMS en dB por segundo. |
| `lib/dht_reader.py` | Lee el DHT11 (GPIO13) cada 10 s, con un reintento un segundo después si falla. |
| `lib/outbox.py` | Cola en la flash para los lotes que no se pudieron enviar. |
| `lib/uplink.py` | POST por HTTPS a la función de ingesta. |
| `config.example.py` | Plantilla de `config.py` (Wi-Fi, `NODE_ID`, URL y llave). |

Decisiones que no vienen en el diagrama:

- **`seq` es el número de ventana** desde el 1 de enero de 2026 (UTC): sube exactamente 1 cada
  30 minutos, no se guarda en la flash y no se reinicia aunque se borre la placa. Un hueco en
  `seq` es una ventana perdida.
- **El Wi-Fi se apaga mientras se espera y se mide**: el radio mete ruido al ADC1 y frena el
  muestreo. Se prende solo para mandar el lote.
- **Watchdog**: si algo se cuelga, la placa se reinicia sola (`WATCHDOG` en `config.py`).
- **Niveles en dB** relativos a 1 cuenta del ADC, más `MIC_CAL_DB`. Para reportar dB SPL hay
  que comparar cada nodo con un sonómetro y poner la diferencia en `MIC_CAL_DB`.
- Guardar audio crudo en SD para las ventanas de entrenamiento sigue sin decidirse y no está
  implementado.

## Cargarlo a la ESP32

Necesitas la ESP32 con MicroPython y [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
pip install mpremote
```

1. Crea tu configuración y llénala (Wi-Fi, `NODE_ID`, `INGEST_URL` e `INGEST_KEY`; la URL y la
   llave te las pasa quien administra el servidor):

   ```bash
   cp firmware/config.example.py firmware/config.py
   ```

   Para pruebas usa un `NODE_ID` como `prueba-01`, así sus datos se distinguen y se borran
   fácil. Mientras pruebas desde la consola, pon `WATCHDOG = False`.

2. Con la placa conectada por USB, copia los archivos (desde la raíz del repo):

   ```bash
   mpremote cp -r firmware/lib :
   ```

   ```bash
   mpremote cp firmware/boot.py firmware/main.py firmware/config.py :
   ```

3. Reinicia la placa y mira lo que imprime (para salir: `Ctrl-]`):

   ```bash
   mpremote reset
   ```

   ```bash
   mpremote repl
   ```

Lo que debes ver: `BOOT`, la conexión al Wi-Fi, `ntp: clock set` y `WAIT ... s until ...`. El
primer lote sale unos 10 minutos después de la siguiente :00 o :30 (hora UTC), con
`SEND_BATCH seq ... confirmed`. Antes de enviar imprime la RAM libre, que es el dato que
decide si el HTTPS cabe en memoria.

## Probar el micrófono sin esperar una ventana

En la consola de la placa (`mpremote repl`, y `Ctrl-C` si `main.py` ya está corriendo):

```python
import sampler
adc = sampler.init_adc()
buf = sampler.new_buffer()
for _ in range(10):
    print(sampler.capture_chunk(adc, buf), sampler.rms_db(buf))
```

Cada línea es un segundo:

- **Primer número**: microsegundos que tardó el chunk. Debe andar cerca de 1 000 000; si sale
  muy arriba, la placa no alcanza los 8 kHz.
- **Segundo número**: el nivel en dB. Debe subir claramente cuando hay ruido. Si sale `None`,
  la entrada está pegada a 0 o a 3.3 V: revisa la alimentación (3.3 V, no 5 V) y la
  polarización del micrófono.

## Pruebas en la PC

Con el hardware simulado, desde la raíz del repo:

```bash
python -m unittest discover -s firmware/tests
```

Requieren `jsonschema` (valida cada lote contra `contracts/message.schema.json`):

```bash
pip install jsonschema
```
