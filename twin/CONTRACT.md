# Twin package — shared interface contract

All modules code against these exact signatures so parallel work integrates.
Reuse constants from `backend/analysis.py` and `config.py` — never redefine them.

Shared domain (import from `backend.analysis`):
- `CV = "423CI400_TT_4"` (primary target, calciner outlet temp)
- `MV` = 8 levers; `SAFETY` = 4 tags; `STATE_PLC` = 3 confounders; `DELTA = 5` (min horizon)
- `STATE_RULES`, `_classify_state`
From `config`: `INTERLOCKS` (dict tag→(op,limit)), `TEMP_STATE_BANDS`, `KILN_FEED_MIN_TPH`.
From `backend.db`: `q(sql,params)`, `tag_catalog()`, `tag_names()`.
DB tables: `process_1min` (33 cols incl `feed_gate`), `lab_hotmeal`, `lab_clinker`.

Optimizer levers (the 4 calciner-side MVs it searches):
`OPT_LEVERS = ["467RL400_TPH", "LAFR_FT", "K3V10_F1", "261WF080_TPH"]`
(coal to MINIMISE; LAFR_FT + K3V10_F1 + 261WF080_TPH are AFR to MAXIMISE.)

---

## twin/features.py  (Agent A)
```python
HORIZON_MIN = 5                      # == analysis.DELTA
def feature_columns() -> list[str]   # ordered feature names (MVs, DV states, lags, rolls, rates, valid masks)
def build_training_data(sample: int | None = None) -> dict
    # returns {
    #   "X": DataFrame[feature_columns],           # rows = feed_gate==1, dead-time aligned
    #   "y": {"temp": Series(temp at t+HORIZON),
    #         "safety_<TAG>": Series, ... for each SAFETY tag,
    #         "quality_free_lime": Series, "quality_doc": Series},   # NaNs allowed; drop per-target
    #   "ts": Series[datetime],
    #   "split": Series[str in {"train","val","test"}] }  # TIME-based (chronological), not random
def build_feature_row(levers: dict, conditions: dict) -> "pd.DataFrame"
    # single row, exactly feature_columns order. Missing lever/condition -> recent median default.
    # `conditions` may include current_temp, temp_trend, and STATE_PLC values.
```

## twin/train_gbt.py  (Agent A)  — run: `python -m twin.train_gbt`
Trains LightGBM on features.build_training_data and writes:
- `models/twin_gbt.pkl` (joblib) = {
    "feature_columns": [...], "horizon_min": 5, "trained_at": iso,
    "temp": {"mean": Booster, "p10": Booster, "p90": Booster},
    "safety": {tag: Booster for tag in SAFETY},
    "quality": {"free_lime": Booster, "doc": Booster} }
- `models/metrics.json` = {"temp":{mae,rmse,r2,interval_coverage}, "safety":{tag:{mae,rmse,r2}},
    "quality":{...}, "baseline_persistence":{"temp":{mae,rmse}}, "n_train","n_test","trained_at"}
    (NN fills metrics["nn"] later — merge, don't overwrite.)

## twin/model.py  (Agent C)  — serving
```python
def is_available() -> bool                        # models/twin_gbt.pkl exists & loads
def predict_core(levers: dict, conditions: dict) -> dict | None
    # None if unavailable. Else:
    # { "prediction": { CV: {"mean","p10","p90"},
    #                   <each SAFETY tag>: {"mean","p10","p90"} },
    #   "contributions": { mv: float_degC for mv in MV },   # SHAP of temp model, °C pull per lever
    #   "quality": {"free_lime": float, "doc": float},
    #   "method": "gbt-twin" }
```
`backend/analysis.simulate()` calls `predict_core`; if it returns a dict, use its prediction +
contributions and skip the kNN core (keep the existing state/band/interlock logic and return shape,
set method accordingly). If None, fall back to the existing kNN surrogate unchanged.

## twin/optimize.py  (Agent C)  — `optimize(conditions, n_trials=200) -> dict`
Optuna over OPT_LEVERS (bounds = dim_tag y_min/y_max). Objective: minimise coal, maximise AFR
(normalised). Hard constraints (reject trial): predicted temp inside state target band, and every
config.INTERLOCKS predicted-satisfied via safety models. Returns:
```
{ "recommended": {mv: value}, "predicted": {"temp":{mean,p10,p90}, "safety":{tag:val}},
  "operating_state": str, "target_band": {"low","high"}, "in_band": bool,
  "interlocks": [{tag,name,rule,predicted,pass}], "objective": {"coal","afr_total"},
  "baseline": {"levers":{...}, "temp":float}, "delta": {"coal": float, "afr": float, "temp": float},
  "explanation": [str, ...], "n_trials": int, "advisory": true }
```
ADVISORY ONLY — recommend, never write to any control system.

## twin/train_nn.py  (Agent B)  — run: `python -m twin.train_nn` (uses CUDA if available)
PyTorch GRU (or TCN) on windowed lag sequences predicting temp at t+HORIZON. Self-contained
windowing from process_1min (feed_gate==1), same chronological split logic. Writes
`models/nn_metrics.json` = {"model":"gru","device":..., "temp":{mae,rmse,r2}, "params":...,
"epochs":...} and merges into models/metrics.json under key "nn". GBT stays the served default.

## Backend endpoints  (Agent C — edits backend/main.py, backend/analysis.py, NOT other files)
- `POST /api/optimize`  body {conditions:{}} -> twin.optimize.optimize(conditions)
- `GET  /api/twin/metrics` -> contents of models/metrics.json (or {available:false})
- `GET  /api/twin/backtest?start=&end=&max_points=800` ->
    { "series":[{ts, actual, pred, p10, p90}], "mae","rmse","r2","n","tag":CV }
    (walk a held-out window; predict temp at each row via twin.model, compare to actual t+Δ.)

## Frontend  (Agent D — edits frontend/lib/api.ts, app/simulator/page.tsx, app/twin/page.tsx ONLY)
Keep the existing design tokens/components (ui.tsx, Recharts, lucide). Do NOT restyle other pages.
- api.ts: add `optimize(conditions)`, `twinMetrics()`, `twinBacktest(params)`.
- simulator/page.tsx: it already renders prediction/contributions/interlocks/band. Add an
  **"Optimize setpoints"** button -> POST /api/optimize; show recommended levers + predicted
  temp/safety + a one-click "apply to sliders". Label contributions as SHAP (°C).
- twin/page.tsx: add a **Model-Quality** panel (from /api/twin/metrics: temp MAE/RMSE/R², GBT vs
  NN vs persistence) and a **twin-vs-actual backtest** line chart (from /api/twin/backtest).
