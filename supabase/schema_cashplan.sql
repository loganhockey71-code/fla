-- =====================================================================
-- "What should I do with the cash?" plans (manual paper trading). Run AFTER the other schema files. Safe to re-run.
-- A plan is only a recommendation. Nothing is invested until the user confirms it.
-- =====================================================================
create table if not exists cash_plans (
  id           bigserial primary key,
  created_at   timestamptz not null default now(),
  sold_coins   text[] not null,
  amount       double precision not null,            -- money that became available from the sale (after fees)
  cash_after   double precision not null,            -- total cash at that moment
  total_value  double precision not null,
  plan         jsonb not null,                       -- recommended / safer / aggressive options + reason + scores
  status       text not null default 'pending' check (status in ('pending','executing','confirmed','dismissed')),
  chosen       text,                                 -- recommended | safer | aggressive | dismissed
  executed     jsonb,
  decided_at   timestamptz
);
create index if not exists cash_plans_status on cash_plans (status, created_at desc);
alter table cash_plans enable row level security;
