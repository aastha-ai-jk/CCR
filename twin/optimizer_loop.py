"""Continuous fuel-advisory runner — the CCR fuel optimiser running BESIDE the
live plant, the sibling of ``twin.shadow``.

Every ``--interval`` seconds it reads the live snapshot, runs the economic
setpoint optimiser (``twin.optimize`` — min ₹/ton | max TSR | min coal, subject
to the temperature band, interlocks, AND the clinker-quality gate), and logs the
advisory plus its predicted coal/₹/TSR savings to ``optimizer_log`` — WITHOUT
ever writing to the plant.

Measurement & Verification: once ~``MV_LAG_MIN`` minutes have passed, each row is
*matured* — we look at what the operator's setpoints ACTUALLY became and record
whether they moved toward the advice (``followed``) and the realised coal/₹/TSR
change vs the baseline at issue time. Followed rows are the honest savings trail;
unfollowed rows are observational.

ADVISORY / OBSERVE-ONLY. Nothing here actuates anything.

    python -m twin.optimizer_loop --interval 45
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time

import pandas as pd
from sqlalchemy import text

import config
from backend import analysis
from twin import economics
from twin import optimize as twin_opt
# reuse the shadow runner's live-snapshot / trend / lab / engine helpers verbatim
from twin.shadow import (_engine, _lab_conditions, _live_snapshot, _num,
                         _temp_trend)

CV = analysis.CV
COAL = economics.COAL
AFR = ["LAFR_FT", "K3V10_F1", "261WF080_TPH"]
MV_LAG_MIN = 8.0        # minutes an operator has to react before we mature a row
FOLLOW_COAL_TPH = 0.5   # min coal move (tph) in the advised direction = "followed"
N_TRIALS = 80         # ±15% trust region converges well under 80; halves tick CPU

_DDL = """
CREATE TABLE IF NOT EXISTS optimizer_log (
  ts TEXT, feed_gate INTEGER, mode TEXT,
  state TEXT, band_lo REAL, band_hi REAL, in_band_now INTEGER,
  cur_coal REAL, cur_afr REAL, rec_coal REAL, rec_afr REAL,
  cur_cost REAL, rec_cost REAL, save_cost REAL,
  cur_tsr REAL, rec_tsr REAL, save_tsr REAL,
  cur_spheat REAL, rec_spheat REAL,
  pred_temp REAL, rec_in_band INTEGER, interlocks_ok INTEGER, quality_ok INTEGER,
  pred_free_lime REAL, pred_doc REAL, is_estimate INTEGER,
  sensitivities TEXT,
  followed INTEGER, matured INTEGER DEFAULT 0,
  realized_coal REAL, realized_cost REAL, realized_tsr REAL
)
"""


def _build_conditions(eng, snap: dict) -> tuple[dict, dict, float] | None:
    """conditions + current levers + current temp, exactly as twin.shadow builds
    them for the twin/optimizer (so the trust region centres on real operation)."""
    cur_temp = _num(snap.get(CV))
    if cur_temp is None:
        return None
    conditions = {"current_temp": cur_temp, "temp_trend": _temp_trend(eng, cur_temp)}
    for t in analysis.STATE_PLC:
        if _num(snap.get(t)) is not None:
            conditions[t] = _num(snap.get(t))
    try:
        from twin.features import SERVE_CONTEXT_TAGS
        for t in SERVE_CONTEXT_TAGS:
            if _num(snap.get(t)) is not None:
                conditions[t] = _num(snap.get(t))
    except Exception:
        pass
    conditions.update(_lab_conditions(eng))
    levers = {mv: _num(snap.get(mv)) for mv in analysis.MV if _num(snap.get(mv)) is not None}
    return conditions, levers, cur_temp


def _later_levers(eng, ts_iso: str, lag_min: float) -> dict | None:
    """The operator's actual fuel setpoints ~lag_min after an advisory was issued
    (the nearest process_live row at or after ts + lag)."""
    cut = (dt.datetime.fromisoformat(ts_iso) + dt.timedelta(minutes=lag_min)
           ).isoformat(timespec="seconds")
    cols = ", ".join(f'"{t}"' for t in [COAL, *AFR, config.KILN_FEED_TAG])
    r = pd.read_sql(text(f"SELECT {cols} FROM process_live WHERE ts >= :c "
                         "ORDER BY ts ASC LIMIT 1"), eng, params={"c": cut})
    if not len(r):
        return None
    return {c: _num(r.iloc[0][c]) for c in r.columns}


def _mature(eng) -> int:
    """M&V: for advisories past their reaction lag, record whether the operator
    followed and the realised coal/₹/TSR change vs the baseline at issue time."""
    now = dt.datetime.now()
    cutoff = (now - dt.timedelta(minutes=MV_LAG_MIN)).isoformat(timespec="seconds")
    due = pd.read_sql(text(
        "SELECT rowid, ts, cur_coal, cur_cost, cur_tsr, rec_coal FROM optimizer_log "
        "WHERE matured=0 AND ts <= :c"), eng, params={"c": cutoff})
    if not len(due):
        return 0
    econ = config.fuel_economics()
    n = 0
    with eng.begin() as con:
        for r in due.itertuples():
            later = _later_levers(eng, r.ts, MV_LAG_MIN)
            if later is None or later.get(COAL) is None:
                continue
            feed = _robust_feed(later.get(config.KILN_FEED_TAG))
            later_kpis = economics.kpis(later, {config.KILN_FEED_TAG: feed}, econ)
            # followed = operator moved coal in the advised direction, meaningfully
            advised = (None if r.rec_coal is None
                       else (later[COAL] - r.cur_coal) * (r.rec_coal - r.cur_coal))
            followed = int(advised is not None and advised > 0
                           and abs(later[COAL] - r.cur_coal) >= FOLLOW_COAL_TPH)
            con.execute(text(
                "UPDATE optimizer_log SET matured=1, followed=:f, "
                "realized_coal=:rc, realized_cost=:rr, realized_tsr=:rt WHERE rowid=:id"),
                {"f": followed,
                 "rc": round(r.cur_coal - later[COAL], 2),           # +ve = coal down
                 "rr": round((r.cur_cost or 0) - later_kpis["cost_per_ton"], 1),
                 "rt": round(later_kpis["tsr_pct"] - (r.cur_tsr or 0), 2),
                 "id": r.rowid})
            n += 1
    return n


def _robust_feed(raw) -> float:
    """Kiln feed for cost/clinker normalisation. Below the feed gate the plant
    isn't making normal clinker and ₹/ton is ill-defined, so fall back to the
    training-median feed — stops the clinker-rate floor from exploding cost when
    the feed tag reads null/low (was producing ₹160k/ton garbage on such ticks)."""
    f = _num(raw)
    if f is not None and f >= config.KILN_FEED_MIN_TPH:
        return f
    try:
        med = analysis.feature_defaults().get(config.KILN_FEED_TAG)
        if med:
            return float(med)
    except Exception:
        pass
    return 480.0


def tick(eng) -> str:
    snap = _live_snapshot(eng)
    if not snap:
        return "no live snapshot yet"
    ts = dt.datetime.now().isoformat(timespec="seconds")
    matured = _mature(eng)

    built = _build_conditions(eng, snap)
    if built is None:
        return f"no calciner temp in snapshot (matured {matured})"
    conditions, levers, cur_temp = built

    econ = config.fuel_economics()
    feed = _robust_feed(snap.get(config.KILN_FEED_TAG))
    cur = economics.kpis(levers, {config.KILN_FEED_TAG: feed}, econ)

    # Pin the SAME feed basis into the optimiser's conditions so its recommended
    # KPIs share the clinker denominator with `cur` — savings stay comparable.
    opt = twin_opt.optimize({**conditions, **levers, config.KILN_FEED_TAG: feed}, n_trials=N_TRIALS)
    rec = opt.get("recommended") or {}
    if not rec:
        return f"optimiser: {opt.get('error', 'no recommendation')} (matured {matured})"
    e = opt["economics"]; rk = e["recommended"]; sv = e["savings"]
    band = opt.get("target_band", {}) or {}
    q = (opt.get("quality") or {}).get("predicted") or {}
    interlocks_ok = int(all(i.get("pass") for i in opt.get("interlocks", [])
                            if i.get("pass") is not None))
    quality_ok = int(twin_opt._quality_ok(q))

    row = {
        "ts": ts, "feed_gate": int(bool(snap.get("feed_gate"))), "mode": opt.get("mode"),
        "state": opt.get("operating_state"),
        "band_lo": band.get("low"), "band_hi": band.get("high"),
        "in_band_now": int(bool(band.get("low") is not None
                                and band["low"] <= cur_temp <= band["high"])),
        "cur_coal": cur["coal_tph"], "cur_afr": cur["afr_total"],
        "rec_coal": rk["coal_tph"], "rec_afr": rk["afr_total"],
        "cur_cost": cur["cost_per_ton"], "rec_cost": rk["cost_per_ton"],
        "save_cost": sv["cost_per_ton"],
        "cur_tsr": cur["tsr_pct"], "rec_tsr": rk["tsr_pct"], "save_tsr": sv["tsr_pts"],
        "cur_spheat": cur["specific_heat_kcal_kg"], "rec_spheat": rk["specific_heat_kcal_kg"],
        "pred_temp": (opt.get("predicted", {}).get("temp", {}) or {}).get("mean"),
        "rec_in_band": int(bool(opt.get("in_band"))),
        "interlocks_ok": interlocks_ok, "quality_ok": quality_ok,
        "pred_free_lime": q.get("free_lime"), "pred_doc": q.get("doc"),
        "is_estimate": int(bool(rk.get("is_estimate"))),
        "sensitivities": json.dumps(opt.get("sensitivities") or {}),
        "followed": None, "matured": 0,
        "realized_coal": None, "realized_cost": None, "realized_tsr": None,
    }
    pd.DataFrame([row]).to_sql("optimizer_log", eng, if_exists="append", index=False)
    return (f"mode={opt.get('mode')} state={opt.get('operating_state')} "
            f"coal {cur['coal_tph']}->{rk['coal_tph']} "
            f"cost {cur['cost_per_ton']}->{rk['cost_per_ton']} (save {sv['cost_per_ton']}) "
            f"TSR {rk['tsr_pct']}% q_ok={quality_ok} matured+{matured}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=45.0)
    ap.add_argument("--once", action="store_true", help="single tick then exit")
    args = ap.parse_args()
    eng = _engine()
    with eng.begin() as con:
        con.execute(text(_DDL))
        try:                      # migration: sensitivities column
            con.execute(text("ALTER TABLE optimizer_log ADD COLUMN sensitivities TEXT"))
        except Exception:
            pass                  # already present
    print(f"[optimizer] DB={config.database_url().split('/')[-1]} "
          f"mode={config.OPTIMIZER_MODE} interval={args.interval}s — ADVISORY/OBSERVE-ONLY")
    if args.once:
        print("[optimizer]", tick(eng)); return
    while True:
        try:
            print(f"[{dt.datetime.now():%H:%M:%S}]", tick(eng))
        except KeyboardInterrupt:
            print("\n[optimizer] stopped"); break
        except Exception as e:
            print(f"[optimizer] tick error: {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
