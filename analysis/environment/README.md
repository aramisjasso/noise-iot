# Ruido contra temperatura y humedad

`compare_env.py` responde: ¿la temperatura o la humedad ayudan a predecir cuánto ruido tiene una
sesión, más allá de lo que ya explican la hora, el fin de semana y el nodo?

- **Unidad:** una sesión (5 min de audio; el DHT11 se lee antes y después y se promedia).
- **Salida:** % de segundos utilizables marcados como ruido en la sesión, tomado de la tabla
  `predictions` que llena `server/mqtt/classifier.py`.
- **Variantes** (MLP de una capa oculta de 16 neuronas, salida entre 0 y 1):

  | Variante | Entradas |
  |---|---|
  | `base` | hora del día, fin de semana, nodo |
  | `temp` | base + temperatura |
  | `hum` | base + humedad |
  | `both` | base + temperatura + humedad |

- **Separación por días, en orden:** los últimos 3 días son prueba, los 2 anteriores validación y
  el resto entrenamiento. Con menos de 3 días separa sesiones en orden y lo marca como prueba
  técnica (`technical_check_only`). Cada variante se entrena con 5 semillas y se promedia.
- **Resultado:** el error absoluto medio (MAE) en puntos porcentuales en las sesiones de prueba,
  total y por nodo. `gain_pp > 0` significa que la variante predice mejor que la base. Solo eso
  cuenta como hallazgo; si la ganancia es cercana a cero, el clima no aporta.

## Uso

Desde la raíz del repo, en el server (usa `PG_DSN` de `server/mqtt/.env`):

```
# Al final de la campaña, solo con sesiones posteriores a la instalación de los nodos
python analysis/environment/compare_env.py --since "2026-11-19 09:00"

# Para probar que corre, sin guardar nada
python analysis/environment/compare_env.py --no-db
```

`--since` es hora local de Tepic. Los datos del salón no deben entrar al resultado final: la
temperatura casi no cambia ahí y los nodos no estaban en su lugar.

## Para el dashboard

```sql
-- Última corrida
select * from environment_runs order by created_at desc limit 1;

-- Qué variante gana, total y por nodo
select variant, node_id, n_sessions, mae_pp, gain_pp
from environment_metrics where run_id = '<run_id>' order by node_id, variant;

-- Real contra predicho por nodo, para graficar
select started_at, node_id, variant, split, noise_pct_real, noise_pct_pred
from environment_predictions where run_id = '<run_id>' and node_id = 'esp32-01'
order by started_at;
```
