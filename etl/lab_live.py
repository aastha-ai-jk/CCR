"""Latest lab values for the twin / advisor — live eLIMS first, fresh only.

The state vote and the twin use free lime, liter weight, liquid phase (clinker)
and hot-meal DOC. Each is taken from the newest result across the live eLIMS
tables (lab_freelime_live, lab_hotmeal_live) and the older Excel-loaded tables
(lab_clinker, lab_hotmeal), and is USED only if its SAMPLE is younger than
the material's limit (clinker 6 h, hot meal 8 h; closes A5): a stale lab value is
left out of the vote rather than steering the band with old data. eLIMS stamps
results a fixed time after sampling (clinker 2 h, hot meal 4 h — QC team), so
sample time = eLIMS time - lag.

    latest_lab(q, max_age_h) -> (conditions, info)
      conditions: {"free_lime", "liter_weight", "liquid", "doc"} — fresh values only
      info:       {name: {"value", "ts", "age_h", "used", "source"}} — for the screen
q is any callable(sql) -> DataFrame (backend.db.q, or a pandas read_sql wrapper).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

LAB_MAX_AGE_H = 8.0      # legacy single limit (still honoured if passed as a number)

# Fixed eLIMS lag (QC team, 26 Oct 2026): an eLIMS result's time stamp is this
# many hours AFTER the sample was taken. Sample time = eLIMS time - lag; the
# age of a lab value is counted from its SAMPLE time.
ELIMS_LAG_H = {"clinker": 2.0, "hotmeal": 4.0}
# Usable while the sample is younger than this (lag + about one-two lab cycles)
MAX_AGE_H = {"clinker": 6.0, "hotmeal": 8.0}

# condition name -> (material, [(table, column, source label, is_elims)])
_SOURCES = {
    "free_lime": ("clinker", [("lab_freelime_live", "free_lime", "eLIMS live", True),
                              ("lab_clinker", "free_lime", "lab file", False)]),
    "liter_weight": ("clinker", [("lab_freelime_live", "liter_weight", "eLIMS live", True),
                                 ("lab_clinker", "liter_weight", "lab file", False)]),
    "liquid": ("clinker", [("lab_freelime_live", "liq", "eLIMS live", True),
                           ("lab_clinker", "liq", "lab file", False)]),
    "doc": ("hotmeal", [("lab_hotmeal_live", "doc", "eLIMS live", True),
                        ("lab_hotmeal", "doc", "lab file", False)]),
}


# Plausible ranges: eLIMS stores "not measured" as 0 (seen 26 Oct: Ltr.Wt. = 0),
# so values outside these are ignored and the last valid result is used.
VALID = {"free_lime": (0.2, 5.0), "liter_weight": (900.0, 1600.0),
         "liq": (20.0, 35.0), "doc": (85.0, 100.0)}


def _latest(q, table: str, col: str):
    """(ts, value) of the newest VALID result; DOC averages the two kiln sides."""
    lo, hi = VALID.get(col, (float("-inf"), float("inf")))
    try:
        d = q(f"SELECT ts, {col} AS v FROM {table} WHERE {col} IS NOT NULL "
              f"AND {col} >= {lo} AND {col} <= {hi} ORDER BY ts DESC LIMIT {4 if col == 'doc' else 1}")
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


def latest_lab(q, max_age_h=None, now: dt.datetime | None = None, lag_h: dict | None = None):
    """max_age_h: {material: hours} (or one number for all); lag_h: {material: hours}."""
    now = now or dt.datetime.now()
    lags = {**ELIMS_LAG_H, **(lag_h or {})}
    if isinstance(max_age_h, (int, float)):
        limits = {m: float(max_age_h) for m in MAX_AGE_H}
    else:
        limits = {**MAX_AGE_H, **(max_age_h or {})}
    cond, info = {}, {}
    for name, (material, sources) in _SOURCES.items():
        best = None
        for table, col, label, is_elims in sources:
            got = _latest(q, table, col)
            if not got:
                continue
            tested, v = got
            # lab files carry the same eLIMS time stamps (checked 26 Oct), so the
            # lag applies to both sources
            sampled = tested - dt.timedelta(hours=lags[material])
            if best is None or sampled > best[0]:
                best = (sampled, v, label, tested)
        if best is None:
            info[name] = {"value": None, "ts": None, "tested": None, "age_h": None,
                          "used": False, "source": None, "max_age_h": limits[material]}
            continue
        sampled, v, label, tested = best
        age = (now - sampled).total_seconds() / 3600.0
        used = age <= limits[material] and v == v        # v == v: not NaN
        info[name] = {"value": round(v, 3), "ts": sampled.isoformat(timespec="minutes"),
                      "tested": tested.isoformat(timespec="minutes"), "age_h": round(age, 1),
                      "used": bool(used), "source": label, "max_age_h": limits[material]}
        if used:
            cond[name] = v
    return cond, info
