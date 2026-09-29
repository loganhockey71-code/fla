-- Adds a 1-hour prediction horizon alongside the original 24h/48h ones. It reuses every existing table and
-- code path (predictions, evaluator, paper trading, metrics) - the only actual schema change needed is widening
-- one CHECK constraint. Feeds autopilot's "standing" (non-sudden) read on a ~15-minute cadence instead of 6h,
-- so it can react roughly every 5-minute tick instead of a handful of times a day. Run this once.
alter table predictions drop constraint if exists predictions_horizon_h_check;
alter table predictions add constraint predictions_horizon_h_check check (horizon_h in (1,24,48));
