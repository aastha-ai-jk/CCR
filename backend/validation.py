"""Continuous validation of the advisor's inputs and recommendations.

Two families of checks, graded by impact:

INPUTS (the live readings the advisor uses)
  freshness  - live feed current?                           ERROR, all levers
  missing    - no numeric value                             ERROR for a critical/lever tag
  range      - outside the history-based hard range         ERROR (critical/lever), else WARNING
               (or a physical rule, e.g. O2 above 15 % = analyser purge)
             - outside the 0.1-99.9 percentile band          WARNING ("rare value")
  spike      - latest value jumps away from its own recent median by more than the
               plant's 99.9th-percentile minute-to-minute change           ERROR (critical/lever)
  stuck      - value exactly unchanged for far longer than that tag ever
               normally is in the live feed                                ERROR (critical/lever)
  analyser   - an analyser RUN flag reads 0                                WARNING (the RUN tags
               are known to share one OPC address; the purge rule above catches a purging
               analyser from its value directly)

RECOMMENDATIONS (what the optimizer proposes)
  not a finite number, negative, outside the PLC range, or outside the hard range   ERROR, that lever
  step larger than 99.9 % of the plant's own 5-minute moves                         ERROR, that lever
  step larger than 99 % of the plant's own 5-minute moves                           WARNING
  predicted temperature not finite or outside the hard range                        ERROR, all levers
  a predicted interlock fails                                                       ERROR, all levers

"Critical" tags feed EVERY recommendation (the model's target and fixed inputs
and the safety analysers): a bad one blocks all levers. A bad lever reading
blocks only that lever. ERROR blocks Accept (screen and API) and the SCADA
write; WARNING is shown only.

Limits: data/validation_limits.json (generated from plant history by
etl/build_validation_limits.py) + the PHYSICAL rules below, which win.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
from pathlib import Path

import pandas as pd

LIMITS_PATH = Path(__file__).resolve().parent.parent / "data" / "validation_limits.json"

CV = "423CI400_TT_4"
LEVERS = ["467RL400_TPH", "LAFR_FT", "K3V10_F1", "261WF080_TPH"]
SAFETY = ["423AM823_CO_PPM", "423AM823_O2", "423AM822_CO_PPM", "423AM822_O2"]
CRITICAL = [CV, "353RL130_TPH", "467RL300_TPH", "423FN010_ST", "433MD140_ST1", *SAFETY]
CHECKED = CRITICAL + LEVERS                     # get spike / stuck / missing checks
RUN_FLAG = {"423AM823_CO_PPM": "423AM823_RUN", "423AM823_O2": "423AM823_RUN",
            "423AM822_CO_PPM": "423AM822_RUN", "423AM822_O2": "423AM822_RUN"}
# Each gas analyser measures O2 and CO from one probe. While it purges (blow-back
# with plant air) its O2 reads ~21 % and its CO is meaningless, so neither is
# judged as an outlier. Measured on 3 months of live data: the calciner-outlet
# analyser purges ~15 times an hour, median 0.8 min, P95 1.8 min, longest 7.1 min.
# A purge longer than PURGE_MAX_S is therefore treated as an analyser fault.
ANALYSERS = {"423AM823_O2": "423AM823_CO_PPM", "423AM822_O2": "423AM822_CO_PPM"}
PURGE_O2 = 15.0
PURGE_MAX_S = 600.0

# Physical rules that override the history-based limits.
PHYSICAL = {
    t: {"hard_hi": 15.0, "why": "O2 above 15 % is analyser purge / blow-back (ambient air), "
                                "not process gas - same rule as twin/features.py _O2_MAX"}
    for t in ("423AM820_O2", "423AM821_O2", "423AM822_O2", "423AM823_O2")
}

_cache = {"mtime": None, "tags": {}}
_lock = threading.Lock()


def limits() -> dict[str, dict]:
    """Per-tag limits, reloaded automatically when the JSON file changes."""
    try:
        m = LIMITS_PATH.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] != m:
            tags = json.loads(LIMITS_PATH.read_text(encoding="utf-8")).get("tags", {})
            for t, rule in PHYSICAL.items():
                if t in tags:
                    tags[t] = {**tags[t], **{k: v for k, v in rule.items() if k != "why"},
                               "physical_rule": rule["why"]}
            _cache.update(mtime=m, tags=tags)
        return _cache["tags"]


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _scope(tag: str) -> str:
    if tag in CRITICAL:
        return "all"
    if tag in LEVERS:
        return tag
    return "info"


def _issue(tag, check, severity, value, detail, scope=None) -> dict:
    return {"tag": tag, "check": check, "severity": severity, "scope": scope or _scope(tag),
            "value": value, "detail": detail}


# ------------------------------------------------------------------ inputs ---
def check_inputs(snapshot: dict, recent: pd.DataFrame | None, lim: dict | None = None) -> list[dict]:
    """snapshot: _snapshot() dict {ts, values, source, age_sec}.
    recent: process_live rows of the last ~30 min (ts + tag columns), oldest first."""
    lim = limits() if lim is None else lim
    issues: list[dict] = []
    src = snapshot.get("source")
    if src in (None, "historical"):
        return [_issue("live feed", "freshness", "error", None,
                       "live feed is down - the backend is serving historical data", scope="all")]
    vals = snapshot.get("values") or {}

    r = None
    if recent is not None and len(recent) >= 5:
        r = recent.copy()
        r["t"] = pd.to_datetime(r["ts"], format="mixed", errors="coerce")
        r = r.dropna(subset=["t"]).sort_values("t")

    # Analyser purge: judged as a purge, not as an outlier.
    in_purge: set[str] = set()
    for o2, co in ANALYSERS.items():
        v = _num(vals.get(o2))
        if v is None or v <= PURGE_O2:
            continue
        in_purge.update({o2, co})
        dur = None
        if r is not None and o2 in r:
            s = pd.to_numeric(r[o2], errors="coerce")
            valid_t = r.loc[s <= PURGE_O2, "t"]
            # time since the last valid reading; if none in the window, at least the window length
            dur = (r["t"].iloc[-1] - (valid_t.iloc[-1] if len(valid_t) else r["t"].iloc[0])).total_seconds()
        long = dur is not None and dur > PURGE_MAX_S
        issues.append(_issue(o2, "analyser purge", "error" if long else "warning", v,
                             (f"O2 {v:.1f} % = plant air: analyser purging for {dur/60:.1f} min. "
                              if dur is not None else f"O2 {v:.1f} % = plant air: analyser purging. ")
                             + ("Longer than any normal purge (max seen 7.1 min) - analyser fault suspected; "
                                "safety reading unknown." if long else
                                "Normal blow-back; the twin uses the last valid reading."),
                             scope="all" if long else "info"))

    for tag, l in lim.items():
        if tag in in_purge:
            continue
        v = _num(vals.get(tag))
        important = tag in CHECKED
        if v is None:
            if important:
                issues.append(_issue(tag, "missing", "error", None, "no live value"))
            continue
        if v < l["hard_lo"] or v > l["hard_hi"]:
            why = l.get("physical_rule") or (f"outside the plausible range "
                                             f"{l['hard_lo']:g} to {l['hard_hi']:g}")
            issues.append(_issue(tag, "range", "error" if important else "warning", v, why))
        elif v < l["p001"] or v > l["p999"]:
            issues.append(_issue(tag, "rare value", "warning", v,
                                 f"outside the 0.1-99.9 % band {l['p001']:g} to {l['p999']:g}"))
    for tag, run in RUN_FLAG.items():
        if _num(vals.get(run)) == 0:
            issues.append(_issue(tag, "analyser", "warning", 0, f"{run} = 0 (analyser not running?)"))

    if r is not None and len(r) >= 5:
        now = r["t"].iloc[-1]
        # rows where each analyser was purging: excluded from its own spike/stuck checks
        purge_rows = {}
        for o2, co in ANALYSERS.items():
            if o2 in r:
                p = pd.to_numeric(r[o2], errors="coerce") > PURGE_O2
                purge_rows[o2] = purge_rows[co] = p
        for tag in CHECKED:
            if tag not in r or tag not in lim or tag in in_purge:
                continue
            s = pd.to_numeric(r[tag], errors="coerce")
            ok = s.notna() & ~purge_rows.get(tag, pd.Series(False, index=r.index))
            if ok.sum() < 5:
                continue
            l = lim[tag]
            # spike: latest vs the median of the preceding 2 minutes
            win = s[(r["t"] >= now - pd.Timedelta("120s")) & ok].iloc[:-1]
            latest = s[ok].iloc[-1]
            if len(win) >= 5 and l.get("step_1min"):
                med = float(win.median())
                if abs(latest - med) > max(l["step_1min"], 1e-6):
                    gas = tag in SAFETY            # a gas spike may be a REAL excursion
                    issues.append(_issue(tag, "spike", "warning" if gas else "error", float(latest),
                                         f"jumped {latest - med:+.4g} from its 2-min median {med:.4g}; "
                                         f"the plant's 99.9 % 1-min change is {l['step_1min']:.4g}"
                                         + (" - may be a real excursion, check the process and the analyser"
                                            if gas else "")))
            # stuck: exactly identical for longer than this tag's allowance
            need = float(l.get("stuck_after_s") or 600)
            span = (r["t"].iloc[-1] - r["t"].iloc[0]).total_seconds()
            if span >= need:
                tail = s[(r["t"] >= now - pd.Timedelta(seconds=need)) & ok]
                if len(tail) >= 10 and tail.nunique() == 1:
                    issues.append(_issue(tag, "stuck", "error", float(latest),
                                         f"unchanged at {latest:.6g} for {need/60:.0f}+ min "
                                         f"(normal longest flat run {l.get('flat_run_p99_s', 0):.0f} s)"))
    return issues


# --------------------------------------------------------- recommendations ---
def check_recommendation(rec: dict, live: dict, predicted_temp, interlocks: list | None,
                         plc_range: dict, lim: dict | None = None) -> list[dict]:
    """rec: {lever: value}. live: {tag: value}. plc_range: {tag: (min, max)} from dim_tag."""
    lim = limits() if lim is None else lim
    out: list[dict] = []
    for tag in LEVERS:
        if tag not in rec:
            continue
        v = _num(rec.get(tag))
        if v is None:
            out.append(_issue(tag, "recommendation", "error", rec.get(tag), "not a finite number", scope=tag))
            continue
        if v < 0:
            out.append(_issue(tag, "recommendation", "error", v, "negative", scope=tag))
        lo, hi = plc_range.get(tag, (None, None))
        if lo is not None and hi is not None and not (lo <= v <= hi):
            out.append(_issue(tag, "recommendation", "error", v, f"outside the PLC range {lo:g} to {hi:g}", scope=tag))
        l = lim.get(tag)
        if l and (v < l["hard_lo"] or v > l["hard_hi"]):
            out.append(_issue(tag, "recommendation", "error", v,
                              f"outside the plausible range {l['hard_lo']:g} to {l['hard_hi']:g}", scope=tag))
        cur = _num(live.get(tag))
        if l and cur is not None and l.get("move5_p999"):
            step = abs(v - cur)
            if step > l["move5_p999"]:
                out.append(_issue(tag, "step", "error", v,
                                  f"step {v - cur:+.4g} is larger than 99.9 % of the plant's own 5-min moves "
                                  f"({l['move5_p999']:.4g})", scope=tag))
            elif l.get("move5_p99") and step > l["move5_p99"]:
                out.append(_issue(tag, "step", "warning", v,
                                  f"step {v - cur:+.4g} is larger than 99 % of the plant's own 5-min moves "
                                  f"({l['move5_p99']:.4g})", scope=tag))
    if predicted_temp is not None:
        p, l = _num(predicted_temp), lim.get(CV)
        if p is None:
            out.append(_issue(CV, "prediction", "error", predicted_temp, "predicted temperature is not a number", scope="all"))
        elif l and (p < l["hard_lo"] or p > l["hard_hi"]):
            out.append(_issue(CV, "prediction", "error", p,
                              f"predicted temperature outside the plausible range {l['hard_lo']:g} to {l['hard_hi']:g}",
                              scope="all"))
    for i in interlocks or []:
        if isinstance(i, dict) and i.get("pass") is False:
            out.append(_issue(i.get("tag", "interlock"), "interlock", "error", i.get("predicted"),
                              f"predicted interlock fails: {i.get('rule', '')}", scope="all"))
    return out


# ------------------------------------------------------------------ report ---
def report(input_issues: list[dict], rec_issues: list[dict]) -> dict:
    issues = input_issues + rec_issues
    errors = [i for i in issues if i["severity"] == "error"]
    global_block = any(i["scope"] == "all" for i in errors)
    blocked = sorted({i["scope"] for i in errors if i["scope"] in LEVERS})
    return {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "error" if errors else ("warning" if issues else "ok"),
        "global_block": global_block,
        "blocked_levers": LEVERS if global_block else blocked,
        "counts": {"error": len(errors), "warning": len(issues) - len(errors)},
        "issues": issues,
    }


_EVENTS_DDL = """CREATE TABLE IF NOT EXISTS validation_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tag TEXT, check_name TEXT, severity TEXT, scope TEXT,
  value REAL, detail TEXT, first_seen TEXT, last_seen TEXT, cleared_at TEXT)"""


def log_events(engine, rep: dict) -> None:
    """Keep validation_events in step with the report: a row opens when a
    problem appears, last_seen is refreshed at most once a minute while it
    persists, and cleared_at is set when it goes away. Logged: every ERROR, and
    WARNINGs that affect a recommendation (not routine purge / info noise)."""
    from sqlalchemy import text
    now = dt.datetime.now()
    stamp = now.isoformat(timespec="seconds")
    active = {(i["tag"], i["check"]): i for i in rep.get("issues", [])
              if i["severity"] == "error" or i["scope"] != "info"}
    with engine.begin() as con:
        con.execute(text(_EVENTS_DDL))
        rows = con.execute(text("SELECT id, tag, check_name, last_seen FROM validation_events "
                                "WHERE cleared_at IS NULL")).fetchall()
        open_ = {(r[1], r[2]): (r[0], r[3]) for r in rows}
        for key, i in active.items():
            val = _num(i.get("value"))
            if key in open_:
                rid, last = open_[key]
                if (now - dt.datetime.fromisoformat(last)).total_seconds() >= 60:
                    con.execute(text("UPDATE validation_events SET last_seen=:s, value=:v, detail=:d, "
                                     "severity=:sev WHERE id=:id"),
                                {"s": stamp, "v": val, "d": i["detail"], "sev": i["severity"], "id": rid})
            else:
                con.execute(text("INSERT INTO validation_events (tag, check_name, severity, scope, value, "
                                 "detail, first_seen, last_seen) VALUES (:t, :c, :sev, :sc, :v, :d, :s, :s)"),
                            {"t": key[0], "c": key[1], "sev": i["severity"], "sc": i["scope"], "v": val,
                             "d": i["detail"], "s": stamp})
        for key, (rid, _) in open_.items():
            if key not in active:
                con.execute(text("UPDATE validation_events SET cleared_at=:s WHERE id=:id"),
                            {"s": stamp, "id": rid})


def refuse_accept(tag: str, rep: dict | None) -> str | None:
    """Reason to refuse an Accept on `tag`, or None. No report = cannot check = refuse."""
    if not rep:
        return "Refused by data validation: no current validation result (cannot check the data)."
    errors = [i for i in rep.get("issues", []) if i["severity"] == "error"
              and (i["scope"] == "all" or i["scope"] == tag)]
    if not errors:
        return None
    why = "; ".join(f"{i['tag']} {i['check']}: {i['detail']}" for i in errors[:4])
    return f"Refused by data validation: {why}. Accept is blocked until the data is valid; Decline is still allowed."
