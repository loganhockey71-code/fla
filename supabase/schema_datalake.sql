-- =====================================================================
-- Metadata ONLY for the free-data-source ingestion layer (worker/crypto_ai/datasources/). The actual raw data from
-- these adapters lives in local (or object-storage) Parquet files, NOT in this database - see
-- worker/crypto_ai/datasources/datalake.py and worker/DATA_SOURCES.md. Run AFTER schema.sql. Safe to re-run.
-- =====================================================================
create table if not exists datalake_manifest (
  id            bigserial primary key,
  category      text not null,        -- e.g. 'funding_rates', 'open_interest', 'liquidations', 'order_books_l2'
  adapter       text not null,        -- e.g. 'okx_funding_rate'
  source        text not null,        -- human name, e.g. 'OKX public API'
  day           date not null,
  path          text,                 -- where the Parquet partition for this (adapter, day) lives; null if it errored or fetched 0 rows
  rows          int not null default 0,
  collected_at  timestamptz not null default now(),
  last_error    text,                 -- set when the most recent run for this (adapter, day) failed; null once a run succeeds
  unique (category, adapter, day)
);
create index if not exists datalake_manifest_day on datalake_manifest (day desc);

alter table datalake_manifest enable row level security;
