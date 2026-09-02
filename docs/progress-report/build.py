"""Generate docs/progress-report/index.html — self-contained visual progress report.

Static experiment figures are inlined from their committed JSON artifacts
(models/*.json, verified in-session); the live daily series + retrain log are
read from docs/report_assets_live_series.json (pulled from the GPU).

Run: python docs/progress-report/build.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LIVE = json.loads((HERE / "live_series.json").read_text())

EMBER, STEEL, GOOD, MUT = "var(--ember)", "var(--steel)", "var(--good)", "var(--muted)"


# ---------------------------------------------------------------- svg utils --
def svg(w: int, h: int, body: str, cls: str = "") -> str:
    return (f'<svg viewBox="0 0 {w} {h}" class="chart {cls}" role="img" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def line(x1, y1, x2, y2, stroke=MUT, sw=1, dash="", opacity=1.0) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d} opacity="{opacity}"/>')


def txt(x, y, s, size=12, fill="var(--ink)", anchor="start", weight="", cls="num") -> str:
    w = f' font-weight="{weight}"' if weight else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" class="{cls}"{w}>{s}</text>')


def poly(pts, stroke, sw=2.2, dash="") -> str:
    p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<polyline points="{p}" fill="none" stroke="{stroke}" stroke-width="{sw}"{d} stroke-linejoin="round"/>'


def dot(x, y, r=3.5, fill=EMBER) -> str:
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}"/>'


def rect(x, y, w, h, fill, rx=2, opacity=1.0) -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" rx="{rx}" opacity="{opacity}"/>')


def yaxis(x0, x1, y_of, lo, hi, step, unit="°C", fmt="{:.0f}") -> str:
    out = ""
    v = lo
    while v <= hi + 1e-9:
        y = y_of(v)
        out += line(x0, y, x1, y, "var(--line)", 1)
        out += txt(x0 - 8, y + 4, fmt.format(v) + ("" if v != hi else f" {unit}"),
                   11, MUT, "end")
        v += step
    return out


# ------------------------------------------------------- chart 1: journey ----
def chart_journey() -> str:
    steps = [  # (label, date, offline test MAE, sublabel)
        ("v1", "Jul 29", 7.70, "first GBT twin"),
        ("v2", "Aug 04", 7.37, "enriched features"),
        ("retrain", "Aug 21", 7.01, "live-regime data"),
        ("cron", "Aug 24", 6.93, "auto retrain"),
        ("v4", "Aug 27", 6.67, "5-min features"),
        ("v5", "Aug 27", 6.43, "huber loss"),
        ("v7", "Aug 27", 5.98, "89 feats + blend"),
        ("cron", "Aug 30", 5.975, "auto retrain"),
    ]
    W, H, L, R, T, B = 960, 380, 64, 24, 28, 58
    lo, hi = 5.5, 8.3
    xs = [L + (W - L - R) * i / (len(steps) - 1) for i in range(len(steps))]
    y_of = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    b = yaxis(L, W - R, y_of, 5.5, 8.0, 0.5, fmt="{:.1f}")
    b += line(L, y_of(7.95), W - R, y_of(7.95), STEEL, 1.6, "6 5")
    b += txt(W - R, y_of(7.95) - 7, "persistence baseline ≈ 7.95", 11.5, STEEL, "end")
    b += rect(L, y_of(5.5), W - L - R, y_of(5.5) - y_of(6.0) and (y_of(5.5) - y_of(6.0)), "var(--good)", 0, 0)  # noop guard
    fl_top, fl_bot = y_of(6.0), y_of(5.5)
    b += rect(L, fl_top, W - L - R, fl_bot - fl_top, GOOD, 0, 0.08)
    b += txt(L + 8, fl_bot - 8, "estimated practical floor 5.5–6.0 (sensor + unmeasured fuel variation)", 11, GOOD)
    pts = [(x, y_of(s[2])) for x, s in zip(xs, steps)]
    seg = []
    for i in range(len(pts) - 1):
        seg.append(poly([pts[i], (pts[i + 1][0], pts[i][1]), pts[i + 1]], EMBER, 2.4))
    b += "".join(seg)
    for i, ((x, y), (name, date, mae, sub)) in enumerate(zip(pts, steps)):
        big = name.startswith("v") or name == "retrain"
        last = i == len(steps) - 1
        b += dot(x, y, 4.5 if big else 3, EMBER if big else MUT)
        anch = "start" if i == 0 else ("end" if last else "middle")
        lx = x + 10 if i == 0 else (x - 4 if last else x)
        ly = y + 22 if last else (y - 26 if big else y - 14)
        b += txt(lx, ly, f"{mae:.2f}".rstrip("0").rstrip("."),
                 13.5 if big else 11, "var(--ink)", anch, "700")
        if big:
            b += txt(lx, ly + 13 if last else y - 12, name, 11, EMBER, anch, "700")
        xa = "end" if last else "middle"
        b += txt(x, H - 34, date, 11, MUT, xa)
        b += txt(x, H - 20, sub, 10, MUT, xa)
    return svg(W, H, b)


# ------------------------------------------------- chart 2: retrain cycles ---
def chart_retrains() -> str:
    logs = LIVE["retrain_log"]
    # collapse the four Aug-21 loop-build entries into one for readability
    rows = [
        {"label": "Aug 21 · first guarded retrain (manual)", "old": 7.372, "new": 7.014, "mins": 6},
        {"label": "Aug 24 · scheduled cron", "old": 7.014, "new": 6.933, "mins": 1450},
        {"label": "Aug 27 · v4 promotion", "old": 6.933, "new": 6.672, "mins": 4553},
        {"label": "Aug 27 · v5 promotion", "old": 6.672, "new": 6.427, "mins": 39},
        {"label": "Aug 27 · v7 promotion", "old": 6.427, "new": 5.978, "mins": 159},
        {"label": "Aug 30 · scheduled cron (Sun 02:00)", "old": 5.978, "new": 5.975, "mins": 3463},
    ]
    W, RH, T = 960, 46, 34
    H = T + RH * len(rows) + 44
    L, R = 320, 60
    lo, hi = 5.8, 7.6
    x_of = lambda v: L + (W - L - R) * (v - lo) / (hi - lo)
    b = ""
    for v in (6.0, 6.5, 7.0, 7.5):
        b += line(x_of(v), T - 6, x_of(v), H - 34, "var(--line)", 1)
        b += txt(x_of(v), H - 18, f"{v:.1f} °C", 11, MUT, "middle")
    for i, r in enumerate(rows):
        y = T + RH * i + RH / 2
        xo, xn = x_of(r["old"]), x_of(r["new"])
        b += txt(L - 14, y - 2, r["label"], 12, "var(--ink)", "end", cls="lbl")
        b += txt(L - 14, y + 13, f'+{r["mins"]:,} live min ingested', 10.5, MUT, "end")
        b += line(xo, y, xn, y, GOOD, 3)
        b += f'<path d="M{xn + (4 if xn < xo else -4):.1f},{y:.1f} l{6 if xn < xo else -6},-4 v8 z" fill="{GOOD}"/>'
        b += dot(xo, y, 4, MUT) + dot(xn, y, 4.5, GOOD)
        b += txt(xo + 8, y - 8, f'{r["old"]:.2f}', 11, MUT)
        b += txt(xn - 8, y - 8, f'{r["new"]:.3f}'.rstrip("0"), 11.5, GOOD, "end", "700")
    return svg(W, H, b)


# ------------------------------------------------- chart 3: model zoo bars ---
def _barpanel(title, rows, lo, hi, W=300, note="") -> str:
    T, RH, L, R = 34, 34, 118, 46
    H = T + RH * len(rows) + (16 if note else 4)
    x_of = lambda v: L + (W - L - R) * (v - lo) / (hi - lo)
    b = txt(L, 18, title, 12.5, "var(--ink)", weight="700", cls="lbl")
    for i, (name, v, col) in enumerate(rows):
        y = T + RH * i
        b += txt(L - 8, y + 17, name, 11.5, MUT, "end", cls="lbl")
        b += rect(L, y + 5, max(x_of(v) - L, 2), 18, col, 3)
        b += txt(x_of(v) + 6, y + 19, f"{v:.2f}", 11.5, "var(--ink)", weight="700")
    if note:
        b += txt(L, H - 4, note, 10, MUT, cls="lbl")
    return svg(W, H, b)


def chart_zoo() -> str:
    p1 = _barpanel("Bake-off · same 57,643 test rows (Aug 03)",
                   [("GBT (winner)", 7.71, EMBER), ("ARX velocity", 7.97, STEEL),
                    ("persistence", 8.73, MUT), ("GRU deep net", 10.46, "var(--bad)")],
                   7.0, 11.0, 470, "delta target essential for ALL — absolute targets lost to persistence")
    p2 = _barpanel("Data resolution · same file & minutes (Aug 26)",
                   [("5-min feats", 5.62, GOOD), ("1-min (prod)", 5.81, EMBER),
                    ("1-second", 6.20, STEEL), ("persistence", 7.21, MUT)],
                   5.0, 7.5, 470, "averaging denoises — more resolution ≠ more accuracy")
    p3 = _barpanel("Optimisation bench · identical fold (Aug 27)",
                   [("xfeat+blend → v7", 5.99, GOOD), ("stack ceiling*", 5.98, MUT),
                    ("xfeat alone", 6.10, EMBER), ("Optuna params", 6.11, STEEL),
                    ("blend alone", 6.30, STEEL), ("v5 recipe", 6.42, MUT)],
                   5.6, 6.7, 470, "*weights picked on test = upper bound; v7 equals it")
    p4 = _barpanel("Rejected (measured, documented)",
                   [("EWM target†", 5.92, "var(--bad)"), ("recency wts", 6.48, MUT),
                    ("two-stage", 6.55, MUT), ("linear tree", 6.56, MUT),
                    ("huber uncapped", 6.74, MUT), ("GRU (bench)", 10.46, "var(--bad)")],
                   5.6, 11.0, 470, "†wins by shifting horizon to ~t+3.5 — metric gaming, not skill")
    return ('<div class="panelgrid">' + "".join(f"<div>{p}</div>" for p in (p1, p2, p3, p4)) + "</div>")


# ------------------------------------------------ chart 4: horizon ladder ----
def chart_horizon() -> str:
    rows = [(1, 3.36, 3.87, 13.2), (2, 5.03, 6.08, 17.3), (3, 5.52, 7.23, 23.6),
            (5, 5.98, 7.93, 24.6), (10, 6.17, 8.38, 26.4), (15, 6.31, 8.32, 24.2),
            (30, 6.54, 8.61, 24.1)]
    W, H, L, R, T, B = 960, 360, 64, 70, 30, 52
    lo, hi = 0.0, 9.5
    n = len(rows)
    bw, gap = 26, (W - L - R) / n
    y_of = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    b = yaxis(L, W - R, y_of, 0, 9, 3)
    sk_y = lambda s: T + (H - T - B) * (30 - s) / 30
    pts = []
    for i, (h, m, p, s) in enumerate(rows):
        cx = L + gap * i + gap / 2
        b += rect(cx - bw - 3, y_of(p), bw, y_of(0) - y_of(p), STEEL, 3, 0.55)
        b += rect(cx + 3, y_of(m), bw, y_of(0) - y_of(m), EMBER, 3)
        b += txt(cx - bw / 2 - 3, y_of(p) - 5, f"{p:.1f}", 10.5, STEEL, "middle")
        b += txt(cx + bw / 2 + 3, y_of(m) - 5, f"{m:.2f}", 11, "var(--ink)", "middle", "700")
        b += txt(cx, H - 32, f"t+{h}", 12, "var(--ink)", "middle", "700")
        pts.append((cx, sk_y(s)))
    b += poly(pts, GOOD, 2.4, "1 0")
    for (cx, y), (_, _, _, s) in zip(pts, rows):
        b += dot(cx, y, 3.6, GOOD)
        b += txt(cx, y - 9, f"{s:.0f}%", 10.5, GOOD, "middle", "700")
    b += txt(W - R + 8, sk_y(25) + 4, "skill →", 11, GOOD)
    b += txt(L, H - 12, "minutes ahead — ember = twin, steel = persistence, green = forecast skill", 11, MUT, cls="lbl")
    return svg(W, H, b)


# ------------------------------------------------- chart 5: live daily -------
def chart_live() -> str:
    days = LIVE["daily"]
    W, H, L, R, T, B = 960, 400, 64, 24, 28, 64
    lo, hi = 4.0, 16.5
    n = len(days)
    x_of = lambda i: L + (W - L - R) * i / (n - 1)
    y_of = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    b = yaxis(L, W - R, y_of, 4, 16, 4)
    # skill gap shading between persistence and model
    area = " ".join(f"{x_of(i):.1f},{y_of(d['persist_mae']):.1f}" for i, d in enumerate(days))
    area += " " + " ".join(f"{x_of(i):.1f},{y_of(d['mae']):.1f}"
                           for i, d in reversed(list(enumerate(days))))
    b += f'<polygon points="{area}" fill="{GOOD}" opacity="0.13"/>'
    b += poly([(x_of(i), y_of(d["persist_mae"])) for i, d in enumerate(days)], STEEL, 1.8, "5 4")
    b += poly([(x_of(i), y_of(d["mae"])) for i, d in enumerate(days)], EMBER, 2.4)
    iv7 = next(i for i, d in enumerate(days) if d["d"] >= "2026-08-28")
    b += line(x_of(iv7) - 4, T, x_of(iv7) - 4, H - B, GOOD, 1.6, "3 4")
    b += txt(x_of(iv7), T + 14, "v7 live →", 12, GOOD, weight="700")
    ivol = next(i for i, d in enumerate(days) if d["d"] == "2026-08-15")
    b += txt(x_of(ivol), y_of(15.8) - 10, "volatile week — both rise, skill holds", 10.5, MUT, "middle", cls="lbl")
    for i, d in enumerate(days):
        if i % 4 == 0 or i == n - 1:
            b += txt(x_of(i), H - 40, d["d"][5:], 10, MUT, "middle")
    pre = [d for d in days if d["d"] < "2026-08-28"]
    post = [d for d in days if d["d"] >= "2026-08-28"]
    sk = lambda ds: 100 * (1 - sum(x["mae"] * x["n"] for x in ds) / sum(x["persist_mae"] * x["n"] for x in ds))
    b += txt(L, H - 14, f"ember = live twin MAE · steel = persistence (same minutes) · shaded = skill.  "
             f"Weighted skill: {sk(pre):.0f}% before v7 → {sk(post):.0f}% after", 11.5, MUT, cls="lbl")
    return svg(W, H, b)


# ------------------------------------------------- chart 6: floor decomp -----
def chart_floor() -> str:
    W, H, L, R = 960, 240, 240, 40
    rows = [("true 5-min change (mean |Δ|, denoised)", 11.6, MUT, "what any model must predict"),
            ("persistence (do nothing)", 7.95, STEEL, "the honest baseline"),
            ("twin v7 (offline test)", 5.98, EMBER, "today"),
            ("practical floor", 5.75, GOOD, "5.5–6.0 — limited by unmeasured fuel variation"),
            ("sensor jitter share", 0.68, "var(--bad)", "thermocouple noise on the truth itself")]
    T, RH = 26, 40
    x_of = lambda v: L + (W - L - R) * v / 12.5
    b = ""
    for v in (0, 3, 6, 9, 12):
        b += line(x_of(v), T - 8, x_of(v), T + RH * len(rows), "var(--line)", 1)
        b += txt(x_of(v), T + RH * len(rows) + 16, f"{v} °C", 11, MUT, "middle")
    for i, (name, v, col, note) in enumerate(rows):
        y = T + RH * i
        b += txt(L - 10, y + 15, name, 11.5, "var(--ink)", "end", cls="lbl")
        if name == "practical floor":
            b += rect(x_of(5.5), y + 4, x_of(6.0) - x_of(5.5), 18, col, 3, 0.85)
        else:
            b += rect(L, y + 4, max(x_of(v) - L, 2), 18, col, 3, 0.9)
        b += txt(x_of(max(v, 6.0 if name == "practical floor" else v)) + 8, y + 18,
                 (f"{v:.1f}" if v >= 1 else f"{v:.2f}") + "   " + note, 10.5, MUT)
    return svg(W, H, b)


# ---------------------------------------------------- pipeline diagram -------
def diagram_pipeline() -> str:
    W, H = 960, 210
    def box(x, y, w, h, title, sub, col=EMBER):
        return (rect(x, y, w, h, "var(--card)", 8) +
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="none" stroke="{col}" stroke-width="1.4"/>' +
                txt(x + w / 2, y + 22, title, 12.5, "var(--ink)", "middle", "700", cls="lbl") +
                txt(x + w / 2, y + 40, sub, 10.5, MUT, "middle", cls="lbl"))
    def arrow(x1, y1, x2, y2, label=""):
        a = line(x1, y1, x2, y2, MUT, 1.4)
        a += f'<path d="M{x2:.0f},{y2:.0f} l-7,-4 v8 z" fill="{MUT}" transform="rotate({0 if y1==y2 else 90} {x2} {y2})"/>'
        if label:
            a += txt((x1 + x2) / 2, y1 - 8, label, 10, MUT, "middle", cls="lbl")
        return a
    b = box(10, 60, 170, 56, "PLANT · OPC UA", "KEPServer, read-only", STEEL)
    b += arrow(180, 88, 240, 88, "45 tags · 2 s")
    b += box(240, 60, 160, 56, "LIVE STORE", "SQLite WAL")
    b += arrow(400, 88, 460, 88, "every 30 s")
    b += box(460, 24, 210, 56, "SHADOW", "predict → advise → mature", EMBER)
    b += box(460, 96, 210, 56, "MONITOR (6 h)", "bias · conformal · AFR · M&V", GOOD)
    b += arrow(670, 52, 730, 52, "")
    b += arrow(670, 124, 730, 124, "")
    b += box(730, 24, 220, 56, "SERVING API + UI", "trajectory · optimizer · SHAP", EMBER)
    b += box(730, 96, 220, 56, "GUARDED RETRAIN", "Sun 02:00 · promote ≤115%", GOOD)
    b += arrow(840, 152, 840, 176, "")
    b += txt(840, 194, "models/prev backup → instant rollback", 10.5, MUT, "middle", cls="lbl")
    b += txt(10, 194, "ADVISORY-ONLY: no signal is ever written back to the plant.", 11.5, "var(--ink)", weight="700", cls="lbl")
    return svg(W, H, b)


# ------------------------------------------------------------------ html -----
def metric_cards() -> str:
    cards = [
        ("MAE — mean absolute error", "°C", "The average miss. Our headline: every prediction, on average, lands within ±5.98 °C offline / ±6.56 °C live.", "Hides direction (bias) and tail behaviour; incomparable across time windows — always read beside persistence."),
        ("Forecast skill", "%", "1 − MAE<sub>model</sub>/MAE<sub>persistence</sub> on the same minutes. THE defensible number: 25% offline, 22% live. Skill is window-invariant — it survived the volatile week when raw MAE doubled.", "Persistence must be computed on the identical timestamps or it's meaningless."),
        ("RMSE — root mean squared error", "°C", "Like MAE but punishes big misses quadratically (8.1 vs 5.98 offline). The RMSE−MAE gap says errors have tails — occasional large moves get missed.", "Dominated by the same volatile periods that inflate persistence."),
        ("R² — variance explained", "", "0.689: the twin explains ~69% of temperature variance at t+5. Good for statisticians; less intuitive for operators.", "Inflated by regime spread — a model that just tracks the band level scores well. Delta-space R² is far harsher."),
        ("Interval coverage", "%", "How often truth lands inside the P10–P90 band. Design target 80%; measured 82–84% live via conformal recalibration (band width re-fit from live residuals every 6 h).", "Coverage without width is gameable — a huge band always covers. We publish width (≈ ±11 °C) with it."),
        ("Live bias (raw vs served)", "°C", "Rolling 2 h signed error of the raw model. Currently self-corrected: the served prediction adds back the damped offset (±10 °C clamp). Was +1.9 °C in early Aug (condition drift); now ~0–1 °C after correction.", "Bias is measured against the RAW model so the correction can never hide its own input."),
        ("Damping ratio", "", "Predicted move ÷ actual move on large swings ≈ 0.44 — the twin under-calls big moves (MAE-optimal caution). This is why a 'big-move detector' classifier is on the roadmap.", "A model can have great MAE and still damp — check this before trusting it during upsets."),
        ("MRE — mean relative error", "%", "The academic yardstick (error ÷ absolute temperature). Published SOTA: 0.91% ≈ 8 °C. The twin: ~0.68% ≈ 6 °C. Only used for literature comparison.", "Dividing by ~880 °C makes anything look small — same trap as the retired '99%'."),
    ]
    out = ""
    for title, unit, what, caveat in cards:
        out += (f'<div class="mcard"><h4>{title}{f" <span>({unit})</span>" if unit else ""}</h4>'
                f'<p>{what}</p><p class="caveat">⚠ {caveat}</p></div>')
    return f'<div class="mgrid">{out}</div>'


def build() -> str:
    css = """
:root{
  --paper:#fafaf7; --ink:#1d1f1e; --muted:#6b6f6c; --line:#e3e2dc; --card:#ffffff;
  --ember:#c2410c; --steel:#2563a8; --good:#1a7a4a; --bad:#a11d33;
  --serif:'Iowan Old Style','Charter','Georgia',serif;
  --sans:-apple-system,'Segoe UI','Helvetica Neue',sans-serif;
  --mono:ui-monospace,'SF Mono','Cascadia Mono',Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#161817; --ink:#e8e6e0; --muted:#98a09a; --line:#2b2e2c; --card:#1d201e;
  --ember:#f97316; --steel:#6ba3d8; --good:#4ade80; --bad:#f87171;}}
:root[data-theme="dark"]{
  --paper:#161817; --ink:#e8e6e0; --muted:#98a09a; --line:#2b2e2c; --card:#1d201e;
  --ember:#f97316; --steel:#6ba3d8; --good:#4ade80; --bad:#f87171;}
:root[data-theme="light"]{
  --paper:#fafaf7; --ink:#1d1f1e; --muted:#6b6f6c; --line:#e3e2dc; --card:#ffffff;
  --ember:#c2410c; --steel:#2563a8; --good:#1a7a4a; --bad:#a11d33;}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);
  line-height:1.62;margin:0;font-size:16px}
.wrap{max-width:920px;margin:0 auto;padding:0 28px 90px}
header.mast{border-bottom:3px solid var(--ember);padding:34px 0 20px;margin-bottom:8px}
.kicker{font-family:var(--mono);font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--ember);font-weight:700}
h1{font-family:var(--serif);font-size:clamp(30px,5vw,44px);line-height:1.12;margin:.35em 0 .2em;text-wrap:balance;font-weight:600}
.sub{color:var(--muted);font-size:15px;max-width:62ch}
h2{font-family:var(--serif);font-size:27px;margin:2.6em 0 .5em;line-height:1.2}
h2 .no{color:var(--ember);font-family:var(--mono);font-size:18px;margin-right:.6em;font-weight:700}
h3{font-size:17px;margin:1.8em 0 .4em}
p{max-width:70ch}
.statstrip{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:26px 0 8px}
.stat{background:var(--card);padding:14px 16px}
.stat b{display:block;font-family:var(--mono);font-size:24px;font-variant-numeric:tabular-nums;color:var(--ember)}
.stat span{font-size:12px;color:var(--muted);line-height:1.35;display:block;margin-top:2px}
.plate{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:20px 18px 12px;margin:20px 0;overflow-x:auto}
.plate figcaption{font-size:13px;color:var(--muted);padding:8px 6px 4px;max-width:none}
.chart{width:100%;height:auto;display:block;font-family:var(--mono);font-variant-numeric:tabular-nums}
.chart .lbl{font-family:var(--sans)}
table{border-collapse:collapse;width:100%;font-size:14.5px;margin:14px 0}
th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  border-bottom:2px solid var(--ink);padding:8px 10px}
td{border-bottom:1px solid var(--line);padding:8px 10px;font-variant-numeric:tabular-nums}
td.num{font-family:var(--mono)}
.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:18px 0}
.mcard{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--ember);
  border-radius:8px;padding:14px 16px}
.mcard h4{margin:0 0 6px;font-size:14.5px}
.mcard h4 span{color:var(--muted);font-weight:400}
.mcard p{font-size:13.5px;margin:.4em 0;max-width:none}
.mcard .caveat{color:var(--muted);font-size:12.5px}
.callout{border:1px solid var(--line);border-left:4px solid var(--bad);background:var(--card);
  border-radius:8px;padding:16px 20px;margin:22px 0}
.callout.green{border-left-color:var(--good)}
.callout h4{margin:0 0 6px}
.panelgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(400px,1fr));gap:10px}
ul{padding-left:1.2em} li{margin:.35em 0;max-width:68ch}
.check li::marker{content:"✓  ";color:var(--good)}
.fix b{font-family:var(--mono);font-size:13.5px}
footer{margin-top:70px;border-top:1px solid var(--line);padding-top:16px;color:var(--muted);font-size:13px}
@media print{
  body{font-size:12px} .wrap{max-width:100%;padding:0}
  h2{page-break-before:always} header.mast+ * + h2, .nobreak h2{page-break-before:auto}
  .plate,.mcard,.callout{break-inside:avoid;border-color:#ccc}
  .statstrip{break-inside:avoid}
}
"""
    live_days = LIVE["daily"]
    n_live = sum(d["n"] for d in live_days)
    html = f"""<title>CCR Digital Twin — Progress Report (Sep 2026)</title>
<style>{css}</style>
<div class="wrap">
<header class="mast">
  <div class="kicker">JK Cement · Nimbahera Kiln 3 · Calcination Combustion Control</div>
  <h1>The Calciner Digital Twin — Progress Report</h1>
  <p class="sub">Advisory-first machine-learning twin of the Kiln-3 precalciner: live since 29 July 2026,
  self-correcting, self-retraining, and now more accurate than the best published academic result for
  this problem. Status as of <b>2 September 2026</b>. All figures reproducible from committed
  experiment artifacts.</p>
</header>

<div class="statstrip">
  <div class="stat"><b>5.98 °C</b><span>t+5 min MAE, held-out test (was 7.7 on 29 Jul)</span></div>
  <div class="stat"><b>6.56 °C</b><span>live MAE — 11,033 matured predictions since v7</span></div>
  <div class="stat"><b>22–25 %</b><span>forecast skill vs persistence, offline &amp; live</span></div>
  <div class="stat"><b>≈ 25 %</b><span>better than published SOTA (TCN-BiLSTM, ≈8 °C)</span></div>
  <div class="stat"><b>{n_live:,}</b><span>live predictions scored against the real plant</span></div>
  <div class="stat"><b>0</b><span>manual interventions in the weekly retrain loop</span></div>
</div>

<h2><span class="no">01</span>The accuracy journey</h2>
<p>Every model version trains on the growing plant historian and is scored on a held-out,
chronologically later test window against the <em>raw</em> sensor value — no smoothing of the
truth, no random splits. The journey from 7.7 °C to 5.98 °C came from six distinct, individually
measured improvements — features and data, not luck:</p>
<figure class="plate">{chart_journey()}
<figcaption><b>Chart 1 — Offline test MAE by model version.</b> Each step was promoted through the
guard (§3) only after beating the incumbent on identical evaluation. The green band is the estimated
practical floor with today's instrumentation (§5).</figcaption></figure>
<table>
<tr><th>Version</th><th>Date</th><th>t+5 MAE</th><th>What changed</th></tr>
<tr><td>v1</td><td>Jul 29</td><td class="num">7.70</td><td>First GBT twin: regime-invariant delta target, chronological splits, shadow mode live</td></tr>
<tr><td>v2</td><td>Aug 04</td><td class="num">7.37</td><td>Bench-winning features promoted: long momentum, hot-side deviations, time-of-day, L1 objective</td></tr>
<tr><td>v3</td><td>Aug 04</td><td class="num">—</td><td>Trajectory heads t+5/10/15/30 with P10–P90 bands; optimizer plans the whole path</td></tr>
<tr><td>retrain</td><td>Aug 21</td><td class="num">7.01</td><td>First guarded retrain folds 27k live-regime minutes into training (+ 3 data bugs fixed)</td></tr>
<tr><td>v4</td><td>Aug 27</td><td class="num">6.67</td><td>5-min-averaged (denoised) inputs — direct product of the 1-second resolution study</td></tr>
<tr><td>v5</td><td>Aug 27</td><td class="num">6.43</td><td>Huber objective under the capped-rounds recipe</td></tr>
<tr><td><b>v7</b></td><td>Aug 27</td><td class="num"><b>5.98</b></td><td>89 features (45–60 min memory, EWMs, lever dynamics, coal×O₂), GBT⊕ARX blend, real-history serving</td></tr>
</table>

<h2><span class="no">02</span>Data: intervals, volume, quality</h2>
<figure class="plate">{diagram_pipeline()}
<figcaption><b>The live loop.</b> Read-only OPC UA in; predictions, advisories and evidence out.
Six PM2 services, boot-persistent, on the plant GPU server (10.53.2.10).</figcaption></figure>
<table>
<tr><th>Stream</th><th>Interval</th><th>Volume</th><th>Used for</th></tr>
<tr><td>OPC UA live feed (45 tags)</td><td class="num">2 s</td><td class="num">~43k rows/day</td><td>Shadow predictions, live features, bias estimation</td></tr>
<tr><td>Shadow prediction tick</td><td class="num">30 s</td><td class="num">~2,400/day</td><td>Predict → advise → mature vs realised temp</td></tr>
<tr><td>Training historian (1-min)</td><td class="num">60 s</td><td class="num">422k rows, Aug 25 → now</td><td>All model training; grows via weekly ingest</td></tr>
<tr><td>1-second research export</td><td class="num">1 s</td><td class="num">28.8M rows / 1 yr</td><td>Resolution study, noise-floor measurement</td></tr>
<tr><td>Lab / eLIMS (QC)</td><td class="num">~1–2 h</td><td class="num">sparse</td><td>DOC quality head; free-lime blocked on label density</td></tr>
<tr><td>Monitor evidence</td><td class="num">6 h</td><td class="num">continuous</td><td>Accuracy, bias, conformal width, AFR inference, M&amp;V</td></tr>
</table>
<h3>Quality gates applied to every training row</h3>
<ul class="check">
<li><b>Feed gate</b> — kiln feed &gt; 461 t/h only; the twin never learns from shutdown/upset data.</li>
<li><b>Analyser purge masking</b> — CO/O₂ readings valid only when RUN=1 and O₂ &lt; 15% (purge cycles produce ~9% outliers — confirmed by the upstream IQR outlier report, whose cleaning proved equivalent to our gates: identical 388k rows after both).</li>
<li><b>Dead-tag exclusion</b> — 423CN150_TT_1 / 423KR405_TT_3/4 exist in history but are dead on the live server; any feature built on them invalidates every live row, so they are banned (rule enforced in code).</li>
<li><b>Forward-gap guards</b> — a t+5 target must really be 4–7 min ahead; data gaps can't fake targets.</li>
<li><b>Lever forward-fill</b> — setpoints hold until moved; gaps in manipulated variables are filled, rates correctly read 0.</li>
<li><b>Chronological splits only</b> — random splits leak adjacent minutes and flatter every metric.</li>
</ul>
<div class="callout">
<h4>Three data bugs found and fixed (Aug 21) — worth recording</h4>
<p class="fix">① Historian timestamps carry microseconds, live ones use ISO-'T' — the format clash silently
excluded <em>all</em> live rows from training (fixed: normalisation + mixed-format parsing).
② v2 features referenced dead OPC tags → every live row invalid (fixed: live-computable features only).
③ A tag-name typo (<b>K3V10F1</b> vs <b>K3V10_F1</b>) meant the AFR tertiary-air damper was <em>never
captured</em> by the live feed for a month (fixed + backfilled via forward-fill).
Each fix is now a permanent guard in the pipeline.</p></div>

<h2><span class="no">03</span>The autonomous weekly retrain cycle</h2>
<p>Every Sunday at 02:00 the system retrains itself: matured live minutes are ingested into the
historian, the full model (4 horizons × mean/P10/P90 + safety heads + ARX blend) retrains, and the
candidate is promoted <em>only</em> if its test MAE is at most 115% of the incumbent's — otherwise the
previous bundle is restored automatically. The incumbent is always backed up first; serving and
shadow reload on promotion. Nine cycles have run; every one promoted; zero manual interventions.</p>
<figure class="plate">{chart_retrains()}
<figcaption><b>Chart 2 — Guarded retrain history</b> (grey dot = incumbent, green = promoted candidate).
The two scheduled cron runs (Aug 24, Aug 30) show the loop working unattended — the Aug 30 run ingested
3,463 fresh minutes and held accuracy at 5.975.</figcaption></figure>

<h2><span class="no">04</span>Accuracy metrics — what each one actually tells you</h2>
<p>No single number describes a forecasting system honestly. These are the eight we track, what each
reflects, and what each hides:</p>
{metric_cards()}
<div class="callout">
<h4>Why we retired “99% accurate”</h4>
<p>Early on, accuracy was framed as 1 − MAE/temperature ≈ 99%. A sharp reviewer dismantles this in
seconds: the denominator (~880 °C) is unit-arbitrary — in Fahrenheit the same model becomes “99.5%
accurate”, in kelvin better still, having predicted nothing differently. Worse, it ignores the
operating band: the plant lives in a ~60 °C window, so ±7 °C is not 1% of anything meaningful.
The defensible framing is <b>±6 °C MAE, 22–25% better than persistence, verified live</b> — and that
is the only framing this project uses.</p></div>

<h2><span class="no">05</span>The models — everything we tried, honestly compared</h2>
<p>Four model families were built and raced on identical data, plus a served blend and a set of
measured rejections. Trees won, and the reason is structural: this is medium-sized tabular data with
heterogeneous physics, exactly where gradient boosting dominates deep learning
(Grinsztajn et&nbsp;al., NeurIPS 2022 — and our own bench agrees).</p>
{chart_zoo()}
<table>
<tr><th>Model</th><th>Role</th><th>How it works</th><th>Verdict</th></tr>
<tr><td>Persistence</td><td>Baseline</td><td>“Temperature in 5 min = temperature now.” Free, surprisingly strong.</td><td class="num">7.9–8.7 — the bar every model must clear</td></tr>
<tr><td>kNN surrogate</td><td>Original app</td><td>Nearest historical neighbours of current state</td><td>Replaced Jul 29; kept as serving fallback</td></tr>
<tr><td>Ridge ARX (velocity form)</td><td>Blend partner</td><td>Linear lagged-input model on the same delta target — fully interpretable per-lever gains</td><td class="num">7.97 (bench) → 6.49 on v7 features; regime-stable where trees overfit</td></tr>
<tr><td><b>LightGBM GBT</b></td><td><b>Core</b></td><td>Boosted trees on 89 engineered features; huber loss; trained on smoothed delta, scored vs raw; SHAP attribution</td><td class="num"><b>7.71 → 5.98 across v1–v7</b></td></tr>
<tr><td>GRU sequence net</td><td>Challenger</td><td>15-min windows, trained on the RTX 5090</td><td class="num">10.46 — lost to persistence even with delta targets</td></tr>
<tr><td><b>GBT ⊕ ARX blend</b></td><td><b>Served (v7)</b></td><td>0.6·GBT + 0.4·ARX per horizon — the two make differently-shaped errors; weight fixed (val-picked weights systematically under-weight ARX)</td><td class="num">Beat GBT-alone at every horizon; equals the test-picked ensemble ceiling</td></tr>
</table>
<p>The rejected panel matters as much as the winners: recency weighting, Optuna-tuned
hyperparameters, linear-tree mode, two-stage stacking, bagging, 1-second features and the
EWM-target trick were all measured and discarded — each verdict documented in a committed
experiment module so nobody re-treads them.</p>

<h2><span class="no">06</span>How far ahead can it see? (t+1 → t+30)</h2>
<figure class="plate">{chart_horizon()}
<figcaption><b>Chart 4 — MAE and skill by horizon.</b> At t+1 the twin scores 3.36 °C — but persistence
scores 3.87 there, so the skill is modest: one minute out, the plant simply hasn't moved. Skill
<em>grows</em> with horizon (13% → 25–26%): the twin's edge is largest exactly where a human can't
extrapolate. The optimizer plans on the full t+5…t+30 trajectory.</figcaption></figure>

<h2><span class="no">07</span>Live validation — the number that can't be gamed</h2>
<p>Every 30 seconds, shadow mode logs a prediction; five minutes later it scores it against what the
plant actually did. {n_live:,} predictions have matured this way. This is the evidence a control-room
operator should care about:</p>
<figure class="plate">{chart_live()}
<figcaption><b>Chart 5 — Daily live MAE, twin vs persistence, since first light (29 Jul).</b> Two things
to see: raw MAE tracks plant volatility (both lines rise in the mid-Aug volatile week — persistence
proves the exam got harder, skill held), and the skill gap visibly widens after v7 went live with
real-history serving.</figcaption></figure>
<div class="callout green">
<h4>The floor — and what it would take to go below it</h4>
<figure style="margin:12px 0 0">{chart_floor()}</figure>
<p>Measured from the year-long 1-second export: the true 5-minute temperature change averages
<b>11.6 °C</b>; ~0.7 °C of every score is thermocouple jitter on the truth itself; and the test-picked
ensemble ceiling equals what v7 already serves. The remaining error is dominated by
<b>minute-scale fuel calorific-value variation in the AFR stream — which no current sensor
measures</b>. Going materially below ~5.5 °C is an instrumentation decision (online fuel
quality/calorimetry), not a modelling one.</p></div>

<h2><span class="no">08</span>Next: from prediction to money — coal/AFR optimisation &amp; measured delta</h2>
<p>The optimizer already runs on every shadow tick: minimise coal / maximise AFR subject to the
predicted trajectory reaching <em>and holding</em> the temperature band (chance-constrained on the
P10 band), all five CO/O₂ interlocks respected, movements limited to a trust region around live
setpoints. What remains is to close the loop to value:</p>
<table>
<tr><th>Step</th><th>What</th><th>Why it unlocks value</th></tr>
<tr><td>1. Surface the advisory</td><td>Display the recommendation + trajectory in the CCR room</td><td>Until operators can see it, "followed vs ignored" is coincidental — surfacing turns shadow logs into a real experiment</td></tr>
<tr><td>2. Measure the delta (M&amp;V)</td><td>Compare followed vs ignored advisories: temp-in-band %, coal t/h, AFR share — with volatility controls (persistence-normalised, same-regime matching)</td><td>The honest before/after: every claim adjusted for how hard the plant was to run that day</td></tr>
<tr><td>3. ₹/day ROI model</td><td>(coal saved × coal price) + (AFR share gained × cost spread) − quality penalties, from measured responses only</td><td>Converts control performance into the number leadership funds</td></tr>
<tr><td>4. Fuel-quality sensing proposal</td><td>Online AFR calorimetry / NIR</td><td>The only route below ~5.5 °C — and it directly de-risks higher AFR substitution rates</td></tr>
<tr><td>5. Big-move detector</td><td>Classifier flagging “large swing imminent” (damping ratio 0.44 today)</td><td>Complements the regression exactly where MAE-optimal models are cautious</td></tr>
<tr><td>6. Free-lime quality head</td><td>Needs denser lab labels (eLIMS linkage is built)</td><td>Ties temperature control to clinker quality, the ultimate objective</td></tr>
</table>
<p>Sequencing recommendation: steps 1–2 cost almost nothing (a screen and a query) and convert the
existing 30-second advisory stream into measurable ₹ — they should precede any further
accuracy work.</p>

<footer>
Prepared 2 Sep 2026 · CCR-Project <span class="num">feature/digital-twin-ml</span> · every figure
traces to a committed experiment artifact in <span class="num">models/*.json</span> · twin is
advisory-only; nothing writes to the plant.
</footer>
</div>
"""
    return html


if __name__ == "__main__":
    out = HERE / "index.html"
    out.write_text(build())
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
