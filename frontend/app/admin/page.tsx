"use client";
import { useEffect, useState } from "react";
import { api, AccuracyResult, AcceptedReachedResult, DeclinedReachedResult, DeclinedReachedRow, SummaryRow } from "@/lib/api";
import { Panel, Stat, Badge, Loading } from "@/components/ui";

// Administrative dashboard — day-wise check of operator DECLINES and ACCEPTS.
// A recommendation made at time R is for R + horizon (T+10). At that target time
// the plant's single reading is compared with the recommended value: within the
// parameter's tolerance (coal ±2.5 t/h, SAFR ±1.5 t/h, PC temp ±5 °C) means the
// plant ended up where the advice said — the decline was not correct, or the
// accept was carried through.

// T+10 avg error colour limit (°C): good up to GOOD_C, red above
const GOOD_C = 10;
const DP: Record<string, number> = { "467RL400_TPH": 2, "261WF080_TPH": 2, "423CI400_TT_4": 1 };
const SHORT: Record<string, string> = { "467RL400_TPH": "coal", "261WF080_TPH": "SAFR", "423CI400_TT_4": "PC temp" };
const isoDay = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const today = () => isoDay(new Date());
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dayLabel = (s: string) => `${Number(s.slice(8, 10))} ${MONTHS[Number(s.slice(5, 7)) - 1]} ${s.slice(0, 4)}`
  + (s === today() ? " · today" : "");
const hhmm = (ts?: string | null) => (ts ? String(ts).slice(11, 16) : "—");
const num = (v: number | null | undefined, tag: string) =>
  v === null || v === undefined ? "—" : v.toFixed(DP[tag] ?? 1);

type Verdicts = Record<DeclinedReachedRow["status"], { tone: string; label: string }>;
const STATUS: Record<DeclinedReachedRow["action"], Verdicts> = {
  declined: {
    reached: { tone: "warn", label: "Decline not correct" },
    not_reached: { tone: "ok", label: "Decline justified" },
    pending: { tone: "muted", label: "Waiting for target time" },
    no_data: { tone: "muted", label: "No plant reading" },
  },
  accepted: {
    reached: { tone: "ok", label: "Reached advice" },
    not_reached: { tone: "warn", label: "Not reached" },
    pending: { tone: "muted", label: "Waiting for target time" },
    no_data: { tone: "muted", label: "No plant reading" },
  },
};

export default function Admin() {
  const [day, setDay] = useState(today());
  const [data, setData] = useState<DeclinedReachedResult | null>(null);
  const [accData, setAccData] = useState<AcceptedReachedResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => Promise.all([api.declinedReached(day), api.acceptedReached(day)])
      .then(([d, a]) => { if (alive) { setData(d); setAccData(a); setErr(null); } })
      .catch((e) => { if (alive) setErr(String(e)); });
    setData(null); setAccData(null); load();
    const t = day === today() ? setInterval(load, 60000) : undefined;   // today: target times still passing
    return () => { alive = false; if (t) clearInterval(t); };
  }, [day]);

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

      <SummaryPanel day={day} onPick={setDay} />

      {err && <Panel><div className="text-sm text-[var(--bad)]">Could not load: {err}</div></Panel>}
      {!data && !err && <Loading />}

      {data && (
        <>
          <Panel title="Declined Decision Report"
            right={<span className="text-xs text-[var(--muted)]">Plant reading at the target time within {tolText}</span>}>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
              <Stat label="Declined recommendations" value={data.declines} sub={`${data.day} · coal, SAFR, PC temp`} />
              <Stat label="Decline not correct" value={data.counts.reached} tone={data.counts.reached ? "warn" : "ink"}
                sub={checked ? `${Math.round((data.counts.reached / checked) * 100)}% of checked declines` : "—"} />
              <Stat label="Decline justified" value={data.counts.not_reached} tone={data.counts.not_reached ? "ok" : "ink"}
                sub="plant was not at the recommended value" />
              <Stat label="Waiting / no reading" value={data.counts.pending + data.counts.no_data}
                sub={`target time = advice + ${data.horizon_min} min`} />
            </div>
            {data.rows.length === 0
              ? <div className="text-sm text-[var(--muted)]">No declined recommendations on this day.</div>
              : <Table rows={data.rows} />}
          </Panel>
        </>
      )}

      {data && accData && <PerformancePanel dec={data} acc={accData} />}

      <AccuracyPanel day={day} />
    </div>
  );
}

// Section 1: recommendations per day for the last 7 days (+ total), per
// parameter (coal, SAFR, calciner temp), at most once per parameter per 10 min
// (max 432 a day). One Accept all click counts for each parameter; no action
// = shown but never decided. Click a row to open that day below.
function SummaryPanel({ day, onPick }: { day: string; onPick: (d: string) => void }) {
  const [rows, setRows] = useState<SummaryRow[] | null>(null);
  useEffect(() => {
    const load = () => api.summary(7).then(setRows).catch(() => setRows([]));
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, []);
  const tot = (rows ?? []).reduce((a, r) => ({ total: a.total + r.total, accepted: a.accepted + r.accepted,
    declined: a.declined + r.declined, no_action: a.no_action + r.no_action }),
    { total: 0, accepted: 0, declined: 0, no_action: 0 });
  return (
    <Panel title="Recommendation Summary"
      right={<span className="text-xs text-[var(--muted)]">last 7 days · per parameter · max 1 per parameter per 10 min (432 a day) · click a day to open it below</span>}>
      {!rows ? <Loading /> : rows.length === 0 ? (
        <div className="text-sm text-[var(--muted)]">No recommendations in the last 7 days.</div>
      ) : (
        <table className="w-full text-sm table-fixed">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-[var(--muted)] border-b border-[var(--line)]">
              <th className="py-2 text-left font-medium">Date</th>
              <th className="font-medium text-center">Total recommendation</th>
              <th className="font-medium text-center">Accepted</th>
              <th className="font-medium text-center">Declined</th>
              <th className="font-medium text-center">No action</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.day} onClick={() => onPick(r.day)}
                className={`cursor-pointer hover:bg-[var(--panel-2)] ${r.day === day ? "bg-[var(--panel-2)]" : ""}`}>
                <td className="py-1.5">{dayLabel(r.day)}</td>
                <td className="text-center mono">{r.total}</td>
                <td className={`text-center mono ${r.accepted ? "text-[var(--ok)]" : "text-[var(--muted)]"}`}>{r.accepted}</td>
                <td className={`text-center mono ${r.declined ? "text-[var(--bad)]" : "text-[var(--muted)]"}`}>{r.declined}</td>
                <td className="text-center mono text-[var(--muted)]">{r.no_action}</td>
              </tr>
            ))}
            <tr className="border-t border-[var(--line)] font-semibold">
              <td className="py-2">Total</td>
              <td className="text-center mono">{tot.total}</td>
              <td className="text-center mono text-[var(--ok)]">{tot.accepted}</td>
              <td className="text-center mono text-[var(--bad)]">{tot.declined}</td>
              <td className="text-center mono text-[var(--muted)]">{tot.no_action}</td>
            </tr>
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// Section 3: advisory performance on the chosen day, counted per parameter
// decision. Positive = accepted + declined-but-not-correct (the advice was right);
// negative = declined and justified. Success % = positive / (positive + negative);
// declines still waiting for their target time count in neither.
// "Last 7 days total" sums the same per-day checks over today and the 6 days before.
type Perf = { accepted: number; declines: number; notCorrect: number; justified: number };
const perfOf = (dec: DeclinedReachedResult, acc: AcceptedReachedResult): Perf => ({
  accepted: acc.accepts, declines: dec.declines,
  notCorrect: dec.counts.reached, justified: dec.counts.not_reached,
});
const lastDays = (n: number) => Array.from({ length: n }, (_, i) => {
  const d = new Date(); d.setDate(d.getDate() - i);
  return isoDay(d);
});

function PerformancePanel({ dec, acc }: { dec: DeclinedReachedResult; acc: AcceptedReachedResult }) {
  const [mode, setMode] = useState<"day" | "week">("day");
  const [week, setWeek] = useState<Perf | null>(null);
  useEffect(() => {
    if (mode !== "week") return;
    let alive = true;
    setWeek(null);
    Promise.all(lastDays(7).map((d) => Promise.all([api.declinedReached(d), api.acceptedReached(d)])))
      .then((res) => {
        if (!alive) return;
        setWeek(res.map(([d, a]) => perfOf(d, a)).reduce((s, p) => ({
          accepted: s.accepted + p.accepted, declines: s.declines + p.declines,
          notCorrect: s.notCorrect + p.notCorrect, justified: s.justified + p.justified,
        })));
      })
      .catch(() => { if (alive) setWeek({ accepted: 0, declines: 0, notCorrect: 0, justified: 0 }); });
    return () => { alive = false; };
  }, [mode]);

  const p = mode === "day" ? perfOf(dec, acc) : week;
  const tabs = (
    <div className="flex items-center gap-3">
      <div className="flex rounded-md border border-[var(--line)] overflow-hidden text-xs">
        {([["day", dec.day], ["week", "Last 7 days total"]] as const).map(([m, label]) => (
          <button key={m} onClick={() => setMode(m)}
            className={`px-2.5 py-1 ${mode === m ? "bg-[var(--panel-2)] text-[var(--ink)] font-semibold" : "text-[var(--muted)]"}`}>
            {label}
          </button>
        ))}
      </div>
      <span className="text-xs text-[var(--muted)]">per parameter decision</span>
    </div>
  );
  if (!p) return <Panel title="Advisory Performance" right={tabs}><Loading /></Panel>;

  const { accepted, notCorrect, justified } = p;
  const total = accepted + p.declines;
  const pos = accepted + notCorrect, neg = justified;
  const success = pos + neg ? Math.round((pos / (pos + neg)) * 100) : null;
  return (
    <Panel title="Advisory Performance" right={tabs}>
      {total === 0 ? (
        <div className="text-sm text-[var(--muted)]">
          No accepted or declined recommendations {mode === "day" ? "on this day" : "in the last 7 days"}.
        </div>
      ) : (
        <div className="grid lg:grid-cols-[1fr_260px] gap-4 items-center">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="Total actions performed" value={total} />
            <Stat label="Accepted" value={accepted} tone={accepted ? "ok" : "ink"} />
            <Stat label="Declined · not correct" value={notCorrect} tone={notCorrect ? "warn" : "ink"} />
            <Stat label="Declined · justified" value={justified} />
            <Stat label="Positive outcomes" value={pos} tone="ok" sub="accepted + declined not correct" />
            <Stat label="Negative outcomes" value={neg} tone="bad" sub="declined justified" />
            <Stat label="Success" value={success ?? "—"} unit={success === null ? undefined : "%"}
              tone={success === null ? "ink" : success >= 50 ? "ok" : "bad"} sub="positive ÷ (positive + negative)" />
          </div>
          <OutcomePie pos={pos} neg={neg} />
        </div>
      )}
    </Panel>
  );
}

function OutcomePie({ pos, neg }: { pos: number; neg: number }) {
  const n = pos + neg;
  if (!n) return <div className="text-sm text-[var(--muted)] text-center">No checked outcomes yet.</div>;
  const R = 80, C = 100;
  const arc = (from: number, to: number) => {
    const p = (f: number) => [C + R * Math.sin(2 * Math.PI * f), C - R * Math.cos(2 * Math.PI * f)];
    const [x0, y0] = p(from), [x1, y1] = p(to);
    return `M${C} ${C} L${x0.toFixed(2)} ${y0.toFixed(2)} A${R} ${R} 0 ${to - from > 0.5 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)} Z`;
  };
  const f = pos / n;
  return (
    <div className="flex flex-col items-center gap-2">
      <svg viewBox="0 0 200 200" className="w-48" role="img" aria-label={`Positive ${pos}, negative ${neg}`}>
        {f >= 1 ? <circle cx={C} cy={C} r={R} fill="#34d399" />
          : f <= 0 ? <circle cx={C} cy={C} r={R} fill="#f87171" />
          : <>
              <path d={arc(0, f)} fill="#34d399" stroke="#0b1117" strokeWidth="2" />
              <path d={arc(f, 1)} fill="#f87171" stroke="#0b1117" strokeWidth="2" />
            </>}
        <circle cx={C} cy={C} r={44} fill="var(--panel)" />
        <text x={C} y={C + 2} textAnchor="middle" fontSize="22" fontWeight="700" fill="#e2e8f0">{Math.round(f * 100)}%</text>
        <text x={C} y={C + 20} textAnchor="middle" fontSize="11" fill="#94a3b8">positive</text>
      </svg>
      <div className="flex gap-4 text-xs">
        <span className="flex items-center gap-1.5"><i className="inline-block w-2.5 h-2.5 rounded-sm bg-[#34d399]" />Positive {pos}</span>
        <span className="flex items-center gap-1.5"><i className="inline-block w-2.5 h-2.5 rounded-sm bg-[#f87171]" />Negative {neg}</span>
      </div>
    </div>
  );
}

// P8: how good the twin's T+10 forecast (the advisor's horizon) has been, day by
// day, from predictions checked against the actual temperature 10 min later.
function AccuracyPanel({ day }: { day: string }) {
  const [acc, setAcc] = useState<AccuracyResult | null>(null);
  useEffect(() => {
    const load = () => api.accuracy(7).then(setAcc).catch(() => setAcc(null));
    load();
    const t = setInterval(load, 300000);
    return () => clearInterval(t);
  }, []);
  const tone = (mae?: number) => (mae === undefined ? "" : mae <= GOOD_C ? "text-[var(--ok)]" : "text-[var(--bad)]");
  return (
    <Panel title="Model accuracy — T+10 forecast (last 7 days)">
      {!acc ? <Loading /> : !acc.available || acc.days.length === 0 ? (
        <div className="text-sm text-[var(--muted)]">{acc.note || "No checked predictions yet — the shadow runner records them every 30 s; the first T+10 checks appear 10 min after it starts."}</div>
      ) : (
        <div className="grid lg:grid-cols-[minmax(240px,320px)_1fr] gap-4 items-start">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)] border-b border-[var(--line)]">
                <th className="py-2 pr-3">Day</th>
                <th className="pr-3 text-right">T+10 avg error</th>
                <th className="text-right">checks</th>
              </tr>
            </thead>
            <tbody>
              {acc.days.map((d) => (
                <tr key={d.day} className={`border-b border-[var(--line)] ${d.day === day ? "bg-[var(--panel-2)]" : ""}`}>
                  <td className="py-2 pr-3 mono">{d.day}</td>
                  <td className={`pr-3 mono text-right ${tone(d.t10?.mae)}`}>{d.t10 ? `${d.t10.mae.toFixed(1)} °C` : "—"}</td>
                  <td className="mono text-right">{d.t10?.n ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div>
            <div className="text-xs text-[var(--muted)] text-center mb-1">7-day accuracy trend</div>
            <AccuracyChart days={acc.days} />
          </div>
        </div>
      )}
    </Panel>
  );
}

// T+10 avg error by day (oldest left), with the good-error line.
function AccuracyChart({ days }: { days: AccuracyResult["days"] }) {
  const pts = days.filter((d) => d.t10).map((d) => ({ day: d.day, v: d.t10!.mae })).reverse();
  if (pts.length === 0) return null;
  const W = 680, H = 240, L = 62, R = 610, T = 18, B = 200;
  const PAD = 28;                                       // keep the first / last point off the axes
  const vMax = Math.ceil(Math.max(GOOD_C + 5, ...pts.map((p) => p.v)) * 1.15 / 5) * 5;
  const x = (i: number) => (pts.length === 1 ? (L + R) / 2 : L + PAD + (i / (pts.length - 1)) * (R - L - 2 * PAD));
  const y = (v: number) => B - (v / vMax) * (B - T);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" ");
  const col = (v: number) => (v <= GOOD_C ? "#34d399" : "#f87171");
  const step = vMax > 30 ? 10 : 5;
  const grid = Array.from({ length: Math.floor(vMax / step) + 1 }, (_, k) => k * step);
  const short = (s: string) => `${Number(s.slice(8, 10))} ${MONTHS[Number(s.slice(5, 7)) - 1]}`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="T+10 average error by day">
      {/* zones: good up to GOOD_C · poor above */}
      <rect x={L} y={y(GOOD_C)} width={R - L} height={B - y(GOOD_C)} fill="#34d399" opacity=".07" />
      <rect x={L} y={T} width={R - L} height={y(GOOD_C) - T} fill="#f87171" opacity=".05" />
      {grid.map((v) => (
        <g key={v}>
          <line x1={L} x2={R} y1={y(v)} y2={y(v)} stroke="#243244" />
          <text x={L - 8} y={y(v) + 4} textAnchor="end" fontSize="12" fill="#94a3b8">{v}</text>
        </g>
      ))}
      {[GOOD_C].map((v) => (
        <g key={v}>
          <line x1={L} x2={R} y1={y(v)} y2={y(v)} stroke={col(v)} strokeDasharray="5 4" opacity=".8" />
          <text x={R + 6} y={y(v) + 4} fontSize="11.5" fill={col(v)}>{v} °C good</text>
        </g>
      ))}
      <line x1={L} x2={L} y1={T} y2={B} stroke="#334155" />
      <line x1={L} x2={R} y1={B} y2={B} stroke="#334155" />
      <text transform={`translate(16 ${(T + B) / 2}) rotate(-90)`} textAnchor="middle" fontSize="12" fill="#94a3b8">
        Avg error (°C)
      </text>
      <path d={d} fill="none" stroke="#38bdf8" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
      {pts.map((p, i) => (
        <g key={p.day}>
          <circle cx={x(i)} cy={y(p.v)} r="5.5" fill={col(p.v)} stroke="#0b1117" strokeWidth="2" />
          <text x={x(i)} y={y(p.v) - 11} textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#e2e8f0">{p.v.toFixed(1)}</text>
          <text x={x(i)} y={B + 20} textAnchor="middle" fontSize="12" fill="#94a3b8">{short(p.day)}</text>
        </g>
      ))}
    </svg>
  );
}

function Table({ rows }: { rows: DeclinedReachedRow[] }) {
  const verb = rows[0]?.action === "accepted" ? "accept" : "decline";
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)] border-b border-[var(--line)]">
            <th className="py-2 pr-3">Time</th>
            <th className="pr-3">Parameter</th>
            <th className="pr-3 text-right">At {verb}</th>
            <th className="pr-3 text-right">Recommended</th>
            <th className="pr-3 text-right">Plant at target time</th>
            <th className="pr-3">{verb === "decline" ? "Decline reason" : "Reason"}</th>
            <th className="pr-3">Verdict</th>
            <th className="pr-3">Operator</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-[var(--line)] align-top">
              <td className="py-2 pr-3 mono whitespace-nowrap">{hhmm(r.recommended_at)}
                <div className="text-[11px] text-[var(--muted)]">{r.action} {hhmm(r.decided_at)}</div></td>
              <td className="pr-3">{r.param_name || r.tag}<div className="text-[11px] text-[var(--muted)]">{r.operating_state || ""}</div></td>
              <td className="pr-3 mono text-right whitespace-nowrap">{num(r.current_value, r.tag)} {r.unit}</td>
              <td className="pr-3 mono text-right whitespace-nowrap">{num(r.recommended_value, r.tag)} {r.unit}
                <div className="text-[11px] text-[var(--muted)]">± {r.tolerance} · for {hhmm(r.target_at)}</div></td>
              <td className="pr-3 mono text-right whitespace-nowrap">
                {r.actual_value !== undefined ? `${num(r.actual_value, r.tag)} ${r.unit ?? ""}` : "—"}
                {r.diff !== undefined && <div className="text-[11px] text-[var(--muted)]">
                  {r.diff >= 0 ? "+" : ""}{num(r.diff, r.tag)} vs advice · {hhmm(r.actual_at)}</div>}
              </td>
              <td className="pr-3">{r.reason || "—"}</td>
              <td className="pr-3 whitespace-nowrap"><Badge tone={STATUS[r.action][r.status].tone}>{STATUS[r.action][r.status].label}</Badge></td>
              <td className="pr-3">{r.operator || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
