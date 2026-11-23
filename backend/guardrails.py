"""Hard guardrails on the advisor's parameters (roof / floor) = GR4, operating
parameters (kiln feed > 461 t/h is the advice gate in backend/main.py).

Numbering (plant deck): GR1 live CO/O2, GR2 direction, GR3 hot meal DOC,
GR4 operating parameter limits, GR5 clinker quality (twin.optimize.clinker_guard),
GR6 model drift, GR7 uncertain forecast, GR8 total heat change.

The limits live in data/plant_settings.json -> "guardrails", re-read on every
call, so the CCR's confirmed values take effect without a restart:

    "guardrails": {"LAFR_FT": {"min": null, "max": 3928}, ...}

A parameter is BREACHED when its live value or its recommended value is above
``max`` or below ``min``. A breach does two things:

* the advisory endpoints report it (``evaluate``), so both dashboards sound the
  alarm and grey out Accept;
* the decision endpoints refuse an Accept (``refuse_accept``). This is the real
  guardrail -- a disabled button is only cosmetic, anything can still POST.

Declines are never blocked: an operator must always be able to say no.
"""
from __future__ import annotations

import math

import config


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def limits() -> dict[str, dict]:
    """{tag: {"min": float|None, "max": float|None}} for every configured tag."""
    g = config.plant_settings().get("guardrails") or {}
    out = {}
    for tag, v in g.items():
        if tag.startswith("_") or not isinstance(v, dict):
            continue
        lo, hi = _num(v.get("min")), _num(v.get("max"))
        if lo is None and hi is None:
            continue
        out[tag] = {"min": lo, "max": hi}
    return out


def _breach(value: float | None, lim: dict) -> str | None:
    if value is None:
        return None
    if lim["max"] is not None and value > lim["max"]:
        return "above_roof"
    if lim["min"] is not None and value < lim["min"]:
        return "below_floor"
    return None


def evaluate(live: dict, recommended: dict, lim: dict | None = None) -> dict:
    """Check live and recommended values against the limits.

    live / recommended: {tag: value}. For the calciner outlet temperature the
    "recommended" value is the twin's predicted temperature.
    """
    lim = limits() if lim is None else lim
    params, breached = {}, []
    for tag, l in lim.items():
        lv, rv = _num(live.get(tag)), _num(recommended.get(tag))
        lb, rb = _breach(lv, l), _breach(rv, l)
        params[tag] = {"min": l["min"], "max": l["max"], "live": lv, "recommended": rv,
                       "live_breach": lb, "recommended_breach": rb, "breach": bool(lb or rb)}
        if lb or rb:
            breached.append(tag)
    return {"limits": lim, "params": params, "breached": breached,
            "any_breach": bool(breached)}


# GR1: the ACTUAL gas readings against the safety interlocks (config.INTERLOCKS,
# plant_settings "interlocks"). High CO / low O2 now = combustion short of air:
# no Accept may ADD fuel until it clears; cutting fuel is still allowed.
PURGE_O2 = 15.0                                   # O2 above this = analyser purging (plant air)
_PROBE = {"423AM823_CO_PPM": "423AM823_O2", "423AM822_CO_PPM": "423AM822_O2"}


def combustion_settings() -> dict:
    """plant_settings guardrails.combustion: {"avg_sec", "limits": {tag: value}}."""
    return (config.plant_settings().get("guardrails") or {}).get("combustion") or {}


def combustion(live: dict, names: dict | None = None) -> dict:
    """{"breaches": [{tag, name, value, rule}], "any": bool} from live values
    (the caller passes the short-window averages)."""
    over = combustion_settings().get("limits") or {}
    out = []
    for tag, (op, lim) in config.INTERLOCKS.items():
        lim = _num(over.get(tag)) if _num(over.get(tag)) is not None else lim
        v = _num(live.get(tag))
        if v is None:
            continue
        o2 = tag if tag.endswith("_O2") else _PROBE.get(tag)
        if o2 and (_num(live.get(o2)) or 0) > PURGE_O2:
            continue                              # analyser purging: reading is plant air
        if not (v < lim if op == "<" else v > lim):
            out.append({"tag": tag, "name": (names or {}).get(tag, tag), "value": v,
                        "rule": f"{op} {lim:g}"})
    return {"breaches": out, "any": bool(out)}


def refuse_fuel_raise(tag: str, live_v, rec_v, comb: dict) -> str | None:
    """GR1: refuse an Accept that raises fuel (or temperature) while live CO/O2 is unsafe."""
    if not comb.get("any"):
        return None
    lv, rv = _num(live_v), _num(rec_v)
    if lv is not None and rv is not None and rv <= lv:
        return None                               # a cut / hold moves towards safety
    why = "; ".join(f"{b['name']} {b['value']:g} (limit {b['rule']})" for b in comb["breaches"])
    return (f"Refused by combustion guard: live {why}. Accept that adds fuel is blocked "
            "until CO/O2 is back inside its limit; a cut and Decline are still allowed.")


# GR2: direction sanity (a CAUTION since 29 Oct, not a block). The predicted
# T+10 temperature WITH the move lands more than margin_c above the band and the
# move still ADDS total calciner heat, or more than margin_c below it and the
# move still CUTS heat = the move pushes the wrong way.
def direction(pred_temp, band: dict | None, heat: dict | None) -> dict:
    """{"flag", "pred_temp", "band_lo", "band_hi", "heat_change_pct", "reason"}."""
    s = (config.plant_settings().get("guardrails") or {}).get("direction") or {}
    tol = _num(s.get("min_heat_change_pct"))
    tol = 1.0 if tol is None else tol
    margin = _num(s.get("margin_c"))
    margin = 5.0 if margin is None else margin
    t, b, h = _num(pred_temp), band or {}, heat or {}
    lo, hi = _num(b.get("low")), _num(b.get("high"))
    h0, h1 = _num(h.get("current_kcal_h")), _num(h.get("recommended_kcal_h"))
    out = {"flag": False, "pred_temp": t, "band_lo": lo, "band_hi": hi,
           "heat_change_pct": None, "reason": None}
    if None in (t, lo, hi, h0, h1) or h0 <= 0:
        return out
    dh = (h1 - h0) / h0 * 100.0
    out["heat_change_pct"] = round(dh, 2)
    if t > hi + margin and dh > tol:
        out.update(flag=True, reason=f"forecast T+10 {t:.1f} °C is above the band {lo:g}-{hi:g} "
                                     f"but the move adds {dh:+.1f} % calciner heat")
    elif t < lo - margin and dh < -tol:
        out.update(flag=True, reason=f"forecast T+10 {t:.1f} °C is below the band {lo:g}-{hi:g} "
                                     f"but the move cuts {dh:+.1f} % calciner heat")
    return out


# GR6: model drift. The shadow runner checks every T+10 forecast against the
# actual temperature; if the recent error is too large or one-sided, the
# forecasts behind the advice are not trustworthy: Accept blocked until it clears.
def drift_settings() -> dict:
    s = (config.plant_settings().get("guardrails") or {}).get("drift") or {}
    d = {"window_min": 60, "min_points": 30, "max_mae_c": 12.0, "max_abs_bias_c": 8.0}
    return {k: (_num(s.get(k)) if _num(s.get(k)) is not None else v) for k, v in d.items()}


def drift(errors) -> dict:
    """errors: actual - predicted T+10 (degC) of the checks in the window."""
    st = drift_settings()
    e = [x for x in (_num(v) for v in errors) if x is not None]
    out = {"flag": False, "n": len(e), "mae": None, "bias": None, "reason": None, **st}
    if len(e) < st["min_points"]:
        return out
    mae, bias = sum(abs(x) for x in e) / len(e), sum(e) / len(e)
    out.update(mae=round(mae, 2), bias=round(bias, 2))
    bad = []
    if mae > st["max_mae_c"]:
        bad.append(f"T+10 error {mae:.1f} °C above {st['max_mae_c']:g}")
    if abs(bias) > st["max_abs_bias_c"]:
        bad.append(f"forecasts {'too low' if bias > 0 else 'too high'} by {abs(bias):.1f} °C on average")
    if bad:
        out.update(flag=True, reason=f"last {st['window_min']:g} min ({len(e)} checks): " + "; ".join(bad))
    return out


# GR8: total calciner heat change. The recommendation against the LIVE heat
# (2-min average) may change total heat by at most optimizer.heat_balance.
# max_change_pct -- catches held advice the plant has moved away from.
def heat_change(live_kcal_h, rec_kcal_h) -> dict:
    s = (config.plant_settings().get("optimizer") or {}).get("heat_balance") or {}
    cap = _num(s.get("max_change_pct"))
    h0, h1 = _num(live_kcal_h), _num(rec_kcal_h)
    out = {"flag": False, "change_pct": None, "max_change_pct": cap, "reason": None}
    if cap is None or h0 is None or h1 is None or h0 <= 0:
        return out
    pct = (h1 - h0) / h0 * 100.0
    out["change_pct"] = round(pct, 2)
    if abs(pct) > cap:
        out.update(flag=True, reason=f"the move changes total calciner heat by {pct:+.1f} % "
                                     f"against the live heat (max ±{cap:g} %)")
    return out


# GR7: uncertain forecast. The T+10 temperature under the recommendation comes
# with a P10-P90 range; too wide, or reaching the temperature roof / floor =
# the move is not safe to accept.
TEMP_TAG = "423CI400_TT_4"


def uncertainty(temp: dict | None) -> dict:
    """temp: {"mean", "p10", "p90"} of the predicted T+10 temperature."""
    s = (config.plant_settings().get("guardrails") or {}).get("uncertainty") or {}
    max_w = _num(s.get("max_width_c"))
    max_w = 35.0 if max_w is None else max_w
    t = temp or {}
    p10, p90 = _num(t.get("p10")), _num(t.get("p90"))
    out = {"flag": False, "p10": p10, "p90": p90, "width": None, "max_width_c": max_w, "reason": None}
    if p10 is None or p90 is None:
        return out
    out["width"] = round(p90 - p10, 1)
    l = limits().get(TEMP_TAG) or {}
    bad = []
    if p90 - p10 > max_w:
        bad.append(f"T+10 range {p10:.0f}-{p90:.0f} °C is {p90 - p10:.0f} °C wide (max {max_w:g})")
    if l.get("max") is not None and p90 > l["max"]:
        bad.append(f"P90 {p90:.0f} °C reaches above the roof {l['max']:g}")
    if l.get("min") is not None and p10 < l["min"]:
        bad.append(f"P10 {p10:.0f} °C reaches below the floor {l['min']:g}")
    if bad:
        out.update(flag=True, reason="; ".join(bad))
    return out


def _say(tag: str, what: str, value: float, kind: str, l: dict) -> str:
    edge = f"above the roof {l['max']:g}" if kind == "above_roof" else f"below the floor {l['min']:g}"
    return f"{tag} {what} {value:g} is {edge}"


def refuse_accept(tag: str, values: dict[str, float | None]) -> str | None:
    """Reason to refuse accepting `tag`, or None if it may be accepted.

    values: {"live": x, "recommended": y, ...} -- every value we know for this
    parameter, from the server's own snapshot AND from the request; any one of
    them out of limits refuses the Accept.
    """
    l = limits().get(tag)
    if not l:
        return None
    problems = []
    for what, v in values.items():
        kind = _breach(_num(v), l)
        if kind:
            problems.append(_say(tag, what, _num(v), kind, l))
    if not problems:
        return None
    return ("Refused by guardrail: " + "; ".join(problems)
            + ". Accept is blocked while a value is outside its limit; Decline is still allowed.")
