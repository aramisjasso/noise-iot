-- Local PostgreSQL schema for the MQTT clip pipeline (contracts/mqtt.md).
-- Safe to run more than once. Run it as the application user (noise_app):
--   psql -U noise_app -d noise_iot -f server/mqtt/schema.sql
--
-- Idempotency: every table has the key of its message as primary key and the
-- subscriber inserts with ON CONFLICT, so QoS 1 duplicates are ignored.
-- There are no foreign keys on purpose: a clip may arrive before (or without)
-- its session/start, and it must still be stored.

create table if not exists sessions (
    node_id          text        not null,
    session_id       text        not null,  -- YYYYMMDDTHHMMSSZ, scheduled start (UTC)
    site             text        not null,  -- tepic | monterrey (from the topic)
    started_at       timestamptz,           -- real node clock at start
    firmware_version text,
    fs               integer,
    adc_bits         smallint,
    clip_samples     integer,
    session_s        integer,
    mic_model        text,
    gain_setting     text,
    dht_model        text,
    ended_at         timestamptz,
    duration_ms      integer,
    clips_captured   integer,
    clips_sent       integer,
    clips_failed     integer,
    late_samples     integer,
    dht_ok           integer,
    dht_failed       integer,
    received_at      timestamptz not null default now(),
    primary key (node_id, session_id)
);

create table if not exists clips (
    node_id         text        not null,
    session_id      text        not null,
    clip_idx        integer     not null,
    t_offset_ms     bigint      not null,  -- clip start since session start
    n_samples       integer     not null,
    capture_us      bigint      not null,
    fs_measured     real        not null,  -- n_samples / capture_us * 1e6
    late_samples    integer     not null,
    adc_mean        real        not null,  -- DC offset of the raw clip
    adc_min         integer     not null,
    adc_max         integer     not null,
    clipped_samples integer     not null,  -- samples at 0 or 4095
    raw_path        text        not null,  -- .u16 file, exact ADC values
    wav_path        text        not null,  -- .wav for listening, DC removed
    received_at     timestamptz not null default now(),
    primary key (node_id, session_id, clip_idx)
);

create table if not exists climate (
    node_id     text        not null,
    session_id  text        not null,
    t_offset_ms bigint      not null,
    temp_c      real,
    rh          real,
    attempts    smallint    not null,
    error       text,
    received_at timestamptz not null default now(),
    primary key (node_id, session_id, t_offset_ms)
);

-- One row per clip and feature definition, so features can be recomputed with
-- other parameters without losing the previous version.
create table if not exists clip_features (
    node_id         text        not null,
    session_id      text        not null,
    clip_idx        integer     not null,
    feature_version text        not null,
    rms_mean        real        not null,
    rms_max         real        not null,
    peak            real        not null,
    zcr             real        not null,
    mfcc_mean       real[]      not null,
    mfcc_std        real[]      not null,
    computed_at     timestamptz not null default now(),
    primary key (node_id, session_id, clip_idx, feature_version)
);

-- Human labels (one row per clip, person and label) for the training stage.
create table if not exists labels (
    id         bigserial   primary key,
    node_id    text        not null,
    session_id text        not null,
    clip_idx   integer     not null,
    rater      text        not null,
    label      text        not null,
    value      real,
    note       text,
    created_at timestamptz not null default now()
);

-- Added for the estacion/ format (firmware/ESP32, Arduino): raw "inicio"/"fin"
-- messages are kept whole in sessions.meta, and each 0.5 s block points into the
-- session's single audio_crudo.u16 (raw_path) at sample_offset.
alter table sessions add column if not exists meta jsonb;
alter table clips add column if not exists sample_offset bigint;

create index if not exists clips_session_idx on clips (node_id, session_id);
create index if not exists climate_session_idx on climate (node_id, session_id);
create index if not exists labels_clip_idx on labels (node_id, session_id, clip_idx);

-- Clips with their absolute time (session start + offset). Dropped and created
-- again because clips gained a column and the view expands c.*.
drop view if exists clips_timed;
create view clips_timed as
select c.*,
       s.site,
       s.started_at + c.t_offset_ms * interval '1 millisecond' as captured_at
from clips c
left join sessions s using (node_id, session_id);

-- Quick health check per session: how many clips arrived versus sent.
create or replace view session_summary as
select s.node_id,
       s.session_id,
       s.site,
       s.started_at,
       s.clips_sent,
       count(c.clip_idx)                as clips_stored,
       coalesce(sum(c.late_samples), 0) as late_samples_stored,
       coalesce(sum(c.clipped_samples), 0) as clipped_samples,
       round(avg(c.fs_measured)::numeric, 1) as fs_measured_avg
from sessions s
left join clips c using (node_id, session_id)
group by s.node_id, s.session_id, s.site, s.started_at, s.clips_sent;
