"""GBT TRIAL (side experiment — does NOT touch the deployed app).

Trains LightGBM models for the calciner (PC) response, two ways:
    Model A : plain            — learns whatever the history says
    Model B : monotonic        — lever directions FORCED to the documented
                                 cardinal relationships (PC_Temp_Variable_Mapping)

Targets (all at t + HORIZON minutes):
    dtemp   : PC outlet temperature CHANGE           (the control objective)
    co_pc   : calciner outlet CO      (absolute)     ] combustion-safety models,
    o2_pc   : calciner outlet O2      (absolute)     ] needed so the optimizer can
    co_kiln : kiln inlet CO           (absolute)     ] never trade safety for fuel
    o2_kiln : kiln inlet O2           (absolute)     ]

Split is by TIME (never random) so there is no leakage from future to past.

Run:  python -m experiments.gbt_trial
Out:  experiments/models/*.txt   (LightGBM boosters)
      experiments/out/*.csv      (metrics, importances, coal partial dependence)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lightgbm as lgb                      # noqa: E402
from sqlalchemy import create_engine        # noqa: E402

import config                               # noqa: E402

HORIZON = 5          # minutes ahead (calciner thermal dead-time)
CHG_WIN = 5          # window for "recent change in lever" features
OUT = Path(__file__).parent / "out"
MODELS = Path(__file__).parent / "models"
OUT.mkdir(parents=True, exist_ok=True)
MODELS.mkdir(parents=True, exist_ok=True)

CV = "423CI400_TT_4"

# ---- the 8 manipulated variables and their documented pull on PC temperature
MV_SIGN = {
    "467RL400_TPH": +1,    # calciner coal      Direct, strong (+)
    "LAFR_FT":      +1,    # LAFR - PC          Direct (+)
    "261WF080_TPH": +1,    # SAFR               Direct (+)
    "K3V10_F1":     +1,    # LAFR - TAD         Indirect (+)
    "467RL300_TPH": +1,    # kiln coal          Indirect (+)
    "353RL130_TPH": -1,    # kiln feed          Direct (-)  dilution
    "423FN010_ST":  -1,    # PH fan speed       Direct (-)  draft
    "433MD140_ST1":  0,    # kiln speed         Weak / context -> unconstrained
}
MV = list(MV_SIGN)

# context (never constrained)
CONTEXT = ["433MD140_TQ", "423AM822_NO", "443KH050_TT1", "423DU415_TT",
           "423CN_TEMP", "423CN150_TT_1", "423CN250_TT_1",
           "423KR405_TT_3", "423KR405_TT_4", "423FN010_IT"]
GAS = ["423AM823_CO_PPM", "423AM823_O2", "423AM822_CO_PPM", "423AM822_O2",
       "423AM820_CO_PPM", "423AM821_CO_PPM", "423AM820_O2", "423AM821_O2"]

# target name -> (source tag, kind).  kind: "delta" or "abs"
TARGETS = {
    "dtemp":   (CV, "delta"),
    "co_pc":   ("423AM823_CO_PPM", "abs"),
    "o2_pc":   ("423AM823_O2", "abs"),
    "co_kiln": ("423AM822_CO_PPM", "abs"),
    "o2_kiln": ("423AM822_O2", "abs"),
}
# how a fuel/draft lever should move each safety signal (physics; doc tables 6/7/8)
SAFETY_SIGN = {
    "co_pc":   {"467RL400_TPH": +1, "LAFR_FT": +1, "261WF080_TPH": +1,
                "K3V10_F1": +1, "423FN010_ST": -1},
    "o2_pc":   {"467RL400_TPH": -1, "LAFR_FT": -1, "261WF080_TPH": -1,
                "K3V10_F1": -1, "423FN010_ST": +1},
    "co_kiln": {"467RL300_TPH": +1, "423FN010_ST": -1},
    "o2_kiln": {"467RL300_TPH": -1, "423FN010_ST": +1},
}

SPLIT_TRAIN_END = "2026-04-30"
SPLIT_VALID_END = "2026-06-30"     # test = everything after


# ---------------------------------------------------------------- features --
def build_frame() -> tuple[pd.DataFrame, list[str]]:
    eng = create_engine(config.database_url())
    cols = list(dict.fromkeys([CV] + MV + CONTEXT + GAS))
    sel = ", ".join(f'"{c}"' for c in cols)
    print("reading process_1min (in-load only) ...")
    df = pd.read_sql(
        f"SELECT ts, {sel} FROM process_1min WHERE feed_gate=1 ORDER BY ts", eng)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("ts")

    feats: list[str] = []

    # Levers are represented RELATIVELY only (change over CHG_WIN, and deviation
    # from the trailing 60-min mean). Absolute lever levels are excluded for the
    # same reason as absolute temperature: train coal averaged 21.8 t/h vs 29.7
    # in the Jul test month, so "high coal" in history meant "operator recovering
    # from a cold calciner" -> an absolute-level feature re-imports the exact
    # confounding we are trying to remove. Relative features also match how the
    # control logic actually acts (+/-5% moves from the current setpoint).
    for m in MV:
        roll = df[m].rolling(60, min_periods=15).mean()
        df[f"{m}__chg"] = df[m] - df[m].shift(CHG_WIN)
        df[f"{m}__dev60"] = df[m] - roll
        feats += [f"{m}__chg", f"{m}__dev60"]

    # temperature state — REGIME-RELATIVE.
    # Absolute temperature is deliberately NOT a feature: the plant's operating
    # point drifts a lot between months (train mean 913 degC vs Jul 871 degC), so
    # a model keyed on absolute level confuses "a transient dip that recovers"
    # with "a lower steady setpoint" and predicts a phantom recovery. Deviation
    # from the recent trailing mean is regime-invariant and generalises.
    roll60 = df[CV].rolling(60, min_periods=15).mean()
    df["temp__dev60"] = df[CV] - roll60                     # where are we vs last hour
    df["temp__chg5"] = df[CV] - df[CV].shift(5)
    df["temp__chg15"] = df[CV] - df[CV].shift(15)
    df["temp__slope60"] = roll60 - roll60.shift(30)          # slow drift of the regime
    feats += ["temp__dev60", "temp__chg5", "temp__chg15", "temp__slope60"]

    # context + current gas state
    feats += CONTEXT + GAS

    # ---- targets at t+HORIZON
    # NOTE: 1-min plant signals carry heavy sensor jitter (esp. the gas
    # analysers). We model the PROCESS response, not the thermocouple noise, so
    # both ends of the target are lightly smoothed:
    #   base   = trailing 3-min mean  (t-2..t)      -> no future leakage
    #   future = 3-min mean centred at t+HORIZON    (t+4..t+6)
    for name, (tag, kind) in TARGETS.items():
        base = df[tag].rolling(3, min_periods=2).mean()
        fut = df[tag].shift(-HORIZON).rolling(3, min_periods=2, center=True).mean()
        df[f"y_{name}"] = (fut - base) if kind == "delta" else fut

    # gap guard: only rows whose +/-window neighbours are genuinely that far away
    idx = df.index.to_series()
    fwd = (idx.shift(-HORIZON) - idx).dt.total_seconds()
    past = (idx - idx.shift(CHG_WIN)).dt.total_seconds()
    ok = fwd.between(HORIZON * 60 - 60, HORIZON * 60 + 120) & \
         past.between(CHG_WIN * 60 - 60, CHG_WIN * 60 + 120)
    df = df[ok]

    ycols = [f"y_{k}" for k in TARGETS]
    df = df.dropna(subset=feats + ycols)
    print(f"usable rows: {len(df):,}   features: {len(feats)}")
    return df, feats


def monotone_vector(feats: list[str], signs: dict[str, int]) -> list[int]:
    """Constraint per feature; applies to both relative forms of each lever."""
    vec = []
    for f in feats:
        base = f.split("__")[0]
        vec.append(int(signs.get(base, 0)))
    return vec


def evaluate(y, pred) -> dict:
    err = pred - y
    return {"mae": float(np.mean(np.abs(err))),
            "rmse": float(np.sqrt(np.mean(err ** 2))),
            "bias": float(np.mean(err))}


def train_one(name, Xtr, ytr, Xva, yva, feats, mono=None, seed=1):
    # L2 (not L1): LightGBM forbids monotone_constraints with regression_l1.
    # Training target is winsorized to the 1st/99th pct so rare extreme
    # excursions don't dominate the fit (raw data is untouched).
    lo, hi = np.percentile(ytr, [1, 99])
    ytr = np.clip(ytr, lo, hi)
    params = dict(objective="regression", metric="l1", learning_rate=0.05,
                  num_leaves=63, min_data_in_leaf=200, feature_fraction=0.85,
                  bagging_fraction=0.85, bagging_freq=1, lambda_l2=1.0,
                  verbosity=-1, seed=seed, num_threads=0)
    if mono is not None:
        params["monotone_constraints"] = mono
        params["monotone_constraints_method"] = "advanced"
    dtr = lgb.Dataset(Xtr, ytr, feature_name=feats)
    dva = lgb.Dataset(Xva, yva, feature_name=feats, reference=dtr)
    bst = lgb.train(params, dtr, num_boost_round=1200, valid_sets=[dva],
                    callbacks=[lgb.early_stopping(60, verbose=False)])
    return bst


def lever_response(bst, X: pd.DataFrame, feats: list[str],
                   lever="467RL400_TPH", deltas=None) -> pd.DataFrame:
    """Causal sweep: 'if I move this lever by D from its current setting, what
    happens to PC temperature in HORIZON minutes?' — both relative features of
    the lever are moved together, which is what an actual setpoint change does."""
    if deltas is None:
        deltas = np.arange(-6, 6.5, 1.0)
    sample = X.sample(min(3000, len(X)), random_state=7).copy()
    rows = []
    for d in deltas:
        s = sample.copy()
        s[f"{lever}__chg"] = d
        s[f"{lever}__dev60"] = d
        rows.append({"delta": round(float(d), 2),
                     "pred_dtemp": round(float(np.mean(bst.predict(s[feats]))), 3)})
    out = pd.DataFrame(rows)
    out["slope_vs_prev"] = out["pred_dtemp"].diff().round(3)
    return out


def main():
    df, feats = build_frame()

    tr = df.loc[df.index <= SPLIT_TRAIN_END]
    va = df.loc[(df.index > SPLIT_TRAIN_END) & (df.index <= SPLIT_VALID_END)]
    te = df.loc[df.index > SPLIT_VALID_END]
    print(f"\nTIME SPLIT  train={len(tr):,} ({tr.index.min().date()} -> {tr.index.max().date()})"
          f"  valid={len(va):,}  test={len(te):,} ({te.index.min().date()} -> {te.index.max().date()})")
    if min(len(tr), len(va), len(te)) < 500:
        print("!! a split is too small — check date boundaries"); return

    Xtr, Xva, Xte = tr[feats], va[feats], te[feats]
    results, summary = {}, []

    # ---------------- primary target: temperature response -----------------
    print("\n=== TEMPERATURE RESPONSE (target: temp change over "
          f"{HORIZON} min) ===")
    ytr, yva, yte = tr["y_dtemp"], va["y_dtemp"], te["y_dtemp"]

    # naive persistence baseline: predict "no change"
    base = evaluate(yte.values, np.zeros(len(yte)))
    print(f"  baseline (persistence, dtemp=0)   MAE={base['mae']:.3f} degC"
          f"  RMSE={base['rmse']:.3f}")
    summary.append({"model": "baseline_persistence", "target": "dtemp", **base})

    mono_t = monotone_vector(feats, MV_SIGN)
    calib = {}
    for label, mono in (("A_plain", None), ("B_monotonic", mono_t)):
        bst = train_one(f"dtemp_{label}", Xtr, ytr, Xva, yva, feats, mono)
        # bias calibration fitted on VALIDATION (never on test)
        b = float(np.mean(bst.predict(Xva) - yva.values))
        calib[label] = b
        m = evaluate(yte.values, bst.predict(Xte) - b)
        gain = 100 * (1 - m["mae"] / base["mae"])
        print(f"  {label:<12} MAE={m['mae']:.3f} degC  RMSE={m['rmse']:.3f}"
              f"  bias={m['bias']:+.3f}   vs baseline: {gain:+.1f}%"
              f"   (calib={-b:+.2f} degC, best_iter={bst.best_iteration})")
        summary.append({"model": label, "target": "dtemp", "calib_offset": round(-b, 3),
                        "improve_vs_baseline_pct": round(gain, 2), **m})
        bst.save_model(str(MODELS / f"dtemp_{label}.txt"))
        results[label] = bst

    # ---- the decisive check: which way does coal move temperature?
    print("\n=== LEVER RESPONSE: move calciner coal by D t/h -> dtemp in "
          f"{HORIZON} min ===")
    for label, bst in results.items():
        lr = lever_response(bst, Xte, feats)
        lr.to_csv(OUT / f"lever_response_coal_{label}.csv", index=False)
        rng = lr["pred_dtemp"].iloc[-1] - lr["pred_dtemp"].iloc[0]
        span = lr["delta"].iloc[-1] - lr["delta"].iloc[0]
        mono_ok = bool((lr["slope_vs_prev"].dropna() >= -1e-9).all())
        verdict = "PHYSICAL (rises)" if rng > 0 else "*** INVERTED (falls) ***"
        print(f"  {label:<12} coal {lr['delta'].iloc[0]:+.0f} -> "
              f"{lr['delta'].iloc[-1]:+.0f} t/h :  dtemp "
              f"{lr['pred_dtemp'].iloc[0]:+.2f} -> {lr['pred_dtemp'].iloc[-1]:+.2f} degC"
              f"   gain={rng/span:+.2f} degC per t/h   monotone={mono_ok}   {verdict}")

    # feature importance (gain) from the monotonic model
    imp = pd.DataFrame({"feature": results["B_monotonic"].feature_name(),
                        "gain": results["B_monotonic"].feature_importance("gain")})
    imp = imp.sort_values("gain", ascending=False)
    imp["gain_pct"] = (100 * imp["gain"] / imp["gain"].sum()).round(2)
    imp.to_csv(OUT / "importance_dtemp_B_monotonic.csv", index=False)
    print("\n  top drivers (gain %) [B_monotonic]:")
    for r in imp.head(8).itertuples():
        print(f"    {r.feature:<22} {r.gain_pct:>6.2f}%")

    # ---------------- combustion-safety models -----------------------------
    print("\n=== COMBUSTION-SAFETY MODELS (absolute value at t+"
          f"{HORIZON} min) ===")
    for tgt in ["co_pc", "o2_pc", "co_kiln", "o2_kiln"]:
        ycol = f"y_{tgt}"
        mono_s = monotone_vector(feats, SAFETY_SIGN[tgt])
        bst = train_one(tgt, Xtr, tr[ycol], Xva, va[ycol], feats, mono_s)
        m = evaluate(te[ycol].values, bst.predict(Xte))
        pers = evaluate(te[ycol].values, te[TARGETS[tgt][0]].values)
        print(f"  {tgt:<9} MAE={m['mae']:.2f}   (persistence MAE={pers['mae']:.2f})"
              f"   unit={'ppm' if tgt.startswith('co') else '%'}")
        summary.append({"model": "monotonic", "target": tgt,
                        "persistence_mae": round(pers["mae"], 3), **m})
        bst.save_model(str(MODELS / f"{tgt}_monotonic.txt"))

    pd.DataFrame(summary).to_csv(OUT / "metrics_summary.csv", index=False)

    # feature list + config + calibration for the optuna trial to reuse
    (MODELS / "feature_spec.json").write_text(json.dumps({
        "features": feats, "mv": MV, "horizon": HORIZON, "chg_win": CHG_WIN,
        "cv": CV, "mv_sign": MV_SIGN, "context": CONTEXT, "gas": GAS,
        "calibration": {k: -v for k, v in calib.items()},
        "safety_targets": {k: TARGETS[k][0] for k in
                           ["co_pc", "o2_pc", "co_kiln", "o2_kiln"]},
    }, indent=2))

    print(f"\nsaved models -> {MODELS}")
    print(f"saved metrics/PDP/importance -> {OUT}")
    print("\nNOTE: monotonic constraints fix the DIRECTION of each lever. The")
    print("      MAGNITUDE is still learned from confounded history, so treat")
    print("      degC-per-tonne as indicative until plant step-tests calibrate it.")


if __name__ == "__main__":
    main()
