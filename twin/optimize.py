"""Advisory setpoint optimiser for the calciner-side fuel levers.

Searches the four calciner MVs with Optuna to minimise coal while maximising
alternative-fuel firing, subject to two HARD constraints predicted by the GBT
twin: the calciner outlet temperature must land inside the current operating
state's target band, and every configured safety interlock must be satisfied.

ADVISORY ONLY — this module recommends setpoints and never writes to any
control system.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from backend import analysis, db
from twin import model as twin_model

OPT_LEVERS = ["467RL400_TPH", "LAFR_FT", "K3V10_F1", "261WF080_TPH"]
COAL = "467RL400_TPH"                 # minimise
AFR = ["LAFR_FT", "K3V10_F1", "261WF080_TPH"]   # maximise
CV = analysis.CV

# Penalty added to the objective for an infeasible trial so the study still
# converges toward the feasible region even when early trials violate.
_INFEASIBLE_PENALTY = 100.0
_MOVE_W = 0.15        # movement penalty weight (calm, followable advice)
_CONF_W = 0.20        # reward weight for band-confidence (chance constraint)


def _band_confidence(point: dict, blo: float, bhi: float) -> float:
    """P(temp in band) from a (mean, p10, p90) triple via a normal approx."""
    import math
    sigma = max((point["p90"] - point["p10"]) / 2.5631, 1e-6)

    def cdf(v: float) -> float:
        return 0.5 * (1 + math.erf((v - point["mean"]) / (sigma * math.sqrt(2))))

    return max(0.0, min(1.0, cdf(bhi) - cdf(blo)))


def _bounds() -> dict[str, tuple[float, float]]:
    """Lever search bounds from dim_tag y_min/y_max (fallback to median-based)."""
    cat = db.tag_catalog().set_index("tag")
    defaults = analysis.feature_defaults()
    out = {}
    for t in OPT_LEVERS:
        lo, hi = cat.loc[t, "y_min"], cat.loc[t, "y_max"]
        if pd.isna(lo) or pd.isna(hi) or hi <= lo:
            med = float(defaults.get(t, 1.0))
            lo, hi = 0.0, max(med * 2.0, 1.0)
        out[t] = (float(lo), float(hi))
    return out


def _band(conditions: dict) -> tuple[str, float, float, dict]:
    """Operating state + calciner temp band from the state-setting votes."""
    votes = {}
    for key in analysis.STATE_RULES:
        val = conditions.get(key)
        s = analysis._classify_state(key, None if val is None else float(val))
        if s:
            votes[key] = s
    state_num = int(round(float(np.mean(list(votes.values()))))) if votes else 2
    state = f"State {state_num}"
    low, high = config.TEMP_STATE_BANDS[state]
    return state, float(low), float(high), votes


def _interlocks(prediction: dict) -> list[dict]:
    names = db.tag_names()
    out = []
    for tag, (op, lim) in config.INTERLOCKS.items():
        pv = prediction.get(tag, {}).get("mean") if tag in prediction else None
        ok = None if pv is None else (pv < lim if op == "<" else pv > lim)
        out.append({"tag": tag, "name": names.get(tag, tag),
                    "rule": f"{op} {lim}", "predicted": pv, "pass": ok})
    return out


def _norms(levers: dict, bounds: dict) -> tuple[float, float]:
    """coal_norm, afr_norm — each in [0,1] over its lever range."""
    clo, chi = bounds[COAL]
    coal_norm = (levers[COAL] - clo) / (chi - clo) if chi > clo else 0.0
    afr_terms = []
    for t in AFR:
        lo, hi = bounds[t]
        afr_terms.append((levers[t] - lo) / (hi - lo) if hi > lo else 0.0)
    return coal_norm, float(np.mean(afr_terms))


def _f(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def optimize(conditions: dict, n_trials: int = 200) -> dict:
    """Run the Optuna search and return the contract's advisory dict."""
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    conditions = conditions or {}

    if not twin_model.is_available():
        return {"available": False, "advisory": True,
                "error": "GBT twin unavailable (models/twin_gbt.pkl not found)."}

    bounds = _bounds()
    state, blo, bhi, votes = _band(conditions)
    defaults = analysis.feature_defaults()
    base_vals = {t: float(conditions.get(t, defaults.get(t, 0.0))) for t in OPT_LEVERS}

    def objective(trial: "optuna.Trial") -> float:
        levers = {t: trial.suggest_float(t, *bounds[t]) for t in OPT_LEVERS}
        core = twin_model.predict_core(levers, conditions)
        if core is None:
            raise optuna.TrialPruned()
        pred = core["prediction"]
        traj = core.get("trajectory") or [
            {"min": analysis.DELTA, **pred[CV]}]        # fallback: single horizon
        inter = _interlocks(pred)
        ok_inter = all(i["pass"] for i in inter if i["pass"] is not None)

        # trajectory feasibility: the plan must REACH the band at some horizon
        # and still be there at the final one (a sustained move, not a spike)
        reach = next((p["min"] for p in traj if blo <= p["mean"] <= bhi), None)
        settled = traj[-1]
        stays = blo <= settled["mean"] <= bhi
        in_band = reach is not None and stays
        conf = _band_confidence(settled, blo, bhi)      # chance constraint (P10/P90)

        coal_norm, afr_norm = _norms(levers, bounds)
        move_pen = float(np.mean([abs(levers[t] - base_vals[t])
                                  / max(bounds[t][1] - bounds[t][0], 1e-6)
                                  for t in OPT_LEVERS]))
        raw = (coal_norm - afr_norm + _MOVE_W * move_pen - _CONF_W * conf
               + 0.01 * ((reach or 30) / 5.0))          # prefer reaching sooner

        trial.set_user_attr("levers", levers)
        trial.set_user_attr("pred", pred)
        trial.set_user_attr("traj", traj)
        trial.set_user_attr("reach", reach)
        trial.set_user_attr("conf", conf)
        trial.set_user_attr("raw", raw)
        trial.set_user_attr("feasible", bool(in_band and ok_inter))

        penalty = 0.0
        if not in_band:
            penalty += _INFEASIBLE_PENALTY + abs(settled["mean"] - (blo + bhi) / 2.0)
        if not ok_inter:
            penalty += _INFEASIBLE_PENALTY
        return raw + penalty

    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=1))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    scored = [t for t in study.trials if "levers" in t.user_attrs]
    feasible = [t for t in scored if t.user_attrs.get("feasible")]
    pool = feasible or scored
    if not pool:
        return {"available": True, "advisory": True,
                "error": "Optimiser produced no valid trials.", "n_trials": n_trials}

    # Feasible trials: rank by the true objective (min coal / max AFR). If NONE are
    # feasible (e.g. current temp is far below the state band and one 5-min step
    # cannot close the gap), rank by the full penalised value so we recommend the
    # setpoints that move CLOSEST to the band — never the furthest (min-coal) point.
    if feasible:
        best = min(pool, key=lambda t: t.user_attrs["raw"])
    else:
        best = min(pool, key=lambda t: t.value if t.value is not None else float("inf"))
    rec = best.user_attrs["levers"]
    pred = best.user_attrs["pred"]
    rec_temp = pred[CV]["mean"]
    rec_traj = best.user_attrs.get("traj")
    rec_reach = best.user_attrs.get("reach")
    rec_conf = best.user_attrs.get("conf")

    # Baseline = current levers (fallback to recent medians) at current conditions.
    base_vals = {t: float(conditions.get(t, defaults.get(t, 0.0))) for t in OPT_LEVERS}
    base_core = twin_model.predict_core(base_vals, conditions)
    base_temp = base_core["prediction"][CV]["mean"] if base_core else None

    interlocks = _interlocks(pred)
    in_band = blo <= rec_temp <= bhi

    explanation = [
        f"Operating state {state}: target band {int(blo)}-{int(bhi)} °C.",
        f"Recommended coal {COAL} = {rec[COAL]:.1f} tph "
        f"({rec[COAL] - base_vals[COAL]:+.1f} vs current).",
        f"Recommended AFR total = {sum(rec[t] for t in AFR):.1f} tph "
        f"({sum(rec[t] for t in AFR) - sum(base_vals[t] for t in AFR):+.1f} vs current).",
        f"Predicted calciner temp {rec_temp:.1f} °C "
        + ("inside the target band."
           if in_band else
           "OUTSIDE the band — the gap is larger than one 5-min step can close; "
           "these setpoints move firing in the correct direction toward the band."),
        (f"Trajectory reaches the band in ~{rec_reach} min and holds it; "
         f"P(in band at t+30) ≈ {rec_conf:.0%}."
         if rec_reach is not None and rec_conf is not None else
         "Trajectory heads unavailable — single-horizon advisory."),
        ("All predicted interlocks satisfied."
         if all(i["pass"] for i in interlocks if i["pass"] is not None)
         else "WARNING: an interlock is predicted violated."),
        f"Searched {n_trials} trials; {len(feasible)} feasible. Advisory only — "
        "not written to any control system.",
    ]

    return {
        "recommended": {t: _f(rec[t]) for t in OPT_LEVERS},
        "predicted": {
            "temp": {"mean": _f(rec_temp, 1), "p10": _f(pred[CV]["p10"], 1),
                     "p90": _f(pred[CV]["p90"], 1)},
            "safety": {tag: _f(pred[tag]["mean"], 1)
                       for tag in analysis.SAFETY if tag in pred},
        },
        "operating_state": state,
        "target_band": {"low": _f(blo, 1), "high": _f(bhi, 1)},
        "in_band": bool(in_band),
        "state_votes": votes,
        "interlocks": [{**i, "predicted": _f(i["predicted"], 1)} for i in interlocks],
        "objective": {"coal": _f(rec[COAL]), "afr_total": _f(sum(rec[t] for t in AFR))},
        "baseline": {"levers": {t: _f(base_vals[t]) for t in OPT_LEVERS},
                     "temp": _f(base_temp, 1)},
        "delta": {
            "coal": _f(rec[COAL] - base_vals[COAL]),
            "afr": _f(sum(rec[t] for t in AFR) - sum(base_vals[t] for t in AFR)),
            "temp": _f(None if base_temp is None else rec_temp - base_temp, 1),
        },
        "explanation": explanation,
        "n_trials": int(n_trials),
        "advisory": True,
        # v3: predicted path under the recommendation + chance-constraint info
        "trajectory": rec_traj,
        "reach_min": rec_reach,
        "band_confidence": _f(rec_conf, 3),
    }
