-- =====================================================================
-- Trade-level risk management (see worker/crypto_ai/risk.py). Run AFTER schema.sql and schema_manual.sql.
-- Safe to re-run. All additive/nullable: existing rows and code that doesn't know about these columns are
-- unaffected. `stop_price` moves (up only, never down - "never increase risk after entering"); the other new
-- columns besides high_water_price and trail_active are set once at entry and read-only after that, same as
-- every other paper_trades column.
-- =====================================================================
alter table paper_trades add column if not exists initial_stop_price double precision;  -- as planned at entry; never changes
alter table paper_trades add column if not exists stop_price          double precision;  -- current stop; trails upward only
alter table paper_trades add column if not exists take_profit_price   double precision;
alter table paper_trades add column if not exists entry_atr_pct       double precision;  -- ATR% used to size the stop/target
alter table paper_trades add column if not exists expected_rr         double precision;  -- planned reward:risk at entry
alter table paper_trades add column if not exists high_water_price    double precision;  -- best price seen since entry (trailing-stop state)
alter table paper_trades add column if not exists trail_active        boolean not null default false;
alter table paper_trades add column if not exists mfe_pct             double precision;  -- maximum favourable excursion since entry, %
alter table paper_trades add column if not exists mae_pct             double precision;  -- maximum adverse excursion since entry, %
