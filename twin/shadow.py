"""Shadow-mode runner: the digital twin runs BESIDE the live CCR.

Every ``--interval`` seconds it reads the current live snapshot (written by
``etl.live_feed`` into ``live_now``), asks the twin to (a) predict the calciner
outlet temperature ``HORIZON`` minutes ahead and (b) recommend optimal setpoints,
and logs both to ``shadow_log`` — WITHOUT ever writing to the plant. Once a
prediction's horizon has elapsed it is *matured*: the realised temperature is
looked up from the live history and the absolute error recorded, giving a
running, live measure of how well the twin tracks reality.

ADVISORY / OBSERVE-ONLY. Nothing here actuates anything.

    python -m twin.shadow --interval 30
"""
from __future__ import annotations

import argparse
import time
import datetime as dt

import pandas as pd
from sqlalchemy import create_engine, text

import config
from backend import analysis
from twin import model as twin_model
from twin import optimize as twin_opt

CV = analysis.CV
COAL = "467RL400_TPH"
AFR = ["LAFR_FT", "K3V10_F1", "261WF080_TPH"]
HORIZON = analysis.DELTA  # minutes ahead the twin predicts (== 5)
# P8: the advisor works at T+10, so its prediction is logged and checked too
ADV_H = twin_opt.ADVISOR_HORIZON
_COLS10 = {"horizon10_ts": "TEXT", "pred10_mean": "REAL", "pred10_p10": "REAL",
           "pred10_p90": "REAL", "matured10": "INTEGER DEFAULT 0",
           "actual10": "REAL", "abs_error10": "REAL"}

_DDL_AFR = """
CREATE TABLE IF NOT EXISTS afr_quality (
  ts TEXT, bias_2h REAL, n_matured_2h INTEGER, afr_share REAL, quality TEXT
)
"""

_DDL = """
CREATE TABLE IF NOT EXISTS shadow_log (
  ts TEXT, horizon_ts TEXT, feed_gate INTEGER,
  actual_now REAL, pred_mean REAL, pred_p10 REAL, pred_p90 REAL,
  state TEXT, band_lo REAL, band_hi REAL, in_band_now INTEGER,
  cur_coal REAL, cur_afr REAL, rec_coal REAL, rec_afr REAL,
  delta_coal REAL, delta_afr REAL, rec_in_band INTEGER, interlocks_ok INTEGER,
  matured INTEGER DEFAULT 0, actual_future REAL, abs_error REAL
)
"""


def _engine():
    url = config.database_url()
    kw = {"connect_args": {"timeout": 15}} if url.startswith("sqlite") else {}
    return create_engine(url, **kw)


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _live_snapshot(eng) -> dict | None:
    try:
        df = pd.read_sql("SELECT * FROM live_now", eng)
    except Exception:
        return None
    return df.iloc[0].to_dict() if len(df) else None


def _temp_trend(eng, now_temp: float) -> float:
    """current CV minus CV ~HORIZON min ago, from the live history (else 0)."""
    try:
        past = pd.read_sql(text(
            f'SELECT "{CV}" AS v FROM process_live '
            "WHERE ts <= :cut ORDER BY ts DESC LIMIT 1"),
            eng, params={"cut": (dt.datetime.now() - dt.timedelta(minutes=HORIZON)
                                 ).isoformat(timespec="seconds")})
        if len(past) and pd.notna(past["v"][0]):
            return float(now_temp) - float(past["v"][0])
    except Exception:
        pass
    return 0.0


def _lab_conditions(eng) -> dict:
    """Fresh lab values only (live eLIMS first) — same rule as the advisor."""
    try:
        from etl.lab_live import latest_lab
        cond, _ = latest_lab(lambda sql: pd.read_sql(sql, eng))
        return {k: v for k, v in cond.items() if v is not None}
    except Exception:
        return {}


# A prediction is graded only against a reading within ACTUAL_TOL_S of its
# target time. Before 27 Oct the first reading AFTER the target was taken, so
# across a feed gap a forecast was graded against a reading hours later (live
# T+10 error showed 15 degC and the drift guard blocked every Accept). No
# reading near the target once GRADE_WAIT_S has passed -> matured = -1
# ("no reading"), left out of every accuracy figure.
ACTUAL_TOL_S = 60
GRADE_WAIT_S = 120


def _actual_at(con, target: str) -> float | None:
    """The live calciner temp nearest `target` within +-ACTUAL_TOL_S, or None."""
    t = dt.datetime.fromisoformat(str(target))
    lo = (t - dt.timedelta(seconds=ACTUAL_TOL_S)).isoformat(timespec="seconds")
    hi = (t + dt.timedelta(seconds=ACTUAL_TOL_S)).isoformat(timespec="seconds")
    rows = con.execute(text(f'SELECT ts, "{CV}" FROM process_live '
                            "WHERE ts >= :lo AND ts <= :hi AND "
                            f'"{CV}" IS NOT NULL'), {"lo": lo, "hi": hi}).fetchall()
    if not rows:
        return None
    best = min(rows, key=lambda r: abs((dt.datetime.fromisoformat(str(r[0])) - t).total_seconds()))
    return float(best[1])


def _grade(eng, ts_col: str, pred_col: str, flag: str, act_col: str, err_col: str) -> int:
    now = dt.datetime.now()
    due = pd.read_sql(text(f"SELECT rowid, {ts_col} AS h, {pred_col} AS p FROM shadow_log "
                           f"WHERE COALESCE({flag},0)=0 AND {pred_col} IS NOT NULL "
                           f"AND {ts_col} <= :now"),
                      eng, params={"now": now.isoformat(timespec="seconds")})
    n = 0
    with eng.begin() as con:
        for r in due.itertuples():
            actual = _actual_at(con, r.h)
            if actual is None:
                if (now - dt.datetime.fromisoformat(str(r.h))).total_seconds() > GRADE_WAIT_S:
                    con.execute(text(f"UPDATE shadow_log SET {flag}=-1, {act_col}=NULL, "
                                     f"{err_col}=NULL WHERE rowid=:id"), {"id": r.rowid})
                continue
            con.execute(text(f"UPDATE shadow_log SET {flag}=1, {act_col}=:a, {err_col}=:e "
                             "WHERE rowid=:id"),
                        {"a": actual, "e": abs(actual - float(r.p)), "id": r.rowid})
            n += 1
    return n


def _mature(eng):
    """Fill realised temperature + error for predictions whose horizon has passed."""
    return _grade(eng, "horizon_ts", "pred_mean", "matured", "actual_future", "abs_error")


def _mature10(eng):
    """Same as _mature for the advisor's T+10 prediction (P8)."""
    return _grade(eng, "horizon10_ts", "pred10_mean", "matured10", "actual10", "abs_error10")


def regrade(eng) -> dict:
    """Re-grade every past prediction with the +-ACTUAL_TOL_S rule (27 Oct fix)."""
    with eng.begin() as con:
        con.execute(text("UPDATE shadow_log SET matured=0 WHERE matured IN (1, -1)"))
        con.execute(text("UPDATE shadow_log SET matured10=0 WHERE matured10 IN (1, -1)"))
    _mature(eng), _mature10(eng)
    with eng.begin() as con:
        c = dict(con.execute(text(
            "SELECT SUM(matured=1), SUM(matured=-1), SUM(matured10=1), SUM(matured10=-1) "
            "FROM shadow_log")).fetchone()._mapping)
    return dict(zip(("t5_graded", "t5_no_reading", "t10_graded", "t10_no_reading"), c.values()))


def _afr_quality(eng, snap: dict, ts: str) -> str:
    """Online AFR calorific-quality inference (observe-only insight).

    The twin predicts the 5-min temperature response FROM the actual lever
    positions. A sustained signed residual (actual − predicted) therefore is
    thermal effect the fuels did NOT deliver (or over-delivered) versus what
    the model learned as normal — with the alternate-fuel streams being the
    dominant unmeasured-quality input. Rolling 2-h mean residual + the current
    AFR share of firing = a live "fuel running hot/cold" indicator.
    """
    try:
        cut = (dt.datetime.now() - dt.timedelta(hours=2)).isoformat(timespec="seconds")
        # residuals vs the RAW model output (pred_raw), so the serving-side
        # offset correction cannot cancel this signal out of its own inputs
        r = pd.read_sql(text(
            "SELECT actual_future - COALESCE(pred_raw, pred_mean) AS resid "
            "FROM shadow_log WHERE matured=1 AND ts >= :cut"),
            eng, params={"cut": cut})
        n = int(len(r))
        if n < 6:
            return "afr: warming up"
        bias = float(r["resid"].mean())
        # publish the rolling offset for the serving layer (offset-free correction)
        import json as _json
        from pathlib import Path as _Path
        (_Path(__file__).resolve().parent.parent / "models" / "live_bias.json"
         ).write_text(_json.dumps({"bias_2h": round(bias, 3), "n": n,
                                   "updated": ts}))
        coal = _num(snap.get(COAL)) or 0.0
        afr_tph = ((_num(snap.get("261WF080_TPH")) or 0.0)
                   + ((_num(snap.get("LAFR_FT")) or 0.0)
                      + (_num(snap.get("K3V10_F1")) or 0.0)) / 1000.0)
        share = afr_tph / max(afr_tph + coal, 1e-6)
        quality = ("fuel_hot" if bias > 2.0 else
                   "fuel_cold" if bias < -2.0 else "normal")
        pd.DataFrame([{"ts": ts, "bias_2h": round(bias, 2), "n_matured_2h": n,
                       "afr_share": round(share, 3), "quality": quality}]
                     ).to_sql("afr_quality", eng, if_exists="append", index=False)
        return f"afr_bias2h={bias:+.1f}C({quality},share={share:.0%})"
    except Exception as e:
        return f"afr err {type(e).__name__}"


def tick(eng) -> str:
    snap = _live_snapshot(eng)
    if not snap:
        return "no live snapshot yet"
    now = dt.datetime.now()
    ts = now.isoformat(timespec="seconds")
    horizon_ts = (now + dt.timedelta(minutes=HORIZON)).isoformat(timespec="seconds")
    feed_gate = int(bool(snap.get("feed_gate")))
    cur_temp = _num(snap.get(CV))

    matured = _mature(eng)
    try:
        matured += _mature10(eng)
    except Exception:
        pass                                  # older table without the T+10 columns
    if cur_temp is None:
        return f"no calciner temp in snapshot (matured {matured})"

    conditions = {"current_temp": cur_temp, "temp_trend": _temp_trend(eng, cur_temp)}
    for t in analysis.STATE_PLC:
        if _num(snap.get(t)) is not None:
            conditions[t] = _num(snap.get(t))
    # v2 twin context: hot-side temps + secondary-air/PH-outlet from the live snapshot
    try:
        from twin.features import SERVE_CONTEXT_TAGS
        for t in SERVE_CONTEXT_TAGS:
            if _num(snap.get(t)) is not None:
                conditions[t] = _num(snap.get(t))
    except Exception:
        pass
    conditions.update(_lab_conditions(eng))
    levers = {mv: _num(snap.get(mv)) for mv in analysis.MV if _num(snap.get(mv)) is not None}

    core = twin_model.predict_core(levers, conditions)
    if core is None:
        return f"twin unavailable (matured {matured})"
    pm = core["prediction"][CV]

    # pass the CURRENT lever setpoints so the optimizer's trust region centres
    # on real operation (not historical medians)
    opt = twin_opt.optimize({**conditions, **levers}, n_trials=120)
    rec = opt.get("recommended", {}) or {}
    cur_coal = _num(snap.get(COAL)) or 0.0
    cur_afr = sum(_num(snap.get(t)) or 0.0 for t in AFR)
    rec_coal = _num(rec.get(COAL))
    rec_afr = sum(_num(rec.get(t)) or 0.0 for t in AFR) if rec else None
    band = opt.get("target_band", {}) or {}

    row = {
        "ts": ts, "horizon_ts": horizon_ts, "feed_gate": feed_gate,
        "actual_now": cur_temp, "pred_mean": pm["mean"], "pred_p10": pm["p10"],
        "pred_p90": pm["p90"],
        # raw (pre-offset-correction) prediction — bias/AFR estimation input
        "pred_raw": round(pm["mean"] - float(core.get("bias_correction") or 0.0), 2),
        "state": opt.get("operating_state"),
        "band_lo": band.get("low"), "band_hi": band.get("high"),
        "in_band_now": int(bool(band.get("low") is not None
                                and band["low"] <= cur_temp <= band["high"])),
        "cur_coal": cur_coal, "cur_afr": cur_afr,
        "rec_coal": rec_coal, "rec_afr": rec_afr,
        "delta_coal": None if rec_coal is None else rec_coal - cur_coal,
        "delta_afr": None if rec_afr is None else rec_afr - cur_afr,
        "rec_in_band": int(bool(opt.get("in_band"))),
        "interlocks_ok": int(all(i.get("pass") for i in opt.get("interlocks", [])
                                 if i.get("pass") is not None)),
        "matured": 0, "actual_future": None, "abs_error": None,
    }
    # P8: the T+10 point of the same trajectory (the advisor's horizon)
    p10 = next((p for p in (core.get("trajectory") or []) if p.get("min") == ADV_H), None)
    if p10 is not None:
        row.update(horizon10_ts=(now + dt.timedelta(minutes=ADV_H)).isoformat(timespec="seconds"),
                   pred10_mean=p10["mean"], pred10_p10=p10.get("p10"), pred10_p90=p10.get("p90"),
                   matured10=0, actual10=None, abs_error10=None)
    pd.DataFrame([row]).to_sql("shadow_log", eng, if_exists="append", index=False)
    afr = _afr_quality(eng, snap, ts)
    return (f"actual={cur_temp:.1f} pred(t+{HORIZON})={pm['mean']:.1f} "
            f"state={opt.get('operating_state')} rec_coal={rec_coal} "
            f"matured+{matured} {afr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true", help="single tick then exit")
    ap.add_argument("--regrade", action="store_true",
                    help="re-grade all past predictions (+-60 s rule) then exit")
    args = ap.parse_args()

    eng = _engine()
    with eng.begin() as con:
        con.execute(text(_DDL))
        con.execute(text(_DDL_AFR))
        try:                      # migration: raw-prediction column (2026-08-06)
            con.execute(text("ALTER TABLE shadow_log ADD COLUMN pred_raw REAL"))
        except Exception:
            pass                  # already present
        for col, typ in _COLS10.items():     # migration: T+10 tracking (P8, 2026-10-26)
            try:
                con.execute(text(f"ALTER TABLE shadow_log ADD COLUMN {col} {typ}"))
            except Exception:
                pass
    try:                          # grading looks up readings by time: index once (27 Oct)
        with eng.begin() as con:
            con.execute(text("CREATE INDEX IF NOT EXISTS ix_process_live_ts ON process_live (ts)"))
    except Exception:
        pass                      # no process_live yet (no live feed on this install)
    print(f"[shadow] DB={config.database_url().split('/')[-1]} horizon={HORIZON}min "
          f"interval={args.interval}s — OBSERVE-ONLY")
    if args.regrade:
        print("[shadow] regrade", regrade(eng)); return
    if args.once:
        print("[shadow]", tick(eng)); return
    while True:
        try:
            print(f"[{dt.datetime.now():%H:%M:%S}]", tick(eng))
        except KeyboardInterrupt:
            print("\n[shadow] stopped"); break
        except Exception as e:  # never die on a transient DB lock / read
            print(f"[shadow] tick error: {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
