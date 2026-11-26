# noise-iot

Sistema IoT para medir ruido ambiental, temperatura y humedad en CICESE Unidad Académica Tepic.
Nodos ESP32 (firmware de Arduino) graban audio con un micrófono KY-037 y leen un DHT11; publican
por MQTT al broker de la laptop servidor, donde un suscriptor guarda cada sesión en disco y en
PostgreSQL, y un clasificador con una red neuronal marca cada segundo de audio como ruido o no
ruido. Un dashboard de Node-RED muestra los resultados.

```
Nodos ESP32 ──MQTT──▶ Mosquitto ──▶ server/mqtt/subscriber.py ──▶ PostgreSQL + sesiones en disco
                                                                        │
                                       server/mqtt/classifier.py ◀──────┘
                                                │  red neuronal (analysis/pytorch_audio)
                                                ▼
                                    tabla predictions ──▶ dashboard (Node-RED)
```

## Estructura del repositorio

```
noise-iot/
├── firmware/ESP32/          Firmware de Arduino de los nodos (esp32_mqtt_5min_5.ino)
├── hardware/                Lista de materiales y esquemático del nodo
├── server/
│   ├── mqtt/                Suscriptor, clasificador y esquema de PostgreSQL
│   └── monitor/             Alertas por Discord
├── analysis/
│   ├── features/            Rasgos MFCC de cada bloque (los calcula el suscriptor)
│   ├── labeling/            Preparación del audio y etiquetas por umbral RMS
│   ├── pytorch_audio/       Red neuronal: modelo, entrenamiento y prueba
│   └── climate_variants/    Experimento: clasificador con temperatura y humedad
├── data/                    Salidas locales de entrenamiento (no van en git)
└── docs/red_neuronal.md     Documentación de la red neuronal
```

## Documentación

- Servidor (instalación, suscriptor, clasificador, tablas): [`server/mqtt/README.md`](server/mqtt/README.md)
- Red neuronal (qué hace, arquitectura, entrenamiento, resultados): [`docs/red_neuronal.md`](docs/red_neuronal.md)
- Etiquetado y entrenamiento: [`analysis/labeling/README.md`](analysis/labeling/README.md)
- Experimento con clima: [`analysis/climate_variants/README.md`](analysis/climate_variants/README.md)
- Alertas: [`server/monitor/README.md`](server/monitor/README.md)

## Pruebas

En la PC, sin broker ni base de datos (requieren `numpy`; las de la red, también `torch`):

```bash
python -m unittest discover -s server/mqtt/tests
```

## Reglas del repositorio

- **Nunca subas** contraseñas (`.env`, datos de Wi-Fi en el firmware) ni audio crudo (`data/`,
  `.wav`, `.u16`).
- Antes de empezar a trabajar: `git pull origin main`.
- Los mensajes de commit van en inglés, en una sola línea y siguiendo
  [Conventional Commits](https://www.conventionalcommits.org/): `tipo(alcance): resumen`.

## Flujo de trabajo básico

Trae lo último de `main`:

```bash
git pull origin main
```

Guarda tus cambios:

```bash
git add <archivos>
```

```bash
git commit -m "feat(server): add node status view"
```

Súbelos:

```bash
git push origin main
```
