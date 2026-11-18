# Monitoreo Discord para noise-iot

Complemento propuesto para el repositorio público `aramisjasso/noise-iot`.
No modifica el firmware ni el clasificador.

## Qué revisa

- `clips.received_at`: falta de audio durante más de 15 min (`estacion`) o 35 min (`noise`).
- `clips.adc_max - clips.adc_min`: últimos 3 bloques con variación demasiado baja.
- `predictions.status/reason/rms_dbfs` cuando existen resultados recientes de `estacion`.
- `climate.temp_c` y `climate.rh`: nulo, NaN, no finito o cero.
- `estacion/+/estado`: últimas voluntades MQTT retenidas; alerta después de 20 min desconectado.
- MQTT broker y avance del clasificador mientras hay audio entrante.
- Ping externo a Healthchecks.io cuando PostgreSQL y MQTT están accesibles.
- Registra incidentes y solo notifica al activarse o recuperarse.

## Instalación

1. Copia `server/monitor/` a la misma ruta dentro del clon de `noise-iot`.
2. Asegúrate de que el servidor ya tenga PostgreSQL, Mosquitto y el suscriptor funcionando.
3. Desde la raíz, instala `python -m pip install -r server/mqtt/requirements.txt`.
4. Ejecuta `psql -U noise_app -d noise_iot -f server/monitor/schema.sql`.
5. Registra explícitamente cada nodo conocido. Por ejemplo:

```sql
INSERT INTO monitored_nodes(node_id,pipeline,missing_minutes,min_adc_span)
VALUES
  ('esp32-01','estacion',15,5),
  ('tepic-01','noise',35,5)
ON CONFLICT (node_id) DO UPDATE SET
  pipeline=EXCLUDED.pipeline,
  missing_minutes=EXCLUDED.missing_minutes;
```

6. Copia `server/monitor/.env.example` a `server/monitor/.env`.
7. Rellena `DISCORD_WEBHOOK_URL` (nuevo webhook de canal de Discord), opcionalmente `DISCORD_ROLE_ID` (solo dígitos) y `HEALTHCHECKS_PING_URL` (URL privada del monitor externo).
8. Ejecuta `python server/monitor/monitor.py`.
9. Importa al dashboard Node-RED dos consultas PostgreSQL:
   - `SELECT * FROM monitor_node_dashboard ORDER BY node_id;`
   - `SELECT * FROM monitor_service_status ORDER BY service;`

No uses el mismo `client_id` MQTT que `noise-subscriber` ni el de una ESP32.

## Pruebas

- Con todo funcionando, verifica que la vista del dashboard actualice `last_clip_at`.
- Apaga solo el audio de un nodo Arduino: después de 15 min debe llegar la alerta `audio_missing`.
- Desconecta la red de un Arduino: su LWT debe cambiar a `desconectado`; alerta MQTT después de 20 min.
- Inserta una lectura DHT 0 o NULL de un nodo de prueba para verificar el aviso.
- Verifica recuperación al recibir nuevos datos válidos; el monitor no repite avisos en cada ciclo.
- Para probar caída de servidor, detén el monitor y espera que Healthchecks marque el check como Down.

## Límites y ajustes antes de producción

- Los tiempos y umbrales son **iniciales**: validar con capturas reales y calibrar por nodo.
- El estado MQTT LWT está implementado en el firmware Arduino `estacion`, no en el firmware MicroPython `noise`.
- El monitor evalúa los últimos 3 clips **cada 15 s**, no cada paquete individual en el instante exacto. Para garantía por bloque habría que incorporar `check_clip` directamente al flujo de ingesta o mantener un cursor durable.
- Las alertas se registran si Discord aceptó el POST. Si el POST funciona pero falla el commit de BD, podría repetirse.
- La primera lectura retenida de LWT recibida tras instalar el monitor indica el estado actual, **no** la hora original de desconexión.
- Un clip reciente de ruido bajo no implica hardware roto por sí solo. El umbral de -55 dBFS es provisional; ajustar por nodo y descartar períodos en silencio ambiental.
- DHT11 temperatura de 0 °C puede ser físicamente posible; se marca como sospechosa según el contexto de esta campaña.
- Supervisar procesos mediante PID, servicios de Windows, espacio de disco y estado de Node-RED puede agregarse antes del despliegue final.
- El script no contiene un servidor/dashboard Node-RED: entrega una vista SQL y una tabla de servicios que el dashboard puede consultar.
- Un servicio `database` o `monitor` puede conservar su última fila cuando el servidor cae: usa `checked_at` y el monitoreo **externo** como fuente de verdad para caída total.
- No pongas `DISCORD_WEBHOOK_URL` en Git. Los webhooks son secretos.
- En el `.ino` público hay una clave Wi-Fi en texto claro. Si es real, cámbiala y elimina el secreto del historial de Git.
