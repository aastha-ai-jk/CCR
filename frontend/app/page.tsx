"use client";
import { useEffect, useRef, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceArea, CartesianGrid } from "recharts";
import { api } from "@/lib/api";
import { Panel, Stat, Badge, Loading } from "@/components/ui";

const TILES: { tag: string; label: string; unit: string }[] = [
  { tag: "467RL400_TPH", label: "Calciner Coal", unit: "t/h" },
  { tag: "353RL130_TPH", label: "Kiln Feed", unit: "t/h" },
  { tag: "LAFR_FT", label: "LAFR (PC)", unit: "l/h" },
  { tag: "K3V10_F1", label: "LAFR (TAD)", unit: "l/h" },
  { tag: "261WF080_TPH", label: "SAFR", unit: "t/h" },
  { tag: "433MD140_TQ", label: "Kiln Torque", unit: "Nm" },
];

type Window = "hour" | "day" | "week" | "month";
const WINDOWS: { key: Window; label: string }[] = [
  { key: "hour", label: "Hour" },
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
];

// Y range that fits the data but is never narrower than minSpan, so a nearly
// flat signal (kiln feed steady at ~481 t/h) is drawn flat, not as big swings.
const spanDomain = (rows: any[], key: string, minSpan: number): [number, number] | ["auto", "auto"] => {
  const v = rows.map((r) => r[key]).filter((x): x is number => typeof x === "number");
  if (!v.length) return ["auto", "auto"];
  const lo = Math.min(...v), hi = Math.max(...v);
  if (hi - lo >= minSpan) return [Math.floor(lo), Math.ceil(hi)];
  const mid = (lo + hi) / 2;
  return [Math.floor(mid - minSpan / 2), Math.ceil(mid + minSpan / 2)];
};

// Trend range: Hour = 1/2/4/8 h; Day/Week/Month spans and point spacing as the
// backend's window spec. The range ends at the latest plant sample ("live") or
// at a chosen past time, and every chart below uses the same range.
const TREND_TAGS = ["423CI400_TT_4", "467RL400_TPH", "353RL130_TPH"];
const HOURS = [1, 2, 4, 8] as const;
type Hours = (typeof HOURS)[number];
const SPAN_MIN: Record<Window, number> = { hour: 60, day: 1440, week: 10080, month: 43200 };
const BUCKET: Record<Window, string> = { hour: "1min", day: "5min", week: "30min", month: "3h" };
const pad = (n: number) => String(n).padStart(2, "0");
// Plant timestamps carry no time zone; parse and format them as plain local time.
const parseTs = (t: string) => new Date(t.slice(0, 16).replace(" ", "T"));
const fmtTs = (d: Date) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
const toInput = (d: Date) => fmtTs(d).slice(0, 16).replace(" ", "T");   // datetime-local value
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const label = (d: Date) => `${d.getDate()} ${MON[d.getMonth()]} ${pad(d.getHours())}:${pad(d.getMinutes())}`;

export default function Overview() {
  const [ov, setOv] = useState<any>(null);
  const [trend, setTrend] = useState<any[]>([]);
  const [win, setWin] = useState<Window>("week");
  const [hrs, setHrs] = useState<Hours>(1);
  const [endAt, setEndAt] = useState<string | null>(null);   // null = live, else "YYYY-MM-DDTHH:MM"
  const latestRef = useRef<string | null>(null);              // latest plant sample time
  const [hasLatest, setHasLatest] = useState(false);
  const spanMin = win === "hour" ? hrs * 60 : SPAN_MIN[win];

  useEffect(() => {
    const load = () => api.overview().then((o: any) => {
      setOv(o);
      if (o?.snapshot?.ts) { latestRef.current = String(o.snapshot.ts); setHasLatest(true); }
    }).catch(console.error);
    load();
    const id = setInterval(load, 2500);
    return () => clearInterval(id);
  }, []);

  // Reload the temperature + calciner coal + kiln feed trend when the window,
  // the hour span or the end time changes. Live hour views refresh every minute.
  useEffect(() => {
    if (!hasLatest) return;
    let alive = true;
    setTrend([]);
    const load = () => {
      const end = parseTs(endAt ?? latestRef.current!);
      const start = new Date(end.getTime() - spanMin * 60_000);
      return api.series(TREND_TAGS, { start: fmtTs(start), end: fmtTs(end), bucket: BUCKET[win] })
        .then((r: any) => { if (alive) setTrend(r.points); }).catch(console.error);
    };
    load();
    const id = !endAt && win === "hour" ? setInterval(load, 60_000) : undefined;
    return () => { alive = false; if (id) clearInterval(id); };
  }, [win, spanMin, endAt, hasLatest]);

  if (!ov) return <Loading label="Connecting to Kiln-3 twin backend…" />;
  const src = ov.source;
  const feedBadge = src === "opc"
    ? { tone: "ok", text: `● LIVE · OPC · ${ov.age_sec ?? "?"}s ago` }
    : src === "edge" ? { tone: "ok", text: `● LIVE · via edge · ${ov.age_sec ?? "?"}s ago` }
    : src === "replay" ? { tone: "warn", text: "REPLAY" }
    : { tone: "muted", text: "historical" };
  const s = ov.snapshot;
  const temp = s["423CI400_TT_4"];
  const band = ov.target_band;
  const inBand = temp >= band.low && temp <= band.high;
  const allOk = ov.interlocks.every((i: any) => i.pass !== false);

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <span className="live-dot text-[var(--ok)]">●</span> Live Overview — Kiln-3 Precalciner
          </h1>
          <div className="text-sm text-[var(--muted)] mono">last sample {s.ts}</div>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={feedBadge.tone}>{feedBadge.text}</Badge>
          <Badge tone="accent">{ov.operating_state}</Badge>
          <Badge tone="muted">Target band {band.low}–{band.high} °C</Badge>
          <Badge tone={allOk ? "ok" : "bad"}>{allOk ? "Interlocks OK" : "Interlock breach"}</Badge>
        </div>
      </header>

      <div className="grid grid-cols-12 gap-4">
        <Panel title="Calciner Outlet Temperature — the controlled variable" className="col-span-12 lg:col-span-8"
          right={
            <div className="flex flex-col items-end gap-1">
              <div className="flex items-center gap-1 panel-2 p-0.5 rounded-md">
                {WINDOWS.map((w) => (
                  <button key={w.key} onClick={() => setWin(w.key)}
                    className={`px-2.5 py-1 text-xs rounded transition-colors ${
                      win === w.key
                        ? "bg-[var(--accent)] text-[#0b1220] font-semibold"
                        : "text-[var(--muted)] hover:text-[var(--ink)]"
                    }`}>
                    {w.label}
                  </button>
                ))}
              </div>
              {win === "hour" && (
                <div className="flex items-center gap-1 panel-2 p-0.5 rounded-md">
                  {HOURS.map((h) => (
                    <button key={h} onClick={() => setHrs(h)}
                      className={`px-2 py-0.5 text-[11px] rounded transition-colors ${
                        hrs === h
                          ? "bg-[var(--accent)] text-[#0b1220] font-semibold"
                          : "text-[var(--muted)] hover:text-[var(--ink)]"
                      }`}>
                      {h}h
                    </button>
                  ))}
                </div>
              )}
            </div>
          }>
          <TrendRange latest={latestRef.current} endAt={endAt} spanMin={spanMin} onChange={setEndAt} />
          <div className="flex items-end gap-6 mb-2">
            <div className="mono text-5xl font-bold glow" style={{ color: inBand ? "var(--ok)" : "var(--warn)" }}>
              {temp?.toFixed(0)}<span className="text-lg text-[var(--muted)] ml-1">°C</span>
            </div>
            <div className="text-sm text-[var(--muted)] pb-2">
              band {band.low}–{band.high} °C · {inBand ? "in spec" : "out of band"}
            </div>
          </div>
          <div style={{ height: 210 }}>
            <ResponsiveContainer>
              <LineChart data={trend} syncId="overview-trend" margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40}
                       tickFormatter={(v: string) => (win === "hour" ? v.slice(11) : v.slice(5))} />
                <YAxis domain={[820, 960]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                <ReferenceArea y1={band.low} y2={band.high} fill="#22c55e" fillOpacity={0.12} />
                <Line type="monotone" dataKey="423CI400_TT_4" stroke="#38bdf8" dot={false} strokeWidth={2} name="Calciner temp" isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          {/* Calciner coal feed, same window and timestamps as the temperature above. */}
          <div className="flex items-center justify-between mt-3 mb-1 text-xs">
            <span className="text-[var(--muted)] uppercase tracking-wide">Calciner coal feed · t/h</span>
            <span className="mono" style={{ color: "#f59e0b" }}>
              {s["467RL400_TPH"] != null ? `${Number(s["467RL400_TPH"]).toFixed(1)} t/h` : "—"}
            </span>
          </div>
          <div style={{ height: 130 }}>
            <ResponsiveContainer>
              <LineChart data={trend} syncId="overview-trend" margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40}
                       tickFormatter={(v: string) => (win === "hour" ? v.slice(11) : v.slice(5))} />
                <YAxis domain={["auto", "auto"]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }}
                         formatter={(v: any) => [typeof v === "number" ? `${v.toFixed(1)} t/h` : "—", "Calciner coal"]} />
                <Line type="monotone" dataKey="467RL400_TPH" stroke="#f59e0b" dot={false} strokeWidth={2} name="Calciner coal" isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          {/* Kiln feed, same window and timestamps; axis at least 20 t/h tall. */}
          <div className="flex items-center justify-between mt-3 mb-1 text-xs">
            <span className="text-[var(--muted)] uppercase tracking-wide">Kiln feed · t/h</span>
            <span className="mono" style={{ color: "#a78bfa" }}>
              {s["353RL130_TPH"] != null ? `${Number(s["353RL130_TPH"]).toFixed(1)} t/h` : "—"}
            </span>
          </div>
          <div style={{ height: 130 }}>
            <ResponsiveContainer>
              <LineChart data={trend} syncId="overview-trend" margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40}
                       tickFormatter={(v: string) => (win === "hour" ? v.slice(11) : v.slice(5))} />
                <YAxis domain={spanDomain(trend, "353RL130_TPH", 20)} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }}
                         formatter={(v: any) => [typeof v === "number" ? `${v.toFixed(1)} t/h` : "—", "Kiln feed"]} />
                <Line type="monotone" dataKey="353RL130_TPH" stroke="#a78bfa" dot={false} strokeWidth={2} name="Kiln feed" isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Panel>

        <Panel title="Combustion-safety interlocks" className="col-span-12 lg:col-span-4">
          <div className="space-y-2">
            {ov.interlocks.map((i: any) => (
              <div key={i.tag} className="flex items-center justify-between panel-2 px-3 py-2">
                <div className="text-xs pr-2">
                  <div className="text-[var(--ink)]">{i.name}</div>
                  <div className="text-[var(--muted)] mono text-[10px]">{i.tag} · limit {i.rule}</div>
                </div>
                <div className="text-right">
                  <div className="mono">{i.value?.toFixed(1) ?? "—"}</div>
                  <Badge tone={i.pass === false ? "bad" : i.pass ? "ok" : "muted"}>
                    {i.pass === false ? "BREACH" : i.pass ? "ok" : "n/a"}
                  </Badge>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {TILES.map((t) => (
          <Stat key={t.tag} label={t.label} unit={t.unit}
            value={s[t.tag] != null ? Number(s[t.tag]).toFixed(1) : "—"}
            tone={t.tag === "467RL400_TPH" ? "ok" : "ink"} />
        ))}
      </div>

      <Panel title="Operating-window state selection (from control logic)">
        <div className="text-sm text-[var(--muted)] mb-3">
          Target band is chosen dynamically by voting across quality &amp; kiln indicators. Current vote →
          <span className="text-[var(--accent)] font-semibold"> {ov.operating_state}</span>.
        </div>
        <div className="flex flex-wrap gap-2">
          {Object.entries(ov.state_votes).map(([k, v]: any) => (
            <div key={k} className="panel-2 px-3 py-2 text-xs">
              <span className="text-[var(--muted)]">{k}</span> → <span className="mono text-[var(--ink)]">State {v}</span>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

// Range bar above the trend: step back / forward by one window, jump to an end
// time, or return to live. The big temperature figure always stays live.
function TrendRange({ latest, endAt, spanMin, onChange }: {
  latest: string | null; endAt: string | null; spanMin: number; onChange: (v: string | null) => void;
}) {
  if (!latest) return null;
  const latestD = parseTs(latest);
  const end = endAt ? parseTs(endAt) : latestD;
  const start = new Date(end.getTime() - spanMin * 60_000);
  const set = (d: Date) => onChange(d.getTime() >= latestD.getTime() ? null : toInput(d));
  const btn = "px-2 py-0.5 text-xs rounded border border-[var(--line)] text-[var(--muted)] hover:text-[var(--ink)] disabled:opacity-40";
  return (
    <div className="flex flex-wrap items-center gap-2 mb-2 text-xs">
      <button className={btn} title="Back one window" onClick={() => set(new Date(end.getTime() - spanMin * 60_000))}>◀</button>
      <input type="datetime-local" value={toInput(end)} max={toInput(latestD)}
        onChange={(e) => e.target.value && set(parseTs(e.target.value))}
        className="bg-[var(--panel-2)] border border-[var(--line)] rounded px-2 py-0.5 text-[var(--ink)]" />
      <button className={btn} title="Forward one window" disabled={!endAt}
        onClick={() => set(new Date(end.getTime() + spanMin * 60_000))}>▶</button>
      <button className={btn} disabled={!endAt} onClick={() => onChange(null)}>Live</button>
      <span className={endAt ? "text-[var(--warn)]" : "text-[var(--muted)]"}>
        {endAt ? "Viewing past: " : "Live: "}{label(start)} → {label(end)}
      </span>
    </div>
  );
}
