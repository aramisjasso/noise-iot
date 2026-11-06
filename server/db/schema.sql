-- PostgreSQL schema for noise-iot (Supabase).
--
-- Idempotency comes from the primary key of batches: a node may resend a
-- batch from its outbox, and (node_id, seq) guarantees it is stored once.
-- The ingest function inserts the batch first and skips the detail rows
-- when the batch already exists.

-- One row per 10-minute window received from a node.
create table if not exists batches (
    node_id          text        not null,
    seq              integer     not null,
    window_start     timestamptz not null,
    firmware_version text        not null,
    transport        text        not null default 'wifi',  -- 'wifi' or 'lora'
    rssi             real,                                 -- LoRa only
    snr              real,                                 -- LoRa only
    received_at      timestamptz not null default now(),
    primary key (node_id, seq)
);

-- One row per 1-second chunk: ts = window_start + chunk index.
create table if not exists noise_levels (
    node_id  text        not null,
    ts       timestamptz not null,
    level_db real,
    primary key (node_id, ts)
);

-- One row per DHT11 reading: ts = window_start + offset_s.
create table if not exists climate (
    node_id text        not null,
    ts      timestamptz not null,
    temp_c  real,
    rh      real,
    primary key (node_id, ts)
);
