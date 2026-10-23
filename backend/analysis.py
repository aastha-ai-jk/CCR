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
    # Off the listed bands: clamp by DIRECTION, not position. Rising bands
    # (torque, NOx, liter weight...): below State 1 -> 1, above State 3 -> 3.
    # Falling bands (free lime: State 1 = 1.4-1.8 ... State 3 = < 1.0): ABOVE
    # State 1 (e.g. 1.99 %, under-burnt) -> 1, below State 3 -> 3.
    rising = bands[0][0] < bands[-1][0]
    if rising:
        return 1 if value < bands[0][0] else 3
    return 1 if value >= bands[0][1] else 3


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
