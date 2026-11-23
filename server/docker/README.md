# Correr el servidor en Docker (local o en la nube)

Empaqueta lo que hoy corre a mano en la laptop de CICESE: Mosquitto +
`subscriber.py` + `classifier.py` + PostgreSQL, con `docker compose`.
El camino v1 por Supabase (`server/ingest/`) no usa esto, ya corre en la nube
de Supabase.

```
                    ┌───────────────── docker compose ─────────────────┐
Nodo ESP32 ──MQTT──▶│ mosquitto ──▶ subscriber ──▶ postgres ◀── classifier │
                    │                   │                                │
                    │                   └──▶ volumes noise_audio /       │
                    │                        noise_estacion               │
                    └────────────────────────────────────────────────────┘
```

| Archivo | Qué hace |
|---|---|
| `Dockerfile` | Imagen de Python compartida por `subscriber`, `classifier` y `monitor`. Se construye desde la raíz del repo porque esos scripts importan `analysis/` y leen `contracts/`. |
| `mosquitto.conf` | Broker con persistencia y `allow_anonymous true` (los nodos actuales no mandan usuario/contraseña). |
| `.env.example` | Plantilla de las variables que lee `../../docker-compose.yml`. |
| `../../docker-compose.yml` | Los 5 servicios: `mosquitto`, `postgres`, `subscriber`, `classifier` y `monitor` (opcional). |

## Correrlo en tu máquina

1. Copia la plantilla de variables a la raíz del repo:

   ```bash
   cp server/docker/.env.example .env
   ```

   Edita `.env` y pon una `POSTGRES_PASSWORD` real.

2. Desde la raíz del repo:

   ```bash
   docker compose up -d --build
   ```

   Esto levanta Mosquitto en el puerto `1883`, PostgreSQL (aplica
   `server/mqtt/schema.sql` y `server/monitor/schema.sql` solo la primera vez
   que crea el volumen) y el suscriptor + clasificador.

3. Revisa los logs:

   ```bash
   docker compose logs -f subscriber classifier
   ```

4. Para probarlo sin nodos reales, publica un mensaje de ejemplo con cualquier
   cliente MQTT contra `localhost:1883` siguiendo `contracts/mqtt.md`.

El servicio `monitor` (alertas a Discord) no arranca con `up -d`: necesita
`DISCORD_WEBHOOK_URL` en `.env` y se levanta aparte con:

```bash
docker compose --profile monitor up -d --build
```

## Desplegarlo en una VM en la nube

La idea es la misma que en la laptop: una máquina con IP pública fija,
Docker instalado, y el puerto del broker abierto. Sirve cualquier VM barata
(DigitalOcean Droplet, AWS Lightsail, Oracle Cloud free tier, un VPS
cualquiera); 1 vCPU / 1-2 GB RAM sobra para este volumen de nodos.

1. **Crea la VM** (Ubuntu 22.04/24.04 es lo más simple) y anota su IP pública.
2. **Instala Docker** en la VM:

   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

3. **Clona el repo** en la VM y crea el `.env` (paso 1 de "Correrlo en tu
   máquina"). En vez de compilar ahí, baja la imagen que publica GitHub
   Actions (ver abajo):

   ```bash
   docker compose pull
   docker compose up -d
   ```
4. **Abre el puerto 1883** en el firewall de la nube (security group /
   firewall rules del proveedor) para tráfico entrante TCP desde internet.
5. **Actualiza los nodos**: en `firmware/config.py` de cada ESP32, cambia

   ```python
   MQTT_HOST = "158.97.122.248"  # IP vieja de la laptop en CICESE
   ```

   por la IP pública (o dominio) de la VM, y vuelve a flashear.
6. **Actualiza la documentación** que menciona la IP de CICESE
   (`README.md`, `contracts/mqtt.md`, `server/mqtt/README.md`) para que
   apunte a la nueva dirección.

### Imagen publicada por GitHub Actions

`.github/workflows/docker-server.yml` corre las pruebas de `server/mqtt/tests` y
construye la imagen cada vez que un push toca el servidor (`server/`,
`analysis/features|labeling|pytorch_audio`, `contracts/mqtt`). **Solo en
`leo-cloud`** (la rama que corre en el servidor, `DEPLOY_BRANCH` en el
workflow) la publica en `ghcr.io/aramisjasso/noise-iot-server` con las
etiquetas `latest` y `sha-<commit>`. En las demás ramas y en los PR solo
verifica que compile.

Para actualizar la VM después de un push a `leo-cloud`:

```bash
git pull
docker compose pull
docker compose up -d
```

Para fijar una versión concreta (o regresar a una anterior), pon
`IMAGE_TAG=sha-<commit>` en el `.env`.

La primera vez que se publica, el paquete queda **privado** en GHCR. Tienes dos
opciones: hacerlo público (en GitHub: *Packages → noise-iot-server → Package
settings → Change visibility*) o iniciar sesión en la VM con un token personal
que tenga permiso `read:packages`:

```bash
echo <token> | docker login ghcr.io -u <usuario-github> --password-stdin
```

### Qué falta para producción real (fuera del alcance de este compose)

- **Seguridad del broker**: hoy es `allow_anonymous true` sin TLS, igual que
  en la laptop. Expuesto a internet (antes solo estaba en la red de CICESE)
  es más riesgoso. Para endurecerlo hace falta usuario/contraseña en
  Mosquitto (`mosquitto_passwd`) y TLS (puerto 8883 con certificados, p.ej.
  Let's Encrypt), más el cambio correspondiente en `firmware/lib` para que
  los nodos se conecten así.
- **Backups**: los volúmenes `postgres_data`, `noise_audio` y
  `noise_estacion` son locales a la VM. Para no perder datos conviene un
  `pg_dump` periódico y copiar el audio a algún storage externo (igual que
  hoy se hace con Drive).
- **Reinicio tras reboot de la VM**: `restart: unless-stopped` ya hace que
  los contenedores vuelvan a arrancar solos; no se necesita el equivalente a
  `schtasks` de Windows.

## Logs: identificar cada ESP32

Con varios nodos conectados, hay dos capas de log y cada una identifica al
nodo de forma distinta:

1. **`mosquitto`**: una línea por conexión/desconexión, con el `client_id`
   que usó el nodo. El firmware MicroPython (`firmware/lib/publisher.py`) se
   conecta con `client_id = NODE_ID` (p.ej. `esp32-01`, fijo). El firmware de
   Arduino (`firmware/ESP32/*.ino`) usa `DISPOSITIVO-<millis>` (p.ej.
   `esp32-04-183921`), así que el id cambia en cada reconexión pero siempre
   empieza con el nombre del dispositivo.

   ```bash
   docker compose logs -f mosquitto
   ```

   ```
   2026-11-23 18:34:24: New client connected from 172.21.0.6:60296 as esp32-01 (p4, c1, k60).
   2026-11-23 18:34:25: Client esp32-01 [172.21.0.6:60296] disconnected.
   ```

   Si ves `Client esp32-02 [...] disconnected: session taken over.`, es que
   **dos nodos se conectaron con el mismo `client_id`** (típicamente
   `NODE_ID` mal copiado en `firmware/config.py` de dos placas): Mosquitto
   desconecta al que ya estaba para dejar entrar al nuevo.

2. **`subscriber` y `classifier`**: cada línea trae el id del nodo como
   segundo token, justo después del tipo de mensaje (`protocol.py`,
   `estacion.py`), así que se puede filtrar por nodo aunque haya varios
   mandando datos a la vez:

   ```bash
   docker compose logs -f subscriber classifier
   ```

   ```
   subscriber-1  | 2026-11-23 18:34:25 INFO session/start esp32-01 20261123T183400Z
   subscriber-1  | 2026-11-23 18:34:26 INFO clip esp32-01 20261123T183400Z #0 late=0 clipped=0
   subscriber-1  | 2026-11-23 18:34:27 WARNING rejected noise/tepic/esp32-03/clip: ...
   classifier-1  | 2026-11-23 18:34:40 INFO esp32-04 20261123T183400Z: 120 clips
   ```

Para seguir solo un nodo a través de los tres servicios:

```bash
docker compose logs -f mosquitto subscriber classifier | grep -i esp32-01
```

(con PowerShell: `docker compose logs -f mosquitto subscriber classifier | Select-String esp32-01`).

Si quieres conservar el historial más allá de lo que guarda Docker
(`docker compose logs` solo lee lo que el daemon tiene retenido), agrega
`LOG_FILE`/`CLASSIFIER_LOG_FILE` como variables de entorno del servicio
correspondiente en `docker-compose.yml`, apuntando a un archivo dentro de los
volúmenes `noise_audio`/`noise_estacion`.

## Pruebas

Las pruebas existentes (`python -m unittest discover -s server/mqtt/tests`)
no cambian: siguen corriendo sin Docker, como antes.
