# noise-iot

Sistema IoT para medir ruido ambiental, temperatura y humedad con nodos ESP32 en MicroPython
(cuatro en Tepic y uno en Monterrey). Los nodos envían lotes por HTTPS a una función serverless
(Supabase Edge Function) que los guarda en PostgreSQL; un módulo en Python analiza los datos
con FFT, una red neuronal pequeña y estadística. Más adelante habrá un camino secundario por
LoRa con una Orange Pi como gateway.

## Estructura del repositorio

```
noise-iot/
├── contracts/            Contrato del mensaje (JSON Schema) entre nodos y servidor
├── firmware/             Código MicroPython de los nodos ESP32
│   ├── lib/              Módulos: muestreo, DHT11, envío y cola local
│   └── tests/            Pruebas del firmware
├── hardware/             Diseño físico del nodo
│   ├── schematic/        Diagramas esquemáticos
│   └── photos/           Fotos de los nodos armados e instalados
├── server/               Lado del servidor
│   ├── ingest/           Edge Function que recibe los lotes
│   └── db/               Esquema de PostgreSQL
├── analysis/             Análisis de datos en Python
│   ├── features/         Extracción de características (FFT, niveles)
│   ├── models/           Red neuronal y modelos entrenados
│   ├── notebooks/        Notebooks de exploración
│   └── stats/            Análisis estadístico
├── data/                 Datos de prueba (el audio crudo no va aquí)
│   └── samples/          Muestras pequeñas para pruebas
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

   `firmware/config.py` está en el `.gitignore` porque contiene la contraseña del Wi-Fi y la
   llave de ingesta.

## Pruebas

Firmware (en la PC, con el hardware simulado):

```bash
python -m unittest discover -s firmware/tests
```

Servidor (función de ingesta y esquema de la base; requiere [Deno](https://deno.com)):

```bash
deno test --allow-read --allow-env server/
```

Para desplegar la base y la función en Supabase, ve [`server/README.md`](server/README.md).

## Reglas del repositorio

- **Nunca subas** `firmware/config.py` ni audio crudo (`.wav`, `.raw`, `.bin`, `data/raw/`).
- `contracts/message.schema.json` solo lo modifican los responsables designados, y cualquier
  cambio se avisa al grupo **antes** del merge.
- Cada quien trabaja en su rama personal y hace merge a `main` seguido (cada día o cada dos
  días), no al final.
- Antes de empezar a trabajar: `git pull origin main`.
- Si un agente de IA resuelve un conflicto en algo acordado en grupo (formato del mensaje,
  cálculo de dB, horario de ventanas), quien hace el merge revisa que quede la versión acordada.

## Versiones del firmware

- `FIRMWARE_VERSION` (en `firmware/config.py`) viaja en cada mensaje que manda el nodo.
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
