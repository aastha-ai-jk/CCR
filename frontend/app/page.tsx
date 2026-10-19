"use client";
import { useEffect, useState } from "react";
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

type Window = "day" | "week" | "month";
const WINDOWS: { key: Window; label: string }[] = [
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
];

export default function Overview() {
  const [ov, setOv] = useState<any>(null);
  const [trend, setTrend] = useState<any[]>([]);
  const [win, setWin] = useState<Window>("week");

  useEffect(() => {
    const load = () => api.overview().then(setOv).catch(console.error);
    load();
    const id = setInterval(load, 2500);
    return () => clearInterval(id);
  }, []);

  // Reload the temperature trend whenever the Day/Week/Month window changes.
  useEffect(() => {
    setTrend([]);
    api.series(["423CI400_TT_4"], { window: win })
      .then((r: any) => setTrend(r.points)).catch(console.error);
  }, [win]);

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
          }>
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
              <LineChart data={trend} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40}
                       tickFormatter={(v: string) => v.slice(5)} />
                <YAxis domain={[820, 960]} tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8, color: "#e6edf7" }} />
                <ReferenceArea y1={band.low} y2={band.high} fill="#22c55e" fillOpacity={0.12} />
                <Line type="monotone" dataKey="423CI400_TT_4" stroke="#38bdf8" dot={false} strokeWidth={2} name="Calciner temp" isAnimationActive={false} />
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
