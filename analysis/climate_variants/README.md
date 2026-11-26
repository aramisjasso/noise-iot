# Clasificador con clima: cuatro variantes

`train_variants.py` entrena el mismo clasificador de ruido con cuatro entradas distintas y
compara cómo cambia:

| Variante | Entrada |
|---|---|
| Sonido | envolvente de energía de 1 s (8 000 valores), la red de solo sonido |
| Sonido + temperatura | lo mismo y la temperatura de ese segundo |
| Sonido + humedad | lo mismo y la humedad |
| Sonido + temperatura + humedad | lo mismo y las dos |

- **Etiqueta:** la misma del clasificador en vivo: ruido si el RMS sin ruido eléctrico es
  ≥ −22 dBFS (`analysis/labeling`).
- **Clima de cada segundo:** interpolado entre la lectura del DHT11 antes y después de la sesión.
- **Cómo entra el clima:** la red procesa el audio igual (8 000 → 32) y la temperatura y la humedad,
  estandarizadas, se concatenan a las 32 salidas de la capa oculta y entran directo a la capa de
  salida (33 o 34 entradas). Así la variante de sonido es exactamente la red de solo sonido.
- **Separación por tiempo** (hora de Tepic): entrenamiento antes de `--val-from`, validación hasta
  `--test-from` y prueba de ahí en adelante. Las cuatro variantes usan los mismos segundos.
- **5 semillas por variante:** se reporta el promedio y la desviación, para distinguir una
  diferencia real del azar del entrenamiento.
- **`changed_vs_sound_pct`:** porcentaje de segundos de prueba en que la variante decide distinto
  que la de solo sonido con la misma semilla.

## Qué se espera y cómo leerlo

La etiqueta sale del volumen del audio, así que el sonido ya trae toda la respuesta: las cuatro
variantes deben quedar prácticamente iguales y `changed_vs_sound_pct` debe ser bajo. Eso demuestra
que el clasificador decide por lo que oye y que la temperatura y la humedad no cambian su decisión.

Si una variante con clima quedara distinta por encima de la desviación entre semillas, la
explicación más probable no es el clima: temperatura y humedad casi identifican al nodo (cada lugar
tiene su clima) y cada nodo tiene distinta ganancia de micrófono.

## Uso (en el server, desde la raíz del repo)

```
psql -U noise_app -d noise_iot -f analysis/climate_variants/schema.sql
python analysis/climate_variants/train_variants.py --estacion-dir C:\noise-data\estacion --save-db
```

Por omisión: datos desde el viernes 20 a las 15:00, validación desde el lunes 23 a las 00:00 y
prueba desde el lunes 23 a las 10:30. Se cambian con `--since`, `--val-from` y `--test-from`
(formato `"2026-11-23 10:30"`). Los resultados quedan en
`data/noise-data/climate_variants/<run_id>/` y, con `--save-db`, en las tablas
`classifier_variant_runs` y `classifier_variant_metrics`.

## Para el dashboard

```sql
select variant_name, node_id, n_test, accuracy_mean, accuracy_std, f1_mean, f1_std, changed_vs_sound_pct
from classifier_variant_metrics
where run_id = (select run_id from classifier_variant_runs order by created_at desc limit 1)
order by node_id, variant;
```
