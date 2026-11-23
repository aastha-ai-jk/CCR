"use client";
import { useEffect, useMemo, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api, Tag } from "@/lib/api";
import { Panel, Loading } from "@/components/ui";

// Admin: trend of any plant parameters, ticked from a list and overlaid on one
// chart. Units differ (rpm, t/h, °C, ppm…), so each line is drawn as % of its
// own range: the tag catalogue's y_min–y_max, or — when a tag has no range or
// its readings in the window fall outside it — the min–max of those readings.
// Hover shows the real value and unit. Parameters with no reading in the last
// hour (checked when the page opens) are left out of the list.

type Win = "hour" | "day" | "week" | "month";
const WINDOWS: { key: Win; label: string }[] = [
  { key: "hour", label: "Hour" },
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
];
const DEFAULT_TAGS = ["423CI400_TT_4"];
const COLORS = ["#38bdf8", "#f59e0b", "#22c55e", "#ef4444", "#a78bfa", "#f472b6",
  "#2dd4bf", "#facc15", "#fb923c", "#818cf8", "#a3e635", "#e879f9"];
type Point = { ts: string } & Record<string, number | string | null>;
type Range = { lo: number; hi: number; from: "catalogue" | "observed" };

const fmt = (v: number) => (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

export default function ParamTrends() {
  const [tags, setTags] = useState<Tag[] | null>(null);
  const [hidden, setHidden] = useState<Tag[]>([]);
  const [listErr, setListErr] = useState<string | null>(null);
  const [sel, setSel] = useState<string[]>(DEFAULT_TAGS);
  const [win, setWin] = useState<Win>("day");
  const [pts, setPts] = useState<Point[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Parameter list: all catalogue tags that have at least one reading in the last hour.
  useEffect(() => {
    let alive = true;
    api.tags()
      .then(async (all) => {
        const r: any = await api.series(all.map((t) => t.tag), { window: "hour" });
        const has = (t: string) => (r.points as Point[]).some((p) => p[t] !== null && p[t] !== undefined);
        if (!alive) return;
        setTags(all.filter((t) => has(t.tag)));
        setHidden(all.filter((t) => !has(t.tag)));
        setSel((s) => s.filter((t) => has(t)));
      })
      .catch((e) => { if (alive) setListErr(String(e)); });
    return () => { alive = false; };
  }, []);

  // Data for the ticked parameters; the hour view refreshes every minute.
  const selKey = sel.join(",");
  useEffect(() => {
    if (!tags || sel.length === 0) { setPts(null); return; }
    let alive = true;
    const load = () => api.series(sel, { window: win })
      .then((r: any) => { if (alive) { setPts(r.points); setErr(null); } })
      .catch((e) => { if (alive) setErr(String(e)); });
    setPts(null); setErr(null); load();
    const id = win === "hour" ? setInterval(load, 60_000) : undefined;
    return () => { alive = false; if (id) clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tags, selKey, win]);

  const byTag = useMemo(() => Object.fromEntries((tags ?? []).map((t) => [t.tag, t])), [tags]);
  const color = (t: string) => COLORS[sel.indexOf(t) % COLORS.length];

  // Range per ticked tag for the loaded window.
  const ranges = useMemo(() => {
    const out: Record<string, Range | null> = {};
    for (const t of sel) {
      const vals = (pts ?? []).map((p) => p[t]).filter((v): v is number => typeof v === "number");
      if (vals.length === 0) { out[t] = null; continue; }
      const vmin = Math.min(...vals), vmax = Math.max(...vals);
      const c = byTag[t];
      const cat = c && c.y_min !== null && c.y_max !== null && c.y_max > c.y_min
        && vmin >= c.y_min && vmax <= c.y_max;
      out[t] = cat ? { lo: c.y_min!, hi: c.y_max!, from: "catalogue" } : { lo: vmin, hi: vmax, from: "observed" };
    }
    return out;
  }, [pts, selKey, byTag]); // eslint-disable-line react-hooks/exhaustive-deps

  // Chart rows: "<tag>" = % of range, "raw:<tag>" = real value for the tooltip.
  const rows = useMemo(() => (pts ?? []).map((p) => {
    const row: Record<string, number | string | null> = { ts: p.ts };
    for (const t of sel) {
      const v = p[t], r = ranges[t];
      row[`raw:${t}`] = typeof v === "number" ? v : null;
      row[t] = typeof v === "number" && r ? (r.hi > r.lo ? ((v - r.lo) / (r.hi - r.lo)) * 100 : 50) : null;
    }
    return row;
  }), [pts, ranges, selKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (t: string) => setSel((s) => (s.includes(t) ? s.filter((x) => x !== t) : [...s, t]));
  const groups = useMemo(() => {
    const g: Record<string, Tag[]> = {};
    for (const t of tags ?? []) (g[t.subsystem] ??= []).push(t);
    return Object.entries(g);
  }, [tags]);

  const windowButtons = (
    <div className="flex items-center gap-1 panel-2 p-0.5 rounded-md">
      {WINDOWS.map((w) => (
        <button key={w.key} onClick={() => setWin(w.key)}
          className={`px-2.5 py-1 text-xs rounded transition-colors ${
            win === w.key ? "bg-[var(--accent)] text-[#0b1220] font-semibold" : "text-[var(--muted)] hover:text-[var(--ink)]"}`}>
          {w.label}
        </button>
      ))}
    </div>
  );

  return (
    <Panel title="Parameter Trends" right={windowButtons}>
      {listErr ? <div className="text-sm text-[var(--bad)]">Could not load the parameter list: {listErr}</div>
        : !tags ? <Loading label="Loading parameters…" /> : (
        <div className="grid lg:grid-cols-[300px_1fr] gap-4">
          <div className="max-h-[420px] overflow-auto scroll-thin pr-1">
            <div className="flex items-center justify-between text-xs mb-2">
              <span className="text-[var(--muted)]">{sel.length} of {tags.length} ticked</span>
              <span className="flex gap-2">
                <button className="text-[var(--accent)] hover:underline" onClick={() => setSel(tags.map((t) => t.tag))}>All</button>
                <button className="text-[var(--accent)] hover:underline" onClick={() => setSel([])}>Clear</button>
              </span>
            </div>
            {groups.map(([sub, list]) => (
              <div key={sub} className="mb-2">
                <div className="text-[11px] uppercase tracking-wide text-[var(--muted)] mb-1">{sub}</div>
                {list.map((t) => {
                  const on = sel.includes(t.tag), r = ranges[t.tag];
                  return (
                    <label key={t.tag} className="flex items-start gap-2 py-1 px-1 rounded cursor-pointer hover:bg-[var(--panel-2)] text-xs">
                      <input type="checkbox" checked={on} onChange={() => toggle(t.tag)} className="mt-0.5" />
                      <i className="inline-block w-2.5 h-2.5 rounded-sm mt-1 shrink-0"
                        style={{ background: on ? color(t.tag) : "transparent", border: on ? "none" : "1px solid var(--line)" }} />
                      <span className="min-w-0">
                        <span className="text-[var(--ink)]">{t.name}</span>
                        <span className="block mono text-[10px] text-[var(--muted)]">
                          {t.tag}{t.uom ? ` · ${t.uom}` : ""}
                          {on && r && ` · 0–100% = ${fmt(r.lo)}–${fmt(r.hi)}${r.from === "observed" ? " (observed)" : ""}`}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            ))}
            {hidden.length > 0 && (
              <div className="text-[11px] text-[var(--muted)] mt-2">
                Not listed (no reading in the last hour): {hidden.map((t) => `${t.name} (${t.tag})`).join(", ")}
              </div>
            )}
          </div>

          <div>
            <div style={{ height: 380 }}>
              {sel.length === 0 ? <div className="text-sm text-[var(--muted)] p-6">Tick one or more parameters to plot.</div>
                : err ? <div className="text-sm text-[var(--bad)] p-6">Could not load the trend: {err}</div>
                : !pts ? <Loading label={`Loading ${sel.length} parameter${sel.length > 1 ? "s" : ""}…`} />
                : rows.length === 0 ? <div className="text-sm text-[var(--muted)] p-6">No readings in this window.</div>
                : (
                <ResponsiveContainer>
                  <LineChart data={rows} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                    <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                    <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={40}
                      tickFormatter={(v: string) => (win === "hour" ? v.slice(11) : v.slice(5))} />
                    <YAxis domain={[0, 100]} tick={{ fill: "#8ea3c4", fontSize: 10 }} tickFormatter={(v: number) => `${v}%`} />
                    <Tooltip content={(p: any) => <TrendTip {...p} byTag={byTag} />} />
                    {sel.map((t) => (
                      <Line key={t} type="monotone" dataKey={t} stroke={color(t)} dot={false} strokeWidth={1.8}
                        connectNulls={false} isAnimationActive={false} />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              )}
            </div>
            <div className="text-[11px] text-[var(--muted)] mt-2">
              Each line is % of its own range (shown under the parameter name); hover for real values.
              {win === "hour" && " Hour view refreshes every minute."}
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

function TrendTip({ active, payload, label, byTag }: {
  active?: boolean; payload?: any[]; label?: string; byTag: Record<string, Tag>;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="text-xs rounded-lg px-3 py-2" style={{ background: "#16233d", border: "1px solid #223252", color: "#e6edf7" }}>
      <div className="mono mb-1">{label}</div>
      {payload.map((e) => {
        const t = String(e.dataKey), raw = e.payload?.[`raw:${t}`];
        const c = byTag[t];
        return (
          <div key={t} className="flex items-center gap-2">
            <i className="inline-block w-2 h-2 rounded-sm" style={{ background: e.color }} />
            <span className="text-[#8ea3c4]">{c?.name ?? t}</span>
            <span className="mono ml-auto pl-3">
              {typeof raw === "number" ? `${fmt(raw)}${c?.uom ? ` ${c.uom}` : ""}` : "—"}
              <span className="text-[#8ea3c4]"> · {typeof e.value === "number" ? `${e.value.toFixed(0)}%` : "—"}</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
