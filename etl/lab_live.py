"""Latest lab values for the twin / advisor — live eLIMS first, fresh only.

The state vote and the twin use free lime, liter weight, liquid phase (clinker)
and hot-meal DOC. Each is taken from the newest result across the live eLIMS
tables (lab_freelime_live, lab_hotmeal_live) and the older Excel-loaded tables
(lab_clinker, lab_hotmeal), and is USED only if that result is younger than
max_age_h (default 8 h, closes A5): a stale lab value is left out of the vote
rather than steering the band with weeks-old data.

    latest_lab(q, max_age_h) -> (conditions, info)
      conditions: {"free_lime", "liter_weight", "liquid", "doc"} — fresh values only
      info:       {name: {"value", "ts", "age_h", "used", "source"}} — for the screen
q is any callable(sql) -> DataFrame (backend.db.q, or a pandas read_sql wrapper).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

LAB_MAX_AGE_H = 8.0

# condition name -> [(table, column, source label)], checked in this order
_SOURCES = {
    "free_lime": [("lab_freelime_live", "free_lime", "eLIMS live"), ("lab_clinker", "free_lime", "lab file")],
    "liter_weight": [("lab_freelime_live", "liter_weight", "eLIMS live"), ("lab_clinker", "liter_weight", "lab file")],
    "liquid": [("lab_freelime_live", "liq", "eLIMS live"), ("lab_clinker", "liq", "lab file")],
    "doc": [("lab_hotmeal_live", "doc", "eLIMS live"), ("lab_hotmeal", "doc", "lab file")],
}


def _latest(q, table: str, col: str):
    """(ts, value) of the newest non-null result; DOC averages the two kiln sides."""
    try:
        if col == "doc":
            d = q(f"SELECT ts, {col} AS v FROM {table} WHERE {col} IS NOT NULL ORDER BY ts DESC LIMIT 4")
        else:
            d = q(f"SELECT ts, {col} AS v FROM {table} WHERE {col} IS NOT NULL ORDER BY ts DESC LIMIT 1")
    except Exception:
        return None
    if not len(d):
        return None
    d["t"] = pd.to_datetime(d["ts"], format="mixed", errors="coerce")
    d = d.dropna(subset=["t"])
    if not len(d):
        return None
    t = d["t"].max()
    if col == "doc":                      # sides A and B, tested minutes apart
        near = d[(t - d["t"]) <= pd.Timedelta(hours=1)]
        return t.to_pydatetime(), float(pd.to_numeric(near["v"], errors="coerce").mean())
    return t.to_pydatetime(), float(d.loc[d["t"].idxmax(), "v"])


def latest_lab(q, max_age_h: float = LAB_MAX_AGE_H, now: dt.datetime | None = None):
    now = now or dt.datetime.now()
    cond, info = {}, {}
    for name, sources in _SOURCES.items():
        best = None
        for table, col, label in sources:
            got = _latest(q, table, col)
            if got and (best is None or got[0] > best[0]):
                best = (*got, label)
        if best is None:
            info[name] = {"value": None, "ts": None, "age_h": None, "used": False, "source": None}
            continue
        t, v, label = best
        age = (now - t).total_seconds() / 3600.0
        used = age <= max_age_h and v == v              # v == v: not NaN
        info[name] = {"value": round(v, 3), "ts": t.isoformat(timespec="minutes"),
                      "age_h": round(age, 1), "used": bool(used), "source": label}
        if used:
            cond[name] = v
    return cond, info
