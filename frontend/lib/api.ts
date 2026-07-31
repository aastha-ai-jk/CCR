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

export const api = {
  health: () => get("/api/health"),
  tags: () => get<Tag[]>("/api/tags"),
  dataQuality: () => get<DQ[]>("/api/data-quality"),
  roadmap: () => get<Milestone[]>("/api/roadmap"),
  overview: () => get("/api/overview"),
  latest: () => get("/api/latest"),
  advisory: () => get("/api/advisory"),
  trend: (tag = "423CI400_TT_4", minutes = 60) =>
    get(`/api/trend?tag=${tag}&minutes=${minutes}`),
  series: (tags: string[], p: { start?: string; end?: string; bucket?: string; in_load?: boolean } = {}) => {
    const q = new URLSearchParams({ tags: tags.join(",") });
    if (p.start) q.set("start", p.start);
    if (p.end) q.set("end", p.end);
    if (p.bucket) q.set("bucket", p.bucket);
    if (p.in_load) q.set("in_load", "true");
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
};

export const ROLE_COLOR: Record<string, string> = {
  target: "#38bdf8", safety: "#ef4444", mv: "#22c55e", dv: "#8ea3c4", status: "#a78bfa",
};
