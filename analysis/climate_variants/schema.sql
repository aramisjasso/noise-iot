-- Results of analysis/climate_variants/train_variants.py --save-db.
-- psql -U noise_app -d noise_iot -f analysis/climate_variants/schema.sql
create table if not exists classifier_variant_runs (
    run_id         text        primary key,
    threshold_dbfs real        not null,
    info           jsonb,                 -- clips, sessions, split dates, seeds
    created_at     timestamptz not null default now()
);

create table if not exists classifier_variant_metrics (
    run_id               text    not null,
    variant              text    not null,  -- sound | sound_temp | sound_hum | sound_temp_hum
    variant_name         text    not null,
    node_id              text    not null,  -- a node, or 'all'
    n_test               integer not null,  -- test seconds
    noise_pct_test       real    not null,  -- % of test seconds labeled noise
    accuracy_mean        real    not null,
    accuracy_std         real    not null,
    f1_mean              real    not null,
    f1_std               real    not null,
    changed_vs_sound_pct real    not null,  -- % of seconds decided differently than sound only
    primary key (run_id, variant, node_id)
);
