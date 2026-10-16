"""Analysis engine: correlation/lag tools + the trial surrogate 'twin'.

The surrogate is deliberately transparent and always-physical: it answers a
what-if by finding the most similar *real historical* operating states and
reporting what the calciner actually did. It also runs the deterministic
operating-window state selection and interlock checks from the logic doc.
The production twin (LightGBM + NN, Phase 2/3) will replace the kNN core
behind the same API.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

import config
from backend import db

CV = "423CI400_TT_4"        # controlled variable: PC outlet temperature

# The 8 MANIPULATED VARIABLES (actuators) = the simulator's control levers.
MV = ["467RL400_TPH", "LAFR_FT", "261WF080_TPH", "K3V10_F1",
      "467RL300_TPH", "353RL130_TPH", "423FN010_ST", "433MD140_ST1"]
FEATURES = MV               # /simulate/defaults + overview build levers from this
# PLC state-setting variables — matched on (confounders) and vote the target band
STATE_PLC = ["433MD140_TQ", "423AM822_NO", "443KH050_TT1"]
SAFETY = ["423AM823_CO_PPM", "423AM823_O2", "423AM822_CO_PPM", "423AM822_O2"]
TARGETS = [CV] + SAFETY
DELTA = 5                   # forward horizon (min) ~ calciner thermal dead-time

# ---- CARDINAL RELATIONSHIPS (PC_Temp_Variable_Mapping.docx, sections 3/6/7) ----
# Each MV's pull on PC outlet temperature: (sign, strength tier).
MV_TEMP = {
    "467RL400_TPH": (+1, "strong"),    # Calciner coal   — Direct, strong (+)
    "LAFR_FT":      (+1, "direct"),    # LAFR feed - PC  — Direct (+)
    "261WF080_TPH": (+1, "direct"),    # SAFR            — Direct (+)
    "K3V10_F1":     (+1, "indirect"),  # LAFR feed - TAD — Indirect (+), via TAD
    "467RL300_TPH": (+1, "indirect"),  # Kiln coal       — Indirect (+)
    "353RL130_TPH": (-1, "direct"),    # Kiln feed       — Direct (-), dilution
    "423FN010_ST":  (-1, "direct"),    # PH fan speed    — Direct (-), draft/dilution
    "433MD140_ST1": (-1, "weak"),      # Kiln speed      — Indirect/weak, context
}
# strength tier -> °C swing for a full operating-range move (relative; tunable)
TIER_DEG = {"strong": 90.0, "direct": 45.0, "indirect": 22.0, "weak": 8.0}

# MV -> combustion-safety signal effects (full-range coefficient; directions
# from mapping doc tables 6/7/8: fuels consume O2 & raise CO, draft adds O2).
SAFETY_EFFECT = {
    "423AM823_O2":     {"467RL400_TPH": -3, "LAFR_FT": -3, "261WF080_TPH": -3,
                        "K3V10_F1": -2, "423FN010_ST": +4},
    "423AM823_CO_PPM": {"467RL400_TPH": +500, "LAFR_FT": +300, "261WF080_TPH": +300,
                        "K3V10_F1": +200, "423FN010_ST": -400},
    "423AM822_O2":     {"467RL300_TPH": -3, "423FN010_ST": +3},
    "423AM822_CO_PPM": {"467RL300_TPH": +500, "423FN010_ST": -300},
}
RELATIONSHIP_LABEL = {"strong": "Direct, strong", "direct": "Direct",
                      "indirect": "Indirect", "weak": "Weak / context"}

# State selection thresholds from the Combustion Control logic doc.
# direction 'up'  : value rises  -> higher state number (lower temp band)
# direction 'down': value falls  -> higher state number
STATE_RULES = {
    "liquid":       ("down_special", [(28.5, 29.0), (29.0, 29.5), (29.5, 99)]),
    "433MD140_TQ":  ("up", [(360, 380), (380, 400), (400, 9999)]),
    "423AM822_NO":  ("up", [(450, 550), (550, 650), (650, 9999)]),
    "443KH050_TT1": ("up", [(980, 1030), (1030, 1080), (1080, 9999)]),
    "free_lime":    ("down", [(1.4, 1.8), (1.0, 1.4), (0.0, 1.0)]),
    "liter_weight": ("up", [(1100, 1150), (1150, 1200), (1200, 9999)]),
    "doc":          ("up", [(92, 94), (94, 95), (95, 96)]),
}
# Plant-confirmed thresholds (data/plant_settings.json "state_rules": key ->
# three [low, high] pairs for State 1/2/3) replace the defaults above.
for _key, _bands in ((config.plant_settings().get("state_rules") or {}).items()):
    if _key in STATE_RULES and _bands and all(b and None not in b for b in _bands):
        STATE_RULES[_key] = (STATE_RULES[_key][0], [tuple(map(float, b)) for b in _bands])


def _classify_state(key: str, value: float | None) -> int | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    _, bands = STATE_RULES[key]
    for i, (lo, hi) in enumerate(bands, start=1):
        if lo <= value < hi:
            return i
    # out of listed bands: clamp to nearest end
    if value < bands[0][0]:
        return 1
    return 3


@lru_cache(maxsize=1)
def _matrix():
    """Matched forward-Δ training set (in-load, gap-safe, sampled).

    Confounders C = [current temp, temp trend, PLC state vars]; manipulated
    variables L (the 8 actuators); current safety signals S; and the forward
    temperature change y = temp(t+DELTA) - temp(t). We match neighbours on C
    to fix the operating regime, then apply the mapping-doc relationships on L."""
    need = [CV] + MV + STATE_PLC + SAFETY
    cols = ", ".join(f'"{c}"' for c in dict.fromkeys(need))
    df = db.q(f"SELECT ts, {cols} FROM process_1min WHERE feed_gate=1 ORDER BY ts")
    df["ts"] = pd.to_datetime(df["ts"])
    for c in need:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    temp = df[CV]
    df["temp_trend"] = temp - temp.shift(DELTA)
    df["dt_past"] = (df["ts"] - df["ts"].shift(DELTA)).dt.total_seconds()
    df["dtemp"] = temp.shift(-DELTA) - temp
    df["dt_fwd"] = (df["ts"].shift(-DELTA) - df["ts"]).dt.total_seconds()
    df = df[df["dt_fwd"].between(4 * 60, 7 * 60) & df["dt_past"].between(4 * 60, 7 * 60)]

    conf = [CV, "temp_trend"] + STATE_PLC
    df = df.dropna(subset=conf + MV + SAFETY + ["dtemp"])
    if len(df) > 60000:
        df = df.sample(60000, random_state=1)

    cat = db.tag_catalog().set_index("tag")
    ranges = {}
    for t in MV:
        lo, hi = cat.loc[t, "y_min"], cat.loc[t, "y_max"]
        span = (hi - lo) if pd.notna(lo) and pd.notna(hi) and hi > lo else None
        ranges[t] = float(span) if span else float(max(df[t].std() * 4, 1.0))

    C = df[conf].to_numpy(float)
    cmu, csd = C.mean(0), C.std(0); csd[csd == 0] = 1.0
    return {
        "C": C, "Cn": (C - cmu) / csd, "L": df[MV].to_numpy(float),
        "S": df[SAFETY].to_numpy(float), "y": df["dtemp"].to_numpy(float),
        "cmu": cmu, "csd": csd, "ranges": ranges,
        "lever_med": dict(zip(MV, np.median(df[MV].to_numpy(float), 0))),
        "state_med": dict(zip(STATE_PLC, np.median(df[STATE_PLC].to_numpy(float), 0))),
        "temp_med": float(np.median(df[CV])),
    }


def feature_defaults() -> dict:
    return dict(_matrix()["lever_med"])


def simulate(levers: dict, conditions: dict | None = None, k: int = 400) -> dict:
    """Relationship-driven what-if. Match neighbours on the operating regime
    (current temp + PLC state), then move PC temperature and the safety signals
    per the cardinal relationships in PC_Temp_Variable_Mapping.docx — every
    manipulated variable pulls temperature by its documented direction/strength."""
    conditions = conditions or {}

    def R(v):
        return round(float(v), 1)   # native float — FastAPI JSON rejects np.float64

    # ---- Trained GBT twin first; kNN surrogate stays the fallback ------------
    # A twin failure must never break the endpoint — swallow and fall back.
    core = None
    try:
        from twin import model as twin_model
        core = twin_model.predict_core(levers, conditions)
    except Exception:
        core = None

    m = _matrix()
    T0 = float(conditions.get("current_temp") or m["temp_med"])
    lv = {t: float(levers.get(t, m["lever_med"][t])) for t in MV}

    if core is not None:
        # Use the twin's prediction + SHAP contributions; skip the kNN core but
        # keep the shared state/band/interlock/return logic below.
        pred = core["prediction"]
        contributions = {t: R(core["contributions"].get(t, 0.0)) for t in MV}
        method = core.get("method", "gbt-twin")
        neighbors = 0
    else:
        trend = float(conditions.get("temp_trend", 0.0))
        state_v = [float(conditions.get(s, m["state_med"][s])) for s in STATE_PLC]
        cq = (np.array([T0, trend] + state_v) - m["cmu"]) / m["csd"]

        d = np.sqrt(((m["Cn"] - cq) ** 2).sum(1))
        k = min(k, len(d) - 1)
        idx = np.argpartition(d, k)[:k]
        w = 1.0 / (d[idx] + 1e-6)
        yk, Sk = m["y"][idx], m["S"][idx]
        Lref = dict(zip(MV, np.average(m["L"][idx], axis=0, weights=w)))

        # ---- PC temperature: neighbourhood baseline + cardinal-relationship pulls ----
        dtemp_ref = float(np.average(yk, weights=w))
        contributions = {}
        for t in MV:
            sign, tier = MV_TEMP[t]
            c = sign * TIER_DEG[tier] / m["ranges"][t] * (lv[t] - Lref[t])
            contributions[t] = round(float(c), 1)
        temp_mean = T0 + dtemp_ref + sum(contributions.values())
        resid_std = float(np.sqrt(np.average((yk - dtemp_ref) ** 2, weights=w)))

        # ---- safety signals: neighbourhood baseline + documented MV effects ----
        Sbase = np.average(Sk, axis=0, weights=w)

        pred = {CV: {"mean": R(temp_mean), "p10": R(temp_mean - 1.28 * resid_std),
                     "p90": R(temp_mean + 1.28 * resid_std)}}
        for j, tag in enumerate(SAFETY):
            val = float(Sbase[j])
            for drv, coef in SAFETY_EFFECT.get(tag, {}).items():
                val += coef / m["ranges"][drv] * (lv[drv] - Lref[drv])
            p10, p90 = np.percentile(Sk[:, j], [10, 90])
            pred[tag] = {"mean": R(max(0.0, val)), "p10": R(max(0.0, p10)), "p90": R(p90)}
        method = "cardinal-relationship"
        neighbors = int(k)

    # operating-window state selection (7 state-setting variables vote the band)
    votes = {}
    for key in STATE_RULES:
        val = conditions.get(key)
        s = _classify_state(key, None if val is None else float(val))
        if s:
            votes[key] = s
    if votes:
        state_num = int(round(np.mean(list(votes.values()))))
    else:
        state_num = 2
    state = f"State {state_num}"
    band = config.TEMP_STATE_BANDS[state]

    # interlock check against predicted safety values
    names = db.tag_names()
    interlocks = []
    for tag, (op, lim) in config.INTERLOCKS.items():
        pv = pred[tag]["mean"] if tag in pred else None
        ok = None if pv is None else (pv < lim if op == "<" else pv > lim)
        interlocks.append({"tag": tag, "name": names.get(tag, tag),
                           "rule": f"{op} {lim}", "predicted": pv, "pass": ok})

    temp = pred[CV]["mean"]
    in_band = band[0] <= temp <= band[1]
    relationships = {t: {"label": RELATIONSHIP_LABEL[MV_TEMP[t][1]],
                         "sign": "+" if MV_TEMP[t][0] > 0 else "-",
                         "contribution": contributions[t]} for t in MV}
    return {
        "prediction": pred,
        "operating_state": state,
        "target_band": {"low": band[0], "high": band[1]},
        "state_votes": votes,
        "temp_in_band": in_band,
        "temp_gap": round(temp - (band[0] + band[1]) / 2, 1),
        "interlocks": interlocks,
        "all_interlocks_ok": all(i["pass"] for i in interlocks if i["pass"] is not None),
        "neighbors": neighbors,
        "feature_input": {t: round(lv[t], 2) for t in MV},
        "contributions": contributions,
        "relationships": relationships,
        "t0": round(T0, 1), "horizon_min": DELTA, "method": method,
        # v3: predicted temperature path (t+5/10/15/30) when the twin serves
        "trajectory": (core or {}).get("trajectory"),
    }


# ---------------------------------------------------------------------------
# LIVE ADVISORY / OPTIMISER
# Searches the 4 calciner fuel-AFR levers for the cheapest mix that still holds
# PC temperature in-band and every interlock safe, within +/-GUARDRAIL of the
# current setpoint (the control doc's ramp limit). The matched neighbourhood
# depends only on the confounders, never on the levers, so it is computed once
# and the whole candidate grid is evaluated vectorised.
COAL = "467RL400_TPH"
AFR_LEVERS = ["LAFR_FT", "K3V10_F1", "261WF080_TPH"]
OPT_LEVERS = [COAL] + AFR_LEVERS
GUARDRAIL = 0.05          # +/-5% per move
LAMBDA_AFR = 0.60         # weight on AFR substitution vs raw coal reduction
# coal-equivalence of the AFR streams. ASSUMPTION: coal CV 5200, AFR CV 2500
# kcal/kg (CCR screen) and ~1 kg/l for the liquid streams. Confirm with the
# plant before treating any monetary figure as firm.
_AFR_CV_RATIO = 2500.0 / 5200.0
AFR_EQ = {"LAFR_FT": 1e-3 * _AFR_CV_RATIO,
          "K3V10_F1": 1e-3 * _AFR_CV_RATIO,
          "261WF080_TPH": _AFR_CV_RATIO}


def advise(levers: dict, conditions: dict | None = None, k: int = 400,
           guardrail: float = GUARDRAIL, lam: float = LAMBDA_AFR,
           grid: int = 7) -> dict:
    """Recommend a cheaper-but-safe fuel mix for the current plant state."""
    m = _matrix()
    conditions = conditions or {}

    T0 = float(conditions.get("current_temp") or m["temp_med"])
    trend = float(conditions.get("temp_trend", 0.0))
    st = [float(conditions.get(s, m["state_med"][s])) for s in STATE_PLC]
    cq = (np.array([T0, trend] + st) - m["cmu"]) / m["csd"]

    d = np.sqrt(((m["Cn"] - cq) ** 2).sum(1))
    k = min(k, len(d) - 1)
    idx = np.argpartition(d, k)[:k]
    w = 1.0 / (d[idx] + 1e-6)
    dtemp_ref = float(np.average(m["y"][idx], weights=w))
    Sbase = np.average(m["S"][idx], axis=0, weights=w)
    Lref = dict(zip(MV, np.average(m["L"][idx], axis=0, weights=w)))

    cur = {t: float(levers.get(t, m["lever_med"][t])) for t in MV}

    # temperature contribution of the levers we are NOT moving
    fixed_c = 0.0
    for t in MV:
        if t in OPT_LEVERS:
            continue
        sign, tier = MV_TEMP[t]
        fixed_c += sign * TIER_DEG[tier] / m["ranges"][t] * (cur[t] - Lref[t])

    # candidate grid: each optimised lever within +/-guardrail of its current value
    axes = []
    for t in OPT_LEVERS:
        span = abs(cur[t]) * guardrail
        axes.append(np.linspace(cur[t] - span, cur[t] + span, grid)
                    if span > 1e-9 else np.array([cur[t]]))
    mesh = np.meshgrid(*axes, indexing="ij")
    cand = np.column_stack([g.ravel() for g in mesh])          # (N, 4)

    # vectorised temperature prediction
    temp = np.full(len(cand), T0 + dtemp_ref + fixed_c)
    for j, t in enumerate(OPT_LEVERS):
        sign, tier = MV_TEMP[t]
        temp += sign * TIER_DEG[tier] / m["ranges"][t] * (cand[:, j] - Lref[t])

    # vectorised safety prediction
    safety = {}
    for s_i, tag in enumerate(SAFETY):
        val = np.full(len(cand), float(Sbase[s_i]))
        for drv, coef in SAFETY_EFFECT.get(tag, {}).items():
            if drv in OPT_LEVERS:
                val += coef / m["ranges"][drv] * (cand[:, OPT_LEVERS.index(drv)] - Lref[drv])
            else:
                val += coef / m["ranges"][drv] * (cur[drv] - Lref[drv])
        safety[tag] = np.maximum(0.0, val)

    # active band (same 7-variable vote as simulate)
    votes = {}
    for key in STATE_RULES:
        v = conditions.get(key)
        s = _classify_state(key, None if v is None else float(v))
        if s:
            votes[key] = s
    state_num = int(round(np.mean(list(votes.values())))) if votes else 2
    state_num = min(3, max(1, state_num))
    state = f"State {state_num}"
    band = config.TEMP_STATE_BANDS[state]

    # Learn from declines: an operator declining a recommendation flags that
    # exact setpoint, in this exact situation (same state, same side of the
    # band), as unwanted. Steer the search away from it next time instead of
    # silently re-offering the same thing -- a real behavioural change, not
    # just a note shown after the fact (see the "locked" banner elsewhere).
    direction = "above" if T0 > band[1] else "below" if T0 < band[0] else "within"
    declined_pts = db.declined_moves_for_situation(state, direction)
    avoid = np.zeros(len(cand), dtype=bool)
    AVOID_TOL = 0.15   # fraction of each lever's guardrail span counted as "the same" setpoint
    for pt in declined_pts:
        if not all(t in pt for t in OPT_LEVERS):
            continue
        close = np.ones(len(cand), dtype=bool)
        for j, t in enumerate(OPT_LEVERS):
            span = max(abs(cur[t]) * guardrail, 1e-6)
            close &= np.abs(cand[:, j] - pt[t]) <= AVOID_TOL * span
        avoid |= close

    ok = (temp >= band[0]) & (temp <= band[1])
    for tag, (op, lim) in config.INTERLOCKS.items():
        if tag in safety:
            ok &= (safety[tag] < lim) if op == "<" else (safety[tag] > lim)

    def afr_equiv(vals):                # t/h coal-equivalent
        return sum(vals[..., OPT_LEVERS.index(t)] * AFR_EQ[t] for t in AFR_LEVERS)

    cur_coal = cur[COAL]
    cur_afr_eq = float(sum(cur[t] * AFR_EQ[t] for t in AFR_LEVERS))
    base = {"coal": round(cur_coal, 2), "afr_coal_equiv": round(cur_afr_eq, 3)}

    # Safety is never relaxed. Reaching the band, however, may take more than one
    # guarded move - the control logic itself steps +/-5% repeatedly "until the
    # target range is restored". So:
    #   mode HOLD    : in-band candidates exist -> pick the cheapest fuel mix
    #   mode APPROACH: none in band -> take the best SAFE step toward the band
    safe = np.ones(len(cand), dtype=bool)
    for tag, (op, lim) in config.INTERLOCKS.items():
        if tag in safety:
            safe &= (safety[tag] < lim) if op == "<" else (safety[tag] > lim)
    if not safe.any():
        return {"feasible": False, "mode": "blocked", "operating_state": state,
                "target_band": {"low": band[0], "high": band[1]},
                "reason": ("Every setting within +/-{:.0%} of the current setpoints "
                           "would breach a combustion-safety interlock. No fuel move "
                           "can be recommended.").format(guardrail),
                "current": base, "assumptions": _assumptions(guardrail, lam)}

    # Prefer candidates that don't repeat a declined setpoint, but never let
    # that preference block a move that would otherwise be offered -- if
    # avoiding every declined point would leave nothing usable, fall back to
    # the full candidate set rather than get stuck or manufacture a worse move.
    ok_use = ok & ~avoid if (ok & ~avoid).any() else ok
    safe_use = safe & ~avoid if (safe & ~avoid).any() else safe
    avoided_declined_repeat = bool(avoid.any() and (
        (ok.any() and not np.array_equal(ok_use, ok)) or
        (not ok.any() and not np.array_equal(safe_use, safe))))

    centre = 0.5 * (band[0] + band[1])
    cost = cand[:, 0] - lam * afr_equiv(cand)
    if ok.any():
        mode = "hold"
        obj = np.where(ok_use, cost, np.inf)
    else:
        # move toward the band (primary), cheapest fuel as the tie-break
        mode = "approach"
        gap = np.abs(temp - centre)
        obj = np.where(safe_use, gap + 1e-3 * cost, np.inf)
    b = int(np.argmin(obj))
    steps_to_band = None
    if mode == "approach":
        gap_now = abs((T0 + dtemp_ref + fixed_c +
                       sum(MV_TEMP[t][0] * TIER_DEG[MV_TEMP[t][1]] / m["ranges"][t]
                           * (cur[t] - Lref[t]) for t in OPT_LEVERS)) - centre)
        gap_after = abs(float(temp[b]) - centre)
        made = gap_now - gap_after
        steps_to_band = int(np.ceil(gap_after / made)) + 1 if made > 0.05 else None

    rec = {t: round(float(cand[b, j]), 2) for j, t in enumerate(OPT_LEVERS)}
    rec_afr_eq = float(afr_equiv(cand[b]))
    saving_tph = cur_coal - float(cand[b, 0])
    names = db.tag_names()
    return {
        "feasible": True,
        "mode": mode,
        "mode_note": ("Cheapest fuel mix that holds the band." if mode == "hold" else
                      "Temperature is outside the band; this is the best SAFE step "
                      "toward it within one guarded move (the control logic repeats "
                      "such steps until the range is restored)."),
        "steps_to_band": steps_to_band,
        "declined_repeats_checked": len(declined_pts),
        "avoided_declined_repeat": avoided_declined_repeat,
        "operating_state": state,
        "target_band": {"low": band[0], "high": band[1]},
        "current": base,
        "recommended": {"coal": rec[COAL], "afr_coal_equiv": round(rec_afr_eq, 3)},
        "predicted_temp": round(float(temp[b]), 1),
        "predicted_safety": {t: round(float(safety[t][b]), 1) for t in SAFETY},
        "moves": [{"tag": t, "name": names.get(t, t),
                   "from": round(cur[t], 2), "to": rec[t],
                   "delta": round(rec[t] - cur[t], 2)} for t in OPT_LEVERS],
        "saving": {
            "coal_tph": round(saving_tph, 3),
            "coal_pct": round(100 * saving_tph / cur_coal, 2) if cur_coal else None,
            "coal_tpd": round(saving_tph * 24, 1),
            "coal_tpy": round(saving_tph * 24 * 330, 0),      # 330 operating days
            "afr_equiv_gain_tph": round(rec_afr_eq - cur_afr_eq, 3),
        },
        "candidates_evaluated": int(len(cand)),
        "in_band_candidates": int(ok.sum()),
        "safe_candidates": int(safe.sum()),
        "assumptions": _assumptions(guardrail, lam),
    }


# --------------------------------------------------------------------------
# SAVINGS BREAKDOWN — per fuel stream: rate change -> thermal energy -> money
# Every stream is converted to MASS first (liquid AFR is metered in l/h, so it
# needs a density) and only then to kcal, so units can never be mixed.
FUEL_STREAMS = [
    # tag, label, native unit, native->tonnes/h, CV attr, price attr
    ("467RL400_TPH", "Calciner coal", "t/h", 1.0, "COAL_CV_KCAL_KG", "COAL_PRICE_PER_T"),
    ("467RL300_TPH", "Kiln coal", "t/h", 1.0, "COAL_CV_KCAL_KG", "KILN_COAL_PRICE_PER_T"),
    ("LAFR_FT", "LAFR — precalciner", "l/h", None, "AFR_LIQ_CV_KCAL_KG", "LAFR_PRICE_PER_L"),
    ("K3V10_F1", "LAFR — tertiary air duct", "l/h", None, "AFR_LIQ_CV_KCAL_KG", "LAFR_PRICE_PER_L"),
    ("261WF080_TPH", "SAFR (solid AFR)", "t/h", 1.0, "AFR_SOL_CV_KCAL_KG", "SAFR_PRICE_PER_T"),
]
AFR_TAGS = {"LAFR_FT", "K3V10_F1", "261WF080_TPH"}


def _to_tph(tag: str, native: float, factor) -> float:
    if factor is not None:
        return native * factor
    # litres/h -> tonnes/h via density
    return native * config.LAFR_DENSITY_KG_PER_L / 1000.0


def savings_breakdown(adv: dict, snapshot: dict | None = None,
                      days_per_year: int = 330) -> dict:
    """Per-parameter energy and cost effect of following the advisory."""
    if not adv.get("feasible"):
        return {"available": False,
                "reason": adv.get("reason", "No advisory available.")}

    rec_by_tag = {m["tag"]: m for m in adv.get("moves", [])}
    snapshot = snapshot or {}
    rows, priced_any, missing_price = [], False, []
    tot = {"kcal_h": 0.0, "cost_h": 0.0}
    heat_now = heat_rec = afr_now = afr_rec = 0.0

    for tag, label, unit, factor, cv_attr, price_attr in FUEL_STREAMS:
        mv = rec_by_tag.get(tag)
        cur = float(mv["from"]) if mv else (
            float(snapshot[tag]) if snapshot.get(tag) is not None else None)
        rec = float(mv["to"]) if mv else cur          # not optimised -> unchanged
        if cur is None:
            continue
        cv = getattr(config, cv_attr)
        price = getattr(config, price_attr) or 0.0

        t_now, t_rec = _to_tph(tag, cur, factor), _to_tph(tag, rec, factor)
        kcal_now, kcal_rec = t_now * 1000 * cv, t_rec * 1000 * cv
        d_kcal = kcal_rec - kcal_now                  # negative = heat saved
        d_cost = (rec - cur) * price if price else None

        heat_now += kcal_now
        heat_rec += kcal_rec
        if tag in AFR_TAGS:
            afr_now += kcal_now
            afr_rec += kcal_rec
        tot["kcal_h"] += d_kcal
        if d_cost is not None:
            tot["cost_h"] += d_cost
            priced_any = True
        elif abs(rec - cur) > 1e-9:
            missing_price.append(label)

        rows.append({
            "tag": tag, "name": label, "unit": unit,
            "current": round(cur, 2), "recommended": round(rec, 2),
            "delta": round(rec - cur, 2),
            "delta_tph": round(t_rec - t_now, 4),
            "cv_kcal_kg": cv,
            "energy_gj_h": round(d_kcal * config.KCAL_TO_GJ, 3),
            "energy_saved_gj_h": round(-d_kcal * config.KCAL_TO_GJ, 3),
            "cost_change_per_h": None if d_cost is None else round(d_cost, 1),
            "cost_saved_per_h": None if d_cost is None else round(-d_cost, 1),
            "price_configured": bool(price),
        })

    saved_kcal_h = -tot["kcal_h"]
    saved_cost_h = -tot["cost_h"] if priced_any else None

    # thermal substitution rate (AFR share of total fuel heat)
    tsr_now = 100 * afr_now / heat_now if heat_now else None
    tsr_rec = 100 * afr_rec / heat_rec if heat_rec else None

    # specific heat consumption (kcal per kg clinker) from kiln feed
    shc = None
    feed = snapshot.get(config.KILN_FEED_TAG)
    if feed:
        clinker_kg_h = float(feed) * config.CLINKER_PER_FEED * 1000.0
        if clinker_kg_h > 0:
            shc = {"now": round(heat_now / clinker_kg_h, 1),
                   "recommended": round(heat_rec / clinker_kg_h, 1),
                   "saved": round(saved_kcal_h / clinker_kg_h, 2),
                   "clinker_tph": round(float(feed) * config.CLINKER_PER_FEED, 1)}

    out = {
        "available": True,
        "currency": config.CURRENCY,
        "streams": rows,
        "total": {
            "energy_saved_gj_h": round(saved_kcal_h * config.KCAL_TO_GJ, 3),
            "energy_saved_gj_day": round(saved_kcal_h * config.KCAL_TO_GJ * 24, 1),
            "energy_saved_gj_year": round(saved_kcal_h * config.KCAL_TO_GJ * 24 * days_per_year, 0),
            "energy_saved_mkcal_h": round(saved_kcal_h / 1e6, 2),
            "heat_now_gj_h": round(heat_now * config.KCAL_TO_GJ, 2),
            "heat_rec_gj_h": round(heat_rec * config.KCAL_TO_GJ, 2),
        },
        "tsr": None if tsr_now is None else {
            "now_pct": round(tsr_now, 2), "recommended_pct": round(tsr_rec, 2),
            "change_pct_points": round(tsr_rec - tsr_now, 2)},
        "specific_heat_kcal_per_kg_clinker": shc,
        "cost": None if saved_cost_h is None else {
            "saved_per_h": round(saved_cost_h, 1),
            "saved_per_day": round(saved_cost_h * 24, 0),
            "saved_per_year": round(saved_cost_h * 24 * days_per_year, 0),
        },
        "prices_configured": priced_any,
        "streams_missing_price": sorted(set(missing_price)),
        "days_per_year": days_per_year,
    }
    return out


def _assumptions(guardrail: float, lam: float) -> dict:
    return {
        "guardrail_pct": round(100 * guardrail, 1),
        "lambda_afr": lam,
        "operating_days_per_year": 330,
        "coal_price_per_t": config.COAL_PRICE_PER_T or None,
        "coal_cv_kcal_kg": 5200,
        "afr_cv_kcal_kg": 2500,
    }


def correlation(tags: list[str], start: str | None, end: str | None,
                bucket: str = "15min") -> dict:
    tags = db.safe_tags(tags)
    if len(tags) < 2:
        return {"tags": tags, "matrix": []}
    cols = ", ".join(f'"{t}"' for t in tags)
    where = "WHERE feed_gate=1"
    params = {}
    if start:
        where += " AND ts >= :start"; params["start"] = start
    if end:
        where += " AND ts <= :end"; params["end"] = end
    df = db.q(f"SELECT ts, {cols} FROM process_1min {where} ORDER BY ts", params)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").apply(pd.to_numeric, errors="coerce")
    if bucket:
        df = df.resample(bucket).mean()
    corr = df.corr().round(3)
    return {"tags": tags, "n": int(len(df)),
            "matrix": corr.where(pd.notna(corr), None).values.tolist()}


def cross_correlation(a: str, b: str, start: str | None, end: str | None,
                      max_lag_min: int = 30) -> dict:
    """Lagged Pearson corr of b vs a (reveals process dead-time). Positive lag =
    b lags a (a leads)."""
    if not {a, b} <= db.valid_tags():
        return {"error": "unknown tag"}
    where = "WHERE feed_gate=1"
    params = {}
    if start:
        where += " AND ts >= :start"; params["start"] = start
    if end:
        where += " AND ts <= :end"; params["end"] = end
    df = db.q(f'SELECT ts, "{a}" AS a, "{b}" AS b FROM process_1min {where} ORDER BY ts', params)
    df["ts"] = pd.to_datetime(df["ts"])
    s = df.set_index("ts")[["a", "b"]].resample("1min").mean()
    lags = range(-max_lag_min, max_lag_min + 1)
    out = []
    for L in lags:
        c = s["a"].corr(s["b"].shift(-L))
        out.append({"lag_min": L, "corr": None if pd.isna(c) else round(float(c), 3)})
    valid = [o for o in out if o["corr"] is not None]
    best = max(valid, key=lambda o: abs(o["corr"])) if valid else None
    return {"a": a, "b": b, "series": out, "best_lag": best}
