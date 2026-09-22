"""FastAPI backend for the Calcination Combustion Control console.

Run:  python -m uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import datetime as _dt
import json

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from backend import db, analysis
from backend import forecast as forecast_mod

app = FastAPI(title="Calcination Combustion Control API", version="0.1")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

KEY_TAGS = ["423CI400_TT_4", "467RL400_TPH", "LAFR_FT", "K3V10_F1", "261WF080_TPH",
            "467RL300_TPH", "353RL130_TPH", "423AM823_CO_PPM", "423AM823_O2",
            "423AM822_CO_PPM", "423AM822_O2", "423AM822_NO", "433MD140_TQ",
            "443KH050_TT1", "423DU415_TT", "423CN_TEMP"]


def _clean_records(df: pd.DataFrame) -> list[dict]:
    """to_dict + guarantee JSON-safe values (NaN/NaT -> None)."""
    recs = df.to_dict("records")
    for r in recs:
        for k, v in r.items():
            if v is not None and not isinstance(v, (list, dict, str, bool)):
                try:
                    if pd.isna(v):
                        r[k] = None
                except (TypeError, ValueError):
                    pass
    return recs


def _san(v):
    if v is None or isinstance(v, (str, bool)):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _snapshot(tags: list[str] | None = None) -> dict:
    """Latest process state — prefers the live OPC feed (`live_now`) when it is
    fresh (< 120 s), otherwise falls back to the newest historical row."""
    cat = db.tag_catalog()
    tags = tags or list(cat["tag"])
    try:
        lr = db.q("SELECT * FROM live_now LIMIT 1")
    except Exception:
        lr = None
    if lr is not None and len(lr):
        row = lr.iloc[0].to_dict()
        ts = row.get("ts")
        age = None
        try:
            age = (_dt.datetime.now() - _dt.datetime.fromisoformat(str(ts))).total_seconds()
        except Exception:
            pass
        if age is None or age < 120:
            # include EVERY live column (31 tags + extra cyclone temps), not
            # just the dim_tag set, so the mimic can show all cyclones live
            exclude = {"ts", "source", "feed_gate", "index"}
            vals = {k: _san(v) for k, v in row.items() if k not in exclude}
            for t in tags:
                vals.setdefault(t, None)
            return {"ts": ts, "values": vals,
                    "source": row.get("source", "opc"),
                    "age_sec": None if age is None else round(age, 1)}
    cols = ", ".join(f'"{t}"' for t in tags)
    r = db.q(f"SELECT ts, {cols} FROM process_1min ORDER BY ts DESC LIMIT 1")
    snap = _clean_records(r)[0]
    ts = snap.pop("ts", None)
    return {"ts": ts, "values": snap, "source": "historical", "age_sec": None}


@app.get("/api/health")
def health():
    n = db.q("SELECT COUNT(*) AS n FROM process_1min")["n"][0]
    rng = db.q("SELECT MIN(ts) AS a, MAX(ts) AS b FROM process_1min")
    return {"status": "ok", "backend": "sqlite" if config.USE_SQLITE else "postgres",
            "process_rows": int(n), "from": str(rng["a"][0]), "to": str(rng["b"][0])}


@app.get("/api/tags")
def tags():
    return _clean_records(db.tag_catalog())


@app.get("/api/data-quality")
def data_quality():
    return _clean_records(db.q("SELECT * FROM data_quality ORDER BY pct_valid"))


@app.get("/api/roadmap")
def roadmap():
    return _clean_records(db.q("SELECT * FROM roadmap_milestone ORDER BY ordinal"))


@app.get("/api/series")
def series(tags: str = Query(...), start: str | None = None, end: str | None = None,
           bucket: str = "5min", in_load: bool = False):
    tlist = db.safe_tags(tags.split(","))
    if not tlist:
        return {"tags": [], "points": []}
    cols = ", ".join(f'"{t}"' for t in tlist)
    where, params = [], {}
    if in_load:
        where.append("feed_gate=1")
    if start:
        where.append("ts >= :start"); params["start"] = start
    if end:
        where.append("ts <= :end"); params["end"] = end
    w = ("WHERE " + " AND ".join(where)) if where else ""
    df = db.q(f"SELECT ts, {cols} FROM process_1min {w} ORDER BY ts", params)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").apply(pd.to_numeric, errors="coerce")
    if bucket and bucket != "raw":
        df = df.resample(bucket).mean()
    df = df.round(2).reset_index()
    df["ts"] = df["ts"].dt.strftime("%Y-%m-%d %H:%M")
    return {"tags": tlist, "bucket": bucket, "points": _clean_records(df)}


@app.get("/api/correlation")
def correlation(tags: str, start: str | None = None, end: str | None = None,
                bucket: str = "15min"):
    return analysis.correlation(tags.split(","), start, end, bucket)


@app.get("/api/crosscorr")
def crosscorr(a: str, b: str, start: str | None = None, end: str | None = None,
              max_lag_min: int = 30):
    return analysis.cross_correlation(a, b, start, end, max_lag_min)


@app.get("/api/lab/hotmeal")
def lab_hotmeal(limit: int = 500):
    return _clean_records(db.q(
        "SELECT * FROM lab_hotmeal ORDER BY ts DESC LIMIT :n", {"n": limit}))


@app.get("/api/lab/clinker")
def lab_clinker(limit: int = 500):
    return _clean_records(db.q(
        "SELECT * FROM lab_clinker ORDER BY ts DESC LIMIT :n", {"n": limit}))


def _lab_conditions() -> dict:
    """Latest lab quality values that vote the operating-state band."""
    lime = db.q("SELECT free_lime, liter_weight, liq FROM lab_clinker "
                "WHERE free_lime IS NOT NULL ORDER BY ts DESC LIMIT 1")
    doc = db.q("SELECT doc FROM lab_hotmeal WHERE doc IS NOT NULL ORDER BY ts DESC LIMIT 1")
    conditions: dict = {}
    if len(lime):
        conditions.update({"free_lime": float(lime["free_lime"][0]),
                           "liter_weight": float(lime["liter_weight"][0]),
                           "liquid": float(lime["liq"][0])})
    if len(doc):
        conditions["doc"] = float(doc["doc"][0])
    return conditions


@app.get("/api/overview")
def overview():
    s = _snapshot()
    snap = dict(s["values"])
    snap["ts"] = s["ts"]
    conditions = _lab_conditions()
    # PLC state-setting vars + current temp so the band/state votes use live data
    conditions["current_temp"] = snap.get("423CI400_TT_4")
    for t in analysis.STATE_PLC:
        if snap.get(t) is not None:
            conditions[t] = snap[t]

    # actual (not predicted) interlock status from the latest snapshot
    names = db.tag_names()
    interlocks = []
    for tag, (op, lim) in config.INTERLOCKS.items():
        pv = snap.get(tag)
        ok = None if pv is None else (pv < lim if op == "<" else pv > lim)
        interlocks.append({"tag": tag, "name": names.get(tag, tag),
                           "rule": f"{op} {lim}", "value": pv, "pass": ok})

    levers = {k: snap.get(k) for k in analysis.FEATURES if snap.get(k) is not None}
    state = analysis.simulate(levers, conditions)
    return {"snapshot": snap, "conditions": conditions,
            "operating_state": state["operating_state"],
            "target_band": state["target_band"], "state_votes": state["state_votes"],
            "interlocks": interlocks, "source": s["source"], "age_sec": s["age_sec"]}


@app.get("/api/latest")
def latest():
    """Latest value of every tag — feeds the live process mimic. Uses the live
    OPC feed when available, else the newest historical row."""
    cat = db.tag_catalog()
    s = _snapshot()
    units = {t: (None if pd.isna(u) else str(u)) for t, u in zip(cat["tag"], cat["uom"])}
    return {"ts": s["ts"], "values": s["values"], "names": db.tag_names(),
            "units": units, "source": s["source"], "age_sec": s["age_sec"]}


@app.get("/api/live")
def live():
    """Alias of /api/latest kept explicit for the live dashboards."""
    return latest()


@app.get("/api/trend")
def trend(tag: str = "423CI400_TT_4", minutes: int = 60, max_points: int = 300):
    """Rolling trend for one tag. Streams from the live OPC feed (`process_live`)
    when it has data, otherwise falls back to the newest historical window.

    Live and historical are never mixed: the two are ~2 weeks apart, so joining
    them would draw a misleading straight line across the gap.
    """
    tag = (db.safe_tags([tag]) or ["423CI400_TT_4"])[0]
    src, df = "live", None
    try:                                    # ~2 s cadence -> 30 rows/min, allow margin
        df = db.q(f'SELECT ts, "{tag}" AS v FROM process_live '
                  f'ORDER BY ts DESC LIMIT :n', {"n": int(minutes * 45)})
        df = df.dropna(subset=["v"]) if df is not None else None
    except Exception:
        df = None
    if df is None or len(df) < 2:
        src = "historical"
        df = db.q(f'SELECT ts, "{tag}" AS v FROM process_1min '
                  f'ORDER BY ts DESC LIMIT :n', {"n": int(minutes)})
        df = df.dropna(subset=["v"])

    if not len(df):
        return {"tag": tag, "source": src, "points": []}

    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts")
    df = df[df["ts"] >= df["ts"].max() - pd.Timedelta(minutes=minutes)]
    if len(df) > max_points:                # even downsample, always keep the last point
        step = int(np.ceil(len(df) / max_points))
        df = pd.concat([df.iloc[::step], df.iloc[[-1]]]).drop_duplicates(subset=["ts"])
    df["v"] = pd.to_numeric(df["v"], errors="coerce").round(2)
    fmt = "%H:%M" if src == "live" else "%d-%b %H:%M"
    return {
        "tag": tag, "name": db.tag_names().get(tag, tag), "source": src,
        "window_min": minutes,
        "from": df["ts"].min().strftime("%Y-%m-%d %H:%M:%S"),
        "to": df["ts"].max().strftime("%Y-%m-%d %H:%M:%S"),
        "points": [{"t": t.strftime(fmt), "v": (None if pd.isna(v) else float(v))}
                   for t, v in zip(df["ts"], df["v"])],
    }


@app.get("/api/advisory")
def advisory():
    """Live fuel-saving advisory for the CURRENT plant state: the cheapest
    in-band, interlock-safe fuel mix within one guarded move, plus the saving
    that would represent. Estimate of potential — see `assumptions`."""
    s = _snapshot()
    snap = s["values"]
    conditions = _lab_conditions()
    conditions["current_temp"] = snap.get("423CI400_TT_4")
    for t in analysis.STATE_PLC:
        if snap.get(t) is not None:
            conditions[t] = snap[t]
    levers = {k: snap[k] for k in analysis.MV if snap.get(k) is not None}

    now = analysis.simulate(levers, conditions)
    adv = analysis.advise(levers, conditions)
    adv["ts"] = s["ts"]
    adv["source"] = s["source"]
    p = now["prediction"]["423CI400_TT_4"]
    adv["baseline"] = {
        "predicted_temp": p["mean"], "p10": p["p10"], "p90": p["p90"],
        "temp_in_band": now["temp_in_band"],
        "all_interlocks_ok": now["all_interlocks_ok"],
        "horizon_min": now["horizon_min"],
    }
    adv["live_actual_temp"] = snap.get("423CI400_TT_4")
    if config.COAL_PRICE_PER_T and adv.get("feasible"):
        sv = adv["saving"]
        adv["saving"]["cost_per_year"] = round(
            sv["coal_tpy"] * config.COAL_PRICE_PER_T, 0)
        adv["saving"]["currency"] = config.CURRENCY
    # per-parameter energy + money breakdown
    adv["breakdown"] = analysis.savings_breakdown(
        adv, snap, days_per_year=adv["assumptions"]["operating_days_per_year"])

    # "Locked" decline: if the operator's last decision for this exact
    # situation (same operating state + same side of the band) was a
    # decline, resurface their own reasoning instead of silently repeating
    # the same suggestion as if it were new.
    band = adv.get("target_band")
    temp = adv.get("live_actual_temp")
    if band and temp is not None:
        direction = "above" if temp > band["high"] else "below" if temp < band["low"] else "within"
        last = db.latest_decision_for_situation(adv["operating_state"], direction)
        if last is not None and last.get("decision") == "declined":
            adv["locked_decline"] = {
                "ts": last.get("ts"),
                "direction": direction,
                "note": _san(last.get("note")),
                "live_actual_temp": _san(last.get("live_actual_temp")),
                "predicted_temp": _san(last.get("predicted_temp")),
            }
    return adv


class AdvisoryDecision(BaseModel):
    decision: str                      # "accepted" | "declined"
    operating_state: str | None = None
    mode: str | None = None
    target_band_low: float | None = None
    target_band_high: float | None = None
    live_actual_temp: float | None = None
    predicted_temp: float | None = None
    moves: list = []
    reasoning: str | None = None
    note: str | None = None            # optional free-text, e.g. decline reason


@app.post("/api/advisory/decision")
def advisory_decision(req: AdvisoryDecision):
    """Log the operator's accept/decline of a shown recommendation, together
    with the reasoning as displayed. Advisory-only: this never writes to any
    control system, it only records what the operator chose to do."""
    if req.decision not in ("accepted", "declined"):
        raise HTTPException(400, "decision must be 'accepted' or 'declined'")
    db.log_advisory_decision({
        "ts": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision": req.decision,
        "operating_state": req.operating_state,
        "mode": req.mode,
        "target_band_low": req.target_band_low,
        "target_band_high": req.target_band_high,
        "live_actual_temp": req.live_actual_temp,
        "predicted_temp": req.predicted_temp,
        "moves_json": json.dumps(req.moves),
        "reasoning": req.reasoning,
        "note": req.note,
    })
    return {"ok": True}


@app.get("/api/savings")
def savings():
    """Per-parameter fuel / energy / cost effect of following the live advisory."""
    a = advisory()
    return {"ts": a.get("ts"), "source": a.get("source"), "mode": a.get("mode"),
            "moves": a.get("moves", []), "breakdown": a.get("breakdown")}


@app.get("/api/simulate/defaults")
def sim_defaults():
    cat = db.tag_catalog().set_index("tag")
    out = {}
    for f in analysis.FEATURES:
        out[f] = {"name": str(cat.loc[f, "name"]), "uom": cat.loc[f, "uom"],
                  "min": float(cat.loc[f, "y_min"]) if pd.notna(cat.loc[f, "y_min"]) else 0,
                  "max": float(cat.loc[f, "y_max"]) if pd.notna(cat.loc[f, "y_max"]) else 100,
                  "default": round(float(analysis.feature_defaults()[f]), 2)}
    return {"features": out, "targets": analysis.TARGETS}


class SimReq(BaseModel):
    levers: dict = {}
    conditions: dict = {}
    k: int = 200


@app.post("/api/simulate")
def simulate(req: SimReq):
    return analysis.simulate(req.levers, req.conditions, req.k)


@app.get("/api/forecast")
def forecast(minutes_back: int = Query(20, ge=6, le=180)):
    """ML twin: calciner temperature 60 s ahead, from REAL recent trailing
    history (not a what-if) -- see backend/forecast.py. Held-out test MAE
    5.73 degC. Needs `python -m etl.live_feed` (or --replay) populating
    process_live; returns available=False with a reason if there isn't
    enough recent buffered history yet."""
    return forecast_mod.forecast_from_live_buffer(minutes_back)
