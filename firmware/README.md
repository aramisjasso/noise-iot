# Firmware del nodo (ESP32 + MicroPython), v2: clips por MQTT

```
BOOT ──▶ WAIT ──▶ SESSION (SESSION_S segundos) ──▶ WAIT ...
 │                 ├─ captura un clip de 1 s (sin transmitir)
 │                 ├─ lo publica por MQTT (QoS 1)
 │                 └─ cada 6 s, una lectura del DHT11
 └─ Wi-Fi + NTP (reintenta hasta lograrlo)
```

El contrato de los mensajes está en [`contracts/mqtt.md`](../contracts/mqtt.md).

| Archivo | Qué hace |
|---|---|
| `main.py` | Máquina de estados: espera a las :00 y :30 (UTC) y graba una sesión de clips. |
| `lib/sampler.py` | Muestrea el micrófono por ADC1 (GPIO34) a 8 kHz directo al mensaje del clip. |
| `lib/dht_reader.py` | Lee el DHT11 cada 6 s, con un reintento si falla; si falla dos veces manda nulos y el error. |
| `lib/publisher.py` | Conexión MQTT: publica con QoS 1, nunca truena y reintenta la conexión. |
| `config.example.py` | Plantilla de `config.py` (Wi-Fi, `NODE_ID`, `SITE`, broker, duración de la sesión). |
| `lib/uplink.py`, `lib/outbox.py` | Del camino anterior por HTTPS (v1); ya no los usa `main.py`. |
| `experiments/tcp_v1/` | Prototipo de Jesús que manda audio crudo por TCP; referencia. |

Decisiones:

- **No se transmite mientras se captura.** En el prototipo TCP, entre 8 % y 18 % de cada
  sesión era relleno por esa razón. Aquí cada clip está completo y los huecos quedan entre
  clips, con su hora (`t_offset_ms`).
- **`session_id` es la hora programada de inicio** (`YYYYMMDDTHHMMSSZ`): todos los nodos
  comparten los mismos ids y las sesiones se pueden comparar entre sitios.
- **Si el broker no responde, la sesión se graba igual**: esos clips se pierden pero el
  horario no se mueve; `session/end` dice cuántos fallaron.
- **Muestras tarde**: si una interrupción retrasa el muestreo, la muestra se toma en cuanto
  se puede y se cuenta en `late`; nunca se rellena.
- **Watchdog**: si algo se cuelga, la placa se reinicia sola (`WATCHDOG` en `config.py`).

## Cargarlo a la ESP32

Necesitas la ESP32 con MicroPython y [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
pip install mpremote
```

1. Crea tu configuración y llénala (Wi-Fi, `NODE_ID`, `SITE` y `DHT_PIN`; el broker ya
   viene apuntando a la laptop de CICESE):

   ```bash
   cp firmware/config.example.py firmware/config.py
   ```

   Para pruebas usa un `NODE_ID` como `prueba-01` y `SESSION_S = 30`, así sus datos se
   distinguen y la sesión dura medio minuto. Mientras pruebas desde la consola, pon
   `WATCHDOG = False`.

   La red tiene que ser de **2.4 GHz**: la ESP32 no se conecta a redes de 5 GHz. Si tu módem
   tiene las dos con el mismo nombre, usa la de 2.4 GHz o el internet compartido del celular.

2. Revisa que la placa tenga la librería MQTT. En la consola de la placa,
   `import umqtt.simple` no debe marcar error (el firmware oficial del ESP32 la trae). Si
   marca `ImportError`, instálala con la placa conectada a internet:

   ```bash
   mpremote mip install umqtt.simple
   ```

3. Cierra Thonny (o cualquier programa que tenga abierta la placa): si el puerto COM está
   ocupado, `mpremote` no se puede conectar. Con la placa conectada por USB, copia los
   archivos (desde la raíz del repo):

   ```bash
   mpremote cp -r firmware/lib :
   ```

   ```bash
   mpremote cp firmware/boot.py firmware/main.py firmware/config.py :
   ```

4. Abre la consola de la placa:

   ```bash
   mpremote repl
   ```

   y presiona `Ctrl-D`. La placa se reinicia ahí mismo y ves todo lo que imprime desde el
   arranque (si reinicias con `mpremote reset` y luego abres la consola, los primeros
   mensajes se pierden). Para salir de la consola: `Ctrl-]`.

Lo que debes ver: `BOOT`, la conexión al Wi-Fi, `ntp: clock set` y `WAIT ... s until ...`. Unos
45 s antes de la siguiente :00 o :30 (hora UTC) se reconecta, y a la hora en punto empieza
`SESSION`; al terminar imprime cuántos clips mandó, cuántas muestras salieron tarde y cuántas
lecturas del DHT11 funcionaron. En la laptop servidor, el suscriptor muestra cada clip que
llega (ver `server/mqtt/README.md`).

### Si algo falla

| Lo que ves | Qué revisar |
|---|---|
| `mpremote` no encuentra la placa o dice que el puerto está ocupado | Cierra Thonny u otra consola serial; prueba otro cable USB (algunos solo cargan). |
| `wifi: timeout` una y otra vez | Nombre y contraseña en `config.py`, y que la red sea de 2.4 GHz. |
| `ntp: failed` y `BOOT no network or no clock` en ciclo | Hay Wi-Fi pero la red bloquea la hora por internet (NTP, puerto UDP 123); pasa en algunas redes institucionales. Prueba con otra red o con el celular. |
| `mqtt: connect failed` | El broker no responde: revisa `MQTT_HOST`, que la laptop servidor esté encendida y que la red deje salir al puerto 1883. |
| `mqtt: publish failed` a media sesión | Se cayó la red o el broker; el nodo sigue grabando y reintenta la conexión cada 10 clips. |
| `done: 0/N clips sent` | Ningún clip llegó al broker en toda la sesión; misma revisión que `connect failed`. |
| Muchas `late samples` | El Wi-Fi interrumpe el muestreo; si son más de unas decenas por clip, avisa. |
| `ImportError` al arrancar | Faltó copiar algún archivo de `lib/`, o falta `config.py` en la placa. |

## Probar el micrófono sin esperar una sesión

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

Requieren `jsonschema` (validan cada mensaje contra los esquemas de `contracts/mqtt/`):

```bash
pip install jsonschema
```
