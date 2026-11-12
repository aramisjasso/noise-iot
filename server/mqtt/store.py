"""PostgreSQL writes for the MQTT pipeline (server/mqtt/schema.sql).

Every insert uses ON CONFLICT on the message key, so a QoS 1 duplicate is a
no-op and the subscriber never fails because of it.
"""

import psycopg


class PgStore:
    """Writes messages into PostgreSQL. One connection, autocommit."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = None

    def _connection(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.dsn, autocommit=True)
        return self.conn

    def _cursor(self):
        return self._connection().cursor()

    def close(self):
        if self.conn is not None:
            self.conn.close()

    def ensure_session(self, site, node_id, session_id):
        """Create an empty session row so clips never wait for session/start."""
        with self._cursor() as cur:
            cur.execute(
                "insert into sessions (node_id, session_id, site) values (%s, %s, %s) "
                "on conflict (node_id, session_id) do nothing",
                (node_id, session_id, site),
            )

    def session_start(self, site, msg):
        with self._cursor() as cur:
            cur.execute(
                """
                insert into sessions (node_id, session_id, site, started_at, firmware_version,
                    fs, adc_bits, clip_samples, session_s, mic_model, gain_setting, dht_model)
                values (%(node_id)s, %(session_id)s, %(site)s, %(started_at)s,
                    %(firmware_version)s, %(fs)s, %(adc_bits)s, %(clip_samples)s,
                    %(session_s)s, %(mic_model)s, %(gain_setting)s, %(dht_model)s)
                on conflict (node_id, session_id) do update set
                    site = excluded.site,
                    started_at = excluded.started_at,
                    firmware_version = excluded.firmware_version,
                    fs = excluded.fs,
                    adc_bits = excluded.adc_bits,
                    clip_samples = excluded.clip_samples,
                    session_s = excluded.session_s,
                    mic_model = excluded.mic_model,
                    gain_setting = excluded.gain_setting,
                    dht_model = excluded.dht_model
                """,
                dict(msg, site=site),
            )

    def session_end(self, site, msg):
        self.ensure_session(site, msg["node_id"], msg["session_id"])
        with self._cursor() as cur:
            cur.execute(
                """
                update sessions set ended_at = %(ended_at)s, duration_ms = %(duration_ms)s,
                    clips_captured = %(clips_captured)s, clips_sent = %(clips_sent)s,
                    clips_failed = %(clips_failed)s, late_samples = %(late_samples)s,
                    dht_ok = %(dht_ok)s, dht_failed = %(dht_failed)s
                where node_id = %(node_id)s and session_id = %(session_id)s
                """,
                msg,
            )

    def climate(self, site, msg):
        self.ensure_session(site, msg["node_id"], msg["session_id"])
        with self._cursor() as cur:
            cur.execute(
                """
                insert into climate (node_id, session_id, t_offset_ms, temp_c, rh, attempts, error)
                values (%(node_id)s, %(session_id)s, %(t_offset_ms)s, %(temp_c)s, %(rh)s,
                    %(attempts)s, %(error)s)
                on conflict (node_id, session_id, t_offset_ms) do nothing
                """,
                msg,
            )

    def clip_exists(self, node_id, session_id, clip_idx):
        with self._cursor() as cur:
            cur.execute(
                "select 1 from clips where node_id = %s and session_id = %s and clip_idx = %s",
                (node_id, session_id, clip_idx),
            )
            return cur.fetchone() is not None

    def clip(self, site, node_id, row, features, feature_version):
        """Insert a clip and its features in one transaction."""
        self.ensure_session(site, node_id, row["session_id"])
        conn = self._connection()
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into clips (node_id, session_id, clip_idx, t_offset_ms, n_samples,
                        capture_us, fs_measured, late_samples, adc_mean, adc_min, adc_max,
                        clipped_samples, raw_path, wav_path)
                    values (%(node_id)s, %(session_id)s, %(clip_idx)s, %(t_offset_ms)s,
                        %(n_samples)s, %(capture_us)s, %(fs_measured)s, %(late_samples)s,
                        %(adc_mean)s, %(adc_min)s, %(adc_max)s, %(clipped_samples)s,
                        %(raw_path)s, %(wav_path)s)
                    on conflict (node_id, session_id, clip_idx) do nothing
                    """,
                    dict(row, node_id=node_id),
                )
                cur.execute(
                    """
                    insert into clip_features (node_id, session_id, clip_idx, feature_version,
                        rms_mean, rms_max, peak, zcr, mfcc_mean, mfcc_std)
                    values (%(node_id)s, %(session_id)s, %(clip_idx)s, %(feature_version)s,
                        %(rms_mean)s, %(rms_max)s, %(peak)s, %(zcr)s, %(mfcc_mean)s, %(mfcc_std)s)
                    on conflict (node_id, session_id, clip_idx, feature_version) do nothing
                    """,
                    dict(
                        features,
                        node_id=node_id,
                        session_id=row["session_id"],
                        clip_idx=row["clip_idx"],
                        feature_version=feature_version,
                    ),
                )
