"use client";
/**
 * Savings breakdown — per-parameter fuel, energy and money effect of following
 * the twin's advisory.
 *
 * Fuel prices are entered by the user (persisted in localStorage) rather than
 * shipped as defaults, so no monetary figure is ever fabricated. Energy, TSR and
 * specific-heat-consumption come from the backend and need no prices.
 *
 * Self-contained: takes the `breakdown` object from /api/advisory (or
 * /api/savings) and renders. Drop-in for any page.
 */
import { useEffect, useState } from "react";
import { Panel, Badge } from "@/components/ui";

type Stream = {
  tag: string; name: string; unit: string;
  current: number; recommended: number; delta: number; delta_tph: number;
  cv_kcal_kg: number; energy_saved_gj_h: number;
};

// which user-entered price applies to each stream, and in what unit
const PRICE_OF: Record<string, "coal" | "lafr" | "safr"> = {
  "467RL400_TPH": "coal",
  "467RL300_TPH": "coal",
  "LAFR_FT": "lafr",
  "K3V10_F1": "lafr",
  "261WF080_TPH": "safr",
};
const PRICE_FIELDS: { key: "coal" | "lafr" | "safr"; label: string; unit: string }[] = [
  { key: "coal", label: "Coal", unit: "per tonne" },
  { key: "lafr", label: "Liquid AFR", unit: "per litre" },
  { key: "safr", label: "Solid AFR", unit: "per tonne" },
];
const LS_KEY = "ccc.fuelPrices.v1";
const n = (v: any, d = 2) =>
  v === null || v === undefined || isNaN(Number(v)) ? "—" : Number(v).toFixed(d);
const money = (v: number, cur: string) =>
  `${cur} ${Math.round(v).toLocaleString()}`;

export default function SavingsPanel({ breakdown, mode }: { breakdown: any; mode?: string }) {
  const [prices, setPrices] = useState<Record<string, number>>({ coal: 0, lafr: 0, safr: 0 });
  const [showPrices, setShowPrices] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (raw) setPrices({ coal: 0, lafr: 0, safr: 0, ...JSON.parse(raw) });
    } catch { /* ignore */ }
  }, []);
  const setPrice = (k: string, v: number) => {
    const next = { ...prices, [k]: v };
    setPrices(next);
    try { localStorage.setItem(LS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
  };

  if (!breakdown?.available) {
    return (
      <Panel title="Savings breakdown — energy & money per parameter">
        <div className="text-xs text-[var(--warn)]">
          {breakdown?.reason ?? "No advisory available for the current state."}
        </div>
      </Panel>
    );
  }

  const cur = breakdown.currency || "INR";
  const streams: Stream[] = breakdown.streams || [];
  const days = breakdown.days_per_year ?? 330;

  // money is computed client-side from the user's own prices
  const priceFor = (s: Stream) => prices[PRICE_OF[s.tag]] ?? 0;
  const savedPerH = (s: Stream) => -s.delta * priceFor(s);   // delta<0 => saving>0
  const anyPrice = Object.values(prices).some((p) => p > 0);
  const totalPerH = streams.reduce((a, s) => a + savedPerH(s), 0);

  const tot = breakdown.total || {};
  const tsr = breakdown.tsr;
  const shc = breakdown.specific_heat_kcal_per_kg_clinker;
  const changed = streams.filter((s) => Math.abs(s.delta) > 1e-9);

  return (
    <Panel
      title="Savings breakdown — energy & money per parameter"
      right={
        <div className="flex items-center gap-2">
          {mode && <Badge tone={mode === "hold" ? "ok" : "warn"}>
            {mode === "hold" ? "HOLD BAND" : "APPROACH BAND"}
          </Badge>}
          <button onClick={() => setShowPrices((v) => !v)}
            className="text-[11px] px-2 py-0.5 rounded-full border border-[var(--line)] text-[var(--muted)] hover:text-[var(--ink)]">
            {showPrices ? "hide prices" : "set fuel prices"}
          </button>
        </div>
      }>

      {/* ---- headline tiles ---- */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <div className="panel-2 p-2.5">
          <div className="text-[10px] text-[var(--muted)] uppercase">Thermal energy saved</div>
          <div className="mono text-2xl text-[var(--ok)]">{n(tot.energy_saved_gj_h, 1)}
            <span className="text-[11px] text-[var(--muted)] ml-1">GJ/h</span></div>
          <div className="text-[10px] text-[var(--muted)]">{n(tot.energy_saved_gj_day, 0)} GJ/day</div>
        </div>
        <div className="panel-2 p-2.5">
          <div className="text-[10px] text-[var(--muted)] uppercase">Specific heat cons.</div>
          <div className="mono text-2xl text-[var(--accent)]">
            {shc ? `−${n(shc.saved, 1)}` : "—"}
            <span className="text-[11px] text-[var(--muted)] ml-1">kcal/kg</span></div>
          <div className="text-[10px] text-[var(--muted)]">
            {shc ? `${n(shc.now, 0)} → ${n(shc.recommended, 0)} kcal/kg clinker` : "needs kiln feed"}
          </div>
        </div>
        <div className="panel-2 p-2.5">
          <div className="text-[10px] text-[var(--muted)] uppercase">AFR share (TSR)</div>
          <div className="mono text-2xl">{tsr ? `${n(tsr.recommended_pct, 1)}%` : "—"}</div>
          <div className="text-[10px] text-[var(--muted)]">
            {tsr ? `${n(tsr.now_pct, 1)}% → ${n(tsr.recommended_pct, 1)}% (${tsr.change_pct_points > 0 ? "+" : ""}${n(tsr.change_pct_points, 2)} pp)` : "—"}
          </div>
        </div>
        <div className="panel-2 p-2.5">
          <div className="text-[10px] text-[var(--muted)] uppercase">Money saved</div>
          {anyPrice ? (
            <>
              <div className="mono text-2xl text-[var(--ok)]">{money(totalPerH * 24, cur)}
                <span className="text-[11px] text-[var(--muted)] ml-1">/day</span></div>
              <div className="text-[10px] text-[var(--muted)]">
                {money(totalPerH, cur)}/h · {money(totalPerH * 24 * days, cur)}/yr
              </div>
            </>
          ) : (
            <>
              <div className="mono text-lg text-[var(--muted)]">—</div>
              <button onClick={() => setShowPrices(true)}
                className="text-[10px] text-[var(--accent)] underline">
                enter your fuel prices
              </button>
            </>
          )}
        </div>
      </div>

      {/* ---- price inputs (user supplied, stored locally) ---- */}
      {showPrices && (
        <div className="panel-2 p-3 mt-3">
          <div className="text-[10px] text-[var(--muted)] uppercase mb-2">
            Your fuel prices ({cur}) — used only in this browser
          </div>
          <div className="grid grid-cols-3 gap-3">
            {PRICE_FIELDS.map((f) => (
              <label key={f.key} className="block">
                <div className="text-[11px] text-[var(--ink)]">{f.label}</div>
                <div className="text-[9px] text-[var(--muted)] mb-1">{cur} {f.unit}</div>
                <input type="number" min={0} step="any" value={prices[f.key] || ""}
                  onChange={(e) => setPrice(f.key, Number(e.target.value) || 0)}
                  placeholder="0"
                  className="w-full bg-[var(--bg)] border border-[var(--line)] rounded px-2 py-1 mono text-sm text-[var(--ink)]" />
              </label>
            ))}
          </div>
        </div>
      )}

      {/* ---- per-parameter table ---- */}
      <div className="mt-3 overflow-x-auto scroll-thin">
        <table className="w-full text-[11px]">
          <thead className="text-[var(--muted)]">
            <tr className="text-left">
              <th className="py-1 font-normal">Parameter</th>
              <th className="font-normal text-right">Current</th>
              <th className="font-normal text-right">Recommended</th>
              <th className="font-normal text-right">Change</th>
              <th className="font-normal text-right">CV</th>
              <th className="font-normal text-right">Energy saved</th>
              <th className="font-normal text-right">Money saved</th>
            </tr>
          </thead>
          <tbody>
            {streams.map((s) => {
              const m = savedPerH(s);
              const moved = Math.abs(s.delta) > 1e-9;
              return (
                <tr key={s.tag} className={`border-t border-[var(--line)] ${moved ? "" : "opacity-45"}`}>
                  <td className="py-1.5 text-[var(--ink)]">{s.name}
                    <span className="text-[var(--muted)]"> ({s.unit})</span></td>
                  <td className="mono text-right">{n(s.current, 1)}</td>
                  <td className="mono text-right text-[var(--accent)]">{n(s.recommended, 1)}</td>
                  <td className={`mono text-right ${s.delta < 0 ? "text-[var(--ok)]" : s.delta > 0 ? "text-[var(--warn)]" : "text-[var(--muted)]"}`}>
                    {s.delta > 0 ? "+" : ""}{n(s.delta, 1)}
                  </td>
                  <td className="mono text-right text-[var(--muted)]">{s.cv_kcal_kg}</td>
                  <td className="mono text-right text-[var(--ok)]">
                    {s.energy_saved_gj_h ? n(s.energy_saved_gj_h, 2) : "—"}
                  </td>
                  <td className="mono text-right">
                    {!moved ? "—" : priceFor(s) > 0
                      ? <span className={m >= 0 ? "text-[var(--ok)]" : "text-[var(--warn)]"}>{money(m * 24, cur)}/d</span>
                      : <span className="text-[var(--muted)]">no price</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t-2 border-[var(--line)]">
              <td className="py-1.5 text-[var(--ink)] font-semibold">Total</td>
              <td colSpan={4} className="text-right text-[var(--muted)]">
                heat {n(tot.heat_now_gj_h, 0)} → {n(tot.heat_rec_gj_h, 0)} GJ/h
              </td>
              <td className="mono text-right text-[var(--ok)] font-semibold">{n(tot.energy_saved_gj_h, 2)}</td>
              <td className="mono text-right font-semibold">
                {anyPrice ? <span className="text-[var(--ok)]">{money(totalPerH * 24, cur)}/d</span>
                          : <span className="text-[var(--muted)]">—</span>}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <div className="text-[10px] text-[var(--muted)] mt-2 leading-relaxed">
        Energy uses each stream&apos;s calorific value (coal {streams.find(s => s.tag === "467RL400_TPH")?.cv_kcal_kg ?? "—"},
        AFR {streams.find(s => s.tag === "LAFR_FT")?.cv_kcal_kg ?? "—"} kcal/kg); liquid AFR is converted
        from l/h to t/h by density. Money uses the prices you enter above.
        Annual figures assume {days} operating days.
        {changed.length === 0 && " No lever change is recommended in the current state."}
        {mode === "approach" && " In APPROACH mode fuel is trimmed to bring temperature back into band, so part of this saving is corrected over-firing."}
      </div>
    </Panel>
  );
}
