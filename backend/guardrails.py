"""Hard guardrails on the advisor's parameters (roof / floor).

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
