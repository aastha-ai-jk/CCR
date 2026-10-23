"""CCR operator decisions on twin recommendations — the labelled trail for
future training (what the twin advised, what the operator did, and why).

Every Accept / Decline / Undo from the advisor screen is one row in
``operator_decisions``; an Undo is its own row (``action='undone'``), never a
delete, so the full history is kept. Rows are keyed by ``day`` (plant-local
calendar date of the decision) for date-wise retrieval. The twin context at
decision time (advisory setpoints, predictions, state, live lever values) is
filled in server-side, not trusted from the browser.

ADVISORY ONLY — recording a decision never writes anything to the plant.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
from sqlalchemy import text

from backend import analysis, db

ACTIONS = ("accepted", "declined", "undone")
MAX_REASON = 200
MAX_NOTE = 1000

_DDL = """
CREATE TABLE IF NOT EXISTS operator_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,              -- YYYY-MM-DD, plant-local date of the decision
  decided_at TEXT NOT NULL,       -- ISO local wall-clock time of the click
  operator TEXT,
  param_id TEXT, tag TEXT NOT NULL, param_name TEXT, unit TEXT,
  action TEXT NOT NULL,           -- accepted | declined | undone
  reason TEXT, note TEXT,         -- decline reason (required) + free-text note
  current_value REAL,             -- value shown to the operator as Current
  recommended_value REAL,         -- value shown to the operator as Recommended
  advisory_ts TEXT,               -- plant snapshot time the advisory was computed from
  data_source TEXT,               -- opc | replay | historical
  horizon_min INTEGER,
  live_temp REAL,                 -- calciner outlet temp at decision time
  predicted_temp REAL,            -- twin t+horizon temp WITH all recommendations applied
  operating_state TEXT, band_lo REAL, band_hi REAL,
  context_json TEXT               -- full advisory + live lever snapshot (for training)
)
"""
_IDX = "CREATE INDEX IF NOT EXISTS ix_operator_decisions_day ON operator_decisions(day)"
_ready = False


def _ensure() -> None:
    global _ready
    if _ready:
        return
    with db.engine().begin() as con:
        con.execute(text(_DDL))
        con.execute(text(_IDX))
    _ready = True


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def record(body: dict, advisory: dict | None, snapshot: dict) -> dict:
    """Validate one decision, attach twin context, insert it. Returns the row."""
    _ensure()
    from twin.optimize import ADVISOR_LEVERS
    tag = str(body.get("tag") or "")
    if tag not in (*ADVISOR_LEVERS, analysis.CV):
        raise ValueError(f"not a recommended parameter: {tag!r}")
    action = str(body.get("action") or "")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    reason = (body.get("reason") or "").strip()[:MAX_REASON] or None
    note = (body.get("note") or "").strip()[:MAX_NOTE] or None
    if action == "declined" and not reason:
        raise ValueError("a decline needs a reason")

    now = dt.datetime.now()
    adv = advisory if advisory and "recommended" in advisory else {}
    band = adv.get("target_band") or {}
    temp = (adv.get("predicted") or {}).get("temp") or {}
    vals = snapshot.get("values", {})
    context = {
        "advisory": {k: adv.get(k) for k in (
            "recommended", "baseline", "predicted", "operating_state", "target_band",
            "in_band", "reach_min", "band_confidence", "mode", "economics") if k in adv},
        "live": {t: vals.get(t) for t in (analysis.CV, *analysis.MV, *analysis.STATE_PLC)},
        "live_ts": snapshot.get("ts"),
    }
    row = {
        "day": now.date().isoformat(),
        "decided_at": now.isoformat(timespec="seconds"),
        "operator": (body.get("operator") or "").strip()[:60] or None,
        "param_id": (body.get("param_id") or "")[:40] or None,
        "tag": tag,
        "param_name": (body.get("param_name") or "")[:80] or None,
        "unit": (body.get("unit") or "")[:20] or None,
        "action": action, "reason": reason, "note": note,
        "current_value": _num(body.get("current_value")),
        "recommended_value": _num(body.get("recommended_value")),
        "advisory_ts": adv.get("ts"),
        "data_source": adv.get("source") or snapshot.get("source"),
        "horizon_min": adv.get("horizon_min") or analysis.DELTA,
        "live_temp": _num(vals.get(analysis.CV)),
        "predicted_temp": _num(temp.get("mean")),
        "operating_state": adv.get("operating_state"),
        "band_lo": _num(band.get("low")), "band_hi": _num(band.get("high")),
        "context_json": json.dumps(context, default=str),
    }
    cols = ", ".join(row)
    with db.engine().begin() as con:
        res = con.execute(text(f"INSERT INTO operator_decisions ({cols}) "
                               f"VALUES ({', '.join(':' + c for c in row)})"), row)
        row["id"] = res.lastrowid
    row.pop("context_json")
    return row


def list_day(day: str | None = None, limit: int = 500) -> dict:
    """Decisions for one date (default today), newest first, plus counts and
    each parameter's latest status (accepted / declined / open)."""
    _ensure()
    day = day or dt.date.today().isoformat()
    dt.date.fromisoformat(day)                       # validates the format
    rows = db.q("SELECT * FROM operator_decisions WHERE day = :d "
                "ORDER BY id DESC LIMIT :n", {"d": day, "n": int(limit)})
    rows = rows.drop(columns=["context_json"]) if len(rows) else rows
    recs = rows.where(pd.notna(rows), None).to_dict("records") if len(rows) else []
    status: dict[str, dict] = {}
    for r in reversed(recs):                         # oldest -> newest
        status[r["tag"]] = {"status": "open" if r["action"] == "undone" else r["action"],
                            "decided_at": r["decided_at"], "reason": r["reason"],
                            "note": r["note"], "param_id": r["param_id"]}
    counts = {a: sum(1 for r in recs if r["action"] == a) for a in ACTIONS}
    return {"day": day, "n": len(recs), "counts": counts,
            "status": status, "rows": recs}


# An accepted setpoint drives the twin (as if applied now) until the plant
# reaches it (live within REACH_TOL_FRAC of the tag's engineering range), it is
# undone / superseded by a newer decision, or EXPIRY_MIN passes.
REACH_TOL_FRAC = 0.005
EXPIRY_MIN = 30


def setpoints(values: dict, now: dt.datetime | None = None) -> dict:
    """tag -> accepted setpoint state for every lever whose latest decision in
    the last EXPIRY_MIN minutes is 'accepted'. ``status`` is ``active`` (still
    driving the twin), ``reached`` (plant is there — live takes over) or
    ``expired``. Levers only: accepting the calciner-temp row sets nothing."""
    import config
    from twin.optimize import ADVISOR_LEVERS     # fixed levers never drive the twin
    _ensure()
    adv = config.plant_settings().get("advisor") or {}
    tol_frac = float(adv.get("setpoint_reach_tol_frac") or REACH_TOL_FRAC)
    expiry = float(adv.get("setpoint_expiry_min") or EXPIRY_MIN)
    now = now or dt.datetime.now()
    since = (now - dt.timedelta(minutes=expiry + 5)).isoformat(timespec="seconds")
    rows = db.q("SELECT * FROM operator_decisions WHERE decided_at >= :s ORDER BY id DESC",
                {"s": since})
    cat = db.tag_catalog().set_index("tag")
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for r in rows.to_dict("records"):                # newest first
        tag = r["tag"]
        if tag in seen:
            continue
        seen.add(tag)
        if r["action"] != "accepted" or tag not in ADVISOR_LEVERS or r["recommended_value"] is None:
            continue
        age = (now - dt.datetime.fromisoformat(r["decided_at"])).total_seconds() / 60.0
        lo, hi = cat.loc[tag, "y_min"], cat.loc[tag, "y_max"]
        span = float(hi - lo) if pd.notna(lo) and pd.notna(hi) and hi > lo else 1.0
        tol = tol_frac * span
        live = _num(values.get(tag))
        rec = float(r["recommended_value"])
        if live is not None and abs(live - rec) <= tol:
            status = "reached"
        elif age > expiry:
            status = "expired"
        else:
            status = "active"
        out[tag] = {"tag": tag, "name": r["param_name"], "unit": r["unit"],
                    "accepted": rec, "live": live, "tolerance": round(tol, 4),
                    "decided_at": r["decided_at"], "age_min": round(age, 1),
                    "status": status, "decision_id": int(r["id"])}
    return out


# Every advisory the optimiser computes is logged in ``advisor_log``. ``issued``
# marks a NEW recommendation: some lever moved by more than ISSUE_TOL_FRAC of
# its range versus the previous one (the screen refreshes every minute, mostly
# with the same advice — those are not counted as new).
ISSUE_TOL_FRAC = 0.005
_ADV_DDL = """
CREATE TABLE IF NOT EXISTS advisor_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,              -- YYYY-MM-DD, plant-local date of the advisory
  ts TEXT,                        -- plant snapshot time the advisory was computed from
  data_source TEXT,               -- opc | edge | replay | historical
  operating_state TEXT, band_lo REAL, band_hi REAL,
  predicted_temp REAL,
  recommended_json TEXT,          -- {tag: value} for the advisor levers
  issued INTEGER NOT NULL         -- 1 = new recommendation, 0 = same as previous
)
"""
_last_rec: dict = {}


def log_advisory(r: dict) -> None:
    """Record one optimiser result; never raises (logging must not break the advisor)."""
    global _last_rec
    try:
        rec = r.get("recommended") or {}
        if not rec:
            return
        lim = r.get("lever_limits") or {}
        with db.engine().begin() as con:
            con.execute(text(_ADV_DDL))
            con.execute(text("CREATE INDEX IF NOT EXISTS ix_advisor_log_day ON advisor_log(day)"))
            if not _last_rec:                        # after a restart: compare with the last row
                last = con.execute(text("SELECT recommended_json FROM advisor_log "
                                        "ORDER BY id DESC LIMIT 1")).fetchone()
                _last_rec = json.loads(last[0]) if last and last[0] else {}
            issued = not _last_rec or any(
                t not in _last_rec or abs(float(v) - float(_last_rec[t]))
                > ISSUE_TOL_FRAC * max(float((lim.get(t) or {}).get("max", 0))
                                       - float((lim.get(t) or {}).get("min", 0)), 1e-9)
                for t, v in rec.items())
            tb = r.get("target_band") or {}
            pt = ((r.get("predicted") or {}).get("temp") or {}).get("mean")
            con.execute(text(
                "INSERT INTO advisor_log (day, ts, data_source, operating_state, band_lo, "
                "band_hi, predicted_temp, recommended_json, issued) VALUES "
                "(:day, :ts, :src, :st, :lo, :hi, :pt, :rec, :iss)"),
                {"day": dt.date.today().isoformat(), "ts": r.get("ts"), "src": r.get("source"),
                 "st": r.get("operating_state"), "lo": tb.get("low"), "hi": tb.get("high"),
                 "pt": pt, "rec": json.dumps(rec), "iss": int(issued)})
        _last_rec = dict(rec)
    except Exception:
        pass


def advisory_summary(day: str | None = None) -> dict:
    """Advisories computed and NEW recommendations issued on one date (live data only)."""
    day = day or dt.date.today().isoformat()
    dt.date.fromisoformat(day)
    try:
        d = db.q("SELECT COUNT(*) AS computed, COALESCE(SUM(issued), 0) AS issued "
                 "FROM advisor_log WHERE day = :d AND data_source != 'historical'", {"d": day})
        return {"day": day, "computed": int(d["computed"][0]), "issued": int(d["issued"][0])}
    except Exception:                                # table not created yet
        return {"day": day, "computed": 0, "issued": 0}


# Admin view: a DECLINED recommendation the plant reached anyway — the lever's
# 1-minute mean came within MATCH_TOL_PCT % of the recommended value within
# MATCH_WINDOW_MIN minutes after the decline (advice was right, not followed).
MATCH_TOL_PCT = 5.0
MATCH_WINDOW_MIN = 30.0
PROOF_BEFORE_MIN = 10                 # evidence chart starts this long before the decline


def _lever_series(tag: str, start: dt.datetime, end: dt.datetime) -> pd.Series:
    """Actual values of one tag in [start, end]: live feed (process_live,
    'YYYY-MM-DDTHH:MM:SS') first, historian (process_1min) for anything missing."""
    parts = []
    for table, fmt in (("process_live", "%Y-%m-%dT%H:%M:%S"),
                       ("process_1min", "%Y-%m-%d %H:%M:%S")):
        try:
            d = db.q(f'SELECT ts, "{tag}" AS v FROM {table} WHERE ts >= :a AND ts <= :b',
                     {"a": start.strftime(fmt), "b": end.strftime(fmt)})
        except Exception:
            continue                                  # table or column absent here
        if len(d):
            d["ts"] = pd.to_datetime(d["ts"], format="mixed", errors="coerce")
            d["v"] = pd.to_numeric(d["v"], errors="coerce")
            parts.append(d.dropna())
    if not parts:
        return pd.Series(dtype=float)
    s = pd.concat(parts).drop_duplicates("ts").set_index("ts")["v"].sort_index()
    return s[(s.index >= start) & (s.index <= end)]


def match_decline(rec: float, decided_at: dt.datetime, series: pd.Series,
                  now: dt.datetime, tol_pct: float = MATCH_TOL_PCT,
                  window_min: float = MATCH_WINDOW_MIN) -> dict:
    """Did the plant reach rec (± tol_pct %) within window_min after decided_at?
    status: reached | not_reached | pending (window still open) | no_data."""
    tol = abs(rec) * tol_pct / 100.0
    end = decided_at + dt.timedelta(minutes=window_min)
    inside = series[(series - rec).abs() <= tol] if len(series) else series
    if len(inside):
        t, v = inside.index[0], float(inside.iloc[0])
        return {"status": "reached", "reached_at": t.isoformat(timespec="seconds"),
                "reached_value": v, "minutes_after": round((t - decided_at).total_seconds() / 60, 1),
                "tolerance": tol}
    if now < end:
        return {"status": "pending", "tolerance": tol}
    return {"status": "not_reached" if len(series) else "no_data", "tolerance": tol,
            "closest_value": None if not len(series) else
            float(series.iloc[(series - rec).abs().argmin()])}


def declined_but_reached(day: str | None = None) -> dict:
    """Every lever decline on one date, each checked against what the plant did
    afterwards. Declines later undone are left out (the decision was withdrawn)."""
    import config
    from twin.optimize import ADVISOR_LEVERS
    _ensure()
    day = day or dt.date.today().isoformat()
    dt.date.fromisoformat(day)
    adv = config.plant_settings().get("advisor") or {}
    tol_pct = float(adv.get("decline_match_tol_pct") or MATCH_TOL_PCT)
    window = float(adv.get("decline_match_window_min") or MATCH_WINDOW_MIN)
    rows = db.q("SELECT id, decided_at, operator, param_id, tag, param_name, unit, action, "
                "reason, note, current_value, recommended_value, operating_state "
                "FROM operator_decisions WHERE day = :d ORDER BY id", {"d": day})
    now = dt.datetime.now()
    recs = rows.where(pd.notna(rows), None).to_dict("records") if len(rows) else []
    out = []
    for i, r in enumerate(recs):
        if (r["action"] != "declined" or r["tag"] not in ADVISOR_LEVERS
                or r["recommended_value"] is None):
            continue
        nxt = next((x for x in recs[i + 1:] if x["tag"] == r["tag"]), None)
        if nxt and nxt["action"] == "undone":
            continue
        t0 = dt.datetime.fromisoformat(r["decided_at"])
        # Match on 1-minute means, not raw 2-s samples: a single noisy reading
        # (weigh-feeder spikes) is not the plant running at the recommended value.
        raw = _lever_series(r["tag"], t0, t0 + dt.timedelta(minutes=window))
        s = raw.resample("1min").mean().dropna() if len(raw) else raw
        row = {**r, **match_decline(float(r["recommended_value"]), t0, s, now, tol_pct, window)}
        if row.get("minutes_after") is not None:
            row["minutes_after"] = max(0.0, row["minutes_after"])   # minute bucket may start before t0
        # evidence chart: the lever's actual run, 10 min before -> window after, 1-min means
        ctx = _lever_series(r["tag"], t0 - dt.timedelta(minutes=PROOF_BEFORE_MIN),
                            t0 + dt.timedelta(minutes=window))
        if len(ctx):
            m = ctx.resample("1min").mean().dropna()
            row["series"] = [{"t": t.isoformat(timespec="seconds"), "v": round(float(v), 3)}
                             for t, v in m.items()]
        else:
            row["series"] = []
        out.append(row)
    n = {k: sum(1 for x in out if x["status"] == k)
         for k in ("reached", "not_reached", "pending", "no_data")}
    return {"day": day, "tolerance_pct": tol_pct, "window_min": window,
            "declines": len(out), "counts": n, "rows": out}


def days() -> list[dict]:
    """Every date that has decisions, with counts — the date-wise index."""
    _ensure()
    d = db.q("SELECT day, COUNT(*) AS n, "
             "SUM(action = 'accepted') AS accepted, SUM(action = 'declined') AS declined "
             "FROM operator_decisions GROUP BY day ORDER BY day DESC")
    return d.to_dict("records")
