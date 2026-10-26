"""FastAPI backend for the Calcination Combustion Control console.

Run:  python -m uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import datetime as _dt
import threading
import time

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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
    # etl.live_feed rewrites live_now (drop + recreate) every tick, so a read can
    # land in that gap. Retry briefly before falling back to historical.
    lr = None
    for attempt in range(4):
        try:
            lr = db.q("SELECT * FROM live_now LIMIT 1")
            break
        except Exception:
            time.sleep(0.15)
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
           bucket: str = "5min", in_load: bool = False, window: str | None = None):
    tlist = db.safe_tags(tags.split(","))
    if not tlist:
        return {"tags": [], "points": []}

    # A "window" (day|week|month) anchors to the NEWEST data across the
    # historian AND the live feed (the historian only grows on the weekly
    # ingest — anchoring to it alone left the "live" overview days stale) and
    # picks a bucket so each range shows ~200-300 points. Explicit start/end
    # override it; the bucket is derived per window.
    if window and not (start or end):
        anchors = [db.q("SELECT MAX(ts) AS m FROM process_1min")["m"][0]]
        try:
            anchors.append(db.q("SELECT MAX(ts) AS m FROM process_live")["m"][0])
        except Exception:
            pass
        end_dt = max(pd.to_datetime(a, format="mixed") for a in anchors if a)
        spec = {"day": (1, "5min"), "week": (7, "30min"), "month": (30, "3h")}
        days, wbucket = spec.get(window, (7, "30min"))
        start = (end_dt - pd.Timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        end = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        bucket = wbucket

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
    df["ts"] = pd.to_datetime(df["ts"], format="mixed")
    df = df.set_index("ts").apply(pd.to_numeric, errors="coerce")

    # Extend with 1-min aggregates from the live feed for minutes the weekly
    # ingest hasn't folded into the historian yet. NB the two tables use
    # different ts text formats ('...T...' vs '... ...'), so SQL gets only a
    # coarse date-prefix filter and the precise bounds are applied in pandas.
    if start or end:
        try:
            feed = config.KILN_FEED_TAG
            lcols = ", ".join(f'"{t}"' for t in dict.fromkeys([*tlist, feed]))
            ldf = db.q(f"SELECT ts, {lcols} FROM process_live WHERE ts >= :d",
                       {"d": (start or "1970-01-01")[:10]})
            if len(ldf):
                ldf["ts"] = pd.to_datetime(ldf["ts"], format="mixed")
                ldf = (ldf.set_index("ts").apply(pd.to_numeric, errors="coerce")
                       .resample("1min").mean())
                if in_load and feed in ldf.columns:
                    ldf = ldf[ldf[feed] > config.KILN_FEED_MIN_TPH]
                ldf = ldf[[t for t in tlist if t in ldf.columns]]
                df = pd.concat([df, ldf[~ldf.index.isin(df.index)]]).sort_index()
        except Exception:
            pass                                  # live table absent -> historian only
        if start:
            df = df[df.index >= pd.to_datetime(start)]
        if end:
            df = df[df.index <= pd.to_datetime(end)]

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


_LAB_INFO: dict = {"info": {}}


def _live_inputs() -> tuple[dict, dict, dict, dict]:
    """Live snapshot -> (snapshot meta, values, twin conditions, twin levers)."""
    s = _snapshot()
    snap = dict(s["values"])
    snap["ts"] = s["ts"]
    # Lab votes (free lime, liter weight, liquid, DOC): newest of live eLIMS and
    # the lab files, used only when fresh (P1 / A5). Stale ones are left out of
    # the state vote; _LAB_INFO keeps what was seen for the advisor screen.
    from etl.lab_live import latest_lab
    conditions, _LAB_INFO["info"] = latest_lab(
        db.q, float(config.plant_settings().get("advisor", {}).get("lab_max_age_h") or 8.0))
    # PLC state-setting vars + current temp so the band/state votes use live data
    conditions["current_temp"] = snap.get("423CI400_TT_4")
    for t in analysis.STATE_PLC:
        if snap.get(t) is not None:
            conditions[t] = snap[t]
    # v2 twin context: hot-side temps + secondary-air/PH-outlet, live not median
    try:
        from twin.features import SERVE_CONTEXT_TAGS
        for t in SERVE_CONTEXT_TAGS:
            if snap.get(t) is not None:
                conditions[t] = snap[t]
    except Exception:
        pass
    levers = {k: snap.get(k) for k in analysis.FEATURES if snap.get(k) is not None}
    # live lever values ride in conditions too: the twin's what-if layer
    # measures every lever change against this same snapshot
    for t in analysis.MV:
        if snap.get(t) is not None:
            conditions[t] = snap[t]
    return s, snap, conditions, levers


@app.get("/api/overview")
def overview():
    s, snap, conditions, levers = _live_inputs()

    # actual (not predicted) interlock status from the latest snapshot
    names = db.tag_names()
    interlocks = []
    for tag, (op, lim) in config.INTERLOCKS.items():
        pv = snap.get(tag)
        ok = None if pv is None else (pv < lim if op == "<" else pv > lim)
        interlocks.append({"tag": tag, "name": names.get(tag, tag),
                           "rule": f"{op} {lim}", "value": pv, "pass": ok})

    state = analysis.simulate(levers, conditions)
    return {"snapshot": snap, "conditions": conditions,
            "operating_state": state["operating_state"],
            "target_band": state["target_band"], "state_votes": state["state_votes"],
            "interlocks": interlocks, "source": s["source"], "age_sec": s["age_sec"]}


@app.get("/api/twin/forecast")
def twin_forecast():
    """Twin forecast of calciner outlet temp for the CURRENT plant state:
    t+5 prediction (mean/p10/p90) plus the t+5/10/15/30 trajectory when the
    GBT twin is serving. `method` says whether it is the twin or the fallback."""
    s, snap, conditions, levers = _live_inputs()
    sim = analysis.simulate(levers, conditions)
    return _json_safe({
        "ts": s["ts"], "source": s["source"], "age_sec": s["age_sec"],
        "tag": analysis.CV, "actual_now": snap.get(analysis.CV),
        "method": sim["method"], "horizon_min": sim["horizon_min"],
        "prediction": sim["prediction"][analysis.CV],
        "trajectory": sim.get("trajectory"),
        "operating_state": sim["operating_state"], "target_band": sim["target_band"],
    })


# Digital twin = the plant DELTA minutes ahead. Levers are the live values with
# every ACTIVE accepted setpoint applied now (backend.decisions.setpoints); the
# calciner temp + safety analysers are the twin's t+DELTA prediction; every
# other tag has no model and stays live (basis "live"). Cached per live
# snapshot + active-setpoint set, so the 2.5 s dashboard poll stays cheap.
_future_cache: dict = {"key": None, "out": None}


@app.get("/api/twin/future")
def twin_future():
    from backend import decisions
    s, snap, conditions, levers = _live_inputs()
    sp = decisions.setpoints(snap)
    active = {t: v["accepted"] for t, v in sp.items() if v["status"] == "active"}
    key = (s["ts"], tuple(sorted(active.items())))
    if _future_cache["key"] == key:
        return _future_cache["out"]

    base = analysis.simulate(levers, conditions)
    twin = analysis.simulate({**levers, **active}, conditions) if active else base
    pred = twin["prediction"]
    values = {}
    for tag, live_v in snap.items():
        if tag == "ts":
            continue
        if tag in pred:
            values[tag] = {"twin": pred[tag]["mean"], "live": live_v, "basis": "twin"}
        elif tag in active:
            values[tag] = {"twin": active[tag], "live": live_v, "basis": "setpoint"}
        else:
            values[tag] = {"twin": live_v, "live": live_v, "basis": "live"}
    try:
        twin_ts = (_dt.datetime.fromisoformat(str(s["ts"]).replace(" ", "T")[:19])
                   + _dt.timedelta(minutes=analysis.DELTA)).isoformat(timespec="seconds")
    except ValueError:
        twin_ts = None
    cv_twin, cv_base = pred[analysis.CV], base["prediction"][analysis.CV]
    out = _json_safe({
        "live_ts": s["ts"], "twin_ts": twin_ts, "horizon_min": analysis.DELTA,
        "source": s["source"], "age_sec": s["age_sec"], "method": twin["method"],
        "values": values,
        "prediction": cv_twin,                       # with active setpoints
        "baseline_prediction": cv_base,              # live levers only
        "setpoint_effect_c": round(cv_twin["mean"] - cv_base["mean"], 1),
        # calciner temp at every trained horizon (t+5/10/15/30), same split
        "trajectory": twin.get("trajectory"),
        "baseline_trajectory": base.get("trajectory"),
        "setpoints": list(sp.values()),
        "operating_state": twin["operating_state"], "target_band": twin["target_band"],
        "interlocks": twin["interlocks"], "all_interlocks_ok": twin["all_interlocks_ok"],
    })
    _future_cache.update(key=key, out=out)
    return out


# CCR advisor: recommendation for the next DELTA minutes over the advisor
# levers (the 4 calciner fuel levers); kiln coal, kiln feed, PH fan speed and
# kiln speed are fixed by the plant team and stay at their live values. One optimisation takes
# ~40 s on a laptop, so it runs in a background thread at most every
# ADVISOR_REFRESH_SEC and the endpoint serves the latest result.
ADVISOR_REFRESH_SEC = 60
ADVISOR_TRIALS = 120


def _latest_clinker() -> dict | None:
    """Newest clinker lab result (ts, free_lime, liter_weight) from lab_clinker
    and, where the eLIMS live feed runs, lab_freelime_live."""
    rows = []
    for sql in ("SELECT ts, free_lime, liter_weight FROM lab_clinker "
                "WHERE free_lime IS NOT NULL OR liter_weight IS NOT NULL ORDER BY ts DESC LIMIT 20",
                "SELECT ts, free_lime, liter_weight FROM lab_freelime_live "
                "WHERE free_lime IS NOT NULL ORDER BY ts DESC LIMIT 20"):
        try:
            rows.append(db.q(sql))
        except Exception:
            try:                                   # live table from before liter weight
                rows.append(db.q(sql.replace("free_lime, liter_weight", "free_lime, NULL AS liter_weight")))
            except Exception:
                pass                               # table absent on this install
    if not rows:
        return None
    d = pd.concat(rows, ignore_index=True)
    d["t"] = pd.to_datetime(d["ts"], format="mixed", errors="coerce")
    d = d.dropna(subset=["t"]).sort_values("t")
    if not len(d):
        return None
    r = d.iloc[-1]
    num = lambda v: None if pd.isna(v) else float(v)
    return {"ts": r["t"].isoformat(timespec="seconds"),
            "free_lime": num(r["free_lime"]), "liter_weight": num(r["liter_weight"])}


def _adv_setting(key: str, default):
    """data/plant_settings.json "advisor" value (re-read each call), else default."""
    v = (config.plant_settings().get("advisor") or {}).get(key)
    return default if v is None else v
_advisor = {"result": None, "computed_at": 0.0, "busy": False, "inputs": {}}
_advisor_lock = threading.Lock()

# "Instant" advice: recompute as soon as the plant moves noticeably since the
# inputs of the last advisory (any of these by more than its threshold), at
# most every ADVISOR_MIN_GAP_SEC, and at least every refresh_sec regardless.
ADVISOR_MIN_GAP_SEC = 15
ADVISOR_TRIGGER = {analysis.CV: 2.0,              # calciner outlet temp, degC
                   "467RL400_TPH": 0.3,           # calciner coal, t/h
                   "261WF080_TPH": 0.5,           # SAFR, t/h
                   "LAFR_FT": 50.0, "K3V10_F1": 50.0,   # LAFR (held, still a model input), l/h
                   config.KILN_FEED_TAG: 5.0}     # kiln feed, t/h


def _advisor_inputs_moved(values: dict) -> bool:
    last = _advisor.get("inputs") or {}
    if not last:
        return True
    for tag, thr in ADVISOR_TRIGGER.items():
        a, b = values.get(tag), last.get(tag)
        if a is not None and b is not None and abs(float(a) - float(b)) > thr:
            return True
    return False


def _advisor_run() -> None:
    try:
        from twin import optimize as twin_opt
        s, snap, conditions, levers = _live_inputs()
        _advisor["inputs"] = {t: s["values"].get(t) for t in ADVISOR_TRIGGER}
        # "Current" value of each advised lever = its 2-minute mean, not the
        # noisy 2-s reading (SAFR weigh-feeder jumps 1-2 t/h between samples):
        # anchors the ±10 % move cap (A10) and the held-advice check (A8).
        try:
            from backend.decisions import _lever_series
            from twin.optimize import ADVISOR_LEVERS
            t_now = _dt.datetime.fromisoformat(str(s["ts"]))
            for t in ADVISOR_LEVERS:
                m = _lever_series(t, t_now - _dt.timedelta(seconds=int(_adv_setting("current_avg_sec", 120))), t_now)
                if len(m) >= 3:
                    conditions[t] = levers[t] = float(m.mean())
        except Exception:
            pass                                      # fall back to the instant reading
        # Kiln stopped / not in load: no fuel advice (control actions only apply
        # above KILN_FEED_MIN_TPH, per the combustion control logic).
        feed = s["values"].get(config.KILN_FEED_TAG)
        if feed is None or float(feed) < config.KILN_FEED_MIN_TPH:
            _advisor.update(result={"available": False, "kiln_running": False,
                                    "kiln_feed": feed, "ts": s["ts"], "source": s["source"],
                                    "lab_inputs": dict(_LAB_INFO["info"])},
                            computed_at=time.time())
            return
        # A8: the advice on screen is re-scored and kept unless clearly beaten;
        # never held beyond its own horizon (then a fresh search decides).
        prev = _advisor.get("result") or {}
        horizon = int(_adv_setting("horizon_min", twin_opt.ADVISOR_HORIZON))
        held_since = prev.get("held_since") or prev.get("ts")
        try:
            prev_age = (_dt.datetime.fromisoformat(str(s["ts"])) -
                        _dt.datetime.fromisoformat(str(held_since))).total_seconds() / 60.0
        except Exception:
            prev_age = None
        incumbent = (prev.get("recommended") if prev.get("recommended") and prev_age is not None
                     and prev_age < horizon and prev.get("source") == s["source"] else None)
        r = twin_opt.optimize({**conditions, **levers},
                              incumbent=incumbent,
                              steps=_adv_setting("step", {"467RL400_TPH": 0.1, "261WF080_TPH": 0.1}),
                              hold=_adv_setting("hold", {"temp_c": 2.0, "coal_tph": 0.5,
                                                         "max_dev_pct": 20.0}),
                              n_trials=int(_adv_setting("trials", ADVISOR_TRIALS)),
                              levers=twin_opt.ADVISOR_LEVERS,
                              clinker=_latest_clinker() or {},
                              objective_mode="least_coal",   # plant aim; settings can override
                              horizon=int(_adv_setting("horizon_min", twin_opt.ADVISOR_HORIZON)),
                              # A10: at most ±10 % of the live value per recommendation
                              max_move_pct=float(_adv_setting("max_move_pct", 10.0)))
        r["ts"], r["source"] = s["ts"], s["source"]
        r["held_since"] = held_since if r.get("held") and incumbent else s["ts"]
        r["lab_inputs"] = dict(_LAB_INFO["info"])      # which lab values voted, and their age
        from twin import reasoning                # "why" text: template, + LLM when enabled
        reasoning.attach(r)
        from backend import decisions               # advisor_log: computed / new-recommendation count
        decisions.log_advisory(r)
        _advisor.update(result=r, computed_at=time.time())
    except Exception as e:                       # never kill the thread silently
        _advisor.update(result={"available": False, "error": repr(e)},
                        computed_at=time.time())
    finally:
        _advisor["busy"] = False


@app.get("/api/twin/advisor")
def twin_advisor():
    """Latest advisory setpoints for the advisor levers (ADVISORY ONLY), plus
    the live current value of each lever. Kicks off a background
    re-optimisation at once when there is no advice yet, when the plant has moved
    noticeably since the last one (ADVISOR_TRIGGER, at most every
    ADVISOR_MIN_GAP_SEC), or when it is older than refresh_sec."""
    from twin.optimize import ADVISOR_LEVERS, FIXED_LEVERS
    s = _snapshot()
    with _advisor_lock:
        age = time.time() - _advisor["computed_at"]
        due = (_advisor["result"] is None
               or age > float(_adv_setting("refresh_sec", ADVISOR_REFRESH_SEC))
               or (age > float(_adv_setting("min_gap_sec", ADVISOR_MIN_GAP_SEC))
                   and _advisor_inputs_moved(s["values"])))
        if due and not _advisor["busy"]:
            _advisor["busy"] = True
            threading.Thread(target=_advisor_run, daemon=True).start()
    cat = db.tag_catalog().set_index("tag")
    live = {t: {"name": str(cat.loc[t, "name"]),
                "uom": None if pd.isna(cat.loc[t, "uom"]) else str(cat.loc[t, "uom"]),
                "current": s["values"].get(t)} for t in ADVISOR_LEVERS}
    r = _advisor["result"]
    out = {"live": live, "live_ts": s["ts"], "live_source": s["source"],
           "computing": _advisor["busy"], "horizon_min": analysis.DELTA,
           "fixed_levers": FIXED_LEVERS}
    if r is None:
        out.update(available=False, pending=True)
    else:
        out.update(r)
        out.setdefault("available", "recommended" in r)
        out["age_sec"] = round(time.time() - _advisor["computed_at"], 1)
    return _json_safe(out)


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


# ---------------------------------------------------------------------------
# Digital-twin (GBT) endpoints — optimise / metrics / backtest.
# ---------------------------------------------------------------------------
def _json_safe(obj):
    """Recursively replace NaN/Inf floats with None so FastAPI can serialise."""
    import math
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


class OptReq(BaseModel):
    conditions: dict = {}


@app.post("/api/optimize")
def optimize(req: OptReq):
    # Enrich with the live snapshot so the trust region centres on the plant's
    # ACTUAL current setpoints (caller-supplied values always win).
    try:
        snap = _snapshot()["values"]
        from twin.features import SERVE_CONTEXT_TAGS
        from twin.optimize import OPT_LEVERS
        fill = ["423CI400_TT_4", *analysis.STATE_PLC, *SERVE_CONTEXT_TAGS, *OPT_LEVERS]
        for t in fill:
            if req.conditions.get(t) is None and snap.get(t) is not None:
                req.conditions[t] = snap[t]
        if req.conditions.get("current_temp") is None and snap.get("423CI400_TT_4") is not None:
            req.conditions["current_temp"] = snap["423CI400_TT_4"]
    except Exception:
        pass
    """Advisory setpoint optimisation over the four calciner fuel levers."""
    from twin import optimize as twin_opt
    return _json_safe(twin_opt.optimize(req.conditions))


@app.get("/api/twin/metrics")
def twin_metrics():
    """Serve models/metrics.json (GBT + NN + persistence) or availability flag."""
    import json
    p = config.ROOT / "models" / "metrics.json"
    if not p.exists():
        return {"available": False}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {"available": False}
    if isinstance(data, dict):
        data.setdefault("available", True)
    return _json_safe(data)


@app.get("/api/twin/backtest")
def twin_backtest(start: str | None = None, end: str | None = None,
                  max_points: int = 800):
    """Replay a held-out feed_gate=1 window: engineer features from the
    HISTORICAL frame exactly as training does, batch-predict the calciner temp
    at t+DELTA, and compare to the realised value. (A per-row rebuild through
    the serving path is wrong here — v7 serving pulls LIVE history, which must
    never leak into a historical replay.)"""
    from twin import features as tf
    from twin import model as twin_model
    CV = analysis.CV
    if not twin_model.is_available():
        return {"available": False, "series": [], "mae": None, "rmse": None,
                "r2": None, "n": 0, "tag": CV}

    delta = analysis.DELTA
    cols = ", ".join(f'"{c}"' for c in tf._raw_columns())
    where, params = ["feed_gate=1"], {}
    if start:
        where.append("ts >= :start"); params["start"] = start
    if end:
        where.append("ts <= :end"); params["end"] = end
    w = "WHERE " + " AND ".join(where)
    df = db.q(f"SELECT ts, {cols} FROM process_1min {w} ORDER BY ts", params)
    if not len(df):
        return {"available": True, "series": [], "mae": None, "rmse": None,
                "r2": None, "n": 0, "tag": CV}

    df["ts"] = pd.to_datetime(df["ts"], format="mixed")
    for c in tf._raw_columns():
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for mv in analysis.MV:
        df[mv] = df[mv].ffill()
    df = tf._engineer(df.sort_values("ts").reset_index(drop=True))
    feats = tf.feature_columns()
    df["actual_fwd"] = df[CV].shift(-delta)
    dt_fwd = (df["ts"].shift(-delta) - df["ts"]).dt.total_seconds()
    df = df[dt_fwd.between(4 * 60, 7 * 60) & df["actual_fwd"].notna()
            & df[feats].notna().all(axis=1) & df[CV].notna()]
    if not len(df):
        return {"available": True, "series": [], "mae": None, "rmse": None,
                "r2": None, "n": 0, "tag": CV}

    # decimate AFTER engineering so lag/rolling features stay intact
    step = max(1, len(df) // max(1, max_points))
    df = df.iloc[::step].head(max_points)

    out = twin_model.predict_frame(df[feats], df[CV].to_numpy(float))
    series, actuals, preds = [], [], []
    for i, rd in enumerate(df.to_dict("records")):
        actual = float(rd["actual_fwd"])
        series.append({"ts": rd["ts"].strftime("%Y-%m-%d %H:%M"),
                       "actual": round(actual, 1),
                       "pred": round(float(out["mean"][i]), 1),
                       "p10": round(float(out["p10"][i]), 1),
                       "p90": round(float(out["p90"][i]), 1)})
        actuals.append(actual); preds.append(float(out["mean"][i]))

    if actuals:
        a = pd.Series(actuals, dtype=float); p = pd.Series(preds, dtype=float)
        err = p - a
        mae = float(err.abs().mean())
        rmse = float((err ** 2).mean() ** 0.5)
        ss_tot = float(((a - a.mean()) ** 2).sum())
        r2 = float(1 - (err ** 2).sum() / ss_tot) if ss_tot > 0 else None
    else:
        mae = rmse = r2 = None

    return _json_safe({"series": series, "mae": mae, "rmse": rmse, "r2": r2,
                       "n": len(series), "tag": CV, "horizon_min": delta,
                       "available": True})


@app.get("/api/bench")
def bench():
    """Model Bench: side-by-side results of the 3-approach training bake-off
    (twin.bench). Static artifact — regenerated by `python -m twin.bench`."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(__file__).resolve().parent.parent / "models" / "bench_results.json"
    if not p.exists():
        return _json_safe({"available": False,
                           "note": "run `python -m twin.bench` to generate results"})
    return _json_safe({"available": True, **_json.loads(p.read_text())})


@app.get("/api/monitor")
def monitor(limit: int = 40):
    """Multi-day monitoring: accuracy trail (monitor_log.jsonl), the live
    conformal calibration, and the latest AFR fuel-quality readings."""
    import json as _json
    from pathlib import Path as _Path
    mdir = _Path(__file__).resolve().parent.parent / "models"
    out: dict = {"available": True}
    log_p = mdir / "monitor_log.jsonl"
    if log_p.exists():
        lines = log_p.read_text().strip().splitlines()[-int(limit):]
        out["log"] = [_json.loads(l) for l in lines]
    else:
        out["log"] = []
    conf_p = mdir / "conformal.json"
    out["conformal"] = _json.loads(conf_p.read_text()) if conf_p.exists() else None
    try:
        out["afr_recent"] = _clean_records(
            db.q("SELECT * FROM afr_quality ORDER BY ts DESC LIMIT 120").iloc[::-1])
    except Exception:
        out["afr_recent"] = []
    return _json_safe(out)


@app.get("/api/shadow")
def shadow(limit: int = 240):
    """Shadow mode: the twin's live prediction/recommendation log beside the CCR,
    with running tracking accuracy over matured (horizon-elapsed) predictions."""
    try:
        rows = db.q(f"SELECT * FROM shadow_log ORDER BY ts DESC LIMIT {int(limit)}")
    except Exception:
        return _json_safe({"available": False, "rows": [], "series": [], "n": 0,
                           "note": "shadow runner is starting — no log yet."})
    if not len(rows):
        return _json_safe({"available": True, "series": [], "n": 0, "matured": 0,
                           "running_mae": None})
    matured = rows[rows["matured"] == 1]
    mae = float(matured["abs_error"].mean()) if len(matured) else None
    within = (float((matured["abs_error"] <= 5).mean()) if len(matured) else None)
    latest = rows.iloc[0].to_dict()
    return _json_safe({
        "available": True,
        "n": int(len(rows)),
        "matured": int(len(matured)),
        "running_mae": mae,
        "pct_within_5C": within,
        "horizon_min": analysis.DELTA,
        "current": {k: latest.get(k) for k in (
            "ts", "actual_now", "pred_mean", "pred_p10", "pred_p90", "state",
            "band_lo", "band_hi", "in_band_now", "cur_coal", "rec_coal",
            "delta_coal", "cur_afr", "rec_afr", "delta_afr", "rec_in_band",
            "interlocks_ok")},
        "series": _clean_records(rows.iloc[::-1]),
    })


@app.get("/api/admin/accuracy")
def admin_accuracy(days: int = Query(7, ge=1, le=60)):
    """P8: day-wise accuracy of the twin's checked predictions (shadow_log),
    kiln running only. T+10 is the advisor's horizon; T+5 for comparison.
    error = actual - predicted; within_5 = share with |error| <= 5 C."""
    cut = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
    try:
        d = db.q("SELECT ts, feed_gate, pred_mean, actual_future, matured, "
                 "pred10_mean, actual10, matured10 FROM shadow_log WHERE ts >= :c",
                 {"c": cut})
    except Exception:
        return {"available": False, "days": [], "note": "no T+10 tracking yet (shadow runner)"}
    out = []
    if len(d):
        d = d[d["feed_gate"].fillna(0).astype(int) == 1].copy()
        d["day"] = d["ts"].astype(str).str[:10]
        for day, g in sorted(d.groupby("day"), reverse=True):
            row = {"day": day}
            for h, pc, ac, mc in ((10, "pred10_mean", "actual10", "matured10"),
                                  (5, "pred_mean", "actual_future", "matured")):
                m = g[(g[mc].fillna(0).astype(int) == 1) & g[ac].notna() & g[pc].notna()]
                err = (m[ac].astype(float) - m[pc].astype(float))
                row[f"t{h}"] = None if not len(m) else {
                    "n": int(len(m)), "mae": round(float(err.abs().mean()), 2),
                    "bias": round(float(err.mean()), 2),
                    "within_5": round(float((err.abs() <= 5).mean()) * 100, 1),
                    "p90_abs": round(float(err.abs().quantile(0.9)), 2)}
            out.append(row)
    return _json_safe({"available": True, "days": out})


def _latest_optimizer_row():
    try:
        r = db.q("SELECT * FROM optimizer_log ORDER BY ts DESC LIMIT 1")
    except Exception:
        return None
    return _clean_records(r)[0] if len(r) else None


@app.get("/api/fuel")
def fuel():
    """Live fuel KPIs from the newest advisory (written by the ccr-optimizer
    loop). All economics carry is_estimate until real prices/GCV are supplied."""
    row = _latest_optimizer_row()
    if row is None:
        return _json_safe({"available": False,
                           "reason": "ccr-optimizer loop has not logged an advisory yet"})
    return _json_safe({
        "available": True, "advisory": True, "mode": row.get("mode"),
        "ts": row.get("ts"), "is_estimate": bool(row.get("is_estimate")),
        "current": {"coal_tph": row.get("cur_coal"), "afr_total": row.get("cur_afr"),
                    "cost_per_ton": row.get("cur_cost"), "tsr_pct": row.get("cur_tsr"),
                    "specific_heat_kcal_kg": row.get("cur_spheat")},
        "recommended": {"coal_tph": row.get("rec_coal"), "afr_total": row.get("rec_afr"),
                        "cost_per_ton": row.get("rec_cost"), "tsr_pct": row.get("rec_tsr"),
                        "specific_heat_kcal_kg": row.get("rec_spheat")},
        "savings": {"cost_per_ton": row.get("save_cost"), "tsr_pts": row.get("save_tsr")},
        "temp_in_band": bool(row.get("in_band_now")),
        "quality_ok": bool(row.get("quality_ok")),
        "interlocks_ok": bool(row.get("interlocks_ok")),
    })


@app.get("/api/savings")
def savings(limit: int = Query(500, ge=1, le=5000)):
    """M&V trend: predicted vs realised coal/₹/TSR from optimizer_log, split by
    whether the operator actually moved toward the advice (followed)."""
    try:
        r = db.q("SELECT * FROM optimizer_log ORDER BY ts DESC LIMIT :n", {"n": limit})
    except Exception:
        r = None
    if r is None or not len(r):
        return _json_safe({"available": False,
                           "reason": "no optimizer advisories logged yet"})
    matured = r[r["matured"] == 1]
    followed = matured[matured["followed"] == 1]

    def _avg(df, col):
        return float(df[col].mean()) if len(df) and df[col].notna().any() else None

    return _json_safe({
        "available": True, "advisory": True,
        "n": int(len(r)), "matured": int(len(matured)), "followed": int(len(followed)),
        "is_estimate": bool(r.iloc[0].get("is_estimate")),
        "predicted_avg": {"cost_per_ton": _avg(r, "save_cost"), "tsr_pts": _avg(r, "save_tsr")},
        "realized_followed_avg": {"coal_tph": _avg(followed, "realized_coal"),
                                  "cost_per_ton": _avg(followed, "realized_cost"),
                                  "tsr_pts": _avg(followed, "realized_tsr")},
        "series": _clean_records(r.iloc[::-1]),
    })


@app.get("/fuel", response_class=HTMLResponse)
@app.get("/fuel-dashboard", response_class=HTMLResponse)
def fuel_dashboard():
    """Self-contained live preview of the fuel optimiser (KPIs, advisory,
    sensitivities, M&V). Same-origin, no build step."""
    from backend._fuel_dashboard import HTML
    return HTML


class DecisionReq(BaseModel):
    tag: str
    action: str                      # accepted | declined | undone
    param_id: str | None = None
    param_name: str | None = None
    unit: str | None = None
    reason: str | None = None
    note: str | None = None
    current_value: float | None = None
    recommended_value: float | None = None
    operator: str | None = None


@app.post("/api/decisions")
def post_decision(req: DecisionReq):
    """Record an operator Accept / Decline / Undo on a twin recommendation,
    with the twin's advisory context at that moment. Never writes to the plant."""
    from fastapi import HTTPException
    from backend import decisions
    try:
        return _json_safe(decisions.record(req.model_dump(), _advisor["result"], _snapshot()))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/decisions")
def get_decisions(day: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    """Operator decisions for one date (YYYY-MM-DD, default today)."""
    from fastapi import HTTPException
    from backend import decisions
    try:
        return _json_safe(decisions.list_day(day, limit))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/advisor/summary")
def get_advisor_summary(day: str | None = None):
    """Advisories computed and new recommendations issued on one date (default today)."""
    from fastapi import HTTPException
    from backend import decisions
    try:
        return _json_safe(decisions.advisory_summary(day))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/admin/declined-reached")
def get_declined_reached(day: str | None = None):
    """Admin: declined lever recommendations on one date, each checked for whether
    the plant reached the recommended value (± tolerance) within the window anyway."""
    from fastapi import HTTPException
    from backend import decisions
    try:
        return _json_safe(decisions.declined_but_reached(day))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/decisions/days")
def get_decision_days():
    """Date-wise index: every day with decisions and its counts."""
    from backend import decisions
    return _json_safe(decisions.days())


@app.get("/advisor", response_class=HTMLResponse)
def advisor_page():
    """CCR Kiln-3 advisor prototype. Only the predicted calciner temperature is
    live (from /api/twin/forecast, same-origin); every other element is mock."""
    from pathlib import Path
    return (Path(__file__).parent / "static" / "ccr-kiln3-advisor.html").read_text(encoding="utf-8")


@app.get("/api/optimizer")
def optimizer_latest():
    """Latest logged advisory + its per-lever sensitivities (what drives temp vs
    cost vs TSR) — read from optimizer_log, so this is instant (no re-optimise)."""
    import json as _json
    latest = _latest_optimizer_row()
    correlation = None
    if latest and latest.get("sensitivities"):
        try:
            correlation = {"mode": latest.get("mode"),
                           "sensitivities": _json.loads(latest["sensitivities"])}
        except (ValueError, TypeError):
            pass
    return _json_safe({"available": latest is not None, "advisory": True,
                       "latest": latest, "correlation": correlation})
