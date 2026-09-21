-- =====================================================================
-- Self-learning + real-time event signals. Run AFTER schema.sql, schema_research.sql, schema_manual.sql. Safe to re-run.
-- PAPER TRADING ONLY. Nothing here can place a real order.
-- =====================================================================

-- ---------- post_mortems : one per WRONG prediction, append-only ----------
create table if not exists post_mortems (
  id             bigserial primary key,
  prediction_id  uuid not null unique references predictions(id),
  created_at     timestamptz not null default now(),
  symbol         text not null,
  horizon_h      int  not null,
  variant        text not null,
  signal         text not null,
  actual_return_pct double precision not null,
  error_class    text not null,           -- primary explanation (see worker/crypto_ai/learning/postmortem.py)
  tags           jsonb not null default '[]',
  findings       jsonb not null default '{}',   -- price path, volume, market, events, drivers, similar past situations
  summary        text not null
);
create index if not exists post_mortems_class on post_mortems (error_class, symbol);
drop trigger if exists post_mortems_no_update on post_mortems;
create trigger post_mortems_no_update before update or delete on post_mortems
  for each row execute function forbid_change();

-- ---------- learned_patterns : statistics over many post-mortems ----------
create table if not exists learned_patterns (
  key            text primary key,        -- e.g. ALL|24|volume_shock
  scope          text not null,           -- ALL or a coin
  horizon_h      int,                     -- null = both
  tag            text not null,
  description    text not null,
  n_with_tag     int  not null,           -- scored predictions where the situation was present
  n_wrong_with   int  not null,
  n_without      int  not null,
  n_wrong_without int not null,
  error_rate_with    double precision,
  error_rate_without double precision,
  lift           double precision,        -- error_rate_with / error_rate_without
  p_value        double precision,        -- Fisher exact, one-sided (is the tag associated with MORE errors?)
  status         text not null,           -- insufficient | candidate | confirmed | noise
  suggestion     text,
  examples       jsonb not null default '[]',
  first_seen     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

-- ---------- model_challenges : every retrain attempt, promoted or not ------
create table if not exists model_challenges (
  id               bigserial primary key,
  created_at       timestamptz not null default now(),
  symbol           text not null,
  variant          text not null,
  champion_version text,
  challenger_version text,
  trigger_info     jsonb not null default '{}',   -- new scored predictions, days since training, thresholds
  holdout_start    timestamptz,
  holdout_end      timestamptz,
  metrics          jsonb not null default '{}',   -- champion vs challenger on the SAME unseen rows
  decision         text not null,                 -- promoted | rejected | insufficient_data | not_due
  reason           text not null
);
create index if not exists model_challenges_sym on model_challenges (symbol, variant, created_at desc);

-- ---------- live_signals : what to do NOW (BUY / HOLD / REDUCE / SELL) -----
create table if not exists live_signals (
  id              bigserial primary key,
  created_at      timestamptz not null default now(),
  symbol          text not null references assets(symbol),
  trigger_kind    text not null check (trigger_kind in ('price','volume','orderbook','news','scheduled')),
  action          text not null check (action in ('BUY','HOLD','REDUCE','SELL')),
  urgency         text not null check (urgency in ('high','medium','low')),
  price           double precision not null,
  model_bull_prob double precision,               -- fresh model read at this moment (not saved as an official prediction)
  score           double precision,
  move_pct        double precision,
  move_minutes    int,
  volume_spike    double precision,
  ob_imbalance    double precision,
  scope           text,                           -- broad | partial | coin_specific
  cause           text,
  cause_confidence int,
  reasons         jsonb not null default '[]',
  explanation     text not null,
  news_event_id   bigint,                         -- research_events.id that triggered/explained it
  event_id        bigint,                         -- events.id of the sudden-move record
  previous_action text,
  expires_at      timestamptz not null
);
create index if not exists live_signals_recent on live_signals (symbol, created_at desc);
create unique index if not exists live_signals_one_per_news on live_signals (symbol, news_event_id) where news_event_id is not null and trigger_kind = 'news';

insert into settings (key, value) values
  ('retrain_min_days',            '30'),
  ('retrain_min_new_scored',      '100'),
  ('retrain_holdout_days',        '14'),
  ('retrain_min_improvement',     '0.002'),
  ('retrain_max_p_value',         '0.10'),
  ('pattern_min_examples',        '30'),
  ('news_trigger_importance',     '60'),
  ('signal_cooldown_min',         '30')
on conflict (key) do nothing;

do $$
declare t text;
begin
  for t in select unnest(array['post_mortems','learned_patterns','model_challenges','live_signals'])
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
