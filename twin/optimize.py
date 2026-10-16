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
from twin import economics

OPT_LEVERS = ["467RL400_TPH", "LAFR_FT", "K3V10_F1", "261WF080_TPH"]
COAL = "467RL400_TPH"                 # minimise
AFR = ["LAFR_FT", "K3V10_F1", "261WF080_TPH"]   # maximise
CV = analysis.CV
# Kiln-side levers. The plant loops (shadow, optimizer_loop) search OPT_LEVERS
# only; the CCR advisor searches ADVISOR_LEVERS.
KILN_COAL = "467RL300_TPH"
KILN_FEED = config.KILN_FEED_TAG
KILN_SIDE = [KILN_COAL, KILN_FEED, "423FN010_ST", "433MD140_ST1"]
# Plant team (2026-10-08): kiln coal, kiln feed, PH fan speed and kiln speed
# stay FIXED — never recommended. They remain model inputs at their live values,
# so the advisor searches the same 4 calciner fuel levers as the plant loops.
FIXED_LEVERS = [KILN_COAL, KILN_FEED, "423FN010_ST", "433MD140_ST1"]
ADVISOR_LEVERS = list(OPT_LEVERS)

# Penalty added to the objective for an infeasible trial so the study still
# converges toward the feasible region even when early trials violate.
_INFEASIBLE_PENALTY = 100.0
_MOVE_W = 0.15        # movement penalty weight (calm, followable advice)
_CONF_W = 0.20        # reward weight for band-confidence (chance constraint)
# TRUST REGION: search only near the CURRENT setpoints. The model is trained on
# closed-loop data, so its causal validity decays away from observed operation —
# a full-range search lets the optimizer exploit model insensitivity (e.g.
# "coal=0 tph looks fine"). ±15% of the engineering range per advisory is also
# roughly what an operator can responsibly move in one 30-min window.
_TRUST_FRAC = 0.15
# Kiln-side levers move the whole kiln, not just the calciner, so they get a much
# tighter trust region (±3% of range ≈ ±16 tph feed, ±0.18 rpm kiln speed,
# ±45 rpm PH fan, ±0.45 tph kiln coal). PLACEHOLDER — needs process sign-off.
_TRUST_FRAC_KILN = 0.03


def _band_confidence(point: dict, blo: float, bhi: float) -> float:
    """P(temp in band) from a (mean, p10, p90) triple via a normal approx."""
    import math
    sigma = max((point["p90"] - point["p10"]) / 2.5631, 1e-6)

    def cdf(v: float) -> float:
        return 0.5 * (1 + math.erf((v - point["mean"]) / (sigma * math.sqrt(2))))

    return max(0.0, min(1.0, cdf(bhi) - cdf(blo)))


def _opt_settings() -> dict:
    """data/plant_settings.json "optimizer" section, re-read every run."""
    return config.plant_settings().get("optimizer") or {}


def _lever_settings(tag: str) -> dict:
    return ((_opt_settings().get("levers") or {}).get(tag)) or {}


_OBSERVED: dict = {}


def _observed_limits() -> dict[str, tuple[float, float]]:
    """1st-99th percentile of each lever while the kiln is feeding: what the
    plant actually runs. The PLC engineering ranges are far wider (SAFR 0-30,
    LAFR PC 0-9,000 l/h) and would let 'most AFR' push past real operation."""
    if not _OBSERVED:
        try:
            tags = [*OPT_LEVERS, KILN_COAL]
            d = db.q("SELECT " + ", ".join(f'"{t}"' for t in tags)
                     + " FROM process_1min WHERE feed_gate=1").apply(pd.to_numeric, errors="coerce")
            for t in tags:
                v = d[t].dropna()
                if len(v) > 1000:
                    _OBSERVED[t] = (float(v.quantile(0.01)), float(v.quantile(0.99)))
        except Exception:
            pass
    return _OBSERVED


def _limit_info(levers: list[str]) -> dict[str, dict]:
    """Each lever's min/max and where it came from: plant / observed / plc."""
    cat = db.tag_catalog().set_index("tag")
    obs = _observed_limits()
    out = {}
    for t in levers:
        ls = _lever_settings(t)
        src, (lo, hi) = "plc", (cat.loc[t, "y_min"], cat.loc[t, "y_max"])
        if t in obs:
            src, (lo, hi) = "observed", obs[t]
        if ls.get("min") is not None or ls.get("max") is not None:
            src = "plant"
            lo = ls["min"] if ls.get("min") is not None else lo
            hi = ls["max"] if ls.get("max") is not None else hi
        out[t] = {"min": _f(lo), "max": _f(hi), "source": src}
    return out


def _bounds(levers: list[str] = OPT_LEVERS) -> dict[str, tuple[float, float]]:
    """Lever search bounds: plant-confirmed min/max when set, else the observed
    operating range (1st-99th percentile), else dim_tag y_min/y_max (fallback
    to median-based)."""
    cat = db.tag_catalog().set_index("tag")
    defaults = analysis.feature_defaults()
    obs = _observed_limits()
    out = {}
    for t in levers:
        lo, hi = cat.loc[t, "y_min"], cat.loc[t, "y_max"]
        if pd.isna(lo) or pd.isna(hi) or hi <= lo:
            med = float(defaults.get(t, 1.0))
            lo, hi = 0.0, max(med * 2.0, 1.0)
        if t in obs and obs[t][1] > obs[t][0]:
            lo, hi = obs[t]
        ls = _lever_settings(t)
        lo = ls["min"] if ls.get("min") is not None else lo
        hi = ls["max"] if ls.get("max") is not None else hi
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


def _quality_ok(quality: dict | None) -> bool:
    """True unless a PREDICTED clinker-quality value is out of config.QUALITY_SPECS.
    None / missing predictions are unknown -> not treated as a violation."""
    if not quality:
        return True
    fl = quality.get("free_lime")
    doc = quality.get("doc")
    fl_max = config.QUALITY_SPECS.get("free_lime", {}).get("max")
    doc_min = config.QUALITY_SPECS.get("doc", {}).get("min")
    if fl is not None and fl_max is not None and float(fl) > float(fl_max):
        return False
    if doc is not None and doc_min is not None and float(doc) < float(doc_min):
        return False
    return True


def clinker_guard(clinker: dict | None) -> dict:
    """Lab-based clinker-quality guard (the free-lime / DOC models are too weak
    to judge recommendations, R2 < 0). From the LATEST clinker lab result vs
    config.CLINKER_ZONE: under-burnt (free lime above max, liter weight below
    min) -> no cut in total calciner heat; over-burnt (free lime below min,
    liter weight above max) -> no rise. Inactive without a result younger than
    config.CLINKER_LAB_MAX_AGE_H."""
    import datetime as _dt
    z = config.CLINKER_ZONE
    c = clinker or {}
    out = {"active": False, "direction": None, "status": "no_recent_lab", "reasons": [],
           "free_lime": c.get("free_lime"), "liter_weight": c.get("liter_weight"),
           "lab_ts": c.get("ts"), "age_h": None,
           "zone": {k: dict(v) for k, v in z.items()},
           "max_age_h": config.CLINKER_LAB_MAX_AGE_H}
    try:
        lab = pd.Timestamp(str(c.get("ts")).replace(" ", "T")[:19])
        now = pd.Timestamp(c["now"]) if c.get("now") else pd.Timestamp(_dt.datetime.now())
        out["age_h"] = round((now - lab).total_seconds() / 3600.0, 2)
    except Exception:
        return out
    if out["age_h"] is None or out["age_h"] > config.CLINKER_LAB_MAX_AGE_H or out["age_h"] < -1:
        return out
    fl, lw = c.get("free_lime"), c.get("liter_weight")
    under, over = [], []
    if fl is not None and z["free_lime"]["max"] is not None and fl > z["free_lime"]["max"]:
        under.append(f"free lime {fl:.2f}% above {z['free_lime']['max']:g}%")
    if lw is not None and z["liter_weight"]["min"] is not None and lw < z["liter_weight"]["min"]:
        under.append(f"liter weight {lw:.0f} below {z['liter_weight']['min']:g} g/l")
    if fl is not None and z["free_lime"]["min"] is not None and fl < z["free_lime"]["min"]:
        over.append(f"free lime {fl:.2f}% below {z['free_lime']['min']:g}%")
    if lw is not None and z["liter_weight"]["max"] is not None and lw > z["liter_weight"]["max"]:
        over.append(f"liter weight {lw:.0f} above {z['liter_weight']['max']:g} g/l")
    if under and not over:
        out.update(active=True, direction="no_heat_cut", status="under_burnt", reasons=under)
    elif over and not under:
        out.update(active=True, direction="no_heat_rise", status="over_burnt", reasons=over)
    elif under and over:                     # conflicting signals: hold heat steady
        out.update(active=True, direction="hold_heat", status="conflicting", reasons=under + over)
    else:
        out["status"] = "in_zone"
    return out


def _f(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def optimize(conditions: dict, n_trials: int = 200,
             levers: list[str] | None = None, clinker: dict | None = None,
             objective_mode: str | None = None) -> dict:
    """Run the Optuna search and return the contract's advisory dict.

    ``levers`` picks which MVs to search (default: the four calciner fuel
    levers). Levers outside the search stay at their current values.
    ``clinker`` (the latest clinker lab result: ts, free_lime, liter_weight)
    switches the quality check from the predicted-quality models to the
    lab-based clinker_guard. The plant loops pass none and are unchanged.
    """
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    conditions = conditions or {}
    opt_levers = list(levers or OPT_LEVERS)

    if not twin_model.is_available():
        return {"available": False, "advisory": True,
                "error": "GBT twin unavailable (models/twin_gbt.pkl not found)."}

    full_bounds = _bounds(opt_levers)
    state, blo, bhi, votes = _band(conditions)
    defaults = analysis.feature_defaults()
    base_vals = {t: float(conditions.get(t, defaults.get(t, 0.0))) for t in opt_levers}

    # Economics over the searched levers. Kiln coal is the same coal as the
    # calciner's, so price it identically — otherwise it would look like free
    # heat. A searched kiln feed changes the clinker rate (₹/ton denominator).
    econ = config.fuel_economics()
    if KILN_COAL in opt_levers and KILN_COAL not in econ:
        econ = {**econ, KILN_COAL: dict(econ[COAL])}

    def econ_cond(lv: dict) -> dict:
        c = dict(conditions)
        c[KILN_FEED] = lv.get(KILN_FEED, c.get(KILN_FEED, defaults.get(KILN_FEED)))
        return c

    # trust region: current setpoint ± _TRUST_FRAC of the engineering range,
    # clipped to the engineering limits
    bounds: dict[str, tuple[float, float]] = {}
    for t in opt_levers:
        lo, hi = full_bounds[t]
        c = min(max(base_vals[t], lo), hi)
        step = _lever_settings(t).get("max_step")
        if step is not None:
            r = float(step)                      # plant-confirmed largest single step
        else:
            # half-width: ±15% of the engineering range OR ±100% of the current
            # value, whichever is SMALLER (engineering ranges are much wider than
            # the operating envelope for some tags), floored at ±1 unit
            frac = _TRUST_FRAC_KILN if t in KILN_SIDE else _TRUST_FRAC
            r = min((hi - lo) * frac, max(abs(c), 1.0))
        bounds[t] = (max(lo, c - r), min(hi, c + r))
    obj = _opt_settings().get("objective") or {}
    # plant setting > caller's mode (advisor: least_coal) > OPTIMIZER_MODE env
    mode = obj.get("mode") or objective_mode or config.OPTIMIZER_MODE
    move_w = float(obj["move_weight"]) if obj.get("move_weight") is not None else _MOVE_W
    conf_w = float(obj["confidence_weight"]) if obj.get("confidence_weight") is not None else _CONF_W

    # Lab-based clinker guard (advisor): compares each trial's total calciner
    # heat with the current heat input of the same levers.
    guard = clinker_guard(clinker) if clinker is not None else None
    base_heat = economics._heat_per_h(base_vals, econ)[1]

    def heat_ok(lv: dict) -> bool:
        if not guard or not guard["active"]:
            return True
        h = economics._heat_per_h(lv, econ)[1]
        tol = 0.002 * max(base_heat, 1.0)            # rounding slack (0.2%)
        if guard["direction"] == "no_heat_cut":
            return h >= base_heat - tol
        if guard["direction"] == "no_heat_rise":
            return h <= base_heat + tol
        return abs(h - base_heat) <= 0.01 * max(base_heat, 1.0)   # hold_heat: ±1%

    # AFR cap (max TSR %) and total-heat rule (max +-% change vs current), both
    # from plant_settings.json; None = not applied.
    afr_s = _opt_settings().get("afr") or {}
    max_tsr = afr_s.get("max_tsr_pct")
    max_heat_pct = (_opt_settings().get("heat_balance") or {}).get("max_change_pct")

    def limits_excess(lv: dict) -> float:
        """How far a setpoint set breaks the AFR cap (TSR points over) and the
        heat rule (% beyond the allowed change), plus the clinker guard (% of
        heat cut/raised against it). 0 = every limit met. Graded, so when no
        option meets them all the search still moves toward the least breach."""
        ex = 0.0
        h = economics._heat_per_h(lv, econ)[1]
        if max_tsr is not None:
            ex += max(0.0, economics.kpis(lv, econ_cond(lv), econ)["tsr_pct"] - float(max_tsr))
        if max_heat_pct is not None and base_heat > 0:
            ex += max(0.0, abs(h - base_heat) / base_heat * 100.0 - float(max_heat_pct))
        if guard and guard["active"] and not heat_ok(lv) and base_heat > 0:
            ex += abs(h - base_heat) / base_heat * 100.0
        return ex

    def objective(trial: "optuna.Trial") -> float:
        levers = {t: trial.suggest_float(t, *bounds[t]) for t in opt_levers}
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

        # HARD quality constraint. Advisor (clinker given): the lab-based guard
        # on total calciner heat. Plant loops: predicted quality vs spec, where
        # missing predictions (None) are unknown, not a violation.
        q_ok = True if guard is not None else _quality_ok(core.get("quality"))
        excess = limits_excess(levers)                  # clinker guard + AFR cap + heat rule
        ok_quality = q_ok and excess == 0.0

        # Economic objective (config.OPTIMIZER_MODE): min ₹/ton | max TSR | min
        # coal. Replaces the old abstract coal_norm - afr_norm. Move + band-
        # confidence terms keep the advice calm and chance-constrained.
        econ_score = economics.objective_score(levers, econ_cond(levers), mode=mode, econ=econ)
        move_pen = float(np.mean([abs(levers[t] - base_vals[t])
                                  / max(bounds[t][1] - bounds[t][0], 1e-6)
                                  for t in opt_levers]))
        raw = (econ_score + move_w * move_pen - conf_w * conf
               + 0.01 * ((reach or 30) / 5.0))          # prefer reaching sooner

        trial.set_user_attr("levers", levers)
        trial.set_user_attr("pred", pred)
        trial.set_user_attr("traj", traj)
        trial.set_user_attr("reach", reach)
        trial.set_user_attr("conf", conf)
        trial.set_user_attr("raw", raw)
        trial.set_user_attr("quality", core.get("quality"))
        trial.set_user_attr("contributions", core.get("contributions"))
        trial.set_user_attr("feasible", bool(in_band and ok_inter and ok_quality))

        penalty = 0.0
        if not in_band:
            penalty += _INFEASIBLE_PENALTY + abs(settled["mean"] - (blo + bhi) / 2.0)
        if not ok_inter:
            penalty += _INFEASIBLE_PENALTY
        if not q_ok:
            penalty += _INFEASIBLE_PENALTY
        if excess > 0.0:
            penalty += _INFEASIBLE_PENALTY + 10.0 * excess
        trial.set_user_attr("limits_excess", excess)
        return raw + penalty

    # Baseline = current levers at current conditions, predicted BEFORE the
    # search: the live history moves on during a run (fast in replay), and the
    # baseline must describe the same moment as the trials it is compared with.
    base_core = twin_model.predict_core(base_vals, conditions)
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

    # Baseline (current levers) was predicted BEFORE the search, from the same
    # live history the trials used (see above).
    base_temp = base_core["prediction"][CV]["mean"] if base_core else None

    interlocks = _interlocks(pred)
    in_band = blo <= rec_temp <= bhi

    # Economic KPIs for the recommendation + baseline, and the savings between.
    # ₹/ton and specific-heat need the kiln feed (clinker rate); if the caller
    # didn't pass it, fall back to the recent-median feed so cost never explodes.
    # Each side uses its own feed when kiln feed is a searched lever.
    rec_kpis = economics.kpis(rec, econ_cond(rec), econ)
    base_kpis = economics.kpis(base_vals, econ_cond(base_vals), econ)
    fuel_savings = {
        "coal_tph": round(base_kpis["coal_tph"] - rec_kpis["coal_tph"], 2),
        "cost_per_ton": round(base_kpis["cost_per_ton"] - rec_kpis["cost_per_ton"], 1),
        "tsr_pts": round(rec_kpis["tsr_pct"] - base_kpis["tsr_pct"], 2),
        "specific_heat_kcal_kg": round(base_kpis["specific_heat_kcal_kg"]
                                       - rec_kpis["specific_heat_kcal_kg"], 1),
        "is_estimate": rec_kpis["is_estimate"],
    }
    rec_quality = best.user_attrs.get("quality")
    # Per-lever attribution: temp pull (°C, from the twin's SHAP) alongside each
    # lever's marginal effect on cost and TSR — the "what drives what" view.
    temp_pull = best.user_attrs.get("contributions") or {}
    sensitivities = {}
    for t in opt_levers:
        bump = dict(rec)
        bump[t] = rec[t] + 1.0                          # per-unit marginal effect
        dk = economics.kpis(bump, econ_cond(bump), econ)
        sensitivities[t] = {
            "temp_pull_c": _f(temp_pull.get(t)),
            "d_cost_per_unit": _f(dk["cost_per_ton"] - rec_kpis["cost_per_ton"], 3),
            "d_tsr_per_unit": _f(dk["tsr_pct"] - rec_kpis["tsr_pct"], 3),
        }

    explanation = [
        f"Operating state {state}: target band {int(blo)}-{int(bhi)} °C.",
        f"Recommended coal {COAL} = {rec[COAL]:.1f} tph "
        f"({rec[COAL] - base_vals[COAL]:+.1f} vs current).",
        f"Recommended AFR total = {sum(rec[t] for t in AFR):.1f} tph "
        f"({sum(rec[t] for t in AFR) - sum(base_vals[t] for t in AFR):+.1f} vs current).",
        # deltas read "recommended minus current" (savings dict keeps its own
        # positive-is-better convention for the KPI cards)
        (f"Fuel: {rec_kpis['cost_per_ton']:.0f} ₹/t clinker "
         f"({-fuel_savings['cost_per_ton']:+.0f} ₹/t vs current), TSR {rec_kpis['tsr_pct']:.1f}% "
         f"({fuel_savings['tsr_pts']:+.1f} pts), coal {-fuel_savings['coal_tph']:+.1f} t/h vs current. "
         + ("[economics are ESTIMATED defaults — edit data/fuel_economics.json]"
            if rec_kpis["is_estimate"] else "")).strip(),
        (("Clinker quality guard: " + "; ".join(guard["reasons"]) + {
            "no_heat_cut": " — total calciner heat may not be cut.",
            "no_heat_rise": " — total calciner heat may not be raised.",
            "hold_heat": " — conflicting signals, total heat held within ±1%."}[guard["direction"]])
         if guard and guard["active"] else
         ("Clinker in quality zone (latest lab result)." if guard and guard["status"] == "in_zone"
          else "No clinker lab result in the last "
               f"{config.CLINKER_LAB_MAX_AGE_H:g} h — clinker guard inactive." if guard else
          (f"Predicted clinker quality: free-lime {rec_quality.get('free_lime')}, "
           f"DOC {rec_quality.get('doc')} — within spec."
           if rec_quality else "Clinker-quality prediction unavailable this tick."))),
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
        "recommended": {t: _f(rec[t]) for t in opt_levers},
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
        "baseline": {"levers": {t: _f(base_vals[t]) for t in opt_levers},
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
        # v4 (fuel optimiser): economics, quality gate, per-lever attribution
        "mode": mode,
        "economics": {"recommended": rec_kpis, "baseline": base_kpis,
                      "savings": fuel_savings},
        "quality": {"predicted": rec_quality, "specs": config.QUALITY_SPECS},
        "clinker_guard": guard,
        "lever_limits": _limit_info(opt_levers),
        "afr_rules": {"max_tsr_pct": max_tsr, "max_heat_change_pct": max_heat_pct},
        # False = no option met band + interlocks + limits; the recommendation
        # is the closest one, and the screen must say so
        "all_limits_met": bool(feasible),
        "limits_excess": _f(best.user_attrs.get("limits_excess"), 2),
        "heat": {"current_kcal_h": _f(base_heat, 0),
                 "recommended_kcal_h": _f(economics._heat_per_h(rec, econ)[1], 0)},
        "sensitivities": sensitivities,
    }
