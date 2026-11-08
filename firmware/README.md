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

   La red tiene que ser de **2.4 GHz**: la ESP32 no se conecta a redes de 5 GHz. Si tu módem
   tiene las dos con el mismo nombre, usa la de 2.4 GHz o el internet compartido del celular.

2. Cierra Thonny (o cualquier programa que tenga abierta la placa): si el puerto COM está
   ocupado, `mpremote` no se puede conectar. Con la placa conectada por USB, copia los
   archivos (desde la raíz del repo):

   ```bash
   mpremote cp -r firmware/lib :
   ```

   ```bash
   mpremote cp firmware/boot.py firmware/main.py firmware/config.py :
   ```

3. Abre la consola de la placa:

   ```bash
   mpremote repl
   ```

   y presiona `Ctrl-D`. La placa se reinicia ahí mismo y ves todo lo que imprime desde el
   arranque (si reinicias con `mpremote reset` y luego abres la consola, los primeros
   mensajes se pierden). Para salir de la consola: `Ctrl-]`.

Lo que debes ver: `BOOT`, la conexión al Wi-Fi, `ntp: clock set` y `WAIT ... s until ...`. El
primer lote sale unos 10 minutos después de la siguiente :00 o :30 (hora UTC), con
`SEND_BATCH seq ... confirmed`. Antes de enviar imprime la RAM libre, que es el dato que
decide si el HTTPS cabe en memoria.

### Si algo falla

| Lo que ves | Qué revisar |
|---|---|
| `mpremote` no encuentra la placa o dice que el puerto está ocupado | Cierra Thonny u otra consola serial; prueba otro cable USB (algunos solo cargan). |
| `wifi: timeout` una y otra vez | Nombre y contraseña en `config.py`, y que la red sea de 2.4 GHz. |
| `ntp: failed` y `BOOT no network or no clock` en ciclo | Hay Wi-Fi pero la red bloquea la hora por internet (NTP, puerto UDP 123); pasa en algunas redes institucionales. Prueba con otra red o con el celular. |
| `uplink: send failed` y luego `QUEUED` | Sin internet, o `INGEST_URL` mal escrita. El lote queda en la cola y se reenvía solo. |
| `uplink: server answered 401` | `INGEST_KEY` no coincide con la del servidor. |
| `uplink: server answered 422` | El lote no cumple el contrato; avisa a quien lleva el servidor. |
| `uplink: server answered 5xx` | Problema del servidor; el lote queda en la cola y se reenvía después. |
| `ImportError` al arrancar | Faltó copiar algún archivo de `lib/`, o falta `config.py` en la placa. |

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
