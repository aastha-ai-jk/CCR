"""Render the guardrail implementation note as a PDF.

    python docs/make_guardrail_pdf.py

Output: docs/Guardrail_Implementation.pdf

Same conventions as the other generators here: Helvetica / Courier only,
ASCII-safe text, the flow diagram drawn as vector geometry (the base-14 fonts
have no box-drawing glyphs), and build-time checks for non-renderable
characters, for bare angle brackets (reportlab Paragraph parses mini-HTML and
silently drops "<tag>"), and for a diagram drawn outside its own box.
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether,
                                PageTemplate, Paragraph, Preformatted, Spacer,
                                Table, TableStyle)

OUT = Path(__file__).resolve().parent / "Guardrail_Implementation.pdf"

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#4b5563")
RULE = colors.HexColor("#9ca3af")
BAND = colors.HexColor("#e5e7eb")
ACCENT = colors.HexColor("#1e3a8a")
GOOD = colors.HexColor("#15803d")
STOP = colors.HexColor("#b91c1c")
AMBER = colors.HexColor("#b45309")

LIMITS = [
    ("Calciner outlet temperature", "423CI400_TT_4", "820", "951", "degC",
     "live reading AND the twin's predicted temperature"),
    ("Calciner coal firing (total)", "467RL400_TPH", "15", "34.90", "t/h", "live AND recommended"),
    ("LAFR feed rate PC (precalciner)", "LAFR_FT", "0", "3,928", "l/h", "live AND recommended"),
    ("LAFR feed rate TAD (tertiary air duct)", "K3V10_F1", "0", "3,517", "l/h", "live AND recommended"),
    ("SAFR feed rate (solid AFR)", "261WF080_TPH", "0", "26.50", "t/h", "live AND recommended"),
]

BEHAVIOUR = [
    ("Alarm banner", "A red banner appears at the top of the screen and lists every value that is "
     "out of limit, for example 'LAFR feed rate PC - recommended 4,100 l/h is above the roof "
     "3,928 l/h'. It is sticky, so it stays visible while the operator scrolls."),
    ("Beep", "Two short 880 Hz pulses every 2 seconds while any breach is active."),
    ("Accept blocked", "The Accept button of the breached parameter is greyed out, the row turns red "
     "and is tagged ABOVE ROOF (or BELOW FLOOR), and the reason is written under the buttons. On "
     "the Digital Twin page one Accept covers the whole move, so any breach blocks it."),
    ("Decline still works", "Declining is never blocked: an operator must always be able to say no "
     "and log a reason."),
    ("Silence alarm", "Stops the beep for the breaches present now. The banner stays until every "
     "value is back inside its limit, and a NEW breach beeps again (silence is not acknowledge-all)."),
    ("Clears by itself", "When all values are back inside their limits the banner disappears and "
     "Accept is enabled again. No operator action is needed."),
    ("No live data", "If the live feed stops, nothing is judged (no false alarm on old data) and "
     "Accept is refused - the system will not accept what it cannot check."),
]

RULES = [
    ("What is checked", "For each configured tag: the LIVE value (OPC feed, every 2 s) and the "
     "RECOMMENDED value (the optimizer's setpoint; for the calciner outlet temperature, the twin's "
     "predicted temperature)."),
    ("Breach", "value greater than max (above_roof), or value less than min (below_floor). Either "
     "the live or the recommended value breaching is enough."),
    ("At the limit", "A value exactly equal to a limit is NOT a breach. So a fuel at exactly 0 "
     "(pump off) is allowed; only a negative reading breaches a 0 floor."),
    ("Missing data", "A missing, non-numeric or NaN value never counts as a breach - no live "
     "reading means nothing to judge, not an alarm."),
    ("Stale data", "Live values are judged only while the feed is current. When it stops, the "
     "backend falls back to the newest historical row; that row, and any recommendation computed "
     "from it, is NOT judged - otherwise months-old values would sound the alarm."),
    ("Fail safe", "If the server has neither a current live value nor a current recommendation "
     "for the parameter, an Accept is refused (HTTP 409). Values sent by the page are checked for "
     "breaches but never count as verification on their own."),
    ("Live re-read", "data/plant_settings.json is re-read on every request, so an edited limit takes "
     "effect immediately without restarting anything."),
]

FILES = [
    ("data/plant_settings.json", "edited",
     "New 'guardrails' section: the single source of truth for every floor and roof."),
    ("data/plant_settings.README.md", "edited", "Documents the new key next to the existing ones."),
    ("backend/guardrails.py", "NEW",
     "limits() reads the settings; evaluate() checks live and recommended values; "
     "refuse_accept() returns the refusal reason used by the decision endpoints."),
    ("backend/main.py", "edited",
     "/api/twin/advisor and /api/advisory return a 'guardrails' block judged on CURRENT data only. "
     "/api/decisions and /api/advisory/decision refuse an Accept with HTTP 409 when anything is out "
     "of limit, or when there is no current data to check (fail safe)."),
    ("backend/static/ccr-kiln3-advisor.html", "edited",
     "The /advisor page: alarm banner, beep, Silence button, red rows, disabled per-row Accept."),
    ("frontend/components/GuardrailAlarm.tsx", "NEW",
     "Reusable alarm banner + beep for the Next.js app, and guardItems() which turns the "
     "backend's verdict into alarm lines."),
    ("frontend/app/twin/page.tsx", "edited",
     "Shows the alarm at PAGE level so it sounds even when the recommendation panel is closed; "
     "the panel button reads 'Accept blocked - guardrail'."),
    ("frontend/components/Advisor.tsx", "edited",
     "Disables 'Accept this move', marks breached rows, shows the reason; surfaces server refusals."),
    ("frontend/lib/api.ts", "edited",
     "POST errors now carry the server's reason (e.g. the guardrail refusal) instead of a bare status."),
]

TESTS = [
    # ---- roofs (first round) ----
    ("ROOF - logic", "8 cases: at-roof allowed, just-above breach, live-only breach, NaN ignored, "
     "refuse / allow, untracked tag, floor mechanism.", "8 / 8 pass"),
    ("ROOF - forced breach", "Roofs temporarily lowered below live OPC values (coal 30 t/h, LAFR PC "
     "1,000 l/h) from a backup copy.", "breach reported"),
    ("ROOF - API refusal", "POST /api/decisions (LAFR_FT) and POST /api/advisory/decision with a "
     "breached move.", "HTTP 409 both, nothing saved"),
    ("ROOF - screens", "/advisor and /twin (panel closed and open): banner, ABOVE ROOF tag, Accept "
     "disabled, Decline enabled. 3 beeps in 6.5 s; after Silence 0.", "pass"),
    # ---- floors (second round) ----
    ("FLOOR - logic", "12 cases with the limits of 21 Oct (temperature floor then 850): 850.0 / 15.0 "
     "exactly at floor allowed; 849.9 "
     "and 14.99 breach; LAFR / TAD at 0 allowed; negative reading breaches; roof still works; normal "
     "values clean; Accept refused below floor.", "12 / 12 pass"),
    ("FLOOR 820 - logic", "After the temperature floor was lowered to 820 (23 Oct 2026), 8 cases with "
     "the live settings file: 820.0 allowed, 819.9 breach, 839.9 and 849.9 now allowed, 951.0 allowed, "
     "951.1 breach, predicted 819.5 breach, predicted 830 allowed; Accept refused at 819, allowed at "
     "825.", "8 / 8 pass"),
    ("FLOOR - real data check", "Raw live OPC history (35,844 rows, Jul-Oct 2026): no negative flow "
     "reading on LAFR PC (min 1.88) or SAFR (min 0.07), so the 0 floors will not false-alarm.",
     "pass"),
    ("FLOOR - forced breach", "Floors temporarily raised above current values (temperature 900 degC, "
     "SAFR 20 t/h). Run on REPLAY data - the OPC server was unreachable from the test laptop.",
     "breach reported"),
    ("FLOOR - API refusal", "POST /api/decisions (SAFR) and POST /api/advisory/decision "
     "(temperature below floor).", "HTTP 409 both"),
    ("FLOOR - /advisor page", "Temperature and SAFR rows tagged BELOW FLOOR, Accept disabled, "
     "Decline enabled, other rows untouched; 4 beeps in 6.5 s.", "pass"),
    ("FLOOR - /twin page", "Panel closed: banner with 4 floor breaches, button 'Accept blocked - "
     "guardrail'. Panel open: Accept disabled, SAFR BELOW FLOOR, reason shown.", "pass"),
    # ---- found during floor testing and fixed ----
    ("Stale data (fix)", "Feed down, forced floors still set: the backend previously judged the "
     "July fallback row. After the fix both advisory APIs report no breach on old data.", "pass"),
    ("Fail safe (fix)", "Feed down: an Accept with no current data was accepted before the fix. "
     "After: both Accept endpoints return 409 and the decision count is unchanged.", "pass"),
    ("Recovery", "Real limits restored from backup (checksum identical); no breach on current "
     "values; replay feed stopped.", "pass"),
]

OPEN = [
    ("Floor alarms during kiln stops",
     "Over the last year the calciner outlet was below the 820 degC floor in 12.02% of ALL minutes "
     "(0.12% of production minutes, kiln feed above 461 t/h; at the earlier 850 floor it was 0.46%), "
     "and calciner coal below 15 t/h in 16.24% of all minutes (2.80% of "
     "production). So the alarm will sound for as long as the kiln is stopped or at low load. "
     "Recommended: suppress guardrail alarms while kiln feed is at or below 461 t/h - the advisor's "
     "own applicability rule. A plant decision; not implemented."),
    ("The live feed can stall silently",
     "Seen during testing: the feed process stayed alive but wrote nothing after an OPC session "
     "timeout. The stale-data fix stops false alarms and the fail-safe stops blind Accepts, but the "
     "CCR should also see 'feed stale' prominently, and the feed should restart itself."),
    ("The roofs equal the plant's P99",
     "All five values are exactly the 99th percentile of plant history (questionnaire, sheet 5), so "
     "the plant itself has run above them about 1% of the time. Confirm with the CCR operators that "
     "these are the physical limits they want, or replace them - one line per parameter."),
    ("Corrective moves are blocked too",
     "If a live value is already outside a limit and the advisor recommends a value BACK inside it "
     "(for example more coal when the temperature is below 820), Accept is still blocked, because "
     "the rule is 'any value beyond a limit'. This can be relaxed to allow a recommendation that is "
     "inside the limit and moves toward it - a plant decision."),
    ("Browser sound policy",
     "Browsers only play sound after someone has clicked the page once; until then the banner says "
     "'Click anywhere on this page to enable the alarm sound'. For an unattended CCR display, start "
     "Chrome with --autoplay-policy=no-user-gesture-required."),
    ("Not yet on the edge box",
     "Implemented and verified on the development machine. The edge deployment (10.4.2.10, run by "
     "the 'ccr' account) runs its own copy and needs this change deployed. Committed to git "
     "branch prakhar/integration on 23 Oct 2026."),
    ("Optimizer search is not yet capped",
     "The guardrail alarms and blocks; it does not stop the optimizer from proposing a value outside "
     "the limits. Setting optimizer.levers.&lt;tag&gt;.min / .max to the same values would prevent "
     "such recommendations at source."),
    ("A test Accept was logged and undone",
     "While finding the fail-safe gap, one SAFR Accept was recorded in the LOCAL decision log "
     "(21 Oct 12:44). It was immediately reversed through the API as 'undone' with an explanatory "
     "note, so the record stays truthful. The edge box was not touched."),
]

SETTINGS_SNIPPET = """"guardrails": {
  "423CI400_TT_4": {"min": 820, "max": 951},
  "467RL400_TPH":  {"min": 15,  "max": 34.90},
  "LAFR_FT":       {"min": 0,   "max": 3928},
  "K3V10_F1":      {"min": 0,   "max": 3517},
  "261WF080_TPH":  {"min": 0,   "max": 26.50}
}"""

REFUSAL_SNIPPET = """HTTP 409 Conflict      -- a limit is breached
{"detail": "Refused by guardrail: 261WF080_TPH live value 8.4 is below
 the floor 20; 261WF080_TPH recommended value 4.52 is below the floor
 20. Accept is blocked while a value is outside its limit; Decline is
 still allowed."}
(forced-breach test, SAFR floor temporarily set to 20 t/h)

HTTP 409 Conflict      -- fail safe, nothing current to check
{"detail": "Refused by guardrail: there is no current live value or
 recommendation for 261WF080_TPH to check against its limits (is the
 live feed down?). Accept is blocked until live data returns; Decline
 is still allowed."}"""

PAYLOAD_SNIPPET = """"guardrails": {
  "any_breach": true,
  "breached": ["LAFR_FT"],
  "params": {
    "LAFR_FT": {"min": 0, "max": 3928, "live": 4012.0,
                "recommended": 4100.0, "live_breach": "above_roof",
                "recommended_breach": "above_roof", "breach": true},
    ...
  }
}"""


# ----------------------------------------------------------------- diagram ---
class FlowDiagram(Flowable):
    """Settings -> guardrails.py -> (advisory APIs -> screens) and (decision APIs -> 409)."""

    W = 170 * mm
    H = 98 * mm      # stack: 2 top + 4 boxes x 15 + gaps 9/13/11 = 95 mm, +3 margin

    def wrap(self, aw, ah):
        return self.W, self.H

    def _box(self, x, y, w, h, title, sub="", fill=colors.white, edge=RULE, tcol=INK):
        c = self.canv
        c.setFillColor(fill)
        c.setStrokeColor(edge)
        c.setLineWidth(0.8)
        c.roundRect(x, y, w, h, 2 * mm, stroke=1, fill=1)
        c.setFillColor(tcol)
        c.setFont("Helvetica-Bold", 8.4)
        if sub:
            c.drawCentredString(x + w / 2, y + h - 5.6 * mm, title)
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 7)
            for i, line in enumerate(sub.split("\n")):
                c.drawCentredString(x + w / 2, y + h - (9.4 + 3.4 * i) * mm, line)
        else:
            c.drawCentredString(x + w / 2, y + h / 2 - 1.1 * mm, title)

    def _arrow(self, x1, y1, x2, y2, col=RULE, label=""):
        c = self.canv
        c.setStrokeColor(col)
        c.setLineWidth(0.9)
        c.line(x1, y1, x2, y2)
        a = math.atan2(y2 - y1, x2 - x1)
        s = 2.1 * mm
        c.setFillColor(col)
        p = c.beginPath()
        p.moveTo(x2, y2)
        p.lineTo(x2 - s * math.cos(a - 0.42), y2 - s * math.sin(a - 0.42))
        p.lineTo(x2 - s * math.cos(a + 0.42), y2 - s * math.sin(a + 0.42))
        p.close()
        c.drawPath(p, stroke=0, fill=1)
        if label:
            c.setFillColor(MUTED)
            c.setFont("Helvetica", 6.8)
            c.drawString((x1 + x2) / 2 + 1.5 * mm, (y1 + y2) / 2 - 1 * mm, label)

    def draw(self):
        bh = 15 * mm
        W = self.W
        top = self.H - 2 * mm
        # row 1: settings
        sw = 92 * mm
        sx = (W - sw) / 2
        y1 = top - bh
        self._box(sx, y1, sw, bh, "data/plant_settings.json  ->  \"guardrails\"",
                  "single source of truth: max / min per tag\nre-read on every request (no restart)",
                  fill=colors.HexColor("#eef2ff"), edge=ACCENT, tcol=ACCENT)
        # row 2: guardrails.py
        y2 = y1 - 9 * mm - bh
        self._arrow(W / 2, y1, W / 2, y2 + bh)
        self._box(sx, y2, sw, bh, "backend/guardrails.py",
                  "evaluate(live, recommended)  |  refuse_accept(tag, values)\nbreach = value > max, or value < min",
                  fill=colors.HexColor("#fef3c7"), edge=AMBER, tcol=AMBER)
        # row 3: two API groups
        cw = 78 * mm
        lx, rx = 0.0, W - cw
        y3 = y2 - 13 * mm - bh
        midy = y2 - 6 * mm
        c = self.canv
        c.setStrokeColor(RULE)
        c.setLineWidth(0.9)
        c.line(W / 2, y2, W / 2, midy)
        c.line(lx + cw / 2, midy, rx + cw / 2, midy)
        self._arrow(lx + cw / 2, midy, lx + cw / 2, y3 + bh)
        self._arrow(rx + cw / 2, midy, rx + cw / 2, y3 + bh)
        self._box(lx, y3, cw, bh, "ADVISORY APIs  -  report",
                  "GET /api/twin/advisor\nGET /api/advisory   (+ 'guardrails' block)")
        self._box(rx, y3, cw, bh, "DECISION APIs  -  enforce",
                  "POST /api/decisions\nPOST /api/advisory/decision",
                  fill=colors.HexColor("#fee2e2"), edge=STOP, tcol=STOP)
        # row 4
        y4 = y3 - 11 * mm - bh
        self._arrow(lx + cw / 2, y3, lx + cw / 2, y4 + bh)
        self._arrow(rx + cw / 2, y3, rx + cw / 2, y4 + bh, col=STOP)
        self._box(lx, y4, cw, bh, "BOTH SCREENS  -  alarm + block",
                  "/advisor page  and  /twin page\nred banner, beep, Accept greyed out",
                  fill=colors.HexColor("#dcfce7"), edge=GOOD, tcol=GOOD)
        self._box(rx, y4, cw, bh, "Accept on a breach  ->  HTTP 409",
                  "refused before anything is saved\nDecline / Undo always allowed",
                  fill=colors.HexColor("#fee2e2"), edge=STOP, tcol=STOP)
        assert y4 >= 0, f"diagram overflows its box by {abs(y4) / mm:.1f} mm; raise H"


# ------------------------------------------------------------------ styles ---
def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=17,
                                leading=21, textColor=ACCENT, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5,
                              leading=13, textColor=MUTED),
        "h2": ParagraphStyle("h", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12,
                             leading=15, textColor=ACCENT, spaceBefore=11, spaceAfter=5),
        "body": ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=9,
                               leading=12.6, textColor=INK),
        "k": ParagraphStyle("k", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=8.6,
                            leading=11, textColor=INK),
        "v": ParagraphStyle("v", parent=ss["Normal"], fontName="Helvetica", fontSize=8.2,
                            leading=10.8, textColor=MUTED),
        "mono": ParagraphStyle("m", parent=ss["Normal"], fontName="Courier", fontSize=8,
                               leading=10.4, textColor=INK),
        "code": ParagraphStyle("c", parent=ss["Code"], fontName="Courier", fontSize=7.8,
                               leading=9.8, textColor=INK, backColor=colors.HexColor("#f3f4f6"),
                               borderPadding=5, leftIndent=4, rightIndent=4),
        "hdr": ParagraphStyle("hd", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=8.5,
                              leading=11, textColor=colors.white),
    }


def _table(rows, widths, S, head=None, keycol=INK, styles=None):
    data = []
    if head:
        data.append([Paragraph(h, S["hdr"]) for h in head])
    styles = styles or ["k"] + ["v"] * (len(widths) - 1)
    for r in rows:
        cells = []
        for i, v in enumerate(r):
            st = S[styles[i]]
            if i == 0 and keycol is not INK:
                st = ParagraphStyle("kk", parent=st, textColor=keycol)
            cells.append(Paragraph(v, st))
        data.append(cells)
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("GRID", (0, 0), (-1, -1), 0.4, RULE),
          ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
          ("TOPPADDING", (0, 0), (-1, -1), 4.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.2)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), ACCENT))
    first = 1 if head else 0
    for i in range(first, len(data)):
        if (i - first) % 2 == 1:
            st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f3f4f6")))
    t.setStyle(TableStyle(st))
    return t


def build_pdf(path: Path = OUT) -> Path:
    S = _styles()
    f = []
    f.append(Paragraph("Advisor Guardrails - Implementation", S["title"]))
    f.append(Paragraph("Calcination Combustion Control digital twin, JK Cement Kiln-3<br/>"
                       f"Prepared {date.today():%d %B %Y}", S["sub"]))
    f.append(Spacer(1, 8))
    box = Table([[Paragraph(
        "<b>In one sentence:</b> when the live value or the recommended value of an advisor "
        "parameter goes above its roof or below its floor, both dashboards sound an alarm with a "
        "beep, the operator cannot Accept that recommendation (the button is greyed out AND the "
        "server refuses it), and Decline remains available. The limits live in one settings entry "
        "and can be changed without touching code.", S["body"])]], colWidths=[170 * mm])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BAND),
                             ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                             ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                             ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    f.append(box)

    f.append(Paragraph("1. The limits in force", S["h2"]))
    f.append(_table(LIMITS, [49 * mm, 28 * mm, 15 * mm, 16 * mm, 11 * mm, 51 * mm], S,
                    head=["Parameter", "Tag", "Floor", "Roof", "Unit", "Checked against"],
                    styles=["k", "mono", "k", "k", "v", "v"]))
    f.append(Spacer(1, 3))
    f.append(Paragraph("Floors and roofs supplied by the CCR operators via the project lead on "
                       "21 Oct 2026; the calciner outlet temperature floor was lowered from 850 to "
                       "820 degC by the project lead on 23 Oct 2026. The roofs equal the plant's own "
                       "P99 (section 9). A value exactly "
                       "at a limit is allowed, so a fuel at 0 (off) is fine; only a negative reading "
                       "breaches a 0 floor.", S["v"]))

    f.append(Paragraph("2. What the operator sees", S["h2"]))
    f.append(_table(BEHAVIOUR, [36 * mm, 134 * mm], S))

    f.append(KeepTogether([Paragraph("3. How it is built", S["h2"]),
                           Paragraph("One settings entry feeds one backend module. The advisory "
                                     "APIs REPORT the verdict so the screens can alarm; the decision "
                                     "APIs ENFORCE it. A disabled button alone is not a guardrail - "
                                     "a stale page or a direct request could still record an Accept "
                                     "- so the server refuses independently.", S["body"]),
                           Spacer(1, 6), FlowDiagram()]))

    f.append(Paragraph("4. Breach rules", S["h2"]))
    f.append(_table(RULES, [34 * mm, 136 * mm], S))

    f.append(Paragraph("5. Interfaces", S["h2"]))
    # Each label travels with its code block so a snippet never splits across pages.
    for label, code in (
        ("<b>Settings</b> - data/plant_settings.json (current values):", SETTINGS_SNIPPET),
        ("<b>Advisory payload</b> - added to GET /api/twin/advisor and GET /api/advisory "
         "(illustrative values):", PAYLOAD_SNIPPET),
        ("<b>Refusal</b> - what POST /api/decisions returns for an Accept on a breached parameter. "
         "The server judges its OWN live value and recommendation as well as the values the page "
         "submitted, so a stale or altered page cannot slip past:", REFUSAL_SNIPPET),
    ):
        f.append(KeepTogether([Paragraph(label, S["body"]), Spacer(1, 3),
                               Preformatted(code, S["code"]), Spacer(1, 6)]))
    f.append(Paragraph("<b>The beep</b> is generated in the browser with the Web Audio API (two "
                       "square-wave pulses at 880 Hz, 0.2 s each), so no sound file is needed. It "
                       "repeats every 2 s while a breach is active and not silenced.", S["body"]))

    f.append(Paragraph("6. Files changed", S["h2"]))
    f.append(_table(FILES, [70 * mm, 16 * mm, 84 * mm], S, head=["File", "Change", "What"],
                    styles=["mono", "k", "v"]))

    f.append(Paragraph("7. Verification", S["h2"]))
    f.append(Paragraph("Two rounds on the development machine. To prove the alarm without waiting for "
                       "a real excursion, limits were moved temporarily from a backup copy and then "
                       "restored - each time the restored file's checksum matched the original exactly. "
                       "The roof round used the live OPC feed; the floor round used replay data because "
                       "the OPC server was unreachable from the laptop that day. The floor round also "
                       "exposed two gaps, both fixed and re-tested (last three rows).", S["body"]))
    f.append(Spacer(1, 4))
    f.append(_table(TESTS, [42 * mm, 98 * mm, 30 * mm], S, head=["Test", "What was done", "Result"],
                    styles=["k", "v", "k"]))

    f.append(Paragraph("8. Changing the limits", S["h2"]))
    f.append(Paragraph("Edit the 'guardrails' section of data/plant_settings.json - change a 'min' "
                       "(floor) or 'max' (roof). Save the file; the next request uses it. No code "
                       "change, no restart. Use null to remove a limit. Keep each floor below its roof.",
                       S["body"]))

    f.append(Paragraph("9. Open points and limitations", S["h2"]))
    f.append(_table(OPEN, [46 * mm, 124 * mm], S, keycol=AMBER))

    def _chrome(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 12 * mm, "Calcination Combustion Control - Kiln-3 digital twin - "
                                            "advisor guardrails")
        canvas.drawRightString(190 * mm, 12 * mm, f"Page {doc.page}")
        canvas.setStrokeColor(BAND)
        canvas.setLineWidth(0.4)
        canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
        canvas.restoreState()

    doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                          topMargin=16 * mm, bottomMargin=20 * mm,
                          title="Advisor Guardrails - Implementation",
                          author="Calcination Combustion Control")
    doc.addPageTemplates([PageTemplate(id="all", frames=[Frame(doc.leftMargin, doc.bottomMargin,
                                                               doc.width, doc.height, id="n")],
                                       onPage=_chrome)])
    doc.build(f)
    return path


def _assert_text() -> None:
    blobs = []
    for group in (LIMITS, BEHAVIOUR, RULES, FILES, TESTS, OPEN):
        for row in group:
            blobs += list(row)
    bad = {c for s in blobs for c in s if ord(c) > 0x2017}
    assert not bad, f"non-renderable characters: {bad!r}"
    raw = [s for s in blobs if "<" in s or ">" in s]
    assert not raw, f"bare angle brackets would be eaten by the Paragraph parser: {raw!r}"
    code = SETTINGS_SNIPPET + REFUSAL_SNIPPET + PAYLOAD_SNIPPET
    assert all(ord(c) < 128 for c in code), "code snippets must be ASCII (Courier)"


if __name__ == "__main__":
    _assert_text()
    p = build_pdf()
    print(f"wrote {p}  ({p.stat().st_size:,} bytes)")
