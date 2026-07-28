"use client";
import { useEffect, useMemo, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { api, Tag, DQ, ROLE_COLOR } from "@/lib/api";
import { Panel, Badge, Loading } from "@/components/ui";

export default function Explorer() {
  const [tags, setTags] = useState<Tag[] | null>(null);
  const [dq, setDq] = useState<Record<string, DQ>>({});
  const [sel, setSel] = useState<string>("423CI400_TT_4");
  const [pts, setPts] = useState<any[]>([]);
  const [roleFilter, setRoleFilter] = useState<string>("all");

  useEffect(() => {
    api.tags().then(setTags).catch(console.error);
    api.dataQuality().then((d) => setDq(Object.fromEntries(d.map((x) => [x.tag, x])))).catch(console.error);
  }, []);
  useEffect(() => {
    setPts([]);
    api.series([sel], { bucket: "1h", start: "2026-06-01", end: "2026-07-19" })
      .then((r: any) => setPts(r.points)).catch(console.error);
  }, [sel]);

  const filtered = useMemo(
    () => (tags || []).filter((t) => roleFilter === "all" || t.control_role === roleFilter),
    [tags, roleFilter]);
  if (!tags) return <Loading />;
  const selTag = tags.find((t) => t.tag === sel);
  const selDq = dq[sel];

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-xl font-bold">Data Explorer</h1>
          <div className="text-sm text-[var(--muted)]">{tags.length} tags · 492,957 rows · 1-min cadence</div>
        </div>
        <div className="flex gap-1">
          {["all", "target", "mv", "safety", "dv", "status"].map((r) => (
            <button key={r} onClick={() => setRoleFilter(r)}
              className={`text-xs px-2.5 py-1 rounded-full border ${roleFilter === r
                ? "bg-[var(--panel-2)] text-[var(--accent)] border-[var(--accent)]"
                : "text-[var(--muted)] border-[var(--line)]"}`}>{r}</button>
          ))}
        </div>
      </header>

      <div className="grid grid-cols-12 gap-4">
        <Panel title="Tag catalogue" className="col-span-12 lg:col-span-5 max-h-[70vh] overflow-auto scroll-thin">
          <table className="w-full text-xs">
            <thead className="text-[var(--muted)] sticky top-0 bg-[var(--panel)]">
              <tr className="text-left"><th className="py-1">Tag</th><th>Parameter</th><th>Role</th><th className="text-right">Valid%</th></tr>
            </thead>
            <tbody>
              {filtered.map((t) => (
                <tr key={t.tag} onClick={() => setSel(t.tag)}
                  className={`cursor-pointer border-t border-[var(--line)] hover:bg-[var(--panel-2)] ${sel === t.tag ? "bg-[var(--panel-2)]" : ""}`}>
                  <td className="py-1.5 mono" style={{ color: ROLE_COLOR[t.control_role] }}>{t.tag}</td>
                  <td className="text-[var(--ink)] pr-2">{t.name}</td>
                  <td><span className="px-1.5 py-0.5 rounded text-[10px]" style={{ background: "#16233d", color: ROLE_COLOR[t.control_role] }}>{t.control_role}</span></td>
                  <td className="text-right mono text-[var(--muted)]">{dq[t.tag]?.pct_valid ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <div className="col-span-12 lg:col-span-7 space-y-4">
          <Panel title={selTag ? `${selTag.name} (${sel})` : sel}
            right={selTag && <Badge tone="accent">{selTag.subsystem}</Badge>}>
            <div className="grid grid-cols-3 gap-2 mb-3 text-xs">
              <div className="panel-2 p-2"><div className="text-[var(--muted)]">Unit</div><div className="mono">{selTag?.uom || "—"}</div></div>
              <div className="panel-2 p-2"><div className="text-[var(--muted)]">Eng. range</div><div className="mono">{selTag?.y_min}–{selTag?.y_max}</div></div>
              <div className="panel-2 p-2"><div className="text-[var(--muted)]">Observed mean</div><div className="mono">{selDq?.vmean?.toFixed(1) ?? "—"}</div></div>
            </div>
            {selTag?.description && <div className="text-xs text-[var(--muted)] mb-3">{selTag.description}</div>}
            <div style={{ height: 240 }}>
              {pts.length === 0 ? <Loading label="Loading trend…" /> : (
                <ResponsiveContainer>
                  <LineChart data={pts} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                    <CartesianGrid stroke="#223252" strokeDasharray="3 3" />
                    <XAxis dataKey="ts" tick={{ fill: "#8ea3c4", fontSize: 10 }} minTickGap={50} />
                    <YAxis tick={{ fill: "#8ea3c4", fontSize: 10 }} domain={["auto", "auto"]} />
                    <Tooltip contentStyle={{ background: "#16233d", border: "1px solid #223252", borderRadius: 8 }} />
                    <Line type="monotone" dataKey={sel} stroke={ROLE_COLOR[selTag?.control_role || "dv"]} dot={false} strokeWidth={1.6} isAnimationActive={false} />
                  </LineChart>
                </ResponsiveContainer>
              )}
            </div>
            <div className="text-[11px] text-[var(--muted)] mt-2">Jun–Jul 2026 · hourly mean · out-of-range flagged: {selDq?.n_out_of_range ?? 0}</div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
