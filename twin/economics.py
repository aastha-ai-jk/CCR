"""Fuel economics for the calciner advisory optimiser — pure calculators over
the four fuel levers (calciner coal + three AFR streams).

GCV / price / density all come from ``config.fuel_economics()`` (estimated
defaults, procurement-editable via data/fuel_economics.json). Every number here
is only as good as that table, so callers should surface the ``is_estimate``
flag to the user until real procurement/QC values are supplied.

ADVISORY / observational only — nothing here writes to any control system.

KPI definitions
---------------
- heat input (kcal/h)      : Σ  rate · mass_per_unit · GCV   over all fuels
- TSR (%)                  : AFR heat / total heat · 100
- specific heat (kcal/kg)  : total heat / clinker mass rate
- fuel cost (₹/ton clinker): total fuel ₹/h / clinker t/h
- clinker t/h              : kiln feed t/h · CLINKER_FACTOR
"""
from __future__ import annotations

import config

COAL = "467RL400_TPH"


def _mass_kg_h(tag: str, rate, econ: dict) -> float:
    """Native rate (t/h or l/h) -> mass flow kg/h. Negative rates clamp to 0."""
    return max(0.0, float(rate or 0.0)) * econ[tag]["mass_per_unit"]


def _heat_per_h(levers: dict, econ: dict) -> tuple[dict, float]:
    """tag -> kcal/h, and the total."""
    per = {t: _mass_kg_h(t, levers.get(t, 0.0), econ) * econ[t]["gcv_kcal_kg"]
           for t in econ}
    return per, float(sum(per.values()))


def _cost_per_h(levers: dict, econ: dict) -> tuple[dict, float]:
    """tag -> ₹/h, and the total."""
    per = {t: _mass_kg_h(t, levers.get(t, 0.0), econ) / 1000.0 * econ[t]["price_per_ton"]
           for t in econ}
    return per, float(sum(per.values()))


def clinker_tph(conditions: dict | None) -> float:
    feed = float((conditions or {}).get(config.KILN_FEED_TAG) or 0.0)
    return max(feed * config.CLINKER_FACTOR, 1e-6)


def kpis(levers: dict, conditions: dict | None, econ: dict | None = None) -> dict:
    """Full economic snapshot for one setpoint vector."""
    econ = econ or config.fuel_economics()
    per_h, heat = _heat_per_h(levers, econ)
    _, rs_h = _cost_per_h(levers, econ)
    cl = clinker_tph(conditions)
    afr_heat = sum(v for t, v in per_h.items() if econ[t]["role"] == "afr")
    afr_total = sum(float(levers.get(t, 0.0)) for t in econ if econ[t]["role"] == "afr")
    return {
        "coal_tph": round(float(levers.get(COAL, 0.0)), 2),
        "afr_total": round(afr_total, 2),
        "tsr_pct": round(100.0 * afr_heat / heat, 2) if heat > 0 else 0.0,
        "specific_heat_kcal_kg": round(heat / (cl * 1000.0), 1) if heat > 0 else 0.0,
        "cost_per_ton": round(rs_h / cl, 1),
        "heat_kcal_h": round(heat, 0),
        "clinker_tph": round(cl, 2),
        "is_estimate": any(econ[t].get("is_estimate") for t in econ),
    }


def savings(rec: dict, base: dict, conditions: dict | None, econ: dict | None = None) -> dict:
    """rec vs base setpoints. Positive = improvement (coal saved, ₹ saved, TSR up)."""
    econ = econ or config.fuel_economics()
    r = kpis(rec, conditions, econ)
    b = kpis(base, conditions, econ)
    return {
        "coal_tph": round(b["coal_tph"] - r["coal_tph"], 2),
        "cost_per_ton": round(b["cost_per_ton"] - r["cost_per_ton"], 1),
        "tsr_pts": round(r["tsr_pct"] - b["tsr_pct"], 2),
        "specific_heat_kcal_kg": round(b["specific_heat_kcal_kg"] - r["specific_heat_kcal_kg"], 1),
        "is_estimate": r["is_estimate"],
    }


def objective_score(levers: dict, conditions: dict | None,
                    mode: str | None = None, econ: dict | None = None) -> float:
    """Scalar to MINIMISE, normalised to ~[-1, 1] so the optimiser's move and
    band-confidence penalties stay comparably weighted across modes."""
    econ = econ or config.fuel_economics()
    mode = mode or config.OPTIMIZER_MODE
    k = kpis(levers, conditions, econ)
    if mode == "least_coal":
        # plant aim: burn as little coal as possible, then as much AFR as the
        # limits allow. Coal dominates (-1 t/h = -0.033); TSR breaks ties
        # (+5 pts = -0.0125), so AFR rises wherever coal cannot fall further.
        return k["coal_tph"] / 30.0 - 0.25 * k["tsr_pct"] / 100.0
    if mode == "coal":
        return k["coal_tph"] / 30.0                    # ~coal engineering range
    if mode == "tsr":
        return -k["tsr_pct"] / 100.0                   # maximise substitution
    return k["cost_per_ton"] / 1000.0                  # cost: ~₹1000/ton nominal scale
