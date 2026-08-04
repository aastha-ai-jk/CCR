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


def _temp_from(bundle: dict, X, conditions: dict) -> tuple[float, float, float]:
    """Predict (mean, p10, p90) calciner temp, reconstructing the delta target.

    The temp boosters are trained on the regime-invariant 5-min *change*
    (bundle['temp_delta'] == True), so absolute temp = current_temp + delta.
    """
    t = bundle["temp"]
    raw_mean = float(t["mean"].predict(X)[0])
    raw_p10 = float(t["p10"].predict(X)[0])
    raw_p90 = float(t["p90"].predict(X)[0])
    if bundle.get("temp_delta"):
        base = _t0(conditions)
        raw_mean, raw_p10, raw_p90 = base + raw_mean, base + raw_p10, base + raw_p90
    lo, hi = min(raw_p10, raw_p90), max(raw_p10, raw_p90)
    return raw_mean, lo, hi


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

        # Per-MV SHAP contributions on the temp-mean booster (°C pull per lever).
        sv = np.asarray(_CACHE["explainer"].shap_values(X, check_additivity=False))
        if sv.ndim == 2:
            sv = sv[0]
        mv_cols = _CACHE["mv_cols"]
        contributions = {mv: _r(float(sv[idxs].sum()) if idxs else 0.0)
                         for mv, idxs in mv_cols.items()}

        quality = {}
        for key in ("free_lime", "doc"):
            bk = bundle.get("quality", {}).get(key)
            quality[key] = _r(float(bk.predict(X)[0]), 3) if bk is not None else None

        return {"prediction": prediction, "contributions": contributions,
                "quality": quality, "method": METHOD}
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
