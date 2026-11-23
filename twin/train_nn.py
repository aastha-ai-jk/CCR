"""Neural-network challenger for the calciner digital twin (Agent B).

A compact GRU sequence-regressor predicting the calciner outlet temperature
(``CV``) ``HORIZON`` minutes ahead from a sliding window of the manipulated
variables, key PLC-state confounders and the temperature itself. The GBT model
(Agent A) stays the served default; this NN is a benchmarked challenger. The
windowing is self-contained (no dependency on ``twin/features.py``).

Run:  ``python -m twin.train_nn --epochs 60 --sample 120000``

torch is imported lazily inside ``main`` so ``python -m py_compile`` succeeds
without torch (training runs later on a CUDA GPU).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend import db
from backend.analysis import CV, DELTA, MV, STATE_PLC

# --- Window / horizon configuration --------------------------------------
WINDOW_MIN = 15                       # sliding-window length W (minutes/rows)
HORIZON = DELTA                       # forward target horizon (== 5 min)
FWD_GAP_MIN, FWD_GAP_MAX = 4.0, 7.0   # allowed real forward gap end->target (min)
FFILL_LIMIT = 2                       # forward-fill only small (<=2 step) holes
# window internal span must look contiguous (14 min nominal): allow 0.5x..1.5x
SPAN_MIN = (WINDOW_MIN - 1) * 0.5
SPAN_MAX = (WINDOW_MIN - 1) * 1.5

FEATURE_COLS: list[str] = MV + STATE_PLC + [CV]   # ordered model inputs
CV_IDX = FEATURE_COLS.index(CV)

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
NN_PT = MODELS_DIR / "nn_gru.pt"
NN_METRICS = MODELS_DIR / "nn_metrics.json"
METRICS_JSON = MODELS_DIR / "metrics.json"

# --- Model hyper-parameters ----------------------------------------------
HIDDEN = 64
LAYERS = 2
BATCH = 256
LR = 1e-3
PATIENCE = 8


# ---------------------------------------------------------------------------
# Data loading + self-contained windowing
# ---------------------------------------------------------------------------
def load_frame(sample: int | None) -> pd.DataFrame:
    """Load in-load rows (``feed_gate=1``) ordered by ts, numeric + ffilled.

    ``sample`` caps to the most-recent N rows to keep the window sequence
    contiguous (a random row sample would destroy the time structure).
    """
    cols = ", ".join(f'"{c}"' for c in dict.fromkeys([CV] + MV + STATE_PLC))
    df = db.q(f"SELECT ts, {cols} FROM process_1min WHERE feed_gate=1 ORDER BY ts")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    for c in FEATURE_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)
    # forward-fill only small gaps; large holes remain NaN and drop out windows.
    df[FEATURE_COLS] = df[FEATURE_COLS].ffill(limit=FFILL_LIMIT)
    if sample and len(df) > sample:
        df = df.tail(sample).reset_index(drop=True)
    return df


def build_windows(df: pd.DataFrame) -> dict[str, Any]:
    """Build leakage-free chronological train/val/test window tensors.

    Windows are assigned to a split only if their ENTIRE span ``[i, target]``
    lies inside one 70/15/15 chronological segment; boundary-straddling windows
    are dropped so no window leaks across the split.
    """
    n = len(df)
    if n < WINDOW_MIN + HORIZON + 10:
        raise ValueError(f"too few rows ({n}) for W={WINDOW_MIN}+H={HORIZON}")

    feat = df[FEATURE_COLS].to_numpy(dtype=np.float64)
    tmin = df["ts"].to_numpy(dtype="datetime64[s]").astype("int64") / 60.0
    row_valid = ~np.isnan(feat).any(axis=1)
    csum = np.concatenate([[0], np.cumsum(row_valid.astype(np.int64))])

    starts = np.arange(n - WINDOW_MIN - HORIZON + 1)          # i
    ends = starts + WINDOW_MIN - 1                            # e (last window row)
    targets = ends + HORIZON                                  # j (label row)

    win_rows_ok = (csum[starts + WINDOW_MIN] - csum[starts]) == WINDOW_MIN
    tgt_ok = row_valid[targets]
    span = tmin[ends] - tmin[starts]
    span_ok = (span >= SPAN_MIN) & (span <= SPAN_MAX)
    fwd = tmin[targets] - tmin[ends]
    fwd_ok = (fwd >= FWD_GAP_MIN) & (fwd <= FWD_GAP_MAX)
    valid = win_rows_ok & tgt_ok & span_ok & fwd_ok

    # Chronological 70/15/15 cut positions (row indices).
    cut1, cut2 = int(0.70 * n), int(0.85 * n)
    split = np.full(len(starts), "", dtype=object)
    split[valid & (targets < cut1)] = "train"
    split[valid & (starts >= cut1) & (targets < cut2)] = "val"
    split[valid & (starts >= cut2)] = "test"

    idx = {s: np.where(split == s)[0] for s in ("train", "val", "test")}
    if any(len(idx[s]) == 0 for s in idx):
        raise ValueError(f"empty split: { {s: len(v) for s, v in idx.items()} }")

    # Standardize with TRAIN-region statistics only (saved for inference).
    train_rows = feat[:cut1]
    mu = np.nanmean(train_rows, axis=0)
    sd = np.nanstd(train_rows, axis=0)
    sd[sd == 0] = 1.0
    feat_n = (feat - mu) / sd
    cv_mean, cv_std = float(mu[CV_IDX]), float(sd[CV_IDX])

    def stack(split_starts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        offs = split_starts[:, None] + np.arange(WINDOW_MIN)      # (M, W)
        x = feat_n[offs].astype(np.float32)                        # (M, W, D)
        j = split_starts + WINDOW_MIN - 1 + HORIZON
        y = ((feat[j, CV_IDX] - cv_mean) / cv_std).astype(np.float32)
        return x, y

    xtr, ytr = stack(starts[idx["train"]])
    xva, yva = stack(starts[idx["val"]])
    xte, yte = stack(starts[idx["test"]])
    return {
        "train": (xtr, ytr), "val": (xva, yva), "test": (xte, yte),
        "mu": mu.tolist(), "sd": sd.tolist(),
        "cv_mean": cv_mean, "cv_std": cv_std,
        "n": {"train": len(ytr), "val": len(yva), "test": len(yte)},
    }


# ---------------------------------------------------------------------------
# Model (torch imported lazily via factory so py_compile needs no torch)
# ---------------------------------------------------------------------------
def build_model(input_dim: int):
    """Return a compact 2-layer GRU regressor -> MLP head -> scalar temp."""
    import torch
    from torch import nn

    class GRURegressor(nn.Module):
        def __init__(self, d_in: int, hidden: int, layers: int) -> None:
            super().__init__()
            self.gru = nn.GRU(
                d_in, hidden, num_layers=layers,
                batch_first=True, dropout=0.1 if layers > 1 else 0.0,
            )
            self.head = nn.Sequential(
                nn.Linear(hidden, hidden), nn.ReLU(),
                nn.Dropout(0.1), nn.Linear(hidden, 1),
            )

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            _, h = self.gru(x)            # h: (layers, B, hidden)
            return self.head(h[-1]).squeeze(-1)   # (B,)

    return GRURegressor(input_dim, HIDDEN, LAYERS)


def _metrics(pred: np.ndarray, true: np.ndarray) -> dict[str, float]:
    err = pred - true
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((true - true.mean()) ** 2)) or 1.0
    return {"mae": round(mae, 4), "rmse": round(rmse, 4),
            "r2": round(1.0 - ss_res / ss_tot, 4)}


def _merge_metrics(temp: dict[str, float]) -> None:
    data: dict[str, Any] = {}
    if METRICS_JSON.exists():
        try:
            data = json.loads(METRICS_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data["nn"] = temp
    METRICS_JSON.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Train
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Train the GRU twin challenger.")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--sample", type=int, default=None,
                        help="cap to most-recent N rows (keeps windows contiguous)")
    args = parser.parse_args()

    import torch
    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset

    device = "cuda" if torch.cuda.is_available() else "cpu"
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    df = load_frame(args.sample)
    if df.empty:
        print("No rows returned (process_1min WHERE feed_gate=1 is empty). Aborting.")
        return
    try:
        win = build_windows(df)
    except ValueError as exc:
        print(f"Windowing failed: {exc}")
        return
    print(f"windows -> {win['n']} | features={len(FEATURE_COLS)} "
          f"W={WINDOW_MIN} horizon={HORIZON} device={device}")

    def loader(split: str, shuffle: bool) -> "DataLoader":
        x, y = win[split]
        ds = TensorDataset(torch.from_numpy(x), torch.from_numpy(y))
        return DataLoader(ds, batch_size=BATCH, shuffle=shuffle)

    tr_dl, va_dl = loader("train", True), loader("val", False)
    cv_mean, cv_std = win["cv_mean"], win["cv_std"]

    torch.manual_seed(0)
    model = build_model(len(FEATURE_COLS)).to(device)
    n_params = int(sum(p.numel() for p in model.parameters()))
    opt = torch.optim.Adam(model.parameters(), lr=LR)
    loss_fn = nn.MSELoss()

    best_val = float("inf")
    best_state: dict[str, Any] | None = None
    stale = 0
    for epoch in range(1, args.epochs + 1):
        model.train()
        tr_loss = 0.0
        for xb, yb in tr_dl:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            opt.step()
            tr_loss += float(loss) * len(yb)
        tr_loss /= len(tr_dl.dataset)

        model.eval()
        preds, trues = [], []
        with torch.no_grad():
            for xb, yb in va_dl:
                preds.append(model(xb.to(device)).cpu().numpy())
                trues.append(yb.numpy())
        vp = np.concatenate(preds) * cv_std + cv_mean
        vt = np.concatenate(trues) * cv_std + cv_mean
        val_mse = float(np.mean((vp - vt) ** 2))
        val_mae = float(np.mean(np.abs(vp - vt)))
        print(f"epoch {epoch:3d}  train_mse={tr_loss:.4f}  "
              f"val_mse={val_mse:.4f}  val_MAE={val_mae:.2f}C")

        if val_mse < best_val - 1e-6:
            best_val = val_mse
            best_state = {k: v.detach().cpu().clone()
                          for k, v in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= PATIENCE:
                print(f"early stop @ epoch {epoch} (no val gain in {PATIENCE})")
                break

    if best_state is not None:
        model.load_state_dict(best_state)

    # --- Test evaluation (metrics in original degC) ----------------------
    model.eval()
    xte, yte = win["test"]
    with torch.no_grad():
        tp = model(torch.from_numpy(xte).to(device)).cpu().numpy()
    pred = tp * cv_std + cv_mean
    true = yte * cv_std + cv_mean
    temp = _metrics(pred, true)

    # --- Persist model + stats + window config ---------------------------
    torch.save({
        "state_dict": model.state_dict(),
        "feature_columns": FEATURE_COLS,
        "standardization": {"mu": win["mu"], "sd": win["sd"],
                            "cv_mean": cv_mean, "cv_std": cv_std},
        "window": {"window_min": WINDOW_MIN, "horizon": HORIZON,
                   "input_dim": len(FEATURE_COLS)},
        "arch": {"hidden": HIDDEN, "layers": LAYERS, "type": "gru"},
    }, NN_PT)

    nn_metrics = {
        "model": "gru", "device": device, "temp": temp,
        "params": n_params, "epochs": epoch, "window_min": WINDOW_MIN,
        "n_train": win["n"]["train"], "n_test": win["n"]["test"],
    }
    NN_METRICS.write_text(json.dumps(nn_metrics, indent=2))
    _merge_metrics(temp)

    print(f"saved {NN_PT.name} ({n_params} params) + metrics")
    print(f"NN temp test MAE={temp['mae']:.3f}  "
          f"RMSE={temp['rmse']:.3f}  R2={temp['r2']:.3f}")


if __name__ == "__main__":
    main()
