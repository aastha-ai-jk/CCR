"""Shadow-mode monitor: rolling accuracy, conformal recalibration, M&V mining.

Runs periodically (PM2 cron, every 6 h) and once per invocation:
  1. ACCURACY   — tracking stats over the last 24 h and since shadow start
                  (MAE, median, %within 5/10 °C, interval coverage).
  2. CONFORMAL  — live recalibration of the t+5 interval: q80/q90 of matured
                  |residuals| over the last 3 days → ``models/conformal.json``
                  (picked up by twin.model at serving time).
  3. AFR        — summary of the online fuel-quality signal (bias distribution,
                  % time hot/cold).
  4. M&V        — followed-vs-ignored advisory mining: did the plant move coal
                  toward the recommendation within 15 min, and was the band
                  held better afterwards? (Coincidental alignment until the
                  advisory is surfaced to operators — labelled as such.)

Every run appends one JSON line to ``models/monitor_log.jsonl`` — the
persistent evidence trail for judging which changes are actually favourable.

Run: python -m twin.monitor            (also safe to run manually any time)
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

import config

MODELS = Path(__file__).resolve().parent.parent / "models"
LOG_PATH = MODELS / "monitor_log.jsonl"
CONFORMAL_PATH = MODELS / "conformal.json"
COAL = "467RL400_TPH"
_FOLLOW_MIN = 15          # window (min) for the plant to act on an advisory
_ADVICE_TPH = 0.5         # |delta_coal| above this counts as actionable advice
_CONF_DAYS = 3            # conformal window
_MIN_CONF_N = 60          # minimum matured residuals before publishing q's


def _engine():
    url = config.database_url()
    kw = {"connect_args": {"timeout": 15}} if url.startswith("sqlite") else {}
    return create_engine(url, **kw)


def _r(v, nd=2):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), nd)


def _accuracy(m: pd.DataFrame) -> dict:
    if not len(m):
        return {"n": 0}
    err = (m["actual_future"] - m["pred_mean"]).abs()
    cov = ((m["actual_future"] >= m["pred_p10"])
           & (m["actual_future"] <= m["pred_p90"])).mean()
    return {"n": int(len(m)), "mae": _r(err.mean()), "median_ae": _r(err.median()),
            "within5C": _r((err <= 5).mean(), 3), "within10C": _r((err <= 10).mean(), 3),
            "interval_coverage": _r(cov, 3),
            "in_band_pct": _r(m["in_band_now"].mean(), 3)}


def _conformal(m3: pd.DataFrame) -> dict | None:
    resid = (m3["actual_future"] - m3["pred_mean"]).abs().dropna()
    if len(resid) < _MIN_CONF_N:
        return None
    out = {"q80": _r(float(np.quantile(resid, 0.80))),
           "q90": _r(float(np.quantile(resid, 0.90))),
           "n": int(len(resid)), "window_days": _CONF_DAYS,
           "updated": dt.datetime.now().isoformat(timespec="seconds")}
    CONFORMAL_PATH.write_text(json.dumps(out, indent=1))
    return out


def _afr_summary(eng, cut24: str) -> dict:
    try:
        a = pd.read_sql(text("SELECT * FROM afr_quality WHERE ts >= :c"),
                        eng, params={"c": cut24})
    except Exception:
        return {"n": 0}
    if not len(a):
        return {"n": 0}
    return {"n": int(len(a)), "bias_mean": _r(a["bias_2h"].mean()),
            "bias_p90_abs": _r(a["bias_2h"].abs().quantile(0.9)),
            "pct_hot": _r((a["quality"] == "fuel_hot").mean(), 3),
            "pct_cold": _r((a["quality"] == "fuel_cold").mean(), 3),
            "afr_share_mean": _r(a["afr_share"].mean(), 3)}


def _mnv_followed(df: pd.DataFrame) -> dict:
    """Did coal move toward the advisory within _FOLLOW_MIN? Band held after?"""
    d = df.dropna(subset=["rec_coal", "cur_coal", "delta_coal"]).reset_index(drop=True)
    d = d[d["delta_coal"].abs() >= _ADVICE_TPH]
    if len(d) < 10:
        return {"n_advice": int(len(d)), "note": "insufficient advisory ticks"}
    d["ts"] = pd.to_datetime(d["ts"])
    followed, ignored = [], []
    full = df.dropna(subset=["cur_coal"]).copy()
    full["ts"] = pd.to_datetime(full["ts"])
    for row in d.itertuples():
        horizon = row.ts + pd.Timedelta(minutes=_FOLLOW_MIN)
        seg = full[(full["ts"] > row.ts) & (full["ts"] <= horizon)]
        if not len(seg):
            continue
        moved = float(seg["cur_coal"].iloc[-1] - row.cur_coal)
        frac = moved / row.delta_coal if row.delta_coal else 0.0   # 1.0 = fully followed
        outcome = seg["in_band_now"].mean()
        (followed if frac >= 0.5 else ignored if frac <= 0.2 else []).append(outcome)
    return {"n_advice": int(len(d)),
            "n_followed": len(followed), "n_ignored": len(ignored),
            "in_band_after_followed": _r(np.mean(followed), 3) if followed else None,
            "in_band_after_ignored": _r(np.mean(ignored), 3) if ignored else None,
            "note": "coincidental alignment (advisory not yet surfaced in CCR)"}


def main() -> None:
    eng = _engine()
    now = dt.datetime.now()
    cut24 = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    cut3d = (now - dt.timedelta(days=_CONF_DAYS)).isoformat(timespec="seconds")

    log = pd.read_sql("SELECT * FROM shadow_log", eng)
    m_all = log[log["matured"] == 1]
    m_24 = m_all[m_all["ts"] >= cut24]
    m_3d = m_all[m_all["ts"] >= cut3d]

    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "shadow_rows": int(len(log)), "matured_total": int(len(m_all)),
        "acc_24h": _accuracy(m_24),
        "acc_all": _accuracy(m_all),
        "conformal": _conformal(m_3d),
        "afr_24h": _afr_summary(eng, cut24),
        "mnv_followed": _mnv_followed(log[log["ts"] >= cut3d]),
        "model_version": _model_version(),
    }
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    a = entry["acc_24h"]
    print(f"[monitor] 24h: n={a.get('n')} MAE={a.get('mae')} "
          f"within10C={a.get('within10C')} coverage={a.get('interval_coverage')}")
    print(f"[monitor] conformal: {entry['conformal']}")
    print(f"[monitor] afr: {entry['afr_24h']}")
    print(f"[monitor] mnv: {entry['mnv_followed']}")
    print(f"[monitor] appended -> {LOG_PATH}")


def _model_version() -> str | None:
    try:
        import joblib
        b = joblib.load(MODELS / "twin_gbt.pkl")
        return str(b.get("version", "v?")) + " @ " + str(b.get("trained_at", ""))[:16]
    except Exception:
        return None


if __name__ == "__main__":
    main()
