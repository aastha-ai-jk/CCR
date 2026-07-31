"""OPTUNA OPTIMIZER TRIAL (side experiment — does NOT touch the deployed app).

For a given plant state, searches the 4 calciner fuel/AFR levers to

    minimise   coal  -  lambda * AFR_coal_equivalent          [t/h]

subject to
    (a) predicted PC outlet temperature inside the active State 1/2/3 band
    (b) every combustion-safety interlock predicted safe (CO / O2)
    (c) each lever within +/- GUARDRAIL_PCT of its current setpoint
        (mirrors the control doc's "+/-5% every 15 s" ramp limit — the optimizer
         is never allowed to propose a physically absurd jump)

Then BACKTESTS on the held-out test month: replay historical minutes, and where
the plant was already running in-band and safe, compare the optimizer's
recommended coal against what the operators actually used. Rows whose
recommendation falls outside the range the training data supports are FLAGGED
and excluded from the headline saving.

Prereq:  python -m experiments.gbt_trial      (trains + saves the models)
Run:     python -m experiments.optuna_trial
Out:     experiments/out/optuna_backtest.csv
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lightgbm as lgb                                   # noqa: E402
import optuna                                            # noqa: E402

import config                                            # noqa: E402
from experiments.gbt_trial import (                       # noqa: E402
    build_frame, SPLIT_TRAIN_END, SPLIT_VALID_END, MODELS, OUT,
)

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore")

# ------------------------------------------------------------------ settings
GUARDRAIL_PCT = 0.05      # +/-5% of the current setpoint (control-doc ramp limit)
LAMBDA_AFR = 0.60         # weight on AFR substitution vs raw coal saving
N_TRIALS = 40             # Optuna trials per plant state
N_ROWS = 1500             # historical minutes to backtest
BAND_TOL = 0.0            # degC slack allowed outside the band (0 = strict)

# calorific assumptions (from the CCR screen: coal CV 5200, AFR CV 2500 kcal/kg)
# liquid AFR is metered in l/h -> assumed ~1.0 kg/l. FLAGGED ASSUMPTION: refine
# with the plant's actual AFR density/CV before using any number operationally.
COAL_CV, AFR_CV = 5200.0, 2500.0
AFR_FACTOR = AFR_CV / COAL_CV

# decision levers: tag -> how to convert its units into t/h coal-equivalent
LEVERS = {
    "467RL400_TPH": None,                    # calciner coal (t/h) — the thing we cut
    "LAFR_FT":      1e-3 * AFR_FACTOR,       # l/h -> t/h -> coal-equivalent
    "K3V10_F1":     1e-3 * AFR_FACTOR,       # l/h -> t/h -> coal-equivalent
    "261WF080_TPH": AFR_FACTOR,              # t/h -> coal-equivalent
}
COAL = "467RL400_TPH"
AFR_LEVERS = [k for k in LEVERS if k != COAL]

INTERLOCKS = {                # model target -> (operator, limit)
    "co_pc":   ("<", 1500.0),
    "o2_pc":   (">", 1.5),
    "co_kiln": ("<", 800.0),
    "o2_kiln": (">", 0.5),
}
# PLC state-setting variables -> which band applies (control doc table)
STATE_PLC = {
    "433MD140_TQ":  [(360, 380), (380, 400), (400, 9999)],
    "423AM822_NO":  [(450, 550), (550, 650), (650, 9999)],
    "443KH050_TT1": [(980, 1030), (1030, 1080), (1080, 9999)],
}
BANDS = {1: (920, 940), 2: (900, 920), 3: (880, 900)}


def pick_band(row) -> tuple[int, tuple[float, float]]:
    """Majority vote of the available PLC state indicators."""
    votes = []
    for tag, bands in STATE_PLC.items():
        v = row.get(tag)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        s = 3
        for i, (lo, hi) in enumerate(bands, start=1):
            if lo <= v < hi:
                s = i
                break
        else:
            s = 1 if v < bands[0][0] else 3
        votes.append(s)
    state = int(round(float(np.mean(votes)))) if votes else 2
    state = min(3, max(1, state))
    return state, BANDS[state]


class Twin:
    """Thin wrapper over the saved LightGBM boosters."""

    def __init__(self, temp_model="B_monotonic"):
        spec = json.loads((MODELS / "feature_spec.json").read_text())
        self.feats: list[str] = spec["features"]
        self.calib: float = spec["calibration"][temp_model]
        self.temp = lgb.Booster(model_file=str(MODELS / f"dtemp_{temp_model}.txt"))
        self.safety = {k: lgb.Booster(model_file=str(MODELS / f"{k}_monotonic.txt"))
                       for k in INTERLOCKS}

    def predict(self, X: np.ndarray) -> dict:
        out = {"dtemp": self.temp.predict(X) + self.calib}
        for k, m in self.safety.items():
            out[k] = m.predict(X)
        return out


def perturb(base_vec: np.ndarray, feat_idx: dict, deltas: dict) -> np.ndarray:
    """Apply lever setpoint changes to a feature row.

    Features are relative (``__chg`` = change over 5 min, ``__dev60`` = deviation
    from the trailing hour), so moving a setpoint by d shifts BOTH by d.
    """
    v = base_vec.copy()
    for tag, d in deltas.items():
        for suf in ("__chg", "__dev60"):
            j = feat_idx.get(f"{tag}{suf}")
            if j is not None:
                v[j] += d
    return v


def optimise_row(twin: Twin, base_vec: np.ndarray, feat_idx: dict,
                 cur: dict, band: tuple[float, float], t_now: float,
                 n_trials: int = N_TRIALS) -> dict | None:
    """Search lever deltas for the cheapest fuel mix that stays in-band and safe."""
    lo_hi = {t: (-GUARDRAIL_PCT * cur[t], GUARDRAIL_PCT * cur[t]) for t in LEVERS}
    lo_c, hi_c = band[0] - BAND_TOL, band[1] + BAND_TOL

    def objective(trial):
        deltas = {t: trial.suggest_float(t, lo_hi[t][0], lo_hi[t][1])
                  if lo_hi[t][1] > lo_hi[t][0] else 0.0 for t in LEVERS}
        X = perturb(base_vec, feat_idx, deltas).reshape(1, -1)
        p = twin.predict(X)
        temp = t_now + float(p["dtemp"][0])

        # cost: coal used, rewarded for AFR substitution
        coal_new = cur[COAL] + deltas[COAL]
        afr_eq = sum((cur[t] + deltas[t]) * LEVERS[t] for t in AFR_LEVERS)
        cost = coal_new - LAMBDA_AFR * afr_eq

        # constraint penalties (kept large so feasibility dominates cost)
        pen = 0.0
        if temp < lo_c:
            pen += 50.0 * (lo_c - temp)
        elif temp > hi_c:
            pen += 50.0 * (temp - hi_c)
        for k, (op, lim) in INTERLOCKS.items():
            val = float(p[k][0])
            viol = (val - lim) if op == "<" else (lim - val)
            if viol > 0:
                pen += 100.0 * viol / max(abs(lim), 1.0)

        trial.set_user_attr("temp", temp)
        trial.set_user_attr("coal", coal_new)
        trial.set_user_attr("feasible", pen == 0.0)
        trial.set_user_attr("safety", {k: float(p[k][0]) for k in INTERLOCKS})
        return cost + pen

    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(seed=7, n_startup_trials=10))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    feas = [t for t in study.trials if t.user_attrs.get("feasible")]
    if not feas:
        return None
    best = min(feas, key=lambda t: t.value)
    return {"deltas": best.params, "temp": best.user_attrs["temp"],
            "coal": best.user_attrs["coal"], "safety": best.user_attrs["safety"]}


def main():
    print("loading feature frame (same pipeline as the GBT trial) ...")
    df, feats = build_frame()
    test = df.loc[df.index > SPLIT_VALID_END]
    train = df.loc[df.index <= SPLIT_TRAIN_END]
    print(f"test rows available: {len(test):,}")

    twin = Twin("B_monotonic")
    assert twin.feats == feats, "feature spec drift — re-run gbt_trial"
    feat_idx = {f: i for i, f in enumerate(feats)}

    # support envelope: how far the training data actually constrains each lever
    env = {}
    for t in LEVERS:
        for suf in ("__chg", "__dev60"):
            col = f"{t}{suf}"
            env[col] = tuple(np.percentile(train[col], [1, 99]))

    rows = test.sample(min(N_ROWS, len(test)), random_state=11).sort_index()
    print(f"backtesting {len(rows)} held-out minutes "
          f"(guardrail +/-{GUARDRAIL_PCT:.0%}, lambda_AFR={LAMBDA_AFR}, "
          f"{N_TRIALS} trials each) ...\n")

    recs = []
    for ts, row in rows.iterrows():
        t_now = float(row["423CI400_TT_4"]) if "423CI400_TT_4" in row else np.nan
        # current temp isn't a feature any more, so read it from the raw column
        state, band = pick_band(row)
        cur = {t: float(row[t]) if t in row and pd.notna(row[t]) else np.nan
               for t in LEVERS}
        if any(np.isnan(v) for v in cur.values()) or np.isnan(t_now):
            continue

        base_vec = row[feats].to_numpy(float)
        p0 = twin.predict(base_vec.reshape(1, -1))
        temp0 = t_now + float(p0["dtemp"][0])
        safe0 = all((float(p0[k][0]) < lim) if op == "<" else (float(p0[k][0]) > lim)
                    for k, (op, lim) in INTERLOCKS.items())
        inband0 = band[0] <= temp0 <= band[1]

        res = optimise_row(twin, base_vec, feat_idx, cur, band, t_now)
        if res is None:
            recs.append({"ts": ts, "state": state, "feasible": False,
                         "actual_coal": cur[COAL], "baseline_inband": inband0,
                         "baseline_safe": safe0})
            continue

        # extrapolation check: is the proposed move inside the supported envelope?
        extrap = False
        for t, d in res["deltas"].items():
            for suf in ("__chg", "__dev60"):
                col = f"{t}{suf}"
                newv = float(row[col]) + d
                lo, hi = env[col]
                if newv < lo or newv > hi:
                    extrap = True
        recs.append({
            "ts": ts, "state": state, "feasible": True,
            "band_lo": band[0], "band_hi": band[1],
            "temp_now": round(t_now, 1),
            "temp_pred_baseline": round(temp0, 1),
            "temp_pred_opt": round(res["temp"], 1),
            "baseline_inband": inband0, "baseline_safe": safe0,
            "actual_coal": round(cur[COAL], 2),
            "rec_coal": round(res["coal"], 2),
            "coal_saving": round(cur[COAL] - res["coal"], 3),
            "d_LAFR_FT": round(res["deltas"].get("LAFR_FT", 0), 1),
            "d_K3V10_F1": round(res["deltas"].get("K3V10_F1", 0), 1),
            "d_SAFR": round(res["deltas"].get("261WF080_TPH", 0), 3),
            "extrapolating": extrap,
        })

    out = pd.DataFrame(recs)
    out.to_csv(OUT / "optuna_backtest.csv", index=False)

    # ------------------------------------------------------------- reporting
    n = len(out)
    feas = out[out["feasible"]]
    print("=" * 72)
    print(f"BACKTEST RESULT   rows={n}   feasible={len(feas)} "
          f"({100*len(feas)/max(n,1):.0f}%)")
    if len(feas) == 0:
        print("no feasible solutions — loosen the guardrail or check the band logic")
        return

    print(f"  extrapolating beyond training support : {int(feas['extrapolating'].sum())} "
          f"({100*feas['extrapolating'].mean():.0f}%)  -> EXCLUDED from the headline")

    # Fair comparison: only where the plant itself was already in-band and safe.
    fair = feas[(~feas["extrapolating"]) & feas["baseline_inband"] & feas["baseline_safe"]]
    print(f"\n  fair-comparison rows (in-band + safe + supported): {len(fair)}")
    if len(fair):
        s = fair["coal_saving"]
        pct = 100 * s.mean() / fair["actual_coal"].mean()
        print(f"    actual coal (mean)      : {fair['actual_coal'].mean():.2f} t/h")
        print(f"    recommended coal (mean) : {fair['rec_coal'].mean():.2f} t/h")
        print(f"    ESTIMATED COAL SAVING   : {s.mean():+.3f} t/h "
              f"({pct:+.1f}%)   median {s.median():+.3f}")
        print(f"    AFR pushed up (mean)    : LAFR {fair['d_LAFR_FT'].mean():+.0f} l/h, "
              f"TAD {fair['d_K3V10_F1'].mean():+.0f} l/h, "
              f"SAFR {fair['d_SAFR'].mean():+.3f} t/h")
        print(f"    predicted temp stays in-band in "
              f"{100*((fair['temp_pred_opt']>=fair['band_lo']) & (fair['temp_pred_opt']<=fair['band_hi'])).mean():.0f}% of cases")
    else:
        print("    (none — the plant was rarely both in-band and safe in this sample)")

    print("\n  by operating state:")
    for st, g in feas.groupby("state"):
        print(f"    State {st}: n={len(g):<4} mean saving={g['coal_saving'].mean():+.3f} t/h")

    print("\n" + "=" * 72)
    print("HOW TO READ THIS")
    print(" * The saving is an ESTIMATE OF POTENTIAL from the model, not realised")
    print("   fuel. It is only banked once recommendations are applied in the CCR.")
    print(f" * Coal gain learned from history is ~1.6 degC per t/h; physics/heat")
    print("   balance suggests it is larger. If the true gain is bigger, the")
    print("   optimizer is being CONSERVATIVE here (it thinks it needs more coal")
    print("   than reality to hold temperature).")
    print(" * AFR coal-equivalence uses CV 2500/5200 kcal/kg and ~1 kg/l for the")
    print("   liquid streams - an ASSUMPTION to be confirmed with the plant.")
    print(" * Step-tests remain the only way to calibrate the true lever gains.")
    print(f"\nsaved -> {OUT / 'optuna_backtest.csv'}")


if __name__ == "__main__":
    main()
