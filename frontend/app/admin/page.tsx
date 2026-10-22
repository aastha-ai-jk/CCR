"use client";
import { useEffect, useState } from "react";
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
          <div className="text-sm text-[var(--muted)]">Kiln-3 digital twin · administration</div>
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
            right={<span className="text-xs text-[var(--muted)]">match = actual within ±{data.tolerance_pct}% of recommended, within {data.window_min} min of the decline</span>}>
            {reached.length === 0 ? (
              <div className="text-sm text-[var(--muted)]">
                {data.declines === 0 ? "No declined recommendations on this day." : "None — in every decline the plant did not reach the recommended value."}
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

function Table({ rows, reached = false }: { rows: DeclinedReachedRow[]; reached?: boolean }) {
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
            <th>Operator</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--line)] align-top">
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
              <td>{r.operator || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
