"""Generate docs/twin-dashboard/index.html — the everything dashboard.

Control-room instrument-panel treatment (deliberately dark, matching the live
Kiln-3 app). Covers: the MAE journey from 11.6 degC down, data intervals /
volume / quality, every modelling approach tried, testing & validation
(offline / backtest / live shadow), bias patterns, and the horizon ladder.

Run: python docs/twin-dashboard/build.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE = json.loads((HERE / "live_series.json").read_text())

INK, MUT, LINE = "#e6edf7", "#8ea3c4", "#223252"
EMBER, CYAN, GOOD, BAD = "#f97316", "#38bdf8", "#22c55e", "#f87171"


def svg(w, h, body):
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def ln(x1, y1, x2, y2, stroke=LINE, sw=1, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d}/>')


def tx(x, y, s, size=12, fill=INK, anchor="start", weight="", mono=True):
    w = f' font-weight="{weight}"' if weight else ""
    cls = "num" if mono else "lbl"
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" class="{cls}"{w}>{s}</text>')


def poly(pts, stroke, sw=2.2, dash=""):
    p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<polyline points="{p}" fill="none" stroke="{stroke}" '
            f'stroke-width="{sw}"{d} stroke-linejoin="round"/>')


def rc(x, y, w, h, fill, rx=3, op=1.0):
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" rx="{rx}" opacity="{op}"/>')


def dot(x, y, r=3.5, fill=EMBER):
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}"/>'


# ------------------------------------------------ waterfall: 11.6 -> 5.98 ----
def chart_waterfall():
    stages = [
        ("11.6", "true 5-min swing", "the raw magnitude any forecaster faces", MUT),
        ("7.95", "persistence", "assume no change — the free baseline", CYAN),
        ("7.70", "v1 · Jul 29", "first GBT twin, delta target", EMBER),
        ("7.37", "v2 · Aug 04", "bench-winning features", EMBER),
        ("7.01", "retrain · Aug 21", "live-regime data folded in", EMBER),
        ("6.67", "v4 · Aug 27", "5-min denoised features", EMBER),
        ("6.43", "v5 · Aug 27", "huber loss", EMBER),
        ("5.98", "v7 · today", "89 feats + ARX blend + live-history serving", GOOD),
    ]
    W, H, L, R, T, B = 1180, 400, 66, 30, 30, 88
    lo, hi = 5.0, 12.2
    n = len(stages)
    bw = (W - L - R) / n * 0.62
    gap = (W - L - R) / n
    y_of = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    b = ""
    for v in (6, 8, 10, 12):
        b += ln(L, y_of(v), W - R, y_of(v)) + tx(L - 8, y_of(v) + 4, f"{v}", 11, MUT, "end")
    tx0 = tx(L - 8, y_of(12) - 14, "°C MAE", 10, MUT, "end")
    b += tx0
    fl_t, fl_b = y_of(6.0), y_of(5.5)
    b += rc(L, fl_t, W - L - R, fl_b - fl_t, GOOD, 0, 0.10)
    b += tx(L + (W - L - R) * 0.42, fl_t - 6, "practical floor 5.5–6.0 with current sensors",
            10.5, GOOD, "middle", mono=False)
    prev_y = None
    for i, (val, name, sub, col) in enumerate(stages):
        v = float(val)
        cx = L + gap * i + gap / 2
        x0 = cx - bw / 2
        b += rc(x0, y_of(v), bw, y_of(lo) - y_of(v), col, 4, 0.92 if col != MUT else 0.55)
        if prev_y is not None:
            b += ln(x0 - gap + bw, prev_y, x0, y_of(v), MUT, 1, "3 3")
        prev_y = y_of(v)
        b += tx(cx, y_of(v) - 8, val, 15, INK, "middle", "700")
        b += tx(cx, H - 62, name, 11.5, INK, "middle", "700", mono=False)
        for j, chunk in enumerate(_wrap(sub, 22)):
            b += tx(cx, H - 47 + j * 13, chunk, 10, MUT, "middle", mono=False)
    return svg(W, H, b)


def _wrap(s, width):
    out, cur = [], ""
    for word in s.split():
        if len(cur) + len(word) + 1 > width and cur:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out[:3]


# --------------------------------------------------------- horizon ladder ----
def chart_horizon():
    rows = [(1, 3.36, 3.87, 13.2), (2, 5.03, 6.08, 17.3), (3, 5.52, 7.23, 23.6),
            (5, 5.98, 7.93, 24.6), (10, 6.17, 8.38, 26.4), (15, 6.31, 8.32, 24.2),
            (30, 6.54, 8.61, 24.1)]
    W, H, L, R, T, B = 570, 320, 52, 60, 26, 50
    lo, hi = 0.0, 9.5
    gap = (W - L - R) / len(rows)
    bw = 17
    y_of = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    b = ""
    for v in (0, 3, 6, 9):
        b += ln(L, y_of(v), W - R, y_of(v)) + tx(L - 6, y_of(v) + 4, f"{v}", 10, MUT, "end")
    sk_y = lambda s: T + (H - T - B) * (30 - s) / 30
    pts = []
    for i, (h, m, p, s) in enumerate(rows):
        cx = L + gap * i + gap / 2
        b += rc(cx - bw - 2, y_of(p), bw, y_of(0) - y_of(p), CYAN, 2, 0.5)
        b += rc(cx + 2, y_of(m), bw, y_of(0) - y_of(m), EMBER, 2)
        b += tx(cx + bw / 2 + 2, y_of(m) - 4, f"{m:.2f}" if h < 4 else f"{m:.1f}", 9.5, INK, "middle", "700")
        b += tx(cx, H - 30, f"t+{h}", 11, INK, "middle", "700")
        pts.append((cx, sk_y(s)))
    b += poly(pts, GOOD, 2)
    for (cx, y), (_, _, _, s) in zip(pts, rows):
        b += dot(cx, y, 3, GOOD) + tx(cx, y - 8, f"{s:.0f}%", 9.5, GOOD, "middle", "700")
    b += tx(L, H - 12, "ember = twin · cyan = persistence · green = forecast skill", 10, MUT, mono=False)
    return svg(W, H, b)


# ------------------------------------------------------------- live daily ----
def chart_live():
    days = LIVE["daily"]
    W, H, L, R, T, B = 570, 320, 52, 16, 24, 58
    lo, hi = 4.0, 16.5
    n = len(days)
    x_of = lambda i: L + (W - L - R) * i / (n - 1)
    y_of = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    b = ""
    for v in (4, 8, 12, 16):
        b += ln(L, y_of(v), W - R, y_of(v)) + tx(L - 6, y_of(v) + 4, f"{v}", 10, MUT, "end")
    area = " ".join(f"{x_of(i):.1f},{y_of(d['persist_mae']):.1f}" for i, d in enumerate(days))
    area += " " + " ".join(f"{x_of(i):.1f},{y_of(d['mae']):.1f}"
                           for i, d in reversed(list(enumerate(days))))
    b += f'<polygon points="{area}" fill="{GOOD}" opacity="0.15"/>'
    b += poly([(x_of(i), y_of(d["persist_mae"])) for i, d in enumerate(days)], CYAN, 1.5, "4 3")
    b += poly([(x_of(i), y_of(d["mae"])) for i, d in enumerate(days)], EMBER, 2)
    iv7 = next(i for i, d in enumerate(days) if d["d"] >= "2026-08-28")
    b += ln(x_of(iv7) - 3, T, x_of(iv7) - 3, H - B, GOOD, 1.4, "3 3")
    b += tx(x_of(iv7) + 2, T + 12, "v7 live", 10.5, GOOD, weight="700", mono=False)
    for i, d in enumerate(days):
        if i % 8 == 0 or i == n - 1:
            b += tx(x_of(i), H - 36, d["d"][5:], 9.5, MUT, "middle")
    pre = [d for d in days if d["d"] < "2026-08-28"]
    post = [d for d in days if d["d"] >= "2026-08-28"]
    sk = lambda ds: 100 * (1 - sum(x["mae"] * x["n"] for x in ds) / sum(x["persist_mae"] * x["n"] for x in ds))
    b += tx(L, H - 12, f"daily live MAE (ember) vs persistence (cyan) · shaded = skill: "
            f"{sk(pre):.0f}% before v7 → {sk(post):.0f}% after", 10, MUT, mono=False)
    return svg(W, H, b)


# --------------------------------------------------------------- bars util ---
def barpanel(rows, lo, hi, W=560, note=""):
    T, RH, L, R = 8, 31, 150, 52
    H = T + RH * len(rows) + (16 if note else 4)
    x_of = lambda v: L + (W - L - R) * (v - lo) / (hi - lo)
    b = ""
    for i, (name, v, col) in enumerate(rows):
        y = T + RH * i
        b += tx(L - 8, y + 16, name, 11, MUT, "end", mono=False)
        b += rc(L, y + 4, max(x_of(v) - L, 2), 17, col, 3)
        b += tx(x_of(v) + 6, y + 17, f"{v:.2f}", 11, INK, weight="700")
    if note:
        b += tx(L, H - 3, note, 9.5, MUT, mono=False)
    return svg(W, H, b)


# ------------------------------------------------------- retrain lollipop ----
def chart_retrains():
    rows = [("Aug 21 · first guarded retrain", 7.372, 7.014, 6),
            ("Aug 24 · scheduled cron", 7.014, 6.933, 1450),
            ("Aug 27 · v4 promotion", 6.933, 6.672, 4553),
            ("Aug 27 · v5 promotion", 6.672, 6.427, 39),
            ("Aug 27 · v7 promotion", 6.427, 5.978, 159),
            ("Aug 30 · scheduled cron", 5.978, 5.975, 3463)]
    W, RH, T = 560, 38, 14
    H = T + RH * len(rows) + 34
    L, R = 212, 46
    lo, hi = 5.8, 7.6
    x_of = lambda v: L + (W - L - R) * (v - lo) / (hi - lo)
    b = ""
    for v in (6.0, 6.5, 7.0, 7.5):
        b += ln(x_of(v), T - 4, x_of(v), H - 28, LINE) + tx(x_of(v), H - 14, f"{v:.1f}", 10, MUT, "middle")
    for i, (label, old, new, mins) in enumerate(rows):
        y = T + RH * i + RH / 2
        b += tx(L - 12, y - 1, label, 10.5, INK, "end", mono=False)
        b += tx(L - 12, y + 11, f"+{mins:,} live min", 9, MUT, "end")
        b += ln(x_of(old), y, x_of(new), y, GOOD, 3)
        b += dot(x_of(old), y, 3.5, MUT) + dot(x_of(new), y, 4, GOOD)
        b += tx(x_of(new) - 7, y - 7, f"{new:.3f}".rstrip("0"), 10, GOOD, "end", "700")
    return svg(W, H, b)


# ------------------------------------------------------ floor decomposition --
def chart_floor():
    W, H, L, R = 560, 190, 190, 30
    rows = [("true 5-min change", 11.6, MUT, ""),
            ("persistence", 7.95, CYAN, ""),
            ("twin v7 today", 5.98, EMBER, ""),
            ("practical floor", 5.75, GOOD, "5.5–6.0"),
            ("sensor jitter", 0.68, BAD, "on the truth itself")]
    T, RH = 14, 32
    x_of = lambda v: L + (W - L - R) * v / 12.5
    b = ""
    for v in (0, 4, 8, 12):
        b += ln(x_of(v), T - 6, x_of(v), T + RH * len(rows)) + \
             tx(x_of(v), T + RH * len(rows) + 14, f"{v}°C", 10, MUT, "middle")
    for i, (name, v, col, note) in enumerate(rows):
        y = T + RH * i
        b += tx(L - 8, y + 15, name, 10.5, INK, "end", mono=False)
        if name == "practical floor":
            b += rc(x_of(5.5), y + 4, x_of(6.0) - x_of(5.5), 15, col, 3, 0.9)
        else:
            b += rc(L, y + 4, max(x_of(v) - L, 2), 15, col, 3, 0.85)
        lbl = note if note == "5.5–6.0" else \
            (f"{v:.1f}" if v >= 1 else f"{v:.2f}") + (f"  {note}" if note else "")
        b += tx(x_of(max(v, 6.0 if note == "5.5–6.0" else v)) + 6, y + 16, lbl, 9.5, MUT)
    return svg(W, H, b)


# ------------------------------------------------------------------ html -----
def build():
    css = f"""
:root{{--bg:#0b1524;--panel:#111e33;--panel2:#16233d;--line:{LINE};--ink:{INK};
--muted:{MUT};--ember:{EMBER};--cyan:{CYAN};--good:{GOOD};--bad:{BAD};
--mono:ui-monospace,'SF Mono',Menlo,monospace;
--sans:-apple-system,'Segoe UI','Helvetica Neue',sans-serif}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--ink);font-family:var(--sans);margin:0;
font-size:15px;line-height:1.55;
background-image:radial-gradient(circle at 20% -10%,#13223c 0%,transparent 50%)}}
.wrap{{max-width:1240px;margin:0 auto;padding:26px 26px 80px}}
.kicker{{font-family:var(--mono);font-size:11px;letter-spacing:.16em;color:var(--ember);
text-transform:uppercase;font-weight:700}}
h1{{font-size:clamp(26px,4vw,38px);margin:.25em 0 .15em;line-height:1.1;letter-spacing:-.01em}}
.sub{{color:var(--muted);max-width:78ch;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-top:18px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;overflow:hidden}}
.panel h2{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);
margin:0 0 10px;font-weight:700}}
.panel h2 b{{color:var(--ink)}}
.s12{{grid-column:span 12}}.s6{{grid-column:span 6}}.s4{{grid-column:span 4}}.s3{{grid-column:span 3}}
@media(max-width:900px){{.s6,.s4,.s3{{grid-column:span 12}}}}
.chart{{width:100%;height:auto;display:block}}
.chart .num{{font-family:var(--mono);font-variant-numeric:tabular-nums}}
.chart .lbl{{font-family:var(--sans)}}
.big{{font-family:var(--mono);font-size:30px;font-weight:700;color:var(--ember);
font-variant-numeric:tabular-nums;line-height:1.1}}
.big.g{{color:var(--good)}}.big.c{{color:var(--cyan)}}
.cap{{font-size:11.5px;color:var(--muted);margin-top:2px}}
p{{font-size:13.5px;color:#c6d2e6;margin:.5em 0}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th{{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.08em;
color:var(--muted);border-bottom:1px solid var(--ink);padding:6px 8px}}
td{{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top}}
td.num{{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}}
.tag{{display:inline-block;font-family:var(--mono);font-size:10.5px;padding:2px 8px;
border-radius:99px;border:1px solid var(--line);color:var(--muted);margin:2px 3px 2px 0}}
.tag.ok{{color:var(--good);border-color:rgba(34,197,94,.4)}}
.tag.no{{color:var(--bad);border-color:rgba(248,113,113,.4)}}
ul{{padding-left:1.15em;margin:.4em 0}}
li{{font-size:13px;color:#c6d2e6;margin:.3em 0}}
.foot{{margin-top:26px;border-top:1px solid var(--line);padding-top:12px;
color:var(--muted);font-size:12px}}
.exam{{display:flex;gap:10px;flex-wrap:wrap}}
.exam>div{{flex:1 1 160px;background:var(--panel2);border:1px solid var(--line);
border-radius:10px;padding:10px 12px}}
.exam b{{font-size:12.5px}}
.exam .r{{font-family:var(--mono);color:var(--ember);font-size:16px;font-weight:700}}
"""
    days = LIVE["daily"]
    n_live = sum(d["n"] for d in days)
    html = f"""<title>Kiln-3 Twin — Everything Dashboard</title>
<style>{css}</style>
<div class="wrap">
<div class="kicker">JK Cement · Nimbahera Kiln 3 · Calciner Digital Twin</div>
<h1>From 11.6° of chaos to a 5.98° forecast</h1>
<p class="sub">One dashboard for the whole story: the accuracy journey, the data feeding it,
every model we raced, how results are tested and validated, and how far ahead the twin can see.
Status 3 Sep 2026 · advisory-only · every number traces to a committed experiment artifact.
(Deliberately dark — it mirrors the control-room app.)</p>

<div class="grid">
  <div class="panel s12">
    <h2>The MAE journey — <b>what must be predicted vs how close we get</b></h2>
    {chart_waterfall()}
    <p class="cap">The 5-minute-ahead problem starts at <b>11.6 °C</b> — the average size of the
    true 5-min temperature change (measured on the year of 1-second data; the year includes wilder
    regimes than the test window). "Assume nothing changes" (persistence) already gets you to ~7.95
    on the held-out window; every step after that is the twin earning its keep. All model bars are
    held-out test MAE vs the raw sensor — never vs a smoothed truth, never on training data.</p>
  </div>

  <div class="panel s3"><h2>Held-out test</h2><div class="big">5.98 °C</div>
    <div class="cap">t+5 MAE · R² 0.689 · was 7.70 on Jul 29</div></div>
  <div class="panel s3"><h2>Live shadow</h2><div class="big">6.56 °C</div>
    <div class="cap">11,033 matured predictions since v7 · persistence 8.45 on same minutes</div></div>
  <div class="panel s3"><h2>Forecast skill</h2><div class="big g">22–25 %</div>
    <div class="cap">better than persistence — offline AND live; the window-proof number</div></div>
  <div class="panel s3"><h2>Live evidence</h2><div class="big c">{n_live:,}</div>
    <div class="cap">predictions scored against the real plant since 29 Jul · 0 manual retrains</div></div>

  <div class="panel s6">
    <h2>Data — <b>intervals &amp; volume</b></h2>
    <table>
      <tr><th>Stream</th><th>Interval</th><th>Volume</th><th>Role</th></tr>
      <tr><td>OPC UA live feed (45 tags, read-only)</td><td class="num">2 s</td><td class="num">~43k rows/day</td><td>live features, shadow, bias</td></tr>
      <tr><td>Shadow prediction tick</td><td class="num">30 s</td><td class="num">~2,400/day</td><td>predict → advise → mature</td></tr>
      <tr><td>Training historian</td><td class="num">1 min</td><td class="num">422k rows · Aug 25→now</td><td>all training; grows weekly</td></tr>
      <tr><td>1-second research export</td><td class="num">1 s</td><td class="num">28.8M rows / 1 yr</td><td>resolution study, noise floor</td></tr>
      <tr><td>Lab / eLIMS quality</td><td class="num">1–2 h</td><td class="num">sparse</td><td>DOC head; free-lime blocked</td></tr>
      <tr><td>Monitor evidence</td><td class="num">6 h</td><td class="num">continuous</td><td>accuracy · bias · conformal · AFR</td></tr>
    </table>
  </div>

  <div class="panel s6">
    <h2>Data — <b>quality gates on every training row</b></h2>
    <ul>
      <li><b>Feed gate</b> — kiln feed &gt; 461 t/h: never learn from shutdowns/upsets.</li>
      <li><b>Analyser purge masking</b> — CO/O₂ valid only when RUN=1 &amp; O₂&lt;15% (~9% of readings are purge artefacts; upstream IQR cleaning proved equivalent to our gates — identical 388k rows).</li>
      <li><b>Dead-tag ban</b> — tags present in history but dead on the live server are excluded by rule (they'd invalidate every live row).</li>
      <li><b>Forward-gap guards</b> — a t+5 label must truly be 4–7 min ahead; gaps can't fake targets.</li>
      <li><b>Chronological splits only</b> — 70/15/15 by time; random splits leak adjacent minutes.</li>
      <li><b>3 data bugs found &amp; fixed</b> — timestamp-format clash silently dropping ALL live rows; features on dead tags; the K3V10F1 tag typo that hid the AFR damper for a month. Each is now a permanent pipeline guard.</li>
    </ul>
  </div>

  <div class="panel s6">
    <h2>Models — <b>the race (identical data, identical splits)</b></h2>
    {barpanel([("GBT boosted trees", 7.71, EMBER), ("ridge ARX (linear)", 7.97, CYAN),
               ("persistence", 8.73, MUT), ("GRU deep net", 10.46, BAD)], 7.0, 11.0,
              note="Aug 03 bake-off, 57,643 identical test rows — trees win; deep learning loses to doing nothing")}
    {barpanel([("5-min features", 5.62, GOOD), ("1-min (production)", 5.81, EMBER),
               ("1-second native", 6.20, CYAN), ("persistence", 7.21, MUT)], 5.0, 7.5,
              note="Aug 26 resolution study, same file & minutes — averaging denoises; resolution ≠ accuracy")}
    <p>The serving model is a <b>0.6·GBT + 0.4·ARX blend</b> per horizon: the trees are sharp, the
    linear model is regime-stable, and their errors differ in shape — the blend beat either alone at
    every horizon and matches the measured ensemble ceiling.</p>
  </div>

  <div class="panel s6">
    <h2>Models — <b>everything we tried &amp; the verdicts</b></h2>
    {barpanel([("xfeat + blend → v7", 5.99, GOOD), ("stack ceiling*", 5.98, MUT),
               ("extended features", 6.10, EMBER), ("Optuna-tuned params", 6.11, CYAN),
               ("ARX blend alone", 6.30, CYAN), ("v5 recipe", 6.42, MUT)], 5.6, 6.7,
              note="Aug 27 optimisation bench, identical fold · *test-picked weights = upper bound; v7 equals it")}
    <div style="margin-top:8px">
      <span class="tag ok">huber loss @capped rounds ✓</span><span class="tag ok">smoothed-anchor ✓</span>
      <span class="tag ok">ARX blend ✓</span><span class="tag ok">live-history serving ✓</span>
      <span class="tag no">GRU deep net ✗</span><span class="tag no">1-second features ✗</span>
      <span class="tag no">recency weighting ✗</span><span class="tag no">linear-tree ✗</span>
      <span class="tag no">two-stage stacking ✗</span><span class="tag no">bagging ✗</span>
      <span class="tag no">uncapped huber ✗</span><span class="tag no">EWM target — metric gaming ✗</span>
    </div>
    <p class="cap">Every ✗ is a committed experiment with a JSON verdict — settled questions stay settled.
    The EWM-target variant "won" the leaderboard (5.92) and was rejected anyway: it scores better by
    quietly predicting ~t+3.5 instead of t+5.</p>
  </div>

  <div class="panel s12">
    <h2>Testing &amp; validation — <b>three exams, one story</b></h2>
    <div class="exam">
      <div><b>1 · Held-out test</b><div class="r">5.98 °C</div>
        <p class="cap">Chronologically later window the model never saw. Scored vs the RAW sensor.
        This gates every promotion (≤115% of incumbent or auto-rollback).</p></div>
      <div><b>2 · Backtest replay</b><div class="r">8.15 °C · R² 0.80</div>
        <p class="cap">Year-long replay incl. volatile regimes, raw model (no live bias help).
        Overlap-as-accuracy chart on the Twin page: the red strip IS the miss.</p></div>
      <div><b>3 · Live shadow (un-gameable)</b><div class="r">6.56 °C</div>
        <p class="cap">Predictions logged BEFORE their answers exist, matured 5 min later —
        {n_live:,} of them. Live ≈ offline is the proof the methodology doesn't leak.</p></div>
      <div><b>Bias patterns (measured)</b><div class="r">+1.9 → ~0 °C</div>
        <p class="cap">Systematic +1.9 °C drift found &amp; self-corrected (rolling 2 h offset vs the
        raw model). Anchoring: corr 0.91 with now vs 0.59 with future. Damping 0.44 on big moves —
        the known weakness; big-move detector is the planned fix.</p></div>
    </div>
    <div class="grid" style="margin-top:12px">
      <div class="s6" style="grid-column:span 6">{chart_live()}</div>
      <div class="s6" style="grid-column:span 6">{chart_retrains()}
        <p class="cap" style="margin-top:4px">The guarded weekly retrain: ingest live minutes → train →
        promote only if not worse → reload. Six promotions, two fully unattended, zero interventions.</p></div>
    </div>
  </div>

  <div class="panel s6">
    <h2>How far ahead can it see? — <b>t+1 → t+30</b></h2>
    {chart_horizon()}
    <p class="cap">At t+1 the twin scores <b>3.36 °C</b> — but one minute out the plant has barely
    moved (persistence 3.87), so the edge is small. Skill <b>grows</b> with horizon, peaking ~26% at
    t+10: the twin is most valuable exactly where a human can't extrapolate. The optimizer plans on
    the full t+5…t+30 trajectory with P10–P90 chance constraints.</p>
  </div>

  <div class="panel s6">
    <h2>The floor — <b>why ~6° is near the physical limit</b></h2>
    {chart_floor()}
    <p class="cap">~0.7 °C of every score is thermocouple jitter on the truth itself; the test-picked
    ensemble ceiling equals what v7 already serves; the unexplained remainder is minute-scale fuel
    calorific-value variation <b>no current sensor measures</b>. Below ~5.5 °C is an instrumentation
    decision (online AFR fuel-quality sensing) — not a modelling one. Next value step instead:
    surface the advisory in the CCR and measure followed-vs-ignored ₹/day.</p>
  </div>
</div>

<div class="foot">Kiln-3 calciner digital twin · branch feature/digital-twin-ml · generated
{__import__('datetime').date.today().isoformat()} by docs/twin-dashboard/build.py · advisory-only —
nothing is ever written to the plant.</div>
</div>
"""
    return html


if __name__ == "__main__":
    out = HERE / "index.html"
    out.write_text(build())
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
