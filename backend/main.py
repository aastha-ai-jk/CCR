"""FastAPI backend for the Calcination Combustion Control console.

Run:  python -m uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from backend import db, analysis

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


@app.get("/api/overview")
def overview():
    s = _snapshot()
    snap = dict(s["values"])
    snap["ts"] = s["ts"]
    lime = db.q("SELECT free_lime, liter_weight, liq FROM lab_clinker "
                "WHERE free_lime IS NOT NULL ORDER BY ts DESC LIMIT 1")
    doc = db.q("SELECT doc FROM lab_hotmeal WHERE doc IS NOT NULL ORDER BY ts DESC LIMIT 1")
    conditions = {}
    if len(lime):
        conditions.update({"free_lime": float(lime["free_lime"][0]),
                           "liter_weight": float(lime["liter_weight"][0]),
                           "liquid": float(lime["liq"][0])})
    if len(doc):
        conditions["doc"] = float(doc["doc"][0])
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
