"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Panel, Badge, Loading } from "@/components/ui";
import { ArrowRight, ArrowUp, ArrowDown, Minus, Check, X } from "lucide-react";

const fmt = (v: any, d = 1) =>
  v === null || v === undefined || isNaN(Number(v)) ? "—" : Number(v).toFixed(d);

const FEED_TAG = "353RL130_TPH";
const FEED_MIN = 461; // Combustion Control logic doc: applicability notice

function MoveArrow({ delta }: { delta: number }) {
  if (Math.abs(delta) < 1e-6) return <Minus size={14} className="text-[var(--muted)]" />;
  return delta > 0
    ? <ArrowUp size={14} className="text-[var(--accent)]" />
    : <ArrowDown size={14} className="text-[var(--ok)]" />;
}

const SAFETY_NAME: Record<string, string> = {
  "423AM823_CO_PPM": "Calciner outlet CO",
  "423AM823_O2": "Calciner outlet O₂",
  "423AM822_CO_PPM": "Kiln inlet CO",
  "423AM822_O2": "Kiln inlet O₂",
};

/** Builds the "why" behind the recommendation in plain operator language —
 * every number still traces back to a field the /api/advisory response
 * already returns (backend/analysis.py's advise()), just phrased simply. */
function buildReasoning(adv: any): string[] {
  if (!adv?.feasible) return [];
  const band = adv.target_band;
  const temp = adv.live_actual_temp;
  const dir = temp > band.high ? "above" : temp < band.low ? "below" : "within";
  const lines: string[] = [];

  lines.push(
    `The plant is running in ${adv.operating_state}, which should be kept between ` +
    `${band.low}–${band.high} °C.`
  );
  lines.push(
    dir === "within"
      ? `Temperature is currently ${fmt(temp, 0)} °C — already inside that range, so the ` +
        `aim is just to hold it there using the least fuel.`
      : `Temperature is currently ${fmt(temp, 0)} °C — ${dir} the target range — so fuel needs ` +
        `to be ${dir === "above" ? "reduced" : "increased"} to bring it back.`
  );
  lines.push(
    `To stay safe, every fuel change is capped at ${adv.assumptions?.guardrail_pct}% at a time ` +
    `(the plant's own rule: adjust up to 5% every 15 seconds). ` +
    `Of ${adv.candidates_evaluated} possible fuel settings checked within that limit, ` +
    `${adv.safe_candidates} were safe, and this is the best one.`
  );
  lines.push(
    adv.mode === "hold"
      ? "A safe setting that keeps the temperature in range was found, so the cheapest one of those was chosen."
      : `No single 5% step fully reaches the target range, so this move is the safe step that ` +
        `gets closest to it` +
        (adv.steps_to_band ? ` — expect to repeat a move like this roughly ${adv.steps_to_band} more times to fully settle in range.` : ".")
  );
  const safetyVals = Object.entries(adv.predicted_safety || {})
    .map(([tag, val]: any) => `${SAFETY_NAME[tag] || tag} ${fmt(val, 1)}`).join(", ");
  if (safetyVals) {
    lines.push(`Gas safety levels after this move stay within safe limits: ${safetyVals}.`);
  }
  if (adv.saving?.coal_pct) {
    const reduces = adv.saving.coal_tph > 0;
    lines.push(
      `As a bonus, this move ${reduces ? "uses" : "needs"} ${fmt(Math.abs(adv.saving.coal_pct), 1)}% ` +
      `${reduces ? "less" : "more"} coal` +
      (reduces && adv.saving.cost_per_year ? ` — worth about ${fmt(adv.saving.cost_per_year, 0)} ${adv.saving.currency} a year if kept up.` : ".")
    );
  }
  return lines;
}

export default function Advisory() {
  const [latest, setLatest] = useState<any>(null);
  const [adv, setAdv] = useState<any>(null);
  const [decisionState, setDecisionState] = useState<"pending" | "accepted" | "declined">("pending");
  const [decisionTs, setDecisionTs] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [declineNote, setDeclineNote] = useState("");
  const [showDeclineBox, setShowDeclineBox] = useState(false);

  const load = () => {
    api.latest().then(setLatest).catch(console.error);
    api.advisory().then((next) => {
      // a genuinely new recommendation (new snapshot timestamp or a changed
      // move set) resets any prior accept/decline so it can't be applied
      // to a stale suggestion.
      setAdv((prev: any) => {
        if (prev && (prev.ts !== next.ts || JSON.stringify(prev.moves) !== JSON.stringify(next.moves))) {
          setDecisionState("pending");
          setDecisionTs(null);
          setShowDeclineBox(false);
          setDeclineNote("");
        }
        return next;
      });
    }).catch(console.error);
  };
  useEffect(() => { load(); const id = setInterval(load, 2500); return () => clearInterval(id); }, []);

  if (!latest || !adv) return <Loading label="Building operator advisory…" />;

  const reasoning = buildReasoning(adv);

  const submitDecision = async (decision: "accepted" | "declined", note?: string) => {
    setSubmitting(true);
    try {
      await api.advisoryDecision({
        decision,
        operating_state: adv.operating_state,
        mode: adv.mode,
        target_band_low: adv.target_band?.low,
        target_band_high: adv.target_band?.high,
        live_actual_temp: adv.live_actual_temp,
        predicted_temp: adv.predicted_temp,
        moves: adv.moves,
        reasoning: reasoning.join(" "),
        note,
      });
      setDecisionState(decision);
      setDecisionTs(new Date().toLocaleTimeString());
      setShowDeclineBox(false);
    } catch (e) {
      console.error(e);
    } finally {
      setSubmitting(false);
    }
  };

  const feed = latest.values?.[FEED_TAG];
  const applicable = feed != null && feed > FEED_MIN;
  const src = latest.source;
  const feedBadge = src === "opc"
    ? { tone: "ok", text: `● LIVE · OPC · ${latest.age_sec ?? "?"}s ago` }
    : src === "replay" ? { tone: "warn", text: "REPLAY (recent history)" }
    : { tone: "muted", text: "historical snapshot" };

  const band = adv.target_band;
  const modeTone = adv.mode === "hold" ? "ok" : adv.mode === "approach" ? "warn" : "bad";
  const modeLabel = adv.mode === "hold" ? "HOLD BAND" : adv.mode === "approach" ? "APPROACH BAND" : "NO SAFE MOVE";

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <span className="live-dot text-[var(--ok)]">●</span> Advisory — Recommended Setpoints
          </h1>
          <div className="text-sm text-[var(--muted)]">
            What to change right now, per the Combustion Control logic: guarded ±5% moves on the
            4 calciner-side fuel levers, never trading safety for temperature.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={feedBadge.tone}>{feedBadge.text}</Badge>
          <Badge tone="accent">{adv.operating_state}</Badge>
          <Badge tone="muted">Target {band.low}–{band.high} °C</Badge>
        </div>
      </header>

      {/* ---- applicability gate: doc's own prerequisite for ANY automated action ---- */}
      {!applicable && (
        <Panel className="border border-[var(--warn)]">
          <div className="text-sm text-[var(--warn)] font-semibold">
            Not applicable — kiln feed {fmt(feed, 0)} t/h is at or below the {FEED_MIN} t/h threshold.
          </div>
          <div className="text-[11px] text-[var(--muted)] mt-1">
            Per the Combustion Control logic doc's Applicability Notice: automated control actions
            only apply when kiln feed ({FEED_TAG}) is above {FEED_MIN} t/h. The plant is in a
            transient/low-load state — any recommendation below should be treated as informational
            only, not actioned.
          </div>
        </Panel>
      )}

      <div className="grid grid-cols-12 gap-4 items-start">
        {/* ---- the recommendation itself ---- */}
        <Panel className="col-span-12 lg:col-span-7" title="Recommended move"
               right={<Badge tone={modeTone as any}>{modeLabel}</Badge>}>
          {!adv.feasible ? (
            <div className="text-sm text-[var(--bad)] p-2">{adv.reason}</div>
          ) : (
            <>
              <div className="text-[11px] text-[var(--muted)] mb-3">{adv.mode_note}</div>
              <div className="space-y-2">
                {adv.moves.map((mv: any) => (
                  <div key={mv.tag} className="flex items-center justify-between panel-2 px-3 py-2">
                    <span className="text-sm text-[var(--ink)]">{mv.name}</span>
                    <span className="mono text-sm flex items-center gap-2">
                      {fmt(mv.from, 2)}
                      <MoveArrow delta={mv.delta} />
                      <span className={mv.delta > 0 ? "text-[var(--accent)] font-semibold" : mv.delta < 0 ? "text-[var(--ok)] font-semibold" : ""}>
                        {fmt(mv.to, 2)}
                      </span>
                      <span className="text-[var(--muted)] text-[11px]">
                        ({mv.delta > 0 ? "+" : ""}{fmt(mv.delta, 2)})
                      </span>
                    </span>
                  </div>
                ))}
              </div>
              <div className="text-[11px] text-[var(--muted)] mt-3 leading-relaxed">
                Each move stays within ±{adv.assumptions?.guardrail_pct}% of the current setpoint —
                the documented ramp limit ("increase/decrease PC fuel up to 5% every 15 seconds
                until the target range is restored"). All safety interlocks are respected by
                construction; a move that would breach one is never offered.
                {adv.mode === "approach" && adv.steps_to_band
                  ? ` Estimated ~${adv.steps_to_band} such guarded steps to fully reach the band.`
                  : ""}
              </div>

              {reasoning.length > 0 && (
                <div className="mt-4 panel-2 p-3">
                  <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide mb-2">Why this recommendation</div>
                  <ul className="space-y-1.5">
                    {reasoning.map((line, i) => (
                      <li key={i} className="text-[11px] text-[var(--ink)] leading-relaxed flex gap-2">
                        <span className="text-[var(--muted)]">•</span><span>{line}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="mt-4">
                {decisionState !== "pending" ? (
                  <div className={`flex items-center gap-2 text-sm ${decisionState === "accepted" ? "text-[var(--ok)]" : "text-[var(--muted)]"}`}>
                    {decisionState === "accepted" ? <Check size={16} /> : <X size={16} />}
                    Recommendation {decisionState} at {decisionTs} — logged for the operator record.
                  </div>
                ) : showDeclineBox ? (
                  <div className="space-y-2">
                    <input
                      type="text"
                      value={declineNote}
                      onChange={(e) => setDeclineNote(e.target.value)}
                      placeholder="Optional: reason for declining (helps refine future advisories)"
                      className="w-full text-xs px-3 py-2 rounded-lg bg-[var(--panel-2)] border border-[var(--line)] text-[var(--ink)] placeholder:text-[var(--muted)]"
                    />
                    <div className="flex gap-2">
                      <button disabled={submitting} onClick={() => submitDecision("declined", declineNote || undefined)}
                        className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-[var(--bad)] text-white disabled:opacity-50">
                        Confirm decline
                      </button>
                      <button disabled={submitting} onClick={() => setShowDeclineBox(false)}
                        className="px-3 py-1.5 rounded-lg text-xs text-[var(--muted)] hover:text-[var(--ink)]">
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex gap-2">
                    <button disabled={submitting} onClick={() => submitDecision("accepted")}
                      className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-semibold bg-[var(--ok)] text-[#04120a] disabled:opacity-50">
                      <Check size={15} /> Accept &amp; apply
                    </button>
                    <button disabled={submitting} onClick={() => setShowDeclineBox(true)}
                      className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-semibold border border-[var(--line)] text-[var(--muted)] hover:text-[var(--ink)] disabled:opacity-50">
                      <X size={15} /> Decline
                    </button>
                  </div>
                )}
                <div className="text-[10px] text-[var(--muted)] mt-2">
                  This only records the operator's decision for the audit trail — accepting does not
                  write any setpoint to the DCS. The operator still makes the change by hand.
                </div>
              </div>
            </>
          )}
        </Panel>

        {/* ---- predicted outcome of following the recommendation ---- */}
        <Panel className="col-span-12 lg:col-span-5" title="Predicted outcome after this move">
          {!adv.feasible ? (
            <div className="text-xs text-[var(--muted)]">No move available to predict.</div>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2 text-center mb-3">
                <div className="panel-2 p-2">
                  <div className="text-[10px] text-[var(--muted)] uppercase">Live actual now</div>
                  <div className="mono text-xl">{fmt(adv.live_actual_temp ?? latest.values?.["423CI400_TT_4"], 0)}
                    <span className="text-xs text-[var(--muted)]"> °C</span></div>
                </div>
                <div className="panel-2 p-2">
                  <div className="text-[10px] text-[var(--muted)] uppercase">Predicted after move</div>
                  <div className="mono text-xl"
                       style={{ color: adv.predicted_temp >= band.low && adv.predicted_temp <= band.high ? "var(--ok)" : "var(--warn)" }}>
                    {fmt(adv.predicted_temp, 0)}<span className="text-xs text-[var(--muted)]"> °C</span>
                  </div>
                </div>
              </div>
              <div className="flex justify-center mb-3">
                <Badge tone={adv.predicted_temp >= band.low && adv.predicted_temp <= band.high ? "ok" : "warn"}>
                  {adv.predicted_temp >= band.low && adv.predicted_temp <= band.high ? "IN BAND" : "STILL OUT OF BAND"}
                </Badge>
              </div>
              <div className="text-[10px] text-[var(--muted)] uppercase tracking-wide mb-1">Predicted safety after move</div>
              <div className="space-y-1">
                {Object.entries(adv.predicted_safety || {}).map(([tag, val]: any) => (
                  <div key={tag} className="flex items-center justify-between text-[11px] panel-2 px-2 py-1">
                    <span className="text-[var(--muted)]">{SAFETY_NAME[tag] || tag}</span>
                    <span className="mono">{fmt(val, 1)}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </Panel>
      </div>

      <div className="text-[11px] text-[var(--muted)]">
        Advisory only — this recommends, it never writes to any control system. Kiln feed
        ({FEED_TAG}, currently {fmt(feed, 0)} t/h) is shown for context only and is not one of the
        recommended levers: per the logic doc it's the applicability gate, not a fuel-mix lever.
      </div>
    </div>
  );
}
