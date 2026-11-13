# Servidor MQTT: suscriptor y base de datos local

Corre en la laptop servidor de CICESE (`158.97.122.248`), junto a Mosquitto y
PostgreSQL.

```
Nodo ESP32 ──MQTT QoS 1──▶ Mosquitto ──▶ subscriber.py ──▶ PostgreSQL
                                              │
                                              └──▶ AUDIO_DIR/<nodo>/<sesión>/clip_NNNN.u16 y .wav
```

| Archivo | Qué hace |
|---|---|
| `schema.sql` | Tablas `sessions`, `clips`, `climate`, `clip_features`, `labels` y dos vistas. Se puede correr las veces que sea. |
| `protocol.py` | Valida tópicos y mensajes contra `contracts/mqtt.md`, decodifica el clip binario y guarda los archivos de audio. |
| `store.py` | Escribe en PostgreSQL; los duplicados de QoS 1 se ignoran por llave. |
| `subscriber.py` | Escucha `noise/+/+/#` y conecta todo lo anterior. Calcula los 30 rasgos de cada clip con `analysis/features/mfcc.py`. |
| `estacion.py` | Recibe los tópicos `estacion/#` del firmware de Arduino de Jesús y los guarda como archivos, igual que su receptor TCP (no van a la base). |
| `.env.example` | Plantilla de la configuración (broker, base de datos, carpetas de audio). |

## Qué se guarda de cada clip

- `clip_NNNN.u16`: valores crudos del ADC, sin tocar. Es la fuente de verdad.
- `clip_NNNN.wav`: solo para escuchar. Es `(crudo − promedio del clip) × 16` en
  PCM de 16 bits; el promedio que se quitó queda en `clips.adc_mean`.
- En `clips`: frecuencia real medida, muestras tarde, promedio, mínimo y máximo
  del ADC y cuántas muestras saturaron (0 o 4095).
- En `clip_features`: RMS medio y máximo, pico, cruces por cero y media y
  desviación de 13 MFCC (`feature_version = mfcc13-v1`).

## Sesiones de `estacion/` (firmware de Arduino de Jesús)

Mientras el firmware de Arduino publique en su propio formato (`estacion/<dispositivo>/...`),
el suscriptor guarda cada sesión en `ESTACION_DIR` para revisar que llegue completa:

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
   la contraseña de `noise_app`. Crea la carpeta de `AUDIO_DIR` (por ejemplo
   `C:\noise-data\audio`). El `.env` no se sube al repo.

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
INFO session/start esp32-01 20261112T180000Z
INFO clip esp32-01 20261112T180000Z #0 late=0 clipped=0
INFO climate esp32-01 20261112T180000Z
```

Un mensaje que no cumple el contrato aparece como `WARNING rejected ...` con la
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

En la PC, sin broker ni base de datos (requieren `numpy` y `jsonschema`):

```
python -m unittest discover -s server/mqtt/tests
```
