# noise-iot

Sistema IoT para medir ruido ambiental, temperatura y humedad con nodos ESP32 en MicroPython
(cuatro en Tepic y uno en Monterrey). Cada nodo graba sesiones de clips de audio de 1 s y los
publica por MQTT al broker de la laptop servidor en CICESE (`158.97.122.248`), donde un
suscriptor guarda el audio en disco, los metadatos en PostgreSQL y calcula los rasgos de cada
clip (MFCC, RMS, pico, cruces por cero) para la red neuronal y el análisis estadístico.

```
Nodos ESP32 ──MQTT──▶ Mosquitto ──▶ server/mqtt/subscriber.py ──▶ PostgreSQL + audio en disco
```

El contrato de los mensajes está en [`contracts/mqtt.md`](contracts/mqtt.md). El camino
anterior por HTTPS a Supabase (`server/ingest/`, `supabase/`, `contracts/message.schema.json`)
se conserva en el repo pero ya no es el de la campaña.

## Estructura del repositorio

```
noise-iot/
├── contracts/            Contrato de los mensajes entre nodos y servidor
│   └── mqtt/             Esquemas JSON de los mensajes MQTT (v2)
├── firmware/             Código MicroPython de los nodos ESP32
│   ├── lib/              Módulos: muestreo, DHT11, MQTT (y los de HTTPS de la v1)
│   ├── experiments/      Prototipos de referencia (TCP v1 de Jesús)
│   └── tests/            Pruebas del firmware
├── hardware/             Diseño físico del nodo
│   ├── schematic/        Diagramas esquemáticos
│   └── photos/           Fotos de los nodos armados e instalados
├── server/               Lado del servidor
│   ├── mqtt/             Suscriptor MQTT y esquema de PostgreSQL local (v2)
│   ├── ingest/           Edge Function de Supabase (v1, HTTPS)
│   └── db/               Esquema de PostgreSQL en Supabase (v1)
├── supabase/             Configuración del CLI de Supabase (despliegue de la función)
├── analysis/             Análisis de datos en Python
│   ├── features/         Extracción de características (MFCC, RMS, cruces por cero)
│   ├── models/           Red neuronal y modelos entrenados
│   ├── notebooks/        Notebooks de exploración
│   └── stats/            Análisis estadístico
├── data/                 Datos de prueba (el audio crudo no va aquí)
│   └── samples/          Muestras pequeñas para pruebas (sesiones de esp32-01)
└── docs/                 Documentación del proyecto
    ├── architecture/     Diagramas de arquitectura
    └── report/           Reporte final de la maestría
```

## Cómo empezar

1. Clona el repositorio:

   ```bash
   git clone https://github.com/aramisjasso/noise-iot.git
   cd noise-iot
   ```

2. Configura tu nombre y correo en git (si no lo has hecho):

   ```bash
   git config user.name "Tu Nombre"
   git config user.email "tu@correo.com"
   ```

3. Copia la configuración de ejemplo del firmware y llena tus datos:

   ```bash
   cp firmware/config.example.py firmware/config.py
   ```

   `firmware/config.py` está en el `.gitignore` porque contiene la contraseña del Wi-Fi.

## Pruebas

Firmware (en la PC, con el hardware simulado; requiere `pip install jsonschema`):

```bash
python -m unittest discover -s firmware/tests
```

Para cargar el firmware a una ESP32, ve [`firmware/README.md`](firmware/README.md).

Suscriptor MQTT y rasgos de audio (requiere `numpy` y `jsonschema`):

```bash
python -m unittest discover -s server/mqtt/tests
```

Para instalar el suscriptor y la base en la laptop servidor, ve
[`server/mqtt/README.md`](server/mqtt/README.md). El camino v1 por Supabase se prueba con
`deno test --allow-read --allow-env server/` y se documenta en [`server/README.md`](server/README.md).

## Reglas del repositorio

- **Nunca subas** `firmware/config.py` ni audio crudo (`.wav`, `.raw`, `.bin`, `data/raw/`).
- `contracts/` (el contrato de los mensajes) solo lo modifican los responsables designados, y cualquier
  cambio se avisa al grupo **antes** del merge.
- Cada quien trabaja en su rama personal y hace merge a `main` seguido (cada día o cada dos
  días), no al final.
- Antes de empezar a trabajar: `git pull origin main`.
- Si un agente de IA resuelve un conflicto en algo acordado en grupo (formato del mensaje,
  cálculo de dB, horario de ventanas), quien hace el merge revisa que quede la versión acordada.

## Versiones del firmware

- `FIRMWARE_VERSION` (en `firmware/config.py`) viaja en el mensaje `session/start` de cada sesión.
- Antes de la campaña de medición se etiqueta `v1.0` y los cinco nodos se flashean con esa
  etiqueta.
- Una persona verifica en la base de datos que los cinco nodos reporten la misma versión.

## Flujo de trabajo básico

Crea tu rama personal (solo la primera vez):

```bash
git switch -c <nombre>
```

Trae lo último de `main` antes de empezar:

```bash
git pull origin main
```

Guarda tus cambios:

```bash
git add <archivos>
```

```bash
git commit -m "feat(firmware): add rms calculation"
```

Sube tu rama:

```bash
git push origin <nombre>
```

Integra a `main` (con un pull request en GitHub o localmente):

```bash
git switch main
```

```bash
git pull origin main
```

```bash
git merge <nombre>
```

```bash
git push origin main
```

Los mensajes de commit van en inglés, en una sola línea y siguiendo
[Conventional Commits](https://www.conventionalcommits.org/): `tipo(alcance): resumen`.
