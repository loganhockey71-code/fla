-- =====================================================================
-- Manual paper trading account. Run AFTER schema.sql and schema_research.sql. Safe to re-run.
-- Manual trades live in the same table but a separate 'account', so they never touch the AI's results.
-- =====================================================================
alter table paper_trades add column if not exists account text not null default 'ai' check (account in ('ai','manual'));
alter table paper_trades add column if not exists ai_advice jsonb;     -- what the AI was saying at the moment of a manual trade
create index if not exists paper_trades_account on paper_trades (account, status, symbol);
