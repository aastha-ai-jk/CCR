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
    out = {}
    try:
        cl = pd.read_sql("SELECT free_lime, liter_weight, liq FROM lab_clinker "
                         "WHERE free_lime IS NOT NULL ORDER BY ts DESC LIMIT 1", eng)
        if len(cl):
            out.update(free_lime=_num(cl["free_lime"][0]),
                       liter_weight=_num(cl["liter_weight"][0]),
                       liquid=_num(cl["liq"][0]))
        doc = pd.read_sql("SELECT doc FROM lab_hotmeal WHERE doc IS NOT NULL "
                          "ORDER BY ts DESC LIMIT 1", eng)
        if len(doc):
            out["doc"] = _num(doc["doc"][0])
    except Exception:
        pass
    return {k: v for k, v in out.items() if v is not None}


def _mature(eng):
    """Fill realised temperature + error for predictions whose horizon has passed."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    due = pd.read_sql(text("SELECT rowid, horizon_ts, pred_mean FROM shadow_log "
                           "WHERE matured=0 AND horizon_ts <= :now"),
                      eng, params={"now": now})
    if not len(due):
        return 0
    n = 0
    with eng.begin() as con:
        for r in due.itertuples():
            act = con.execute(text(
                f'SELECT "{CV}" AS v FROM process_live '
                "WHERE ts >= :h ORDER BY ts ASC LIMIT 1"),
                {"h": r.horizon_ts}).fetchone()
            if act is None or act[0] is None:
                continue
            actual = float(act[0])
            con.execute(text("UPDATE shadow_log SET matured=1, actual_future=:a, "
                             "abs_error=:e WHERE rowid=:id"),
                        {"a": actual, "e": abs(actual - float(r.pred_mean)), "id": r.rowid})
            n += 1
    return n


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
        r = pd.read_sql(text(
            "SELECT actual_future - pred_mean AS resid FROM shadow_log "
            "WHERE matured=1 AND ts >= :cut"), eng, params={"cut": cut})
        n = int(len(r))
        if n < 6:
            return "afr: warming up"
        bias = float(r["resid"].mean())
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

    opt = twin_opt.optimize(conditions, n_trials=120)
    rec = opt.get("recommended", {}) or {}
    cur_coal = _num(snap.get(COAL)) or 0.0
    cur_afr = sum(_num(snap.get(t)) or 0.0 for t in AFR)
    rec_coal = _num(rec.get(COAL))
    rec_afr = sum(_num(rec.get(t)) or 0.0 for t in AFR) if rec else None
    band = opt.get("target_band", {}) or {}

    row = {
        "ts": ts, "horizon_ts": horizon_ts, "feed_gate": feed_gate,
        "actual_now": cur_temp, "pred_mean": pm["mean"], "pred_p10": pm["p10"],
        "pred_p90": pm["p90"], "state": opt.get("operating_state"),
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
    pd.DataFrame([row]).to_sql("shadow_log", eng, if_exists="append", index=False)
    afr = _afr_quality(eng, snap, ts)
    return (f"actual={cur_temp:.1f} pred(t+{HORIZON})={pm['mean']:.1f} "
            f"state={opt.get('operating_state')} rec_coal={rec_coal} "
            f"matured+{matured} {afr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true", help="single tick then exit")
    args = ap.parse_args()

    eng = _engine()
    with eng.begin() as con:
        con.execute(text(_DDL))
        con.execute(text(_DDL_AFR))
    print(f"[shadow] DB={config.database_url().split('/')[-1]} horizon={HORIZON}min "
          f"interval={args.interval}s — OBSERVE-ONLY")
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
