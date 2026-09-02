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

from backend import analysis

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "twin_gbt.pkl"

CV = analysis.CV
MV = analysis.MV
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


def _feature_row(levers: dict, conditions: dict):
    """Build the single ordered feature row via the Agent-A feature builder."""
    from twin import features  # imported lazily; may not exist during parallel dev

    return features.build_feature_row(levers or {}, conditions or {})


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

        mean, lo, hi = _temp_from(bundle, X, conditions)
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
               "bias_correction": _r(_live_bias(), 2)}
        traj = predict_trajectory(levers, conditions)
        if traj:
            out["trajectory"] = traj
        return out
    except Exception:
        return None


def predict_trajectory(levers: dict, conditions: dict) -> list[dict] | None:
    """Predicted temperature path over every trained horizon (v3 bundles).

    Returns [{"min": h, "mean": ..., "p10": ..., "p90": ...}, ...] with the
    delta reconstructed onto current_temp, or None when unavailable.
    """
    bundle = _load()
    if bundle is None or not bundle.get("temp_multi"):
        return None
    try:
        X = _feature_row(levers, conditions)
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
            m, lo, hi = m + c, lo + c, hi + c
            lo, hi = min(lo, hi), max(lo, hi)
            out.append({"min": int(h), "mean": _r(m), "p10": _r(lo), "p90": _r(hi)})
        return out
    except Exception:
        return None


def predict_temp(levers: dict, conditions: dict) -> dict | None:
    """Fast temp-only prediction (no SHAP / safety) for the backtest walk."""
    bundle = _load()
    if bundle is None:
        return None
    try:
        X = _feature_row(levers, conditions)
        m, lo, hi = _temp_from(bundle, X, conditions)
        return {"mean": m, "p10": lo, "p90": hi}
    except Exception:
        return None
