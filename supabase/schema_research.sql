-- =====================================================================
-- Research / data-intelligence layer. Run AFTER schema.sql. Safe to re-run.
-- Read-only public data only. No trading, wallets or brokerage anywhere.
-- =====================================================================

-- ---------- source_registry : every source, its trust tier and polling state
create table if not exists source_registry (
  key                text primary key,
  name               text not null,
  kind               text not null,                 -- rss | api
  tier               int  not null check (tier between 1 and 6),
  tier_label         text not null,
  credibility_score  int  not null check (credibility_score between 0 and 100),
  url                text,
  poll_interval_s    int  not null,
  enabled            boolean not null default true,
  last_polled_at     timestamptz,
  last_success_at    timestamptz,
  last_status        text,                          -- ok | not_modified | error
  last_error         text,                          -- always redacted of secrets
  items_last_run     int default 0,
  etag               text,
  last_modified      text
);

-- ---------- research_events : one row per UNDERLYING event ------------
create table if not exists research_events (
  id                        bigserial primary key,
  kind                      text not null default 'news',   -- news | legislation | macro | prediction_market
  title                     text not null,
  source                    text not null,
  source_key                text not null references source_registry(key),
  source_url                text,
  external_id               text not null,
  published_at              timestamptz,
  detected_at               timestamptz not null default now(),
  event_category            text not null check (event_category in ('regulation','legislation','fed','interest_rates',
                              'inflation','ETF','lawsuit','enforcement','exchange','hack','whale_activity','partnership',
                              'token_or_network_update','macro','adoption','other')),
  affected_coins            text[] not null default '{}',
  sentiment                 text not null default 'neutral' check (sentiment in ('positive','neutral','negative')),
  importance_score          int  not null check (importance_score between 0 and 100),
  source_credibility_score  int  not null check (source_credibility_score between 0 and 100),
  novelty_score             int  not null check (novelty_score between 0 and 100),
  confidence_score          int  not null check (confidence_score between 0 and 100),
  btc_impact_score          int  not null default 0 check (btc_impact_score between -100 and 100),
  eth_impact_score          int  not null default 0 check (eth_impact_score between -100 and 100),
  xrp_impact_score          int  not null default 0 check (xrp_impact_score between -100 and 100),
  event_probability         double precision check (event_probability between 0 and 1),
  event_probability_source  text,
  summary                   text,
  raw_text_or_reference     text,
  fingerprint               text,
  duplicate_count           int  not null default 0,      -- copies folded into this event
  independent_confirmations int  not null default 1,      -- copies do NOT increase this
  origin_tier               int,
  details                   jsonb not null default '{}',
  updated_at                timestamptz not null default now(),
  unique (source_key, external_id)
);
create index if not exists research_events_detected on research_events (detected_at desc);
create index if not exists research_events_cat on research_events (event_category, detected_at desc);

-- ---------- event_duplicates : copies grouped under the canonical event
create table if not exists event_duplicates (
  id                    bigserial primary key,
  event_id              bigint not null references research_events(id) on delete cascade,
  source_key            text not null,
  source_name           text not null,
  url                   text,
  title                 text not null,
  published_at          timestamptz,
  seen_at               timestamptz not null default now(),
  similarity            double precision not null,
  counts_as_independent boolean not null default false,
  unique (event_id, url)
);

-- ---------- macro_data : FRED history, point-in-time ---------------------
create table if not exists macro_data (
  series_id     text not null,
  obs_date      date not null,
  value         double precision not null,
  available_at  timestamptz not null,           -- when the market could first have known it (release-lag adjusted)
  fetched_at    timestamptz not null default now(),
  primary key (series_id, obs_date)
);

-- ---------- legislative_items : bills / hearings, change-tracked -----------
create table if not exists legislative_items (
  id                  bigserial primary key,
  item_type           text not null check (item_type in ('bill','hearing')),
  external_id         text not null unique,       -- e.g. 119-hr-3633
  congress            int,
  bill_type           text,
  number              text,
  title               text not null,
  sponsor             text,
  origin_chamber      text,
  introduced_date     date,
  policy_area         text,
  latest_action_date  date,
  latest_action_text  text,
  status_stage        text,                       -- introduced|committee|passed_one_chamber|passed_both|to_president|law|failed
  committees          jsonb not null default '[]',
  actions             jsonb not null default '[]',
  amendments          jsonb not null default '[]',
  hearings            jsonb not null default '[]',
  house_votes         jsonb not null default '[]',
  state_hash          text not null,              -- unchanged hash => no new event
  crypto_relevance    int not null default 0,
  url                 text,
  first_seen_at       timestamptz not null default now(),
  last_changed_at     timestamptz not null default now(),
  last_checked_at     timestamptz not null default now()
);

-- ---------- prediction_market_snapshots (public, read-only) --------------
create table if not exists prediction_market_snapshots (
  id           bigserial primary key,
  platform     text not null check (platform in ('polymarket','kalshi')),
  market_id    text not null,
  title        text not null,
  category     text,
  coins        text[] not null default '{}',
  direction    int  not null default 0,          -- +1 if "Yes" is good for crypto, -1 if bad, 0 unknown
  yes_prob     double precision not null check (yes_prob between 0 and 1),
  liquidity    double precision,
  volume       double precision,
  volume_24h   double precision,
  end_date     timestamptz,
  url          text,
  captured_at  timestamptz not null default now()
);
create index if not exists pms_market on prediction_market_snapshots (platform, market_id, captured_at desc);

-- ---------- predictions / models / metrics: A-vs-B variants ---------------
-- variant 'market'   = market-data model (the one that paper-trades)
-- variant 'research' = same model + macro & event features (shadow: logged and scored, never traded)
alter table model_versions add column if not exists variant text not null default 'market';
drop index if exists model_versions_one_active;
create unique index if not exists model_versions_one_active_v on model_versions (symbol, variant) where is_active;

alter table predictions add column if not exists variant text not null default 'market';
alter table predictions add column if not exists run_id uuid;                  -- pairs the two variants made in the same run
alter table predictions add column if not exists research_features jsonb;      -- research context that existed at that instant
create index if not exists predictions_run on predictions (run_id);

alter table performance_metrics add column if not exists variant text not null default 'market';

insert into settings (key, value) values ('trade_variant', '"market"') on conflict (key) do nothing;

do $$
declare t text;
begin
  for t in select unnest(array['source_registry','research_events','event_duplicates','macro_data',
                               'legislative_items','prediction_market_snapshots'])
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

alter table legislative_items add column if not exists source_updated_at timestamptz;   -- Congress.gov updateDate; skip refetch if unchanged
