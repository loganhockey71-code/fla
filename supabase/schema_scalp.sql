-- =====================================================================
-- Short-term scalper (worker/crypto_ai/scalp). Run AFTER schema.sql. Safe to re-run. PAPER TRADING ONLY.
-- New tables only, plus a cleanup of stale `settings` rows (see bottom). Existing tables are untouched.
-- =====================================================================

-- One row per scalper trade. Open rows are updated while the trade lives (stop moves up only, never wider);
-- once closed a row never changes.
create table if not exists scalp_trades (
  id                bigserial primary key,
  symbol            text not null references assets(symbol),
  direction         text not null check (direction in ('long','short')),
  status            text not null check (status in ('open','closed')),
  opened_at         timestamptz not null,              -- open time of the 1m candle the entry filled on
  closed_at         timestamptz,
  duration_min      double precision,
  entry_mid         double precision,
  entry_price       double precision,                  -- simulated fill (spread + slippage against us)
  exit_mid          double precision,
  exit_price        double precision,
  qty               double precision,
  notional          double precision,
  initial_stop_px   double precision,
  stop_px           double precision,                  -- current stop; only ever moves in the trade's favour
  take_profit_px    double precision,
  stop_dist_pct     double precision,
  tp_dist_pct       double precision,
  unit_pct          double precision,
  trail_active      boolean not null default false,
  stop_moves        int not null default 0,
  best_px           double precision,
  mfe_pct           double precision,                  -- maximum favourable excursion, % of entry
  mae_pct           double precision,                  -- maximum adverse excursion, % of entry
  mfe_r             double precision,                  -- ... in multiples of the initial risk
  mae_r             double precision,
  fees_usd          double precision,
  slippage_usd      double precision,
  gross_pnl_usd     double precision,                  -- mid-to-mid, before slippage and fees
  net_pnl_usd       double precision,
  net_pnl_pct       double precision,                  -- % of entry notional
  exit_reason       text,                              -- stop_loss | trailing_stop | take_profit | time_stop | momentum_reversal | no_follow_through | kill_switch
  profitable        boolean,
  setup             text,
  regime            text,
  pred_edge_pct     double precision,                  -- model's predicted net edge at entry
  features          jsonb not null default '{}',       -- market conditions at entry
  state             jsonb not null default '{}',       -- engine state of an open trade (pending exit, bars held, ...)
  model_id          bigint,
  policy            jsonb,                             -- exit multipliers in force when the trade opened
  last_bar_ts       timestamptz                        -- last 1m candle already applied to an open trade (catch-up cursor)
);
create index if not exists scalp_trades_status on scalp_trades (status, symbol);
create index if not exists scalp_trades_closed on scalp_trades (closed_at desc);
alter table scalp_trades add column if not exists signal_id bigint;   -- the confirmed signal this trade came from (scalp_signals.id)

-- EVERY prediction the model makes that clears its edge threshold is recorded here, whether or not it ever trades:
--   pending    raised at a candle's close, waiting for the NEXT candle to confirm it
--   confirmed  the next candle agreed -> a trade was attempted (trade_id)
--   failed     the next candle did not agree (reason says how) -> no trade
--   expired    the confirmation candle was not seen in time (a late worker) -> no trade
--   blocked    a risk / news / liquidity gate refused it before it could be confirmed (reason says which)
-- Once the hold window has passed every row is GRADED against what the market actually did AFTER the decision (right / wrong / flat,
-- and whether it would have made money after costs), so wrong calls that were never traded are studied as much as the traded ones.
create table if not exists scalp_signals (
  id              bigserial primary key,
  created_at      timestamptz not null default now(),
  symbol          text not null references assets(symbol),
  direction       text not null check (direction in ('long','short')),
  decision_ts     timestamptz not null,              -- open time of the 1m candle the prediction was made from
  ref_price       double precision not null,         -- that candle's close
  pred_edge_pct   double precision not null,         -- predicted NET edge (after costs) when raised
  atr_pct         double precision,
  setup           text,
  regime          text,
  features        jsonb not null default '{}',       -- market conditions + candle patterns at the decision
  news            jsonb not null default '{}',       -- news context at the decision (bias, score, independent confirmations, top headlines)
  model_id        bigint,
  status          text not null check (status in ('pending','confirmed','failed','expired','blocked')),
  reason          text,                              -- why it failed / was blocked (no_follow_through, reversal, news_conflict, illiquid, ...)
  resolved_at     timestamptz,
  confirm_move_atr double precision,                 -- confirmation candle's follow-through, in 1m ATRs
  confirm_edge_pct double precision,                 -- the model's fresh edge on the confirmation candle
  trade_id        bigint references scalp_trades(id),
  graded_at       timestamptz,
  fwd_gross_pct   double precision,                  -- signed in the predicted direction: close H bars after the decision vs the next open
  fwd_net_pct     double precision,                  -- ... after the round-trip cost of the settings in force
  fwd_mfe_pct     double precision,                  -- best / worst excursion in the predicted direction over the window
  fwd_mae_pct     double precision,
  outcome         text check (outcome in ('right','wrong','flat')),
  profitable      boolean,                           -- would it have made money after costs?
  lesson          text,
  tags            text[] not null default '{}',
  unique (symbol, decision_ts)
);
create index if not exists scalp_signals_status on scalp_signals (status, created_at desc);
create index if not exists scalp_signals_ungraded on scalp_signals (decision_ts) where graded_at is null;

-- Why each closed trade worked or failed (append-only).
create table if not exists scalp_lessons (
  id          bigserial primary key,
  trade_id    bigint not null unique references scalp_trades(id),
  created_at  timestamptz not null default now(),
  symbol      text not null,
  verdict     text not null,                           -- win | loss | cost_casualty
  tags        text[] not null default '{}',
  lesson      text not null,
  details     jsonb not null default '{}'
);

-- Models trained on trade outcomes (net P&L), promoted only when better out-of-sample.
create table if not exists scalp_models (
  id          bigserial primary key,
  created_at  timestamptz not null default now(),
  bundle      jsonb not null,                          -- {"long": <lightgbm text>, "short": <lightgbm text>}
  threshold   double precision,                        -- validated edge threshold in %; null = never trade
  meta        jsonb not null default '{}',
  is_active   boolean not null default false
);
create unique index if not exists scalp_models_one_active on scalp_models ((true)) where is_active;

-- Small key/value state owned by the worker: exit_policy, setup_stats, blocked_setups, retrain_log, heartbeat, status.
create table if not exists scalp_state (
  key         text primary key,
  value       jsonb not null,
  updated_at  timestamptz not null default now()
);

do $$
declare t text;
begin
  for t in select unnest(array['scalp_trades','scalp_signals','scalp_lessons','scalp_models','scalp_state']) loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

-- ---------------------------------------------------------------------
-- Stale settings. Older versions seeded (and the Settings page wrote) strategy rows that silently overrode the
-- code's intended values - e.g. trade_horizons=[24,48], sudden_move_pct=1.0, signal_cooldown_min=30. The worker now
-- only reads an allow-list of keys from this table (config.USER_EDITABLE), and here we delete the retired ones.
-- The kill switch is normalised to a real JSON boolean (a JSON *string* "false" used to count as "on").
-- ---------------------------------------------------------------------
delete from settings where key in (
  'trade_horizons','sudden_move_pct','sudden_move_window_min','volume_spike_x','signal_cooldown_min','signal_threshold_pct',
  'prediction_interval_h','hold_band_pct_1h','hold_band_pct_24h','hold_band_pct_48h','normal_position_pct','high_conf_position_pct',
  'high_confidence_threshold','short_prediction_interval_min','autopilot_trade_window_enabled','autopilot_trade_window_start_h',
  'autopilot_trade_window_end_h','trade_variant');
update settings set value = case when lower(trim(both '"' from value #>> '{}')) in ('true','t','1','yes','on') then 'true'::jsonb else 'false'::jsonb end
  where key = 'autopilot_enabled' and jsonb_typeof(value) <> 'boolean';
