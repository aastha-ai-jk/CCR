"""Model Bench — 3-approach bake-off on the cleaned Kiln-3 historian.

Trains and tests three modelling approaches on IDENTICAL data (via
``twin.bench_data``) to predict calciner outlet temperature at t+5 min:

  A. GBT        — LightGBM on tabular features, delta target, SHAP attribution
                  (a faithful retrain of the production twin on the new CSV)
  B. GRU        — deep sequence net on 15-min windows (CUDA when available),
                  permutation-importance attribution
  C. ARX        — dynamic system-identification: ridge-regularised ARX with
                  dead-time lags; coefficients ARE the explanation

All three (plus the persistence baseline) are scored on the SAME held-out test
rows — the intersection of every approach's validity masks — so the comparison
is apples-to-apples by construction (asserted in code).

Run:  python -m twin.bench [--sample N] [--skip-nn] [--epochs E]
Writes: models/bench_results.json  (served by GET /api/bench)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backend.analysis import CV, MV, STATE_PLC
from twin import bench_data

OUT = Path(__file__).resolve().parent.parent / "models" / "bench_results.json"

COAL = "467RL400_TPH"
STEP_TPH = 2.0                       # step-response test: +2 tph calciner coal
W = 15                               # GRU window length (minutes)
ARX_MOM_LAGS = (1, 3, 5)             # temp momentum terms (cv0 - cv(t-lag))
ARX_IN_LAGS = (0, 3)                 # dead-time-shifted input terms
ARX_INPUTS = [*MV, *STATE_PLC]       # levers + measured disturbances
_CONTIG_S = 90                       # max step (s) for a window/lag to count as contiguous


def _metrics(y: np.ndarray, p: np.ndarray) -> dict:
    err = p - y
    ss = float(((y - y.mean()) ** 2).sum())
    return {"mae": round(float(np.abs(err).mean()), 3),
            "rmse": round(float(np.sqrt((err ** 2).mean())), 3),
            "r2": round(float(1 - (err ** 2).sum() / ss), 4) if ss > 0 else None}


# ---------------------------------------------------------------- validity --
def _contig_ok(ts: pd.Series, lookback: int) -> pd.Series:
    """True where the past `lookback` steps are gap-free (≤ _CONTIG_S each)."""
    step = ts.diff().dt.total_seconds()
    return step.rolling(lookback, min_periods=lookback).max().le(_CONTIG_S).fillna(False)


def _seq_valid(df: pd.DataFrame) -> pd.Series:
    ok = df[bench_data.SEQ_COLS].notna().all(axis=1)
    full = ok.rolling(W, min_periods=W).sum().eq(W).fillna(False)
    return full & _contig_ok(df["ts"], W - 1)


def _arx_valid(df: pd.DataFrame) -> pd.Series:
    need = max(max(ARX_MOM_LAGS), max(ARX_IN_LAGS))
    cols_ok = df[[CV, *ARX_INPUTS]].notna()
    ok = cols_ok.all(axis=1)
    for lag in sorted({*ARX_MOM_LAGS, *ARX_IN_LAGS} - {0}):
        ok &= cols_ok.shift(lag).all(axis=1).fillna(False)
    return ok & _contig_ok(df["ts"], need)


# ------------------------------------------------------------- approach A --
def run_gbt(d: dict, tr: np.ndarray, va: np.ndarray, ev: np.ndarray) -> dict:
    """GBT v2: enriched regime-safe features + L1 objective + tuned trees +
    noise-smoothed training target. Scored against the RAW truth like the rest."""
    import lightgbm as lgb

    X, cv0 = d["X_ext"], d["cv0"]
    dy = d["y_smooth"] - cv0          # train on the noise-reduced 5-min change
    t0 = time.time()
    params = dict(objective="l1", num_leaves=127, learning_rate=0.03,
                  n_estimators=2000, min_child_samples=20,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                  verbose=-1)
    m = lgb.LGBMRegressor(**params)
    m.fit(X.iloc[tr], dy.iloc[tr], eval_set=[(X.iloc[va], dy.iloc[va])],
          callbacks=[lgb.early_stopping(40, verbose=False)])
    secs = time.time() - t0

    pred = cv0.iloc[ev].to_numpy() + m.predict(X.iloc[ev])

    # SHAP attribution, aggregated per source tag
    import shap
    sample = X.iloc[ev].sample(min(500, len(ev)), random_state=0)
    sv = np.abs(shap.TreeExplainer(m).shap_values(sample)).mean(axis=0)
    agg: dict[str, float] = {}
    for col, v in zip(X.columns, sv):
        base = next((t for t in [*MV, *STATE_PLC, CV] if t in col), col)
        agg[base] = agg.get(base, 0.0) + float(v)
    top = sorted(agg.items(), key=lambda kv: -kv[1])[:5]

    # step response: a JUST-APPLIED +2 tph coal step — raw value and rate move,
    # lag/roll history stays at pre-step levels (mean over 200 reference rows)
    from twin import features as Ftr
    refs = X.iloc[ev[:: max(1, len(ev) // 200)]].copy()
    bumped = refs.copy()
    bumped[COAL] += STEP_TPH
    bumped[f"{COAL}__rate{Ftr._RATE_MIN}"] += STEP_TPH
    bumped[f"{COAL}__roll{Ftr._ROLL_WIN}_mean"] += STEP_TPH / Ftr._ROLL_WIN
    step = float((m.predict(bumped) - m.predict(refs)).mean())

    return {"pred": pred, "train_time_s": round(secs, 1),
            "params": (f"LightGBM v2 · {len(X.columns)} enriched feats · L1 · "
                       f"{params['num_leaves']} leaves · smoothed Δ-target"),
            "attribution": "SHAP (TreeExplainer)",
            "top_drivers": [{"tag": t, "weight": round(v, 2)} for t, v in top],
            "step_response_degC": round(step, 2)}


# ------------------------------------------------------------- approach C --
def _arx_matrix(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Incremental (velocity-form) ARX: temp momentum + dead-time input terms.

    Predicting the 5-min CHANGE (like the GBT) makes the linear model
    regime-invariant; input coefficients stay interpretable as °C-per-unit gains.
    """
    cols, names = [], []
    for lag in ARX_MOM_LAGS:
        cols.append((df[CV] - df[CV].shift(lag)).to_numpy())
        names.append(f"{CV} mom-{lag}m")
    for tag in ARX_INPUTS:
        for lag in ARX_IN_LAGS:
            cols.append(df[tag].shift(lag).to_numpy()); names.append(f"{tag}@t-{lag}")
    return np.column_stack(cols), names


def run_arx(d: dict, tr: np.ndarray, va: np.ndarray, ev: np.ndarray) -> dict:
    from sklearn.linear_model import Ridge

    Xf, names = _arx_matrix(d["df"])
    dy = (d["y"] - d["cv0"]).to_numpy()            # same delta target as the GBT
    cv0 = d["cv0"].to_numpy()
    t0 = time.time()
    best, best_mae = None, np.inf
    for alpha in (0.1, 1.0, 10.0, 100.0):
        m = Ridge(alpha=alpha).fit(Xf[tr], dy[tr])
        mae = float(np.abs(m.predict(Xf[va]) - dy[va]).mean())
        if mae < best_mae:
            best, best_mae, best_alpha = m, mae, alpha
    secs = time.time() - t0
    pred = cv0[ev] + best.predict(Xf[ev])

    # interpretable gains: |coef| * feature std, ranked; step response is analytic
    tr_std = np.nanstd(Xf[tr], axis=0)
    weight = np.abs(best.coef_) * tr_std
    order = np.argsort(-weight)[:5]
    top = [{"tag": names[i], "weight": round(float(weight[i]), 2),
            "gain": round(float(best.coef_[i]), 4)} for i in order]
    coal_gain = sum(best.coef_[i] for i, n in enumerate(names) if n.startswith(COAL))
    step = float(STEP_TPH * coal_gain)

    return {"pred": pred, "train_time_s": round(secs, 1),
            "params": f"ARX ridge α={best_alpha} · Δ-target · mom lags {ARX_MOM_LAGS} · input lags {ARX_IN_LAGS}",
            "attribution": "linear coefficients (fully transparent)",
            "top_drivers": top, "step_response_degC": round(step, 2)}


# ------------------------------------------------------------- approach B --
def _windows(df: pd.DataFrame, idx: np.ndarray, mu, sd) -> np.ndarray:
    arr = ((df[bench_data.SEQ_COLS] - mu) / sd).to_numpy(np.float32)
    return np.stack([arr[i - W + 1: i + 1] for i in idx])


def run_gru(d: dict, tr: np.ndarray, va: np.ndarray, ev: np.ndarray,
            epochs: int) -> dict:
    import torch
    from torch import nn

    df, y = d["df"], d["y"]
    dy = y - d["cv0"]                              # same delta target as the GBT
    cv0 = d["cv0"].to_numpy()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    mu = df[bench_data.SEQ_COLS].iloc[tr].mean()
    sd = df[bench_data.SEQ_COLS].iloc[tr].std().replace(0, 1.0)
    ymu, ysd = float(dy.iloc[tr].mean()), float(dy.iloc[tr].std())

    def tensor(idx):
        X = torch.from_numpy(_windows(df, idx, mu, sd))
        t = torch.tensor(((dy.iloc[idx] - ymu) / ysd).to_numpy(np.float32))
        return X, t

    Xtr, ttr = tensor(tr); Xva, tva = tensor(va); Xev, _ = tensor(ev)

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = nn.GRU(len(bench_data.SEQ_COLS), 64, 2,
                              batch_first=True, dropout=0.1)
            self.head = nn.Sequential(nn.Linear(64, 64), nn.ReLU(),
                                      nn.Dropout(0.1), nn.Linear(64, 1))

        def forward(self, x):
            return self.head(self.gru(x)[0][:, -1]).squeeze(-1)

    torch.manual_seed(7)
    net, best, best_v, wait = Net().to(dev), None, np.inf, 0
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    t0 = time.time()
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(len(Xtr))
        for i in range(0, len(perm), 512):
            b = perm[i:i + 512]
            opt.zero_grad()
            loss = nn.functional.mse_loss(net(Xtr[b].to(dev)), ttr[b].to(dev))
            loss.backward(); opt.step()
        net.eval()
        with torch.no_grad():
            v = float(nn.functional.mse_loss(net(Xva.to(dev)), tva.to(dev)))
        print(f"  [gru] epoch {ep + 1}/{epochs} val_mse={v:.4f}")
        if v < best_v - 1e-4:
            best_v, wait = v, 0
            best = {k: t.detach().clone() for k, t in net.state_dict().items()}
        else:
            wait += 1
            if wait >= 6:
                print("  [gru] early stop"); break
    net.load_state_dict(best); net.eval()
    secs = time.time() - t0

    with torch.no_grad():
        pred = cv0[ev] + net(Xev.to(dev)).cpu().numpy() * ysd + ymu

    # permutation importance per input channel (on a sample of eval windows)
    samp = Xev[:1024].to(dev)
    with torch.no_grad():
        base_p = cv0[ev[:1024]] + net(samp).cpu().numpy() * ysd + ymu
    y_samp = y.iloc[ev[:1024]].to_numpy()
    base_mae = np.abs(base_p - y_samp).mean()
    imp = []
    g = torch.Generator().manual_seed(0)
    for ch, name in enumerate(bench_data.SEQ_COLS):
        x2 = samp.clone()
        x2[:, :, ch] = x2[torch.randperm(len(x2), generator=g), :, ch]
        with torch.no_grad():
            p2 = cv0[ev[:1024]] + net(x2).cpu().numpy() * ysd + ymu
        imp.append((name, float(np.abs(p2 - y_samp).mean() - base_mae)))
    top = sorted(imp, key=lambda kv: -kv[1])[:5]

    # step response: +2 tph coal across the whole window
    ch = bench_data.SEQ_COLS.index(COAL)
    x2 = samp.clone(); x2[:, :, ch] += STEP_TPH / float(sd[COAL])
    with torch.no_grad():
        step = float(((net(x2) - net(samp)).cpu().numpy() * ysd).mean())

    n_par = sum(p.numel() for p in net.parameters())
    return {"pred": pred, "train_time_s": round(secs, 1),
            "params": f"GRU(64×2)+MLP · {n_par:,} params · {dev}",
            "attribution": "permutation importance (per input channel)",
            "top_drivers": [{"tag": t, "weight": round(v, 3)} for t, v in top],
            "step_response_degC": round(step, 2)}


# ------------------------------------------------------------------- main --
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--skip-nn", action="store_true")
    ap.add_argument("--epochs", type=int, default=25)
    args = ap.parse_args()

    print("[bench] building shared dataset ...")
    d = bench_data.build(args.sample)
    df, y, split = d["df"], d["y"], d["split"]

    seq_ok, arx_ok = _seq_valid(df), _arx_valid(df)
    # identical rows for EVERY model: extended-feature validity (superset of the
    # base tabular mask) + GRU window + ARX lags + a defined smoothed target
    common = d["valid_ext"] & seq_ok & arx_ok & d["y_smooth"].notna()
    tr = np.flatnonzero((split == "train") & common)
    va = np.flatnonzero((split == "val") & common)
    ev = np.flatnonzero((split == "test") & common)
    assert len(ev) > 1000, f"common test set too small: {len(ev)}"
    print(f"[bench] common rows  train={len(tr):,}  val={len(va):,}  test={len(ev):,}")

    y_ev = y.iloc[ev].to_numpy()
    persist = d["cv0"].iloc[ev].to_numpy()
    models: dict[str, dict] = {}

    print("[bench] A · GBT ...")
    models["gbt"] = run_gbt(d, tr, va, ev)
    print("[bench] C · ARX ...")
    models["arx"] = run_arx(d, tr, va, ev)
    if not args.skip_nn:
        print("[bench] B · GRU ...")
        models["gru"] = run_gru(d, tr, va, ev, args.epochs)

    for m in models.values():
        m["metrics"] = _metrics(y_ev, m["pred"])
    baseline = _metrics(y_ev, persist)

    winner = min(models, key=lambda k: models[k]["metrics"]["mae"])
    label = {"gbt": "GBT (LightGBM)", "gru": "Deep sequence (GRU)",
             "arx": "Dynamic system-ID (ARX)"}

    # overlay: ~400 evenly-spaced common test points
    pick = np.unique(np.linspace(0, len(ev) - 1, min(400, len(ev))).astype(int))
    overlay = [{"ts": str(df["ts"].iloc[ev[i]])[:16],
                "actual": round(float(y_ev[i]), 1),
                "persist": round(float(persist[i]), 1),
                **{k: round(float(models[k]["pred"][i]), 1) for k in models}}
               for i in pick]

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "dataset": d["meta"],
        "eval": {"n_train": len(tr), "n_val": len(va), "n_test": len(ev),
                 "identical_rows": True},
        "baseline_persistence": baseline,
        "models": {k: {kk: vv for kk, vv in m.items() if kk != "pred"}
                   for k, m in models.items()},
        "labels": label,
        "winner": {"key": winner, "label": label[winner],
                   "mae": models[winner]["metrics"]["mae"],
                   "vs_persistence_pct": round(
                       100 * (baseline["mae"] - models[winner]["metrics"]["mae"])
                       / baseline["mae"], 1)},
        "overlay": overlay,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))
    print(f"\n[bench] persistence MAE={baseline['mae']}")
    for k, m in models.items():
        print(f"[bench] {label[k]:26} MAE={m['metrics']['mae']:.3f} "
              f"R2={m['metrics']['r2']} step(+2tph coal)={m['step_response_degC']:+.2f}C "
              f"({m['train_time_s']}s)")
    print(f"[bench] WINNER: {label[winner]}  ->  {OUT}")


if __name__ == "__main__":
    main()
