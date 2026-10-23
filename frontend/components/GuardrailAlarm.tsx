"use client";
import { useEffect, useRef, useState } from "react";

export type GuardItem = { key: string; text: string };

const CV = "423CI400_TT_4";
const n2 = (v: any) => (v === null || v === undefined || isNaN(Number(v)) ? "—" : Number(v).toFixed(2));

/** Breaches from the /api/advisory payload (`adv.guardrails`, evaluated by the
 *  backend on live AND recommended values), as alarm lines. */
export function guardItems(adv: any, latest: any): GuardItem[] {
  const params: Record<string, any> = adv?.guardrails?.params || {};
  const name = (tag: string) =>
    tag === CV ? "Calciner outlet temperature" : adv?.moves?.find((m: any) => m.tag === tag)?.name || tag;
  const unit = (tag: string) => latest?.units?.[tag] || "";
  const out: GuardItem[] = [];
  Object.entries(params).forEach(([tag, g]) => {
    const checks: [string, any, string | null][] = [
      ["live", g.live, g.live_breach],
      [tag === CV ? "predicted" : "recommended", g.recommended, g.recommended_breach],
    ];
    checks.forEach(([what, v, kind]) => {
      if (!kind) return;
      const edge = kind === "above_roof" ? `above the roof ${g.max}` : `below the floor ${g.min}`;
      out.push({ key: `${tag}:${what}:${kind}`,
                 text: `${name(tag)} — ${what} ${n2(v)} ${unit(tag)} is ${edge} ${unit(tag)}`.trim() });
    });
  });
  // data-validation errors (backend/validation.py) block Accept the same way
  for (const i of validationErrors(adv)) {
    const who = i.scope === "all" ? "affects every lever" : name(i.scope);
    out.push({ key: `val:${i.tag}:${i.check}`,
               text: `Data check — ${i.tag} ${i.check} (${who}): ${i.detail}` });
  }
  return out;
}

/** ERROR-level data-validation issues from the advisory payload. */
export function validationErrors(adv: any): any[] {
  return (adv?.validation?.issues || []).filter((i: any) => i.severity === "error");
}

/** True if data validation blocks this lever (its own error, or one affecting all levers). */
export function validationBlocks(adv: any, tag: string): boolean {
  const v = adv?.validation;
  return !!v && (v.global_block || (v.blocked_levers || []).includes(tag));
}

/**
 * Guardrail alarm: sticky red banner + a repeating two-pulse beep while any
 * advisor parameter is outside its roof / floor (data/plant_settings.json
 * "guardrails", evaluated by the backend).
 *
 * "Silence" stops the sound for the breaches present now; the banner stays
 * until every value is back inside its limit, and a NEW breach sounds again.
 * Browsers only allow sound after the user has interacted with the page, so
 * the audio context is created on the first click / key press.
 */
export default function GuardrailAlarm({ items }: { items: GuardItem[] }) {
  const key = items.map((i) => i.key).sort().join("|");
  const [silencedKey, setSilencedKey] = useState("");
  const [audioOn, setAudioOn] = useState(false);
  const ctx = useRef<AudioContext | null>(null);
  const el = useRef<HTMLDivElement | null>(null);
  const beeps = useRef(0);
  const keyRef = useRef(key);
  const silRef = useRef(silencedKey);
  keyRef.current = key;
  silRef.current = silencedKey;

  useEffect(() => { if (!key) setSilencedKey(""); }, [key]);

  useEffect(() => {
    const unlock = () => {
      const C = window.AudioContext || (window as any).webkitAudioContext;
      if (!C) return;
      if (!ctx.current) {
        try { ctx.current = new C(); } catch { return; }
      }
      const ac = ctx.current;
      if (ac.state === "suspended") ac.resume().then(() => setAudioOn(true)).catch(() => {});
      else setAudioOn(ac.state === "running");
    };
    const evs = ["pointerdown", "keydown", "touchstart"];
    evs.forEach((e) => document.addEventListener(e, unlock, { capture: true, passive: true }));
    const id = setInterval(() => {
      const ac = ctx.current;
      if (!keyRef.current || keyRef.current === silRef.current || !ac || ac.state !== "running") return;
      beep(ac);
      beeps.current += 1;
      el.current?.setAttribute("data-beeps", String(beeps.current));
    }, 2000);
    return () => {
      evs.forEach((e) => document.removeEventListener(e, unlock, { capture: true }));
      clearInterval(id);
      ctx.current?.close().catch(() => {});
      ctx.current = null;
    };
  }, []);

  if (!key) return null;
  const silenced = key === silencedKey;
  return (
    <div ref={el} className={`galarm${silenced ? " is-silenced" : ""}`} role="alert" aria-live="assertive">
      <span className="galarm__icon" aria-hidden="true">!</span>
      <div className="galarm__body">
        <div className="galarm__title">ALARM — Accept is blocked: guardrail or data check failed</div>
        <ul className="galarm__list">
          {items.map((i) => <li key={i.key}>{i.text}</li>)}
        </ul>
        {!silenced && !audioOn && (
          <div className="galarm__hint">Click anywhere on this page to enable the alarm sound.</div>
        )}
      </div>
      <button className="galarm__btn" type="button" disabled={silenced} onClick={() => setSilencedKey(key)}>
        {silenced ? "Silenced" : "Silence alarm"}
      </button>
      <style jsx>{`
        .galarm{position:sticky;top:0;z-index:30;display:flex;gap:16px;align-items:flex-start;
          margin:0 0 14px;padding:14px 20px;background:#3A1512;border:1px solid #D9604F;border-radius:6px;
          color:#FFE3DE;animation:galarmPulse 1.2s ease-in-out infinite;font-family:"IBM Plex Sans",sans-serif;}
        .galarm.is-silenced{animation:none;background:#2A1715;}
        @keyframes galarmPulse{0%,100%{box-shadow:inset 0 0 0 0 rgba(217,96,79,0);}50%{box-shadow:inset 0 0 0 3px rgba(217,96,79,.85);}}
        .galarm__icon{flex:0 0 auto;width:30px;height:30px;border-radius:50%;background:#D9604F;color:#fff;
          display:flex;align-items:center;justify-content:center;font-weight:700;font-size:18px;}
        .galarm__body{flex:1 1 auto;min-width:0;}
        .galarm__title{font-weight:700;font-size:15px;letter-spacing:.02em;color:#fff;}
        .galarm__list{margin:5px 0 0;padding-left:18px;font-size:13.5px;line-height:1.55;}
        .galarm__hint{margin-top:6px;font-size:12.5px;color:#F5B7AD;}
        .galarm__btn{flex:0 0 auto;background:transparent;border:1px solid #F5B7AD;color:#FFE3DE;
          padding:7px 14px;border-radius:4px;font:inherit;font-size:13px;cursor:pointer;}
        .galarm__btn:hover{background:rgba(255,255,255,.08);}
        .galarm__btn[disabled]{opacity:.6;cursor:default;}
        @media (prefers-reduced-motion:reduce){.galarm{animation:none;}}
      `}</style>
    </div>
  );
}

function beep(ac: AudioContext) {
  const t = ac.currentTime;
  [0, 0.28].forEach((off) => {          // two short 880 Hz pulses
    const o = ac.createOscillator();
    const g = ac.createGain();
    o.type = "square";
    o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, t + off);
    g.gain.exponentialRampToValueAtTime(0.25, t + off + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + off + 0.2);
    o.connect(g);
    g.connect(ac.destination);
    o.start(t + off);
    o.stop(t + off + 0.22);
  });
}
