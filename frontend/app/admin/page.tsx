"use client";
import { Fragment, useEffect, useState } from "react";
import { api, DeclinedReachedResult, DeclinedReachedRow } from "@/lib/api";
import { Panel, Stat, Badge, Loading } from "@/components/ui";

// Administrative dashboard — day-wise check of operator DECLINES.
// A recommendation made at time R is for R + horizon (T+10). At that target time
// the plant's single reading is compared with the recommended value: within the
// parameter's tolerance (coal ±2.5 t/h, SAFR ±1.5 t/h, PC temp ±5 °C) means the
// plant ended up where the advice said — the decline was not correct.

const DP: Record<string, number> = { "467RL400_TPH": 2, "261WF080_TPH": 2, "423CI400_TT_4": 1 };
const SHORT: Record<string, string> = { "467RL400_TPH": "coal", "261WF080_TPH": "SAFR", "423CI400_TT_4": "PC temp" };
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const hhmm = (ts?: string | null) => (ts ? String(ts).slice(11, 16) : "—");
const num = (v: number | null | undefined, tag: string) =>
  v === null || v === undefined ? "—" : v.toFixed(DP[tag] ?? 1);

const STATUS: Record<DeclinedReachedRow["status"], { tone: string; label: string }> = {
  reached: { tone: "warn", label: "Decline not correct" },
  not_reached: { tone: "ok", label: "Decline justified" },
  pending: { tone: "muted", label: "Waiting for target time" },
  no_data: { tone: "muted", label: "No plant reading" },
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
    const t = day === today() ? setInterval(load, 60000) : undefined;   // today: target times still passing
    return () => { alive = false; if (t) clearInterval(t); };
  }, [day]);

  const wrong = data?.rows.filter((r) => r.status === "reached") ?? [];
  const others = data?.rows.filter((r) => r.status !== "reached") ?? [];
  const tolText = data ? Object.entries(data.tolerances)
    .map(([t, v]) => `${SHORT[t] ?? t} ±${v}`).join(" · ") : "";
  const checked = data ? data.counts.reached + data.counts.not_reached : 0;

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
            <Stat label="Declined recommendations" value={data.declines} sub={`${data.day} · coal, SAFR, PC temp`} />
            <Stat label="Decline not correct" value={data.counts.reached} tone={data.counts.reached ? "warn" : "ink"}
              sub={checked ? `${Math.round((data.counts.reached / checked) * 100)}% of checked declines` : "—"} />
            <Stat label="Decline justified" value={data.counts.not_reached} tone={data.counts.not_reached ? "ok" : "ink"}
              sub="plant was not at the recommended value" />
            <Stat label="Waiting / no reading" value={data.counts.pending + data.counts.no_data}
              sub={`target time = advice + ${data.horizon_min} min`} />
          </div>

          <Panel title="Declines that were not correct"
            right={<span className="text-xs text-[var(--muted)]">plant reading at the target time (advice + {data.horizon_min} min) within {tolText}</span>}>
            {wrong.length === 0 ? (
              <div className="text-sm text-[var(--muted)]">
                {data.declines === 0 ? "No declined recommendations on this day."
                  : data.counts.pending > 0 && checked === 0
                  ? "None yet — waiting for the target time of the declines below."
                  : "None — at every target time the plant was not at the recommended value."}
              </div>
            ) : (
              <Table rows={wrong} wrong />
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

// Evidence chart for one decline: the parameter's actual run (30-s means) from
// 10 min before the advice to 10 min after its target time, the recommended
// value ± tolerance, the advice / decline / target moments, and the single
// reading at the target time that decides the verdict.
function ProofChart({ r }: { r: DeclinedReachedRow }) {
  const pts = (r.series ?? []).map((p) => ({ t: new Date(p.t).getTime(), v: p.v }));
  if (pts.length < 2) return <div className="text-xs text-[var(--muted)] py-2">No plant data around this decline.</div>;
  const W = 760, H = 200, L = 56, R = 744, T = 26, B = 168;
  const tRec = new Date(r.recommended_at).getTime(), tDec = new Date(r.decided_at).getTime();
  const tTar = new Date(r.target_at).getTime();
  const tMin = Math.min(pts[0].t, tRec), tMax = Math.max(pts[pts.length - 1].t, tTar + 60000);
  const lo = r.recommended_value - r.tolerance, hi = r.recommended_value + r.tolerance;
  const vs = pts.map((p) => p.v).concat([lo, hi]);
  const pad = (Math.max(...vs) - Math.min(...vs)) * 0.1 || 1;
  const vMin = Math.min(...vs) - pad, vMax = Math.max(...vs) + pad;
  const x = (t: number) => L + ((t - tMin) / (tMax - tMin)) * (R - L);
  const y = (v: number) => B - ((v - vMin) / (vMax - vMin)) * (B - T);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  const dp = DP[r.tag] ?? 1;
  const ticks = [0, 1, 2, 3, 4].map((k) => tMin + (k * (tMax - tMin)) / 4);
  const verdictColor = r.status === "reached" ? "#fbbf24" : "#34d399";
  const marker = (t: number, color: string, label: string, row: number, dash?: string) => (
    <g>
      <line x1={x(t)} x2={x(t)} y1={T} y2={B} stroke={color} strokeWidth="1.5" strokeDasharray={dash} />
      <text x={x(t) + 4} y={T - 14 + row * 11} fontSize="10.5" fill={color}>{label}</text>
    </g>
  );
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
      aria-label={`${r.param_name} actual run against the recommended value`}>
      <rect x={L} y={y(hi)} width={R - L} height={y(lo) - y(hi)} fill="rgba(52,211,153,.13)" />
      <line x1={L} x2={R} y1={y(r.recommended_value)} y2={y(r.recommended_value)} stroke="#34d399" strokeDasharray="5 4" />
      <text x={R - 4} y={y(lo) + 13} textAnchor="end" fontSize="11" fill="#34d399">
        recommended {r.recommended_value.toFixed(dp)} ±{r.tolerance} {r.unit}
      </text>
      {[vMin + pad, (vMin + vMax) / 2, vMax - pad].map((v) => (
        <g key={v}>
          <line x1={L} x2={R} y1={y(v)} y2={y(v)} stroke="#1f2a37" />
          <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="10.5" fill="#7b8794">{v.toFixed(dp)}</text>
        </g>
      ))}
      {marker(tRec, "#94a3b8", `advice ${hhmm(r.recommended_at)}`, 0, "3 3")}
      {marker(tDec, "#f87171", `declined ${hhmm(r.decided_at)}`, 1)}
      {marker(tTar, verdictColor, `target ${hhmm(r.target_at)} (T+${r.horizon_min})`, 0, "6 3")}
      <path d={d} fill="none" stroke="#cbd5e1" strokeWidth="1.8" strokeLinejoin="round" />
      {r.actual_at && r.actual_value !== undefined && (
        <g>
          <circle cx={x(new Date(r.actual_at).getTime())} cy={y(r.actual_value)} r="5.5"
            fill={verdictColor} stroke="#0b1117" strokeWidth="2" />
          <text x={x(new Date(r.actual_at).getTime()) + 9} y={y(r.actual_value) + 4} fontSize="11" fill={verdictColor}>
            {r.actual_value.toFixed(dp)} at {hhmm(r.actual_at)}
          </text>
        </g>
      )}
      {ticks.map((t) => (
        <text key={t} x={x(t)} y={H - 8} textAnchor="middle" fontSize="10.5" fill="#7b8794">
          {hhmm(new Date(t - new Date(t).getTimezoneOffset() * 60000).toISOString())}
        </text>
      ))}
    </svg>
  );
}

function Table({ rows, wrong = false }: { rows: DeclinedReachedRow[]; wrong?: boolean }) {
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const isOpen = (id: number) => open[id] ?? wrong;        // "not correct" rows open by default
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)] border-b border-[var(--line)]">
            <th className="py-2 pr-3">Advice · declined</th>
            <th className="pr-3">Parameter</th>
            <th className="pr-3 text-right">At decline</th>
            <th className="pr-3 text-right">Recommended</th>
            <th className="pr-3 text-right">Plant at target time</th>
            <th className="pr-3">Verdict</th>
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
              <td className="py-2 pr-3 mono whitespace-nowrap">{hhmm(r.recommended_at)}
                <div className="text-[11px] text-[var(--muted)]">declined {hhmm(r.decided_at)}</div></td>
              <td className="pr-3">{r.param_name || r.tag}<div className="text-[11px] text-[var(--muted)]">{r.operating_state || ""}</div></td>
              <td className="pr-3 mono text-right whitespace-nowrap">{num(r.current_value, r.tag)} {r.unit}</td>
              <td className="pr-3 mono text-right whitespace-nowrap">{num(r.recommended_value, r.tag)} {r.unit}
                <div className="text-[11px] text-[var(--muted)]">± {r.tolerance} · for {hhmm(r.target_at)}</div></td>
              <td className="pr-3 mono text-right whitespace-nowrap">
                {r.actual_value !== undefined ? `${num(r.actual_value, r.tag)} ${r.unit ?? ""}` : "—"}
                {r.diff !== undefined && <div className="text-[11px] text-[var(--muted)]">
                  {r.diff >= 0 ? "+" : ""}{num(r.diff, r.tag)} vs advice · {hhmm(r.actual_at)}</div>}
              </td>
              <td className="pr-3"><Badge tone={STATUS[r.status].tone}>{STATUS[r.status].label}</Badge></td>
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
