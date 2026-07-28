"use client";
import React from "react";

/* ---- interlock limits (mirror of backend config.INTERLOCKS) ---- */
const LIMITS: Record<string, [string, number]> = {
  "423AM823_CO_PPM": ["<", 1500], "423AM823_O2": [">", 1.5],
  "423AM822_CO_PPM": ["<", 800], "423AM822_O2": [">", 0.5],
  "423AM820_CO_PPM": ["<", 3000], "423AM821_CO_PPM": ["<", 3000],
};

const C = { gas: "#22d3ee", meal: "#f59e0b", air: "#60a5fa", steel: "#33507d",
  vessel: "#16233d", led: "#4ade80", warn: "#f59e0b", bad: "#f87171", ink: "#cfe0f5", mut: "#7f97bd" };

// per-cyclone gas-exit temperature tag (from the live OPC feed).
// CN150 uses _TT_2 — its _TT_1 thermocouple reads bad on the live server.
const CYC_TEMP: Record<string, string> = {
  CN110: "423CN110_TT_1", CN120: "423CN120_TT_1", CN130: "423CN130_TT_1",
  CN140: "423CN140_TT_1", CN150: "423CN150_TT_2",
  CN210: "423CN210_TT_1", CN220: "423CN220_TT_1", CN230: "423CN230_TT_1",
  CN240: "423CN240_TT_1", CN250: "423CN250_TT_1",
};

export interface MimicProps {
  values: Record<string, number | null | undefined>;
  units?: Record<string, string | null>;
  band?: { low: number; high: number };
  mode?: "live" | "sim";
}

export default function Mimic({ values, units = {}, band, mode = "live" }: MimicProps) {
  const num = (t: string) => {
    const x = values[t];
    return x == null || (typeof x === "number" && isNaN(x)) ? null : (x as number);
  };
  const fmt = (t: string, d = 0) => { const x = num(t); return x == null ? "—" : x.toFixed(d); };
  const u = (t: string) => units[t] || "";
  const safeTone = (t: string) => {
    const x = num(t); const lim = LIMITS[t];
    if (x == null || !lim) return "led";
    const ok = lim[0] === "<" ? x < lim[1] : x > lim[1];
    return ok ? "led" : "bad";
  };
  const tempTone = () => {
    const x = num("423CI400_TT_4");
    if (x == null || !band) return "accent";
    return x >= band.low && x <= band.high ? "led" : "warn";
  };

  const Readout = ({ x, y, label, t, d = 0, w = 122, tone = "led", val, unit }:
    { x: number; y: number; label: string; t?: string; d?: number; w?: number; tone?: string; val?: string; unit?: string }) => {
    const col: Record<string, string> = { led: C.led, warn: C.warn, bad: C.bad, accent: C.gas, ink: C.ink };
    const value = val ?? (t ? fmt(t, d) : "—");
    const unt = unit ?? (t ? u(t) : "");
    return (
      <g>
        <rect x={x} y={y} width={w} height={38} rx={5} fill="#0a1120" stroke={C.steel} />
        <text x={x + 8} y={y + 14} fill={C.mut} fontSize="8.5" style={{ letterSpacing: 0.4 }}>{label.toUpperCase()}</text>
        <text x={x + 8} y={y + 31} fill={col[tone]} fontSize="15" fontFamily="ui-monospace,Consolas,monospace" fontWeight={700}>
          {value}<tspan fontSize="9" fill={C.mut}> {unt}</tspan>
        </text>
      </g>
    );
  };

  const Analyzer = ({ x, y, title, rows }: { x: number; y: number; title: string; rows: { k: string; t: string; d?: number }[] }) => (
    <g>
      <rect x={x} y={y} width={132} height={22 + rows.length * 16} rx={6} fill="#0d1830" stroke={C.steel} />
      <rect x={x} y={y} width={132} height={17} rx={6} fill="#12213c" />
      <text x={x + 8} y={y + 12} fill={C.gas} fontSize="9.5" fontWeight={700}>{title}</text>
      {rows.map((r, i) => {
        const tone = safeTone(r.t);
        return (
          <text key={r.k} x={x + 8} y={y + 32 + i * 16} fontSize="11" fontFamily="ui-monospace,monospace">
            <tspan fill={C.mut}>{r.k} </tspan>
            <tspan fill={tone === "bad" ? C.bad : C.led} fontWeight={700}>{fmt(r.t, r.d ?? (r.k === "O₂" ? 1 : 0))}</tspan>
            <tspan fill={C.mut} fontSize="8"> {u(r.t)}</tspan>
          </text>
        );
      })}
    </g>
  );

  const Cyclone = ({ cx, cy, label }: { cx: number; cy: number; label: string }) => {
    const w = 80;
    const tv = CYC_TEMP[label] ? fmt(CYC_TEMP[label], 0) : null;
    return (
      <g>
        <rect x={cx - w / 2} y={cy} width={w} height={28} rx={3} fill={C.vessel} stroke={C.steel} />
        <polygon points={`${cx - w / 2},${cy + 28} ${cx + w / 2},${cy + 28} ${cx},${cy + 54}`} fill={C.vessel} stroke={C.steel} />
        <text x={cx} y={cy + 12} textAnchor="middle" fill={C.mut} fontSize="9.5" fontFamily="ui-monospace,monospace">{label}</text>
        {tv && tv !== "—" && (
          <text x={cx} y={cy + 24} textAnchor="middle" fill={C.led} fontSize="13" fontWeight={700} fontFamily="ui-monospace,monospace">
            {tv}<tspan fontSize="8" fill={C.mut}>°C</tspan>
          </text>
        )}
      </g>
    );
  };

  const leftCyc = ["CN110", "CN120", "CN130", "CN140", "CN150"];
  const rightCyc = ["CN210", "CN220", "CN230", "CN240", "CN250"];
  const cyY = [78, 158, 238, 318, 398];
  const LX = 205, RX = 995, tempT = tempTone();

  return (
    <div style={{ overflowX: "auto" }} className="scroll-thin">
      <svg viewBox="0 0 1200 720" width="100%" style={{ minWidth: 900, display: "block" }}>
        <defs>
          <linearGradient id="flame" x1="0" y1="1" x2="0" y2="0">
            <stop offset="0" stopColor="#fca5a5" /><stop offset="0.5" stopColor="#f59e0b" /><stop offset="1" stopColor="#fde047" />
          </linearGradient>
          <radialGradient id="glow"><stop offset="0" stopColor="#f59e0b" stopOpacity="0.5" /><stop offset="1" stopColor="#f59e0b" stopOpacity="0" /></radialGradient>
        </defs>

        {/* ------- gas risers (towers) ------- */}
        {[LX, RX].map((x) => <line key={x} x1={x} y1={cyY[0] + 10} x2={x} y2={cyY[4] + 40} stroke={C.gas} strokeWidth={3} opacity={0.5} />)}
        {/* cross ducts from calciner up to bottom cyclones */}
        <path d={`M600 300 L${LX} 430`} stroke={C.gas} strokeWidth={3} opacity={0.5} fill="none" />
        <path d={`M600 300 L${RX} 430`} stroke={C.gas} strokeWidth={3} opacity={0.5} fill="none" />

        {/* ------- kiln feed ------- */}
        <rect x={520} y={8} width={160} height={40} rx={6} fill="#12213c" stroke={C.steel} />
        <text x={600} y={24} textAnchor="middle" fill={C.mut} fontSize="10">KILN FEED</text>
        <text x={600} y={40} textAnchor="middle" fill={C.meal} fontSize="15" fontWeight={700} fontFamily="ui-monospace,monospace">{fmt("353RL130_TPH", 0)} <tspan fontSize="9" fill={C.mut}>t/h</tspan></text>
        <path d={`M540 48 Q ${LX} 40 ${LX} ${cyY[0]}`} stroke={C.meal} strokeWidth={2} strokeDasharray="5 4" fill="none" />
        <path d={`M660 48 Q ${RX} 40 ${RX} ${cyY[0]}`} stroke={C.meal} strokeWidth={2} strokeDasharray="5 4" fill="none" />

        {/* ------- cyclone towers ------- */}
        {leftCyc.map((l, i) => <Cyclone key={l} cx={LX} cy={cyY[i]} label={l} />)}
        {rightCyc.map((l, i) => <Cyclone key={l} cx={RX} cy={cyY[i]} label={l} />)}
        <text x={LX} y={cyY[0] - 8} textAnchor="middle" fill={C.mut} fontSize="10" fontWeight={700}>PREHEATER STRING-1</text>
        <text x={RX} y={cyY[0] - 8} textAnchor="middle" fill={C.mut} fontSize="10" fontWeight={700}>PREHEATER STRING-2</text>

        {/* PH fan + outlet temp */}
        <circle cx={90} cy={70} r={22} fill={C.vessel} stroke={C.steel} />
        <text x={90} y={66} textAnchor="middle" fill={C.mut} fontSize="8">PH FAN</text>
        <text x={90} y={78} textAnchor="middle" fill={C.led} fontSize="10" fontFamily="ui-monospace,monospace">{fmt("423FN010_ST", 0)}</text>
        <Readout x={30} y={100} label="PH outlet temp" t="423CN_TEMP" unit="°C" tone="ink" w={120} />

        {/* analyzers */}
        <Analyzer x={28} y={150} title="PH-1 ANALYSER" rows={[{ k: "O₂", t: "423AM820_O2" }, { k: "CO", t: "423AM820_CO_PPM" }]} />
        <Analyzer x={1040} y={150} title="PH-2 ANALYSER" rows={[{ k: "O₂", t: "423AM821_O2" }, { k: "CO", t: "423AM821_CO_PPM" }]} />

        {/* feed-pipe temps */}
        <Readout x={300} y={455} label="CN150 feed pipe" t="423KR405_TT_3" unit="°C" tone="ink" w={118} />
        <Readout x={782} y={455} label="CN250 feed pipe" t="423KR405_TT_4" unit="°C" tone="ink" w={118} />

        {/* ------- CALCINER vessel ------- */}
        <path d="M545 320 Q545 296 570 296 L630 296 Q655 296 655 320 L648 470 Q648 486 632 486 L568 486 Q552 486 552 470 Z"
          fill={C.vessel} stroke={tempT === "warn" ? C.warn : C.gas} strokeWidth={2} />
        <text x={600} y={360} textAnchor="middle" fill={C.mut} fontSize="12" fontWeight={700}>CALCINER</text>
        <text x={600} y={376} textAnchor="middle" fill={C.mut} fontSize="10" fontFamily="ui-monospace,monospace">CI400</text>
        {/* calciner flame */}
        <ellipse cx={600} cy={478} rx={46} ry={26} fill="url(#glow)" />
        <polygon points="588,486 600,452 612,486" fill="url(#flame)" />
        <polygon points="596,486 604,462 612,486" fill="#fde047" opacity={0.8} />

        {/* calciner outlet temp — the controlled variable */}
        <rect x={520} y={244} width={160} height={46} rx={7} fill="#0a1120" stroke={tempT === "warn" ? C.warn : C.led} strokeWidth={2} />
        <text x={528} y={260} fill={C.mut} fontSize="9">CALCINER OUTLET TEMP (CI400)</text>
        <text x={528} y={282} fill={tempT === "warn" ? C.warn : C.led} fontSize="20" fontWeight={800} fontFamily="ui-monospace,monospace">
          {fmt("423CI400_TT_4", 0)}<tspan fontSize="11" fill={C.mut}> °C</tspan>
        </text>
        {band && <text x={604} y={282} fill={C.mut} fontSize="9">band {band.low}-{band.high}</text>}

        {/* calciner analyser */}
        <Analyzer x={690} y={300} title="CALCINER ANALYSER" rows={[{ k: "O₂", t: "423AM823_O2" }, { k: "CO", t: "423AM823_CO_PPM" }]} />

        {/* calciner fuel + AFR feeds (levers) */}
        <line x1={470} y1={360} x2={545} y2={370} stroke={C.meal} strokeWidth={2} markerEnd="" />
        <Readout x={348} y={342} label="Calciner coal" t="467RL400_TPH" unit="t/h" tone="warn" w={118} />
        <Readout x={348} y={384} label="LAFR · PC" t="LAFR_FT" unit="l/h" tone="accent" w={118} />
        <Readout x={210} y={520} label="LAFR · TAD" t="K3V10_F1" unit="l/h" tone="accent" w={118} />
        <Readout x={348} y={520} label="SAFR (solid AFR)" t="261WF080_TPH" unit="t/h" tone="accent" w={130} />

        {/* ------- KILN ------- */}
        {/* riser from calciner base to kiln inlet */}
        <path d="M600 486 L560 540" stroke={C.gas} strokeWidth={3} opacity={0.5} fill="none" />
        {/* rotary kiln tube (inclined) */}
        <polygon points="470,560 820,600 820,632 470,592" fill="#1b2c4b" stroke={C.steel} strokeWidth={2} />
        {[0, 1, 2, 3, 4, 5, 6].map((i) => (
          <line key={i} x1={490 + i * 48} y1={562 + i * 4.6} x2={490 + i * 48} y2={594 + i * 4.6} stroke={C.steel} opacity={0.6} />
        ))}
        <text x={600} y={584} fill={C.mut} fontSize="11" fontWeight={700}>ROTARY KILN</text>
        {/* burner flame at kiln outlet */}
        <ellipse cx={815} cy={616} rx={40} ry={22} fill="url(#glow)" />
        <polygon points="838,616 790,606 838,626" fill="url(#flame)" />

        {/* kiln inlet analyser */}
        <Analyzer x={360} y={548} title="KILN INLET ANALYSER"
          rows={[{ k: "O₂", t: "423AM822_O2" }, { k: "CO", t: "423AM822_CO_PPM" }, { k: "NOx", t: "423AM822_NO" }]} />

        {/* kiln readouts */}
        <Readout x={512} y={636} label="Kiln torque" t="433MD140_TQ" unit="Nm" tone="ink" w={112} />
        <Readout x={628} y={636} label="Kiln speed" t="433MD140_ST1" d={2} unit="rpm" tone="ink" w={110} />
        <Readout x={742} y={636} label="Kiln coal" t="467RL300_TPH" d={1} unit="t/h" tone="warn" w={104} />

        {/* ------- COOLER ------- */}
        <polygon points="840,596 990,596 970,672 860,672" fill={C.vessel} stroke={C.steel} strokeWidth={2} />
        <text x={915} y={632} textAnchor="middle" fill={C.mut} fontSize="11" fontWeight={700}>COOLER</text>
        {/* tertiary air duct cooler -> calciner */}
        <path d="M905 596 Q 905 400 656 380" stroke={C.air} strokeWidth={2} strokeDasharray="6 5" fill="none" opacity={0.7} />
        <Readout x={856} y={508} label="Tertiary air temp" t="423DU415_TT" unit="°C" tone="ink" w={128} />
        <Readout x={856} y={550} label="Secondary air temp" t="443KH050_TT1" unit="°C" tone="ink" w={128} />

        {/* legend */}
        <g fontSize="10" fill={C.mut}>
          <line x1={30} y1={694} x2={54} y2={694} stroke={C.gas} strokeWidth={3} /><text x={60} y={698}>gas</text>
          <line x1={110} y1={694} x2={134} y2={694} stroke={C.meal} strokeWidth={2} strokeDasharray="5 4" /><text x={140} y={698}>meal / fuel</text>
          <line x1={230} y1={694} x2={254} y2={694} stroke={C.air} strokeWidth={2} strokeDasharray="6 5" /><text x={260} y={698}>air</text>
        </g>
      </svg>
    </div>
  );
}
