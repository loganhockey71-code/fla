-- =====================================================================
-- Real-time microstructure data (worker/crypto_ai/scalp/micro_collect.py). Research only: nothing here feeds live
-- trading yet (see worker/MAKER_RESEARCH.md). Run AFTER schema.sql. Safe to re-run.
--
-- Storage design: raw ticks are kept only briefly (default 48h quotes/trades, 7d depth) so the database stays small;
-- `compact-microstructure` (worker/crypto_ai/scalp/micro_compact.py) rolls anything older into 1-second bars
-- (micro_bars_1s, one row per symbol per second that had activity - NOT one row per second of wall clock) and then
-- deletes the raw rows it compacted. Budget at the default retention, all three coins:
--   book_ticks   ~1-3 BBO CHANGES/sec/coin (not every message) x 48h  ~= 550k rows  x ~60B  ~= 35 MB
--   trade_ticks  ~1-5 trades/sec/coin (real prints, not throttled)    x 48h  ~= 1.3M rows x ~70B  ~= 95 MB
--   depth_snapshots  1 poll / 2s / coin                               x 7d   ~= 900k rows x ~90B  ~= 85 MB
--   micro_bars_1s (long-term, one row per active second)              grows ~1-2 MB/day/coin after compaction
-- i.e. low hundreds of MB steady-state, not gigabytes - review after a week and shorten retention if your plan is tighter.
-- =====================================================================

-- Real-time best bid/ask, written only when it CHANGES (throttled in micro_collect.py) - this is the series a
-- bid-ask-bounce / spread-change / short-horizon-return study is built from.
create table if not exists book_ticks (
  id          bigserial primary key,
  symbol      text not null references assets(symbol),
  ts          timestamptz not null,          -- exchange-reported time of the ticker message
  received_at timestamptz not null default now(),
  best_bid    double precision not null,
  best_ask    double precision not null,
  bid_size    double precision,
  ask_size    double precision,
  mid         double precision not null,
  spread_pct  double precision not null,
  last_trade_price double precision,
  sequence    bigint
);
create index if not exists book_ticks_symbol_ts on book_ticks (symbol, ts);

-- Every trade print (`matches` channel). `side` is Coinbase's MAKER side; `aggressor` is the opposite (the taker,
-- i.e. who crossed the spread) - same convention as coinbase.buy_pressure().
create table if not exists trade_ticks (
  trade_id    bigint not null,
  symbol      text not null references assets(symbol),
  ts          timestamptz not null,
  price       double precision not null,
  size        double precision not null,
  aggressor   text not null check (aggressor in ('buy','sell')),
  primary key (symbol, trade_id)
);
create index if not exists trade_ticks_symbol_ts on trade_ticks (symbol, ts);

-- Free public REST order-book snapshot (level=2, aggregated), polled every few seconds: depth and imbalance at a
-- couple of price bands around the mid. `full`/`level2` push channels now require auth (checked 2026-09); this
-- polled snapshot is the free substitute for real-time depth.
create table if not exists depth_snapshots (
  id            bigserial primary key,
  symbol        text not null references assets(symbol),
  ts            timestamptz not null default now(),
  mid           double precision not null,
  spread_pct    double precision,
  bid_depth_5bp   double precision,   -- displayed size within 0.05% of mid
  ask_depth_5bp   double precision,
  imbalance_5bp   double precision,   -- (bid-ask)/(bid+ask) within the band; +1 = all bids
  bid_depth_25bp  double precision,
  ask_depth_25bp  double precision,
  imbalance_25bp  double precision
);
create index if not exists depth_snapshots_symbol_ts on depth_snapshots (symbol, ts);

-- Compacted long-term series: one row per symbol per SECOND that had at least one quote or trade (not a dense
-- per-second grid). Built by micro_compact.py from book_ticks + trade_ticks once they age past their raw retention;
-- the raw rows are deleted after a bucket is written here, so this is what most-of-history research reads from.
create table if not exists micro_bars_1s (
  symbol        text not null references assets(symbol),
  ts            timestamptz not null,     -- second, truncated
  mid_open      double precision,
  mid_high      double precision,
  mid_low       double precision,
  mid_close     double precision,
  spread_avg    double precision,
  spread_max    double precision,
  n_quotes      int not null default 0,
  n_trades      int not null default 0,
  buy_volume    double precision not null default 0,   -- aggressor-buy size
  sell_volume   double precision not null default 0,   -- aggressor-sell size
  vwap          double precision,
  primary key (symbol, ts)
);

do $$
declare t text;
begin
  for t in select unnest(array['book_ticks','trade_ticks','depth_snapshots','micro_bars_1s']) loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
