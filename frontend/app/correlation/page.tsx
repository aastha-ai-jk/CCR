"use client";
import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine, Cell } from "recharts";
import { api, Tag } from "@/lib/api";
import { Panel, Badge, Loading } from "@/components/ui";

function heat(v: number | null) {
  if (v == null) return "#16233d";
  const a = Math.abs(v);
  return v >= 0 ? `rgba(56,189,248,${a})` : `rgba(239,68,68,${a})`;
}

export default function Correlation() {
  const [tags, setTags] = useState<Tag[]>([]);
  const [sel, setSel] = useState<string[]>([
    "423CI400_TT_4", "467RL400_TPH", "353RL130_TPH", "423AM823_CO_PPM", "423AM823_O2", "433MD140_TQ",
  ]);
  const [matrix, setMatrix] = useState<any>(null);
  const [a, setA] = useState("467RL400_TPH");
  const [b, setB] = useState("423CI400_TT_4");
  const [cc, setCc] = useState<any>(null);

  useEffect(() => { api.tags().then((t) => setTags(t.filter((x) => !["status"].includes(x.control_role)))).catch(console.error); }, []);
  useEffect(() => { if (sel.length >= 2) { setMatrix(null); api.correlation(sel).then(setMatrix).catch(console.error); } }, [sel]);
  useEffect(() => { setCc(null); api.crosscorr(a, b, 30).then(setCc).catch(console.error); }, [a, b]);

  const toggle = (t: string) => setSel((s) => s.includes(t) ? s.filter((x) => x !== t) : [...s, t]);
  const label = (t: string) => tags.find((x) => x.tag === t)?.name || t;

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-bold">Correlation Studio</h1>
        <div className="text-sm text-[var(--muted)]">In-load data (feed &gt; 461 t/h) · discover relationships &amp; process dead-time</div>
      </header>

      <Panel title="Select parameters">
        <div className="flex flex-wrap gap-1.5">
          {tags.map((t) => (
            <button key={t.tag} onClick={() => toggle(t.tag)}
              className={`text-[11px] px-2.5 py-1 rounded-full border ${sel.includes(t.tag)
                ? "bg-[var(--panel-2)] text-[var(--accent)] border-[var(--accent)]"
                : "text-[var(--muted)] border-[var(--line)]"}`}
              title={t.tag}>{t.name}</button>
          ))}
        </div>
      </Panel>

      <div className="grid grid-cols-12 gap-4">
        <Panel title="Correlation matrix (Pearson, 15-min mean)" className="col-span-12 lg:col-span-6 overflow-auto scroll-thin">
          {!matrix ? <Loading /> : (
            <table className="text-[11px] mono border-collapse">
              <thead><tr><th></th>{matrix.tags.map((t: string) => (
                <th key={t} className="p-1 text-[var(--muted)] rotate-0 whitespace-nowrap">{t.slice(0, 8)}</th>))}</tr></thead>
              <tbody>
                {matrix.matrix.map((row: number[], i: number) => (
                  <tr key={i}>
                    <td className="pr-2 text-right text-[var(--muted)] whitespace-nowrap">{matrix.tags[i].slice(0, 12)}</td>
                    {row.map((v, j) => (
                      <td key={j} className="text-center" style={{ background: heat(v), width: 42, height: 30, color: Math.abs(v) > 0.5 ? "#fff" : "#8ea3c4" }}>
                        {v == null ? "" : v.toFixed(2)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="text-[11px] text-[var(--muted)] mt-3">Blue = positive, red = negative. n = {matrix?.n} buckets.</div>
        </Panel>

        <Panel title="Lagged cross-correlation → process dead-time" className="col-span-12 lg:col-span-6">
          <div className="flex items-center gap-2 mb-3 text-xs flex-wrap">
            <span className="text-[var(--muted)]">Lead</span>
            <select value={a} onChange={(e) => setA(e.target.value)} className="panel-2 px-2 py-1 mono">
              {tags.map((t) => <option key={t.tag} value={t.tag}>{t.name}</option>)}
            </select>
            <span className="text-[var(--muted)]">→ response</span>
            <select value={b} onChange={(e) => setB(e.target.value)} className="panel-2 px-2 py-1 mono">
              {tags.map((t) => <option key={t.tag} value={t.tag}>{t.name}</option>)}
            </select>
          </div>
          {!cc ? <Loading /> : (
            <>
              <div style={{ height: 200 }}>
                <ResponsiveContainer>
                  <BarChart data={cc.series} margin={{ top: 5, right: 5, left: -15, bottom: 0 }}>
                    <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                    <XAxis dataKey="lag_min" tick={{ fill: "#8ea3c4", fontSize: 10 }} />
                    <YAxis tick={{ fill: "#8ea3c4", fontSize: 10 }} domain={[-1, 1]} />
                    <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8 }} />
                    <ReferenceLine x={0} stroke="#8ea3c4" />
                    <Bar dataKey="corr" isAnimationActive={false}>
                      {cc.series.map((d: any, i: number) => (
                        <Cell key={i} fill={d.lag_min === cc.best_lag?.lag_min ? "#f59e0b" : "#38bdf8"} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
              {cc.best_lag && (
                <div className="text-xs text-[var(--muted)] mt-2">
                  Strongest coupling at <Badge tone="warn">lag {cc.best_lag.lag_min} min</Badge>{" "}
                  (r = {cc.best_lag.corr}). {cc.best_lag.lag_min > 0
                    ? `${label(b)} responds ~${cc.best_lag.lag_min} min after ${label(a)} moves — the dead-time the twin must model.`
                    : "Response is near-instant or the driver lags — a sign of confounding, not causation."}
                </div>
              )}
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}
