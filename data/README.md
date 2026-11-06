# Datos

El audio crudo **no va en git**: pesa demasiado y, además, puede tener voces de personas.
Se guarda en la carpeta compartida de Google Drive del proyecto con esta organización:

```
<node_id>/<YYYY-MM-DD>/<HHMM>.wav
```

Por ejemplo, `tepic-01/2026-11-05/1830.wav` es la ventana que empezó a las 18:30 (UTC) del
5 de noviembre de 2026 en el nodo `tepic-01`.

## Qué sí va en este directorio

- `samples/`: solo muestras pequeñas para pruebas (unos cuantos segundos de audio o lotes JSON
  de ejemplo). Ten en cuenta que el `.gitignore` excluye `*.wav`, `*.raw` y `*.bin`; si de
  verdad necesitas subir una muestra de audio pequeña, coméntalo antes con el grupo.
- `raw/`: si descargas audio de Drive para trabajar localmente, ponlo aquí. Está en el
  `.gitignore` y nunca se sube.

Los niveles en dB y las lecturas de clima viven en PostgreSQL (ver `server/db/schema.sql`),
no en archivos de este directorio.
