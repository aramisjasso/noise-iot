-- PostgreSQL schema for noise-iot (Supabase).
--
-- The whole file is idempotent: run it again after any change (Supabase SQL
-- editor, or psql) and it only creates or replaces what is missing.
--
-- Idempotency of the data comes from the primary key of batches: a node may
-- resend a batch from its outbox, and (node_id, seq) guarantees it is stored
-- once. ingest_batch() inserts the batch first and skips the detail rows when
-- the batch already exists, all in one transaction.

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

-- Closed tables: RLS on and no policies, so the anon and authenticated roles
-- (the keys a node or a browser could hold) can neither read nor write.
-- Only service_role, used inside the ingest function, and direct Postgres
-- connections (analysis with psycopg) get through.
alter table batches      enable row level security;
alter table noise_levels enable row level security;
alter table climate      enable row level security;

-- Store one batch (contracts/message.schema.json) atomically.
--
-- The payload must already be validated against the contract; this function
-- only maps it to rows. Returns:
--   'stored'    new batch, all rows inserted
--   'duplicate' same (node_id, seq) and same window_start already stored:
--               a resend from the outbox, nothing to do
--   'conflict'  same (node_id, seq) but a different window_start: the node
--               reused a sequence number (e.g. flash wiped). Nothing is
--               stored, so the data is not silently lost as a "duplicate".
create or replace function ingest_batch(payload jsonb)
returns text
language plpgsql
set search_path = public
as $$
declare
    v_node_id      text        := payload ->> 'node_id';
    v_seq          integer     := (payload ->> 'seq')::integer;
    v_window_start timestamptz := (payload ->> 'window_start')::timestamptz;
    v_existing     timestamptz;
begin
    insert into batches (node_id, seq, window_start, firmware_version)
    values (v_node_id, v_seq, v_window_start, payload ->> 'firmware_version')
    on conflict (node_id, seq) do nothing;

    if not found then
        select window_start into v_existing
        from batches
        where node_id = v_node_id and seq = v_seq;

        if v_existing = v_window_start then
            return 'duplicate';
        end if;
        return 'conflict';
    end if;

    -- levels_db[i] is chunk i (0-based) -> window_start + i seconds.
    -- A JSON null becomes a SQL null (failed chunk).
    insert into noise_levels (node_id, ts, level_db)
    select v_node_id,
           v_window_start + make_interval(secs => l.idx - 1),
           (l.value #>> '{}')::real
    from jsonb_array_elements(payload -> 'levels_db') with ordinality as l(value, idx)
    on conflict (node_id, ts) do nothing;

    insert into climate (node_id, ts, temp_c, rh)
    select v_node_id,
           v_window_start + make_interval(secs => (c ->> 'offset_s')::integer),
           (c ->> 'temp_c')::real,
           (c ->> 'rh')::real
    from jsonb_array_elements(payload -> 'climate') as c
    on conflict (node_id, ts) do nothing;

    return 'stored';
end;
$$;

-- Only the ingest function (service_role) may call it.
revoke execute on function ingest_batch(jsonb) from public;
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke execute on function ingest_batch(jsonb) from anon, authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function ingest_batch(jsonb) to service_role;
    end if;
end;
$$;
