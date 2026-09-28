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
    tag = str(body.get("tag") or "")
    if tag not in (*analysis.MV, analysis.CV):
        raise ValueError(f"unknown parameter tag: {tag!r}")
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
        "horizon_min": analysis.DELTA,
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


def days() -> list[dict]:
    """Every date that has decisions, with counts — the date-wise index."""
    _ensure()
    d = db.q("SELECT day, COUNT(*) AS n, "
             "SUM(action = 'accepted') AS accepted, SUM(action = 'declined') AS declined "
             "FROM operator_decisions GROUP BY day ORDER BY day DESC")
    return d.to_dict("records")
