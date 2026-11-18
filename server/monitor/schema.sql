-- Apply after server/mqtt/schema.sql. Safe to run repeatedly.
CREATE TABLE IF NOT EXISTS monitored_nodes (
    node_id text PRIMARY KEY,
    pipeline text NOT NULL CHECK (pipeline IN ('estacion', 'noise')),
    missing_minutes integer NOT NULL CHECK (missing_minutes >= 1),
    min_adc_span integer NOT NULL DEFAULT 5 CHECK (min_adc_span >= 0),
    min_rms_dbfs real NOT NULL DEFAULT -55,
    enabled boolean NOT NULL DEFAULT true,
    registered_at timestamptz NOT NULL DEFAULT now()
);

-- Last Will messages are retained. First observation after a monitor restart
-- is not necessarily the actual moment a node disconnected.
CREATE TABLE IF NOT EXISTS mqtt_node_status (
    node_id text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('en_linea', 'desconectado')),
    changed_at timestamptz NOT NULL DEFAULT now(),
    observed_at timestamptz NOT NULL DEFAULT now(),
    disconnected_since timestamptz
);

CREATE TABLE IF NOT EXISTS monitor_alert_state (
    target text NOT NULL,
    kind text NOT NULL,
    active boolean NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target, kind)
);

CREATE TABLE IF NOT EXISTS monitor_service_status (
    service text PRIMARY KEY,
    state text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(),
    detail text
);

CREATE TABLE IF NOT EXISTS monitor_alert_history (
    id bigserial PRIMARY KEY,
    target text NOT NULL,
    kind text NOT NULL,
    active boolean NOT NULL,
    description text NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS monitor_clips_recent_idx ON clips(node_id, received_at DESC);
CREATE INDEX IF NOT EXISTS monitor_climate_recent_idx ON climate(node_id, received_at DESC);
CREATE INDEX IF NOT EXISTS monitor_predictions_recent_idx ON predictions(node_id, computed_at DESC);

-- Node-RED dashboard: SELECT * FROM monitor_node_dashboard ORDER BY node_id;
CREATE OR REPLACE VIEW monitor_node_dashboard AS
SELECT n.node_id, n.pipeline,
       m.state AS mqtt_state,
       CASE
          WHEN n.pipeline='noise' THEN 'NO IMPLEMENTADO EN FIRMWARE'
          WHEN m.state IS NULL THEN 'SIN ESTADO'
          WHEN m.state='en_linea' THEN 'CONECTADO'
          WHEN m.disconnected_since IS NOT NULL
               AND now()-m.disconnected_since >= interval '20 minutes'
            THEN 'DESCONECTADO >20 MIN'
          ELSE 'DESCONECTADO EN TOLERANCIA'
       END AS mqtt_health,
       m.changed_at AS mqtt_state_since,
       m.disconnected_since,
       c.last_clip_at,
       CASE WHEN c.last_clip_at IS NULL THEN NULL
            ELSE round((extract(epoch FROM now() - c.last_clip_at) / 60)::numeric, 1)
       END AS minutes_without_audio,
       d.last_climate_at,
       d.temp_c,
       d.rh,
       n.missing_minutes,
       coalesce(a.active_faults, 0) AS active_faults,
       CASE WHEN c.last_clip_at IS NULL THEN 'SIN DATOS'
            WHEN now() - c.last_clip_at > n.missing_minutes * interval '1 minute' THEN 'SIN AUDIO'
            WHEN coalesce(a.active_faults, 0) > 0 THEN 'ALERTA'
            ELSE 'OK'
       END AS status
FROM monitored_nodes n
LEFT JOIN mqtt_node_status m ON m.node_id=n.node_id
LEFT JOIN LATERAL (
    SELECT max(received_at) AS last_clip_at FROM clips WHERE node_id=n.node_id
) c ON true
LEFT JOIN LATERAL (
    SELECT received_at AS last_climate_at, temp_c, rh
    FROM climate WHERE node_id=n.node_id ORDER BY received_at DESC LIMIT 1
) d ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS active_faults FROM monitor_alert_state
    WHERE target=n.node_id AND active
) a ON true
WHERE n.enabled;
