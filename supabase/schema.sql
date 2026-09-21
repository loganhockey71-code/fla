-- =====================================================================
-- Crypto AI Paper-Trading Lab : database schema (Supabase / PostgreSQL)
-- Paste this whole file into Supabase -> SQL Editor -> Run. Safe to re-run.
-- PAPER TRADING ONLY. Nothing here connects to a brokerage or exchange account.
-- =====================================================================

create extension if not exists pgcrypto;

-- ---------- assets ---------------------------------------------------
create table if not exists assets (
  symbol            text primary key check (symbol in ('BTC','ETH','XRP')),
  name              text not null,
  coinbase_product  text not null,
  coingecko_id      text not null
);
insert into assets (symbol, name, coinbase_product, coingecko_id) values
  ('BTC','Bitcoin','BTC-USD','bitcoin'),
  ('ETH','Ethereum','ETH-USD','ethereum'),
  ('XRP','XRP','XRP-USD','ripple')
on conflict (symbol) do nothing;

-- ---------- market_data : live snapshots (price, book, flow) ---------
create table if not exists market_data (
  id            bigserial primary key,
  symbol        text not null references assets(symbol),
  ts            timestamptz not null,
  price         double precision not null,      -- mid price
  bid           double precision,
  ask           double precision,
  spread_pct    double precision,
  volume_24h    double precision,
  ob_imbalance  double precision,               -- (bid depth - ask depth)/(sum), top of book
  buy_pressure  double precision,               -- taker buy volume share of recent trades, 0..1
  cg_price      double precision,               -- CoinGecko reference price
  source        text not null default 'coinbase',
  unique (symbol, ts)
);
create index if not exists market_data_symbol_ts on market_data (symbol, ts desc);

-- ---------- candles --------------------------------------------------
create table if not exists candles (
  symbol       text not null references assets(symbol),
  granularity  int  not null,                   -- seconds: 60, 900, 3600 ...
  ts           timestamptz not null,            -- candle OPEN time
  open         double precision not null,
  high         double precision not null,
  low          double precision not null,
  close        double precision not null,
  volume       double precision not null,
  primary key (symbol, granularity, ts)
);

-- ---------- model_versions ------------------------------------------
create table if not exists model_versions (
  id             serial primary key,
  symbol         text not null references assets(symbol),
  version        text not null unique,          -- e.g. lgbm-BTC-20260920T1200Z
  algorithm      text not null default 'lightgbm',
  trained_at     timestamptz not null default now(),
  train_start    timestamptz,
  train_end      timestamptz,
  n_samples      int,
  feature_names  jsonb not null,
  model_blob     jsonb not null,                -- {"24": "<lightgbm text>", "48": "..."}
  calibration    jsonb not null default '{}',   -- centered Platt scaling per horizon: {"a","center"}
  backtest_metrics jsonb not null default '{}', -- walk-forward OUT-OF-SAMPLE results. BACKTEST, not live.
  is_active      boolean not null default false
);
create unique index if not exists model_versions_one_active
  on model_versions (symbol) where is_active;

-- ---------- predictions : IMMUTABLE ---------------------------------
create table if not exists predictions (
  id                  uuid primary key default gen_random_uuid(),
  created_at          timestamptz not null default now(),
  symbol              text not null references assets(symbol),
  horizon_h           int  not null check (horizon_h in (24,48)),
  target_time         timestamptz not null,
  price_at_prediction double precision not null,
  signal              text not null check (signal in ('BUY','HOLD','SELL')),
  bullish_prob        double precision not null check (bullish_prob between 0 and 1),
  bearish_prob        double precision not null check (bearish_prob between 0 and 1),
  confidence          double precision not null check (confidence between 0 and 1),
  range_low           double precision not null,
  range_high          double precision not null,
  reasons             jsonb not null default '[]',
  explanation         text not null,
  model_version_id    int  not null references model_versions(id),
  model_version       text not null,
  features            jsonb not null,           -- exact feature vector the model saw
  data_cutoff         timestamptz not null,     -- newest data used; must not be in the future
  check (data_cutoff <= created_at),
  check (target_time > created_at)
);
create index if not exists predictions_symbol_created on predictions (symbol, created_at desc);

create or replace function forbid_change() returns trigger language plpgsql as $$
begin
  raise exception '% on % is not allowed: this table is append-only', tg_op, tg_table_name;
end $$;

drop trigger if exists predictions_no_update on predictions;
create trigger predictions_no_update before update or delete on predictions
  for each row execute function forbid_change();
drop trigger if exists predictions_no_truncate on predictions;
create trigger predictions_no_truncate before truncate on predictions
  for each statement execute function forbid_change();

-- ---------- prediction_results : written once, when the horizon ends -
create table if not exists prediction_results (
  prediction_id       uuid primary key references predictions(id),
  evaluated_at        timestamptz not null default now(),
  actual_price        double precision not null,   -- real price at target_time
  actual_return_pct   double precision not null,
  directional_correct boolean not null,            -- did the price move the way bullish_prob>0.5 implied?
  signal_correct      boolean not null,            -- BUY: up | SELL: down | HOLD: |move| < hold band
  in_range            boolean not null,
  hold_band_pct       double precision not null,
  high_confidence     boolean not null
);
drop trigger if exists results_no_update on prediction_results;
create trigger results_no_update before update or delete on prediction_results
  for each row execute function forbid_change();

-- ---------- paper_trades --------------------------------------------
create table if not exists paper_trades (
  id                     bigserial primary key,
  prediction_id          uuid references predictions(id),
  symbol                 text not null references assets(symbol),
  horizon_h              int  not null,
  signal                 text not null check (signal in ('BUY','SELL')),
  status                 text not null check (status in ('open','closed','skipped')),
  skip_reason            text,
  opened_at              timestamptz not null default now(),
  market_price           double precision,        -- real mid price at signal time
  exec_price             double precision,        -- simulated fill (spread + slippage)
  amount_invested        double precision,        -- notional USD bought
  quantity               double precision,
  fee_entry              double precision,
  slippage_entry_pct     double precision,
  spread_entry_pct       double precision,
  planned_exit_at        timestamptz,
  closed_at              timestamptz,
  exit_market_price      double precision,
  exit_price             double precision,
  fee_exit               double precision,
  exit_reason            text,                    -- horizon | sell_signal
  pnl_usd                double precision,
  pnl_pct                double precision,
  balance_after_open     double precision,
  balance_after          double precision         -- portfolio total after close
);
create index if not exists paper_trades_status on paper_trades (status, symbol);

-- once closed, a trade can never change again
create or replace function paper_trade_guard() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' then raise exception 'paper_trades rows cannot be deleted'; end if;
  if old.status <> 'open' then raise exception 'closed/skipped paper trades are immutable'; end if;
  return new;
end $$;
drop trigger if exists paper_trades_guard on paper_trades;
create trigger paper_trades_guard before update or delete on paper_trades
  for each row execute function paper_trade_guard();

-- ---------- portfolio : equity snapshots -----------------------------
create table if not exists portfolio (
  id              bigserial primary key,
  ts              timestamptz not null default now(),
  cash            double precision not null,
  positions_value double precision not null,
  total_value     double precision not null,
  note            text
);
create index if not exists portfolio_ts on portfolio (ts);

-- ---------- news_items : raw ingested feed entries -------------------
create table if not exists news_items (
  id           bigserial primary key,
  source       text not null,
  source_tier  text not null check (source_tier in ('official','project','media')),
  url          text not null unique,
  title        text not null,
  summary      text,
  published_at timestamptz,
  fetched_at   timestamptz not null default now(),
  coins        text[] not null default '{}',
  sentiment    text not null default 'neutral' check (sentiment in ('positive','neutral','negative')),
  importance   int  not null default 0 check (importance between 0 and 100),
  confidence   int  not null default 0 check (confidence between 0 and 100)
);
create index if not exists news_items_published on news_items (published_at desc);

-- ---------- events : detected important events + sudden moves --------
create table if not exists events (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),
  occurred_at   timestamptz not null default now(),
  kind          text not null check (kind in ('news','sudden_move')),
  title         text not null,
  source        text not null,
  source_tier   text,
  coins         text[] not null default '{}',
  sentiment     text not null default 'neutral' check (sentiment in ('positive','neutral','negative')),
  importance    int  not null check (importance between 0 and 100),
  confidence    int  not null check (confidence between 0 and 100),
  explanation   text,
  details       jsonb not null default '{}',
  news_item_id  bigint references news_items(id),
  unique (news_item_id)
);
create index if not exists events_occurred on events (occurred_at desc);

-- ---------- performance_metrics : snapshots computed by the worker ---
create table if not exists performance_metrics (
  id                   bigserial primary key,
  computed_at          timestamptz not null default now(),
  scope                text not null,             -- 'ALL' | 'BTC' | 'ETH' | 'XRP'
  horizon_h            int,                       -- 24 | 48 | null = both
  slice                text not null,             -- 'all' | 'BUY' | 'HOLD' | 'SELL' | 'high_conf'
  total_predictions    int not null,
  correct_predictions  int not null,
  directional_accuracy double precision,
  win_rate             double precision,
  avg_win              double precision,
  avg_loss             double precision,
  profit_factor        double precision,
  max_drawdown         double precision,
  best_trade           double precision,
  worst_trade          double precision,
  extra                jsonb not null default '{}'
);
create index if not exists perf_lookup on performance_metrics (scope, horizon_h, slice, computed_at desc);

-- ---------- settings -------------------------------------------------
create table if not exists settings (
  key         text primary key,
  value       jsonb not null,
  updated_at  timestamptz not null default now()
);
insert into settings (key, value) values
  ('starting_balance',        '1000'),
  ('trading_fee_pct',         '0.4'),
  ('slippage_pct',            '0.10'),
  ('use_real_spread',         'true'),
  ('normal_position_pct',     '10'),
  ('high_conf_position_pct',  '20'),
  ('high_confidence_threshold','75'),
  ('signal_threshold_pct',   '52'),
  ('prediction_interval_h',   '6'),
  ('max_spread_pct_to_trade', '0.30'),
  ('hold_band_pct_24h',       '1.5'),
  ('hold_band_pct_48h',       '2.0'),
  ('trade_horizons',          '[24,48]'),
  ('sudden_move_pct',         '1.0'),
  ('sudden_move_window_min',  '15'),
  ('volume_spike_x',          '3.0')
on conflict (key) do nothing;

-- Lock the auto-generated REST API: the app talks to Postgres directly with
-- a server-side connection string, so anon/authenticated roles get nothing.
do $$
declare t text;
begin
  for t in select unnest(array['assets','market_data','candles','model_versions','predictions',
    'prediction_results','paper_trades','portfolio','news_items','events','performance_metrics','settings'])
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
