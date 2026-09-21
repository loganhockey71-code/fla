"""Self-learning: learn from mistakes without ever changing a model because of one of them.

  1. postmortem.run_postmortems  - explain every wrong prediction (append-only rows)
  2. patterns.update_patterns    - statistics over MANY examples; suggestions only
  3. retrain.maybe_retrain       - guarded champion/challenger retrain, promoted only if better on unseen data
"""
from . import patterns, postmortem, retrain


def _patterns_stale(db) -> bool:
    r = db.one("select (select max(evaluated_at) from prediction_results) e, (select max(updated_at) from learned_patterns) p")
    return r["e"] is not None and (r["p"] is None or r["e"] > r["p"])


def run_all(db, cfg: dict, force_retrain: bool = False, verbose: bool = False) -> dict:
    out = {}
    frames = None
    needs = db.one("""select count(*) n from prediction_results r left join post_mortems m on m.prediction_id = r.prediction_id
                      where r.signal_correct = false and m.id is null""")["n"]
    if needs or _patterns_stale(db):
        from .data import load_frames
        frames = load_frames(db, days=400)
    out["post_mortems"] = postmortem.run_postmortems(db, cfg, frames) if needs else 0
    out["patterns"] = patterns.update_patterns(db, cfg, frames) if _patterns_stale(db) else 0
    res = retrain.maybe_retrain(db, cfg, force=force_retrain)
    out["retrain"] = {f"{r['symbol']}/{r['variant']}": r["decision"] for r in res if r["decision"] != "not_due"} or "nothing due"
    if verbose:
        out["retrain_detail"] = [f"{r['symbol']}/{r['variant']}: {r['decision']} - {r['reason']}" for r in res]
    return out
