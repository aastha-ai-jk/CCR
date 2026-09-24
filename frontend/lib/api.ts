const BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

async function get<T = any>(path: string, retries = 2): Promise<T> {
  try {
    const r = await fetch(`${BASE}${path}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  } catch (e) {
    if (retries > 0) {
      await new Promise((res) => setTimeout(res, 400));
      return get<T>(path, retries - 1);
    }
    throw e;
  }
}
async function post<T = any>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export interface Tag {
  tag: string; name: string; uom: string | null;
  y_min: number | null; y_max: number | null; description: string | null;
  control_role: string; subsystem: string; ai_tag: string | null;
}
export interface DQ {
  tag: string; name: string; control_role: string; n: number; n_valid: number;
  pct_valid: number; n_out_of_range: number; vmin: number; vmax: number; vmean: number;
}
export interface Milestone {
  phase: string; title: string; detail: string; status: string; ordinal: number;
}
// GBT model trained on 1-second PLC data (nn_twin/plc_pipeline.py) -- see
// backend/forecast.py and README.md "uat branch" section for the honest
// accuracy caveat (model_test_mae is a training-time, non-causal-smoothing
// number; real live-serving accuracy is closer to ~7-9 degC).
export interface ForecastResponse {
  available: boolean;
  reason?: string;
  predicted_temp_60s?: number;
  current_temp?: number | null;
  predicted_change?: number | null;
  horizon_s?: number;
  method?: string;
  model_test_mae?: number;
  model_test_r2?: number;
  as_of?: string;
  imputed_tags?: string[];
}

// ---- Twin / optimizer response shapes (see twin/CONTRACT.md) ----
export interface Interval { mean: number; p10: number; p90: number; }

export interface OptimizeInterlock {
  tag: string; name: string; rule: string; predicted: number | null; pass: boolean | null;
}
export interface TrajectoryPoint { min: number; mean: number; p10: number; p90: number }
export interface OptimizeResult {
  trajectory?: TrajectoryPoint[] | null;
  reach_min?: number | null;
  band_confidence?: number | null;
  recommended: Record<string, number>;
  predicted: { temp: Interval; safety: Record<string, number> };
  operating_state: string;
  target_band: { low: number; high: number };
  in_band: boolean;
  interlocks: OptimizeInterlock[];
  objective: { coal: number; afr_total: number };
  baseline: { levers: Record<string, number>; temp: number };
  delta: { coal: number; afr: number; temp: number };
  explanation: string[];
  n_trials: number;
  advisory: boolean;
}

export interface TempMetrics { mae: number; rmse: number; r2: number; interval_coverage?: number; }
export interface TwinMetrics {
  available?: boolean;
  temp?: TempMetrics;
  safety?: Record<string, { mae: number; rmse: number; r2: number }>;
  quality?: Record<string, { mae?: number; rmse?: number; r2?: number }>;
  baseline_persistence?: { temp: { mae: number; rmse: number } };
  nn?: { model?: string; device?: string; temp?: TempMetrics; params?: number; epochs?: number };
  n_train?: number;
  n_test?: number;
  trained_at?: string;
}

export interface BacktestPoint { ts: string; actual: number; pred: number; p10: number; p90: number; }
export interface TwinBacktest {
  series: BacktestPoint[];
  mae: number; rmse: number; r2: number; n: number; tag: string;
}

export interface ShadowPoint {
  ts: string; horizon_ts: string; actual_now: number | null;
  pred_mean: number | null; pred_p10: number | null; pred_p90: number | null;
  state: string | null; band_lo: number | null; band_hi: number | null;
  in_band_now: number | null; matured: number;
  actual_future: number | null; abs_error: number | null;
  rec_coal: number | null; delta_coal: number | null;
  rec_afr: number | null; delta_afr: number | null;
  rec_in_band: number | null; interlocks_ok: number | null;
}
export interface BenchDriver { tag: string; weight: number; gain?: number }
export interface BenchModel {
  metrics: { mae: number; rmse: number; r2: number | null };
  train_time_s: number; params: string; attribution: string;
  top_drivers: BenchDriver[]; step_response_degC: number;
}
export interface BenchResult {
  available: boolean; note?: string;
  generated_at?: string;
  dataset?: { csv: string; rows_after_gate: number; span: string[]; horizon_min: number; target: string };
  eval?: { n_train: number; n_val: number; n_test: number; identical_rows: boolean };
  baseline_persistence?: { mae: number; rmse: number; r2: number | null };
  models?: Record<string, BenchModel>;
  labels?: Record<string, string>;
  winner?: { key: string; label: string; mae: number; vs_persistence_pct: number };
  overlay?: Array<Record<string, any>>;
}

export interface ShadowResult {
  available: boolean; n: number; matured?: number;
  running_mae?: number | null; pct_within_5C?: number | null; horizon_min?: number;
  current?: ShadowPoint | null; series?: ShadowPoint[]; note?: string;
}

export const api = {
  health: () => get("/api/health"),
  tags: () => get<Tag[]>("/api/tags"),
  dataQuality: () => get<DQ[]>("/api/data-quality"),
  roadmap: () => get<Milestone[]>("/api/roadmap"),
  overview: () => get("/api/overview"),
  latest: () => get("/api/latest"),
  advisory: () => get("/api/advisory"),
  advisoryDecision: (payload: {
    decision: "accepted" | "declined";
    operating_state?: string; mode?: string;
    target_band_low?: number; target_band_high?: number;
    live_actual_temp?: number; predicted_temp?: number;
    moves?: unknown[]; reasoning?: string; reason_category?: string; note?: string;
  }) => post("/api/advisory/decision", payload),
  declineReasons: () => get<{ reasons: string[] }>("/api/advisory/decline-reasons"),
  advisoryDecisions: (limit = 50) => get<{ decisions: any[] }>(`/api/advisory/decisions?limit=${limit}`),
  trend: (tag = "423CI400_TT_4", minutes = 60) =>
    get(`/api/trend?tag=${tag}&minutes=${minutes}`),
  series: (tags: string[], p: { start?: string; end?: string; bucket?: string; in_load?: boolean; window?: "day" | "week" | "month" } = {}) => {
    const q = new URLSearchParams({ tags: tags.join(",") });
    if (p.start) q.set("start", p.start);
    if (p.end) q.set("end", p.end);
    if (p.bucket) q.set("bucket", p.bucket);
    if (p.in_load) q.set("in_load", "true");
    if (p.window) q.set("window", p.window);
    return get(`/api/series?${q}`);
  },
  correlation: (tags: string[], bucket = "15min") =>
    get(`/api/correlation?tags=${tags.join(",")}&bucket=${bucket}`),
  crosscorr: (a: string, b: string, maxLag = 30) =>
    get(`/api/crosscorr?a=${a}&b=${b}&max_lag_min=${maxLag}`),
  labHotmeal: () => get("/api/lab/hotmeal"),
  labClinker: () => get("/api/lab/clinker"),
  simDefaults: () => get("/api/simulate/defaults"),
  simulate: (levers: Record<string, number>, conditions: Record<string, number>, k = 200) =>
    post("/api/simulate", { levers, conditions, k }),
  forecast: (minutesBack = 20) =>
    get<ForecastResponse>(`/api/forecast?minutes_back=${minutesBack}`),
  optimize: (conditions: Record<string, number>) =>
    post<OptimizeResult>("/api/optimize", { conditions }),
  twinMetrics: () => get<TwinMetrics>("/api/twin/metrics"),
  shadow: (limit = 240) => get<ShadowResult>(`/api/shadow?limit=${limit}`),
  bench: () => get<BenchResult>("/api/bench"),
  twinBacktest: (p: { start?: string; end?: string; max_points?: number } = {}) => {
    const q = new URLSearchParams();
    if (p.start) q.set("start", p.start);
    if (p.end) q.set("end", p.end);
    if (p.max_points) q.set("max_points", String(p.max_points));
    const qs = q.toString();
    return get<TwinBacktest>(`/api/twin/backtest${qs ? `?${qs}` : ""}`);
  },
};

export const ROLE_COLOR: Record<string, string> = {
  target: "#38bdf8", safety: "#ef4444", mv: "#22c55e", dv: "#8ea3c4", status: "#a78bfa",
};
