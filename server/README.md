# Servidor: ingesta y base de datos

```
Nodo ──HTTPS + JSON──▶ Edge Function (ingest) ──rpc──▶ PostgreSQL (Supabase)
       x-ingest-key     valida contra                  ingest_batch():
                        message.schema.json            idempotente por (node_id, seq)
```

| Archivo | Qué hace |
|---|---|
| `db/schema.sql` | Tablas, RLS y la función `ingest_batch()`. Se puede correr las veces que sea. |
| `ingest/handler.ts` | Lógica HTTP: llave, validación contra el contrato, códigos de respuesta. |
| `ingest/index.ts` | Punto de entrada de la Edge Function; conecta el handler con Supabase. |
| `supabase/config.toml` | Configuración del CLI de Supabase (apunta la función a `ingest/`). |

## Qué responde la función

El nodo solo distingue **2xx** (confirmado, se saca de la cola) de **cualquier otra cosa**
(se queda en la cola y se reintenta en el siguiente ciclo).

| Código | Cuándo |
|---|---|
| `200` | Lote guardado (`"status": "stored"`) o ya estaba guardado (`"duplicate"`, reenvío de la cola). |
| `400` | El cuerpo no es JSON. |
| `401` | Falta `x-ingest-key` o no coincide. |
| `405` | Método distinto de `POST`. |
| `409` | El `seq` ya se usó para **otra** `window_start` (ver abajo). |
| `413` | Cuerpo de más de 64 KB. |
| `422` | El JSON no cumple `contracts/message.schema.json`; la respuesta dice qué campo falló. |
| `500` | Error de la base; el nodo reintenta después sin riesgo de duplicar. |

### Sobre el 409

Si a un nodo se le borra la flash (por ejemplo, al flashear `v1.0` con borrado completo), su
`seq` puede volver a empezar en 0 y chocar con lotes de prueba que ya están en la base. En
lugar de tratarlos como duplicados y tirarlos sin avisar, la función responde `409` y el lote
**se queda en la cola del nodo**. Para destrabarlo, borra los lotes viejos de ese nodo (ver
"Antes de la campaña") y en el siguiente ciclo el nodo los reenvía solo.

## Desplegar en Supabase (una sola vez)

1. Crea un proyecto en [supabase.com](https://supabase.com) (plan gratuito).
2. En el **SQL Editor** del proyecto pega y corre el contenido de `server/db/schema.sql`.
   Si después cambia el esquema, se vuelve a correr completo.
3. Inicia sesión en el CLI y liga el proyecto (el *project ref* viene en la URL del panel):

   ```bash
   supabase login
   ```

   ```bash
   supabase link --workdir server --project-ref <project-ref>
   ```

4. Genera una llave para los nodos y guárdala como secreto de la función:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   ```bash
   supabase secrets set --workdir server INGEST_KEY=<la-llave>
   ```

5. Despliega la función:

   ```bash
   supabase functions deploy ingest --workdir server
   ```

6. En el `firmware/config.py` de cada nodo:

   ```python
   INGEST_URL = "https://<project-ref>.supabase.co/functions/v1/ingest"
   INGEST_KEY = "<la-llave>"
   ```

La llave de los nodos solo sirve para llamar a esta función. Las tablas tienen RLS sin
políticas, así que las llaves `anon` y `authenticated` de Supabase no pueden leer ni escribir;
solo la función (con la `service_role`, que nunca sale del servidor) y las conexiones directas
a Postgres.

### Probar el despliegue

Desde la raíz del repo, manda el lote de ejemplo del contrato:

```bash
python -c "import json; print(json.dumps(json.load(open('contracts/message.schema.json'))['examples'][0]))" > example.json
```

```bash
curl -i -X POST "https://<project-ref>.supabase.co/functions/v1/ingest" -H "Content-Type: application/json" -H "x-ingest-key: <la-llave>" --data @example.json
```

La primera vez responde `"status":"stored"` y la segunda `"status":"duplicate"`. Borra
`example.json` y el lote de prueba al terminar.

## Antes de la campaña

Borra los lotes de prueba para que los `seq` reales no choquen con ellos (en el SQL Editor):

```sql
delete from noise_levels; delete from climate; delete from batches;
```

Y para verificar que los cinco nodos traen la misma versión del firmware:

```sql
select node_id, firmware_version, max(received_at) as ultimo_lote
from batches group by node_id, firmware_version order by node_id;
```

## Leer los datos para el análisis

Desde Python o Colab, con la cadena de conexión del panel (**Connect → Session pooler**):

```python
import pandas as pd
import psycopg

with psycopg.connect("<cadena-de-conexión>") as conn:
    df = pd.read_sql("select * from noise_levels where node_id = 'tepic-01' order by ts", conn)
```

## Pruebas

Las pruebas no necesitan Docker ni un proyecto de Supabase: la base corre en
[PGlite](https://pglite.dev) (Postgres compilado a WASM) y la función con una base falsa.
Desde la raíz del repo:

```bash
deno test --allow-read --allow-env server/
```

## Correr todo en local (opcional)

Con Docker Desktop abierto:

```bash
supabase start --workdir server
```

Corre `db/schema.sql` en el SQL Editor de Supabase Studio (`http://localhost:54323`) y luego:

```bash
supabase functions serve ingest --workdir server --env-file server/supabase/.env.local
```

donde `server/supabase/.env.local` (ya está en el `.gitignore`) tiene `INGEST_KEY=<una-llave>`.
La función queda en `http://localhost:54321/functions/v1/ingest`.
