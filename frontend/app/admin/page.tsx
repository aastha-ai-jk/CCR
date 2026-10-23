"use client";
import { Fragment, useEffect, useState } from "react";
import { api, DeclinedReachedResult, DeclinedReachedRow } from "@/lib/api";
import { Panel, Stat, Badge, Loading } from "@/components/ui";

// Administrative dashboard.
// Section 1: declined advisor recommendations the plant reached anyway — the
// lever's actual value came within ±tolerance of the recommended value within
// the window after the decline (the advice was right but not accepted).

const DP: Record<string, number> = { "467RL400_TPH": 2, "261WF080_TPH": 2, "LAFR_FT": 0, "K3V10_F1": 0 };
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const hhmm = (ts?: string | null) => (ts ? String(ts).slice(11, 16) : "—");
const num = (v: number | null | undefined, tag: string) =>
  v === null || v === undefined ? "—" : v.toFixed(DP[tag] ?? 1);

const STATUS: Record<DeclinedReachedRow["status"], { tone: string; label: string }> = {
  reached: { tone: "ok", label: "Reached anyway" },
  not_reached: { tone: "muted", label: "Not reached" },
  pending: { tone: "warn", label: "Window open" },
  no_data: { tone: "muted", label: "No plant data" },
};

export default function Admin() {
  const [day, setDay] = useState(today());
  const [data, setData] = useState<DeclinedReachedResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [days, setDays] = useState<{ day: string; declined: number }[]>([]);

  useEffect(() => {
    api.decisionDays().then((d) => setDays(d.filter((x) => x.declined > 0))).catch(() => {});
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () => api.declinedReached(day)
      .then((d) => { if (alive) { setData(d); setErr(null); } })
      .catch((e) => { if (alive) setErr(String(e)); });
    setData(null); load();
    const t = day === today() ? setInterval(load, 60000) : undefined;   // today: windows still closing
    return () => { alive = false; if (t) clearInterval(t); };
  }, [day]);

  const reached = data?.rows.filter((r) => r.status === "reached") ?? [];
  const others = data?.rows.filter((r) => r.status !== "reached") ?? [];

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold">Admin Dashboard</h1>
          <div className="text-sm text-[var(--muted)]">
            Kiln-3 digital twin · administration · decisions come from the{" "}
            <a className="text-[var(--accent)] hover:underline" target="_blank" rel="noopener"
              href={`${process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000"}/advisor`}>CCR Advisor ↗</a>
          </div>
        </div>
        <label className="text-sm text-[var(--muted)] flex items-center gap-2">
          Day
          <input type="date" value={day} max={today()} onChange={(e) => e.target.value && setDay(e.target.value)}
            className="bg-[var(--panel-2)] border border-[var(--line)] rounded px-2 py-1 text-[var(--ink)]" />
        </label>
      </header>

      {days.length > 0 && (
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="text-[var(--muted)]">Days with declines:</span>
          {days.slice(0, 14).map((d) => (
            <button key={d.day} onClick={() => setDay(d.day)}
              className={`px-2 py-0.5 rounded-full border ${d.day === day ? "border-[var(--accent)] text-[var(--accent)]" : "border-[var(--line)] text-[var(--muted)]"}`}>
              {d.day} · {d.declined}
            </button>
          ))}
        </div>
      )}

      {err && <Panel><div className="text-sm text-[var(--bad)]">Could not load: {err}</div></Panel>}
      {!data && !err && <Loading />}

      {data && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <Stat label="Declined recommendations" value={data.declines} sub={`${data.day} · calciner levers`} />
            <Stat label="Plant reached it anyway" value={data.counts.reached} tone={data.counts.reached ? "warn" : "ink"}
              sub={data.declines ? `${Math.round((data.counts.reached / data.declines) * 100)}% of declines` : "—"} />
            <Stat label="Not reached" value={data.counts.not_reached + data.counts.no_data}
              sub={data.counts.no_data ? `${data.counts.no_data} without plant data` : "plant stayed away"} />
            <Stat label="Window still open" value={data.counts.pending} sub={`checked for ${data.window_min} min after decline`} />
          </div>

          <Panel title="Declined, but the plant reached the recommended value"
            right={<span className="text-xs text-[var(--muted)]">match = 1-minute average of the actual value within ±{data.tolerance_pct}% of recommended, within {data.window_min} min of the decline</span>}>
            {reached.length === 0 ? (
              <div className="text-sm text-[var(--muted)]">
                {data.declines === 0 ? "No declined recommendations on this day."
                  : data.counts.pending > 0
                  ? `None yet — ${data.counts.pending} decline${data.counts.pending > 1 ? "s are" : " is"} still within ${data.window_min > 1 ? "the " + data.window_min + "-min" : "its"} window (see below).`
                  : "None — in every decline the plant did not reach the recommended value."}
              </div>
            ) : (
              <Table rows={reached} reached />
            )}
          </Panel>

          {others.length > 0 && (
            <Panel title="Other declines on this day">
              <Table rows={others} />
            </Panel>
          )}
        </>
      )}
    </div>
  );
}

// Evidence chart for one decline: the lever's actual run (10 min before -> 30 min
// after) against the recommended value ±tolerance, with the decline moment and
// the first minute the plant entered the recommended zone.
function ProofChart({ r }: { r: DeclinedReachedRow }) {
  const pts = (r.series ?? []).map((p) => ({ t: new Date(p.t).getTime(), v: p.v }));
  if (pts.length < 2) return <div className="text-xs text-[var(--muted)] py-2">No plant data around this decline.</div>;
  const W = 760, H = 190, L = 56, R = 744, T = 14, B = 160;
  const t0 = new Date(r.decided_at).getTime();
  const tMin = Math.min(pts[0].t, t0), tMax = Math.max(pts[pts.length - 1].t, t0 + 60000);
  const lo = r.recommended_value - r.tolerance, hi = r.recommended_value + r.tolerance;
  const vs = pts.map((p) => p.v).concat([lo, hi, r.current_value ?? r.recommended_value]);
  const pad = (Math.max(...vs) - Math.min(...vs)) * 0.12 || 1;
  const vMin = Math.min(...vs) - pad, vMax = Math.max(...vs) + pad;
  const x = (t: number) => L + ((t - tMin) / (tMax - tMin)) * (R - L);
  const y = (v: number) => B - ((v - vMin) / (vMax - vMin)) * (B - T);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  const reachedT = r.reached_at ? new Date(r.reached_at).getTime() : null;
  const dp = DP[r.tag] ?? 1;
  const ticks = [0, 1, 2, 3, 4].map((k) => tMin + (k * (tMax - tMin)) / 4);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
      aria-label={`${r.param_name} actual run against the recommended value`}>
      {/* recommended zone ±tolerance */}
      <rect x={L} y={y(hi)} width={R - L} height={y(lo) - y(hi)} fill="rgba(52,211,153,.13)" />
      <line x1={L} x2={R} y1={y(r.recommended_value)} y2={y(r.recommended_value)} stroke="#34d399" strokeDasharray="5 4" />
      <text x={R - 4} y={y(hi) - 4} textAnchor="end" fontSize="11" fill="#34d399">
        recommended {r.recommended_value.toFixed(dp)} ±{r.tolerance.toFixed(dp)} {r.unit}
      </text>
      {/* y axis */}
      {[vMin + pad, (vMin + vMax) / 2, vMax - pad].map((v) => (
        <g key={v}>
          <line x1={L} x2={R} y1={y(v)} y2={y(v)} stroke="#1f2a37" />
          <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="10.5" fill="#7b8794">{v.toFixed(dp)}</text>
        </g>
      ))}
      {/* decline moment */}
      <line x1={x(t0)} x2={x(t0)} y1={T} y2={B} stroke="#f87171" strokeWidth="1.5" />
      <text x={x(t0) + 4} y={T + 10} fontSize="11" fill="#f87171">declined {hhmm(r.decided_at)}</text>
      {/* actual run */}
      <path d={d} fill="none" stroke="#cbd5e1" strokeWidth="2" strokeLinejoin="round" />
      {reachedT !== null && r.reached_value !== undefined && (
        <g>
          <circle cx={x(reachedT)} cy={y(r.reached_value)} r="5" fill="#34d399" stroke="#0b1117" strokeWidth="2" />
          <text x={x(reachedT) + 8} y={y(r.reached_value) + 16} fontSize="11" fill="#34d399">
            plant reached {r.reached_value.toFixed(dp)} at {hhmm(r.reached_at)} (+{r.minutes_after} min)
          </text>
        </g>
      )}
      {ticks.map((t) => (
        <text key={t} x={x(t)} y={H - 10} textAnchor="middle" fontSize="10.5" fill="#7b8794">
          {hhmm(new Date(t - new Date(t).getTimezoneOffset() * 60000).toISOString())}
        </text>
      ))}
    </svg>
  );
}

function Table({ rows, reached = false }: { rows: DeclinedReachedRow[]; reached?: boolean }) {
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const isOpen = (id: number) => open[id] ?? reached;      // reached-anyway rows open by default
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)] border-b border-[var(--line)]">
            <th className="py-2 pr-3">Declined at</th>
            <th className="pr-3">Parameter</th>
            <th className="pr-3 text-right">At decline</th>
            <th className="pr-3 text-right">Recommended</th>
            <th className="pr-3 text-right">{reached ? "Plant reached" : "Closest actual"}</th>
            <th className="pr-3">{reached ? "When" : "Status"}</th>
            <th className="pr-3">Decline reason</th>
            <th className="pr-3">Note</th>
            <th className="pr-3">Operator</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <Fragment key={r.id}>
            <tr className="border-b border-[var(--line)] align-top">
              <td className="py-2 pr-3 mono">{hhmm(r.decided_at)}</td>
              <td className="pr-3">{r.param_name || r.tag}<div className="text-[11px] text-[var(--muted)]">{r.operating_state || ""}</div></td>
              <td className="pr-3 mono text-right">{num(r.current_value, r.tag)} {r.unit}</td>
              <td className="pr-3 mono text-right">{num(r.recommended_value, r.tag)} {r.unit}
                <div className="text-[11px] text-[var(--muted)]">± {num(r.tolerance, r.tag)}</div></td>
              <td className="pr-3 mono text-right">
                {reached ? `${num(r.reached_value, r.tag)} ${r.unit ?? ""}` : `${num(r.closest_value, r.tag)} ${r.closest_value != null ? r.unit ?? "" : ""}`}
              </td>
              <td className="pr-3">
                {reached ? <span className="mono">{hhmm(r.reached_at)} <span className="text-[var(--muted)]">(+{r.minutes_after} min)</span></span>
                         : <Badge tone={STATUS[r.status].tone}>{STATUS[r.status].label}</Badge>}
              </td>
              <td className="pr-3">{r.reason || "—"}</td>
              <td className="pr-3 text-[var(--muted)]">{r.note || "—"}</td>
              <td className="pr-3">{r.operator || "—"}</td>
              <td>
                <button onClick={() => setOpen({ ...open, [r.id]: !isOpen(r.id) })}
                  className="text-xs text-[var(--accent)] hover:underline whitespace-nowrap">
                  {isOpen(r.id) ? "Hide run" : "Show run"}
                </button>
              </td>
            </tr>
            {isOpen(r.id) && (
              <tr className="border-b border-[var(--line)]">
                <td colSpan={10} className="py-2">
                  {/* pinned to the visible width of the scrolling table */}
                  <div className="sticky left-0" style={{ width: "min(820px, calc(100vw - 21rem))" }}>
                    <ProofChart r={r} />
                  </div>
                </td>
              </tr>
            )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
