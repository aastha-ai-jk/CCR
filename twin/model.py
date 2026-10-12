"""Serving layer for the trained GBT digital twin.

Loads ``models/twin_gbt.pkl`` (produced by ``twin.train_gbt``, see CONTRACT.md)
and answers what-if queries behind the same shape ``backend.analysis.simulate()``
expects. A SHAP TreeExplainer on the temperature-mean booster attributes each
manipulated variable's pull (°C) on the calciner outlet temperature.

Everything is lazy and module-cached. If the artifact is missing or fails to
load we return ``None`` / ``False`` so the kNN surrogate stays the fallback.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from backend import analysis

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "twin_gbt.pkl"

CV = analysis.CV
MV = analysis.MV
HORIZON = analysis.DELTA          # min; the point prediction's horizon (t+5)
SAFETY = analysis.SAFETY
METHOD = "gbt-twin"

# Module-level lazy cache: {"bundle", "explainer", "mv_cols"}. Only a successful
# load is cached, so the model can appear after the process started (Agent A
# trains in parallel) and later calls will pick it up.
_CACHE: dict = {}


def _load() -> dict | None:
    """Return the loaded model bundle (+ cached SHAP explainer) or None."""
    bundle = _CACHE.get("bundle")
    if bundle is not None:
        return bundle
    if not MODEL_PATH.exists():
        return None
    try:
        import joblib
        import shap

        bundle = joblib.load(MODEL_PATH)
        explainer = shap.TreeExplainer(bundle["temp"]["mean"])
        cols = list(bundle["feature_columns"])
        # Map each MV to the indices of every feature column derived from it
        # (raw value, lags, rolls, rates, valid mask). Tag names are distinct
        # enough that a substring test is unambiguous.
        mv_cols = {mv: [i for i, c in enumerate(cols) if mv in c] for mv in MV}
        _CACHE.update(bundle=bundle, explainer=explainer, mv_cols=mv_cols)
        return bundle
    except Exception:
        return None


def is_available() -> bool:
    """True iff ``models/twin_gbt.pkl`` exists and loads cleanly."""
    return _load() is not None


def _feature_row(levers: dict, conditions: dict, live: bool = True):
    """Build the single ordered feature row via the Agent-A feature builder."""
    from twin import features  # imported lazily; may not exist during parallel dev

    return features.build_feature_row(levers or {}, conditions or {},
                                      use_live_history=live)


def _r(v: float, nd: int = 1) -> float:
    return round(float(v), nd)


def _t0(conditions: dict) -> float:
    """Anchor temperature for delta reconstruction: the current calciner temp.

    Uses ``conditions['current_temp']`` when supplied (every real caller passes
    it), else falls back to the recent-median CV from the feature builder.
    """
    t = (conditions or {}).get("current_temp")
    if t is not None:
        try:
            return float(t)
        except (TypeError, ValueError):
            pass
    try:
        from twin import features
        return float(features._defaults()["_cv"])
    except Exception:
        return 900.0  # mid of the 880-940 degC operating bands


_BIAS_DAMP = 0.8      # apply 80% of the measured offset (stability margin)
_BIAS_CLAMP = 10.0    # never shift more than this (degC) on a rolling estimate
_BIAS_MIN_N = 30      # matured residuals needed before trusting the estimate


def _live_bias() -> float:
    """Offset-free correction: rolling 2-h signed bias of the RAW model vs the
    realised plant (written by twin.shadow to ``models/live_bias.json``).

    The model systematically over/under-predicts when running conditions drift
    from the training year; adding back the (damped, clamped) measured offset
    is the standard industrial fix. Returns the correction to ADD to raw
    predictions (0.0 when unavailable). mtime-cached.
    """
    path = MODEL_PATH.parent / "live_bias.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    cached = _CACHE.get("bias")
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import json
        d = json.loads(path.read_text())
        c = 0.0
        if int(d.get("n", 0)) >= _BIAS_MIN_N:
            c = _BIAS_DAMP * max(-_BIAS_CLAMP, min(_BIAS_CLAMP, float(d["bias_2h"])))
        _CACHE["bias"] = (mtime, c)
        return c
    except Exception:
        return 0.0


def _conformal_q() -> float | None:
    """Live conformal width for the t+5 interval (see twin.monitor).

    ``models/conformal.json`` holds q80 of matured |shadow residuals| over the
    last few days — an 80%-coverage half-width measured on the LIVE plant, not
    the offline test split. Cached with an mtime check so serving stays fast.
    """
    path = MODEL_PATH.parent / "conformal.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _CACHE.get("conformal")
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import json
        q = float(json.loads(path.read_text())["q80"])
        _CACHE["conformal"] = (mtime, q)
        return q
    except Exception:
        return None


# --- Measured fuel response (hybrid what-if) --------------------------------
# The GBT learned from closed-loop data (operators move fuel to HOLD the
# temperature), so its own lever response is ~20-30x too weak (+0.1-0.2 C per
# t/h coal). For a what-if, the temperature is predicted with the levers at
# their LIVE values and the MEASURED plant response is added for each lever's
# deviation from live. Defaults: event studies on Aug 2025 - Jul 2026 history
# (coal: 3,328 operator steps vs a matched no-change control with the same
# prior temperature trend; SAFR: ~1,400 feeder trips/restarts with coal steady;
# LAFR: ~820 LAFR PC steps, TAD assumed equal). Units: C per lever unit
# (t/h, or l/h for LAFR) at each horizon (min). Overridable per lever in
# data/plant_settings.json "process_response".
RESPONSE_DEFAULTS = {
    "467RL400_TPH": {5: 0.3, 10: 2.2, 15: 4.0, 30: 3.3},          # calciner coal, C per t/h
    "261WF080_TPH": {5: 0.0, 10: 0.35, 15: 0.4, 30: 0.45},        # SAFR, C per t/h
    "LAFR_FT":      {5: 0.0015, 10: 0.0025, 15: 0.0025, 30: 0.0025},  # C per l/h
    "K3V10_F1":     {5: 0.0015, 10: 0.0025, 15: 0.0025, 30: 0.0025},  # C per l/h
}


def _response() -> dict:
    """lever -> {horizon_min: C per unit}; plant_settings overrides defaults."""
    import config
    out = {t: dict(g) for t, g in RESPONSE_DEFAULTS.items()}
    for tag, g in ((config.plant_settings().get("process_response") or {}).items()):
        if tag in out and isinstance(g, dict):
            for h, v in g.items():
                if v is not None and not str(h).startswith("_"):
                    out[tag][int(h)] = float(v)
    return out


def _gain(g: dict, h: int) -> float:
    """Response at horizon h, linear between the measured horizons."""
    hs = sorted(g)
    if h <= hs[0]:
        return g[hs[0]]
    if h >= hs[-1]:
        return g[hs[-1]]
    for a, b in zip(hs, hs[1:]):
        if a <= h <= b:
            return g[a] + (g[b] - g[a]) * (h - a) / (b - a)
    return 0.0


def _live_levers() -> dict:
    """Current value of each response lever from the live history (last row)."""
    try:
        from twin import features
        hist = features._live_history()
        if hist is None or not len(hist):
            return {}
        last = hist.iloc[-1]
        return {t: float(last[t]) for t in RESPONSE_DEFAULTS
                if t in hist.columns and pd.notna(last[t])}
    except Exception:
        return {}


def _whatif(levers: dict, conditions: dict) -> tuple[dict, dict]:
    """(levers with response levers reset to their live values, deviations).
    No live reference -> levers unchanged and no deviations (plain model)."""
    # reference = the caller's live snapshot (conditions) when given, so a
    # forecast at the live levers has zero deviation; else the live history
    ref = {t: float(conditions[t]) for t in RESPONSE_DEFAULTS
           if (conditions or {}).get(t) is not None}
    for t, v in _live_levers().items():
        ref.setdefault(t, v)
    dev = {t: float(levers[t]) - ref[t] for t in RESPONSE_DEFAULTS
           if levers.get(t) is not None and t in ref and abs(float(levers[t]) - ref[t]) > 1e-9}
    return {**(levers or {}), **{t: ref[t] for t in dev}}, dev


_REF_X: dict = {}


def _ref_row(ref_levers: dict, conditions: dict):
    """Feature row at the live levers. An optimizer run asks for the same row
    in every trial (only the response levers vary, and they are reset to live
    here), so the last few rows are memoised on their exact inputs."""
    def _key(d):
        return tuple(sorted((k, v) for k, v in (d or {}).items()
                            if isinstance(v, (int, float, str, type(None)))))
    key = (_key(ref_levers), _key(conditions))
    X = _REF_X.get(key)
    if X is None:
        X = _feature_row(ref_levers, conditions)
        if len(_REF_X) > 8:
            _REF_X.clear()
        _REF_X[key] = X
    return X


def _response_at(dev: dict, h: int) -> float:
    if not dev:
        return 0.0
    r = _response()
    return sum(_gain(r[t], h) * d for t, d in dev.items())


def _mean_delta(heads: dict, X) -> float:
    """Served mean delta: GBT blended with the bundled ridge ARX (v6).

    Pre-v6 bundles have no 'arx' entry and fall through to GBT-alone, so old
    artifacts keep serving unchanged."""
    g = float(heads["mean"].predict(X)[0])
    arx = heads.get("arx")
    if not arx:
        return g
    a = float(np.asarray(X, dtype=float)[0] @ arx["coef"] + arx["intercept"])
    w = float(arx.get("w", 0.4))
    return (1 - w) * g + w * a


def _temp_from(bundle: dict, X, conditions: dict) -> tuple[float, float, float]:
    """Predict (mean, p10, p90) calciner temp, reconstructing the delta target.

    The temp boosters are trained on the regime-invariant 5-min *change*
    (bundle['temp_delta'] == True), so absolute temp = current_temp + delta.
    When a live conformal width is available it REPLACES the offline quantile
    interval — calibrated on the running plant via shadow mode.
    """
    t = bundle["temp"]
    raw_mean = _mean_delta(t, X)
    raw_p10 = float(t["p10"].predict(X)[0])
    raw_p90 = float(t["p90"].predict(X)[0])
    if bundle.get("temp_delta"):
        base = _t0(conditions)
        raw_mean, raw_p10, raw_p90 = base + raw_mean, base + raw_p10, base + raw_p90
    c = _live_bias()                       # offset-free correction (may be 0)
    mean = raw_mean + c
    lo, hi = min(raw_p10, raw_p90) + c, max(raw_p10, raw_p90) + c
    q = _conformal_q()
    if q is not None:
        lo, hi = mean - q, mean + q
    return mean, lo, hi


def predict_core(levers: dict, conditions: dict) -> dict | None:
    """Predict temp (mean/p10/p90), each SAFETY tag, per-MV SHAP pulls (°C) and
    quality. Returns the contract dict with method 'gbt-twin', or None if the
    twin is unavailable or anything goes wrong (caller keeps the kNN fallback).
    """
    bundle = _load()
    if bundle is None:
        return None
    try:
        X = _feature_row(levers, conditions)
        # temperature: ML at live lever values + measured response to deviations
        ref_levers, dev = _whatif(levers, conditions)
        X_t = _ref_row(ref_levers, conditions) if dev else X

        mean, lo, hi = _temp_from(bundle, X_t, conditions)
        shift = _response_at(dev, HORIZON)
        mean, lo, hi = mean + shift, lo + shift, hi + shift
        prediction = {CV: {"mean": _r(mean),
                           "p10": _r(min(lo, mean)), "p90": _r(max(hi, mean))}}

        # SAFETY tags: point (mean) models only — mirror the value into p10/p90.
        for tag, booster in bundle.get("safety", {}).items():
            v = float(booster.predict(X)[0])
            prediction[tag] = {"mean": _r(v), "p10": _r(v), "p90": _r(v)}

        # Per-MV SHAP contributions on the temp-mean booster (°C pull per
        # lever). v6: the served mean blends GBT with a linear ARX, so honest
        # attribution = (1-w)*SHAP + w*coef*(x - train_mean) per feature.
        sv = np.asarray(_CACHE["explainer"].shap_values(X, check_additivity=False))
        if sv.ndim == 2:
            sv = sv[0]
        arx = bundle["temp"].get("arx")
        if arx is not None:
            w = float(arx.get("w", 0.4))
            lin = arx["coef"] * (np.asarray(X, dtype=float)[0] - arx["x_mean"])
            sv = (1 - w) * sv + w * lin
        mv_cols = _CACHE["mv_cols"]
        contributions = {mv: _r(float(sv[idxs].sum()) if idxs else 0.0)
                         for mv, idxs in mv_cols.items()}

        quality = {}
        for key in ("free_lime", "doc"):
            bk = bundle.get("quality", {}).get(key)
            quality[key] = _r(float(bk.predict(X)[0]), 3) if bk is not None else None

        out = {"prediction": prediction, "contributions": contributions,
               "quality": quality, "method": METHOD,
               "bias_correction": _r(_live_bias(), 2),
               "fuel_response_c": {str(h): _r(_response_at(dev, h), 2) for h in (5, 10, 15, 30)}
               if dev else None}
        traj = predict_trajectory(levers, conditions, _X=X_t, _dev=dev)
        if traj:
            out["trajectory"] = traj
        return out
    except Exception:
        return None


def predict_trajectory(levers: dict, conditions: dict, _X=None, _dev=None) -> list[dict] | None:
    """Predicted temperature path over every trained horizon (v3 bundles).

    Returns [{"min": h, "mean": ..., "p10": ..., "p90": ...}, ...] with the
    delta reconstructed onto current_temp, or None when unavailable. Lever
    deviations from live get the measured fuel response at each horizon.
    """
    bundle = _load()
    if bundle is None or not bundle.get("temp_multi"):
        return None
    try:
        if _X is None:
            ref_levers, _dev = _whatif(levers, conditions)
            _X = _ref_row(ref_levers, conditions) if _dev else _feature_row(levers, conditions)
        X, dev = _X, (_dev or {})
        base = _t0(conditions)
        c = _live_bias()                   # same offset applies along the path
        out = []
        for h in sorted(bundle["temp_multi"]):
            heads = bundle["temp_multi"][h]
            m = _mean_delta(heads, X)
            lo = float(heads["p10"].predict(X)[0])
            hi = float(heads["p90"].predict(X)[0])
            if bundle.get("temp_delta"):
                m, lo, hi = base + m, base + lo, base + hi
            s = _response_at(dev, int(h))
            m, lo, hi = m + c + s, lo + c + s, hi + c + s
            lo, hi = min(lo, hi), max(lo, hi)
            out.append({"min": int(h), "mean": _r(m), "p10": _r(lo), "p90": _r(hi)})
        return out
    except Exception:
        return None


def predict_frame(X, cv0) -> dict | None:
    """Batch temp prediction over a pre-engineered feature frame (backtest).

    Uses the RAW model (blended mean, no live-bias offset) — a historical
    replay measures intrinsic skill, not today's drift correction. ``cv0`` is
    the anchor temperature per row; returns dict of numpy arrays.
    """
    bundle = _load()
    if bundle is None:
        return None
    t = bundle["temp"]
    mean = t["mean"].predict(X)
    arx = t.get("arx")
    if arx:
        w = float(arx.get("w", 0.4))
        mean = (1 - w) * mean + w * (np.asarray(X, dtype=float) @ arx["coef"]
                                     + arx["intercept"])
    p10 = t["p10"].predict(X)
    p90 = t["p90"].predict(X)
    if bundle.get("temp_delta"):
        mean, p10, p90 = cv0 + mean, cv0 + p10, cv0 + p90
    return {"mean": mean, "p10": np.minimum(p10, p90), "p90": np.maximum(p10, p90)}


def predict_temp(levers: dict, conditions: dict, live: bool = True) -> dict | None:
    """Fast temp-only prediction (no SHAP / safety) for the backtest walk.

    Pass ``live=False`` when replaying HISTORICAL moments — the v7 live-history
    feature path describes the plant right now and must not leak into replays.
    """
    bundle = _load()
    if bundle is None:
        return None
    try:
        X = _feature_row(levers, conditions, live=live)
        m, lo, hi = _temp_from(bundle, X, conditions)
        return {"mean": m, "p10": lo, "p90": hi}
    except Exception:
        return None
