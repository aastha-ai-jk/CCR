"""Build the CCR operator guardrail questionnaire for the Kiln-3 advisor.

    python docs/make_guardrail_xlsx.py

Output: docs/CCR_Advisor_Guardrail_Questionnaire.xlsx

Why this exists: nothing the plant has confirmed currently limits how far the
advisory dashboard may move a setpoint. data/plant_settings.json has every
optimizer.levers.*.min / .max / .max_step set to null, so twin/optimize.py falls
back to the wide PLC engineering range (dim_tag y_min / y_max) and a trust
region of +/-15% of that range or +/-100% of the current value, whichever is
smaller. This workbook collects the operators' real limits, with a reason for
each, in a form that maps 1:1 onto plant_settings.json (see data/
plant_settings.README.md for the question numbers).

The plant-history reference numbers are computed here from the 1-minute PLC
history so the workbook is reproducible, then the Guardrails and
Recent_Recommendations sheets reference them by formula rather than repeating
hard-coded copies.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "digital_twin.sqlite"
OUT = Path(__file__).resolve().parent / "CCR_Advisor_Guardrail_Questionnaire.xlsx"

# ------------------------------------------------------------------ style ---
FN = "Arial"
NAVY, GREY, INPUT = "1E3A8A", "F3F4F6", "FFF59D"
F_BODY = Font(name=FN, size=10)
F_BOLD = Font(name=FN, size=10, bold=True)
F_HEAD = Font(name=FN, size=10, bold=True, color="FFFFFF")
F_TITLE = Font(name=FN, size=15, bold=True, color=NAVY)
F_SUB = Font(name=FN, size=10, color="4B5563")
F_NOTE = Font(name=FN, size=9, italic=True, color="4B5563")
F_EXAMPLE = Font(name=FN, size=10, italic=True, color="6B7280")
F_LINK = Font(name=FN, size=10, color="008000")          # cross-sheet link
F_DATA = Font(name=FN, size=10, color="0000FF")          # hard-coded data input
FILL_HEAD = PatternFill("solid", fgColor=NAVY)
FILL_GREY = PatternFill("solid", fgColor=GREY)
FILL_IN = PatternFill("solid", fgColor=INPUT)
GROUP = {
    "ref":  PatternFill("solid", fgColor="6B7280"),
    "hist": PatternFill("solid", fgColor="475569"),
    "soft": PatternFill("solid", fgColor="3B5BA9"),
    "in":   PatternFill("solid", fgColor="B45309"),
    "why":  PatternFill("solid", fgColor="92400E"),
    "sign": PatternFill("solid", fgColor="15803D"),
    "chk":  PatternFill("solid", fgColor="6B7280"),
}
THIN = Side(style="thin", color="9CA3AF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(wrap_text=True, vertical="center", horizontal="center")

STATUS = '"Pending,Confirmed,Disputed,Needs review"'


def cell(ws, ref, value=None, font=F_BODY, fill=None, align=WRAP, border=BOX, fmt=None):
    c = ws[ref]
    if value is not None:
        c.value = value
    c.font, c.alignment = font, align
    if fill is not None:
        c.fill = fill
    if border is not None:
        c.border = border
    if fmt:
        c.number_format = fmt
    return c


def header_row(ws, row, labels, fill=FILL_HEAD, height=48):
    for i, lab in enumerate(labels, 1):
        cell(ws, f"{L(i)}{row}", lab, F_HEAD, fill, CENTER)
    ws.row_dimensions[row].height = height


def widths(ws, spec):
    for col, w in spec.items():
        ws.column_dimensions[col].width = w


def title(ws, text, sub=None, span=8):
    ws["A1"].value = text
    ws["A1"].font = F_TITLE
    ws.row_dimensions[1].height = 24
    if sub:
        ws["A2"].value = sub
        ws["A2"].font = F_SUB
        ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
        ws.row_dimensions[2].height = 42


def landscape(ws, repeat_rows=None):
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    if repeat_rows:
        ws.print_title_rows = repeat_rows


# ---------------------------------------------------------------- history ---
HIST_TAGS = [
    ("423CI400_TT_4", "Calciner outlet temperature", "degC"),
    ("467RL400_TPH", "Calciner coal firing (total)", "t/h"),
    ("LAFR_FT", "LAFR feed rate PC (precalciner)", "l/h"),
    ("K3V10_F1", "LAFR feed rate TAD (tertiary air duct)", "l/h"),
    ("261WF080_TPH", "SAFR feed rate (solid AFR)", "t/h"),
]
DECIMALS = {"423CI400_TT_4": 1, "467RL400_TPH": 2, "LAFR_FT": 0, "K3V10_F1": 0, "261WF080_TPH": 2}


def compute_history() -> tuple[dict, dict]:
    """Percentiles over production hours and 5-minute step-change percentiles."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tags = [t for t, _, _ in HIST_TAGS]
    df = pd.read_sql('SELECT ts, feed_gate, ' + ",".join(f'"{t}"' for t in tags)
                     + " FROM process_1min", con)
    cat = pd.read_sql("SELECT tag, y_min, y_max FROM dim_tag", con).set_index("tag")
    con.close()
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    g = df[df.feed_gate == 1]
    meta = {"rows": int(len(df)), "prod_rows": int(len(g)),
            "t0": df.ts.min().strftime("%Y-%m-%d"), "t1": df.ts.max().strftime("%Y-%m-%d")}
    stats = {}
    for t in tags:
        s = pd.to_numeric(g[t], errors="coerce").dropna()
        s = s[s >= 0]
        run = s if t == "423CI400_TT_4" else s[s > 0.05 * s.quantile(0.5)]
        col = pd.to_numeric(df[t], errors="coerce")
        d5 = (col.shift(-5) - col)[(df.feed_gate == 1) & (df.feed_gate.shift(-5) == 1)].abs().dropna()
        stats[t] = dict(
            zero_pct=float((s <= 0.01).mean()),
            p1=run.quantile(.01), p5=run.quantile(.05), p50=run.quantile(.50),
            p95=run.quantile(.95), p99=run.quantile(.99), mx=run.max(),
            d50=d5.quantile(.50), d95=d5.quantile(.95), d99=d5.quantile(.99), dmx=d5.max(),
            ymin=cat.loc[t, "y_min"], ymax=cat.loc[t, "y_max"])
    return stats, meta


HIST_FIRST_ROW = 6          # first data row on 5_History_Reference
HIST_ROW = {t: HIST_FIRST_ROW + i for i, (t, _, _) in enumerate(HIST_TAGS)}
HS = "'5_History_Reference'"


# ------------------------------------------------------------------ sheets ---
def sheet_readme(wb):
    ws = wb.active
    ws.title = "Read Me"
    ws.sheet_view.showGridLines = False
    widths(ws, {"A": 3, "B": 30, "C": 100})
    ws["B1"].value = "Kiln-3 Calciner Advisor - Guardrail Questionnaire"
    ws["B1"].font = F_TITLE
    ws.row_dimensions[1].height = 26
    ws["B2"].value = (f"For: CCR operators and process team, Nimbahera Kiln-3   |   "
                      f"Prepared {date.today():%d %B %Y}   |   Advisory only - nothing is written to the DCS")
    ws["B2"].font = F_SUB

    rows = [
        ("Why we are asking",
         "The advisory dashboard recommends new setpoints for calciner outlet temperature, calciner coal firing, "
         "LAFR to the precalciner (PC), LAFR to the tertiary air duct (TAD) and SAFR. Today NO limit that the plant "
         "has confirmed restricts how far a recommendation can go. The software falls back on the wide PLC "
         "engineering range (for example LAFR PC 0 to 9,000 l/h) and allows one recommendation to move a setpoint by "
         "up to 15% of that range or 100% of its current value, whichever is smaller. A recent LAFR PC recommendation "
         "moved from 1,217 to 2,426 l/h in one step - exactly that 100% cap (see sheet 6). Only you can tell us what "
         "is actually safe, so the advisor can be made to stay inside your limits."),
        ("What we need from you",
         "For each parameter on sheet 1: the ABSOLUTE FLOOR (never recommend below this), the ABSOLUTE CEILING (never "
         "recommend above this), the normal operating range, the largest single change you would accept, and - most "
         "important - the REASON for each limit. A limit with no reason cannot be defended later, so please always "
         "write one. If you do not know, write 'don't know' in the reason cell rather than guessing a number."),
        ("Terms",
         "ABSOLUTE FLOOR / CEILING = the hard stop. The advisor must never recommend a value outside it, whatever "
         "the model predicts.\n"
         "NORMAL RANGE = where you expect it to run in routine operation.\n"
         "MAX SINGLE STEP = the largest change you would accept in ONE recommendation (the size you would apply in "
         "one go).\n"
         "MIN MINUTES BETWEEN CHANGES = how long the process needs to settle before the next change is meaningful."),
        ("Colour legend",
         "YELLOW cells  = please fill in.\n"
         "GREY / white cells = reference, pre-filled from plant history or from the software - please do not edit.\n"
         "GREEN numbers = pulled from the history sheet by formula.   BLUE numbers = data copied in, with its source noted.\n"
         "ITALIC GREY row = an EXAMPLE showing the expected format. Its numbers are invented for illustration and are "
         "not plant data. Ignore it."),
        ("The sheets",
         "1_Guardrails  - floor, ceiling, step and reason for temperature, coal, LAFR PC, LAFR TAD, SAFR.  START HERE.\n"
         "2_Fuel_Grades - calorific value (CV) and flow limits per fuel. LAFR limits depend on CV: low CV lowers the "
         "temperature, high CV raises it.\n"
         "3_Safety_and_Bands - temperature state bands, CO / O2 interlocks and quality limits.\n"
         "4_Open_Questions - questions that do not fit a table.\n"
         "5_History_Reference - what the plant has actually done over the last year (reference).\n"
         "6_Recent_Recommendations - a snapshot of the dashboard against that history (reference)."),
        ("What happens to your answers",
         "Each row names the plant_settings.json key it feeds (column 'plant_settings.json key'). After the status "
         "is 'Confirmed' and signed off below, the project team copies the values into data/plant_settings.json and "
         "restarts the backend. Rows that are not Confirmed are not used."),
        ("Known gaps in the data",
         "1) There is NO separate measurement for 'PC coal' versus 'Indian coal'. The advisor only sees one total "
         "calciner coal flow (tag 467RL400_TPH), so it cannot recommend or limit each coal on its own. Please tell us "
         "on sheet 2 whether separate feeders or tags exist.\n"
         "2) There is NO calorific-value signal for LAFR. The software assumes one fixed CV for all LAFR "
         "(5,500 kcal/kg, an estimate). If your LAFR varies between low and high CV, the same l/h means a different "
         "amount of heat, so sheet 2 asks how you know the current grade."),
    ]
    r = 4
    for head, body in rows:
        cell(ws, f"B{r}", head, F_BOLD, FILL_GREY)
        cell(ws, f"C{r}", body)
        ws.row_dimensions[r].height = max(30, 14.5 * (len(body) // 95 + 1 + body.count("\n")))
        r += 1

    r += 1
    ws[f"B{r}"].value = "Sign-off"
    ws[f"B{r}"].font = F_BOLD
    r += 1
    for lab in ("CCR in-charge - name / signature", "Process head - name / signature",
                "Date reviewed", "Shift"):
        cell(ws, f"B{r}", lab, F_BODY, FILL_GREY)
        cell(ws, f"C{r}", None, F_BODY, FILL_IN)
        ws.row_dimensions[r].height = 24
        r += 1
    landscape(ws)


GUARD_COLS = [
    # (header, width, group)
    ("#", 5, "ref"), ("Parameter", 30, "ref"), ("Tag", 17, "ref"), ("Unit", 8, "ref"),
    ("Why we ask - what this limit controls", 52, "ref"),
    ("Plant P5\n(low end)", 10, "hist"), ("Plant median", 10, "hist"), ("Plant P95\n(high end)", 10, "hist"),
    ("Plant max\n(may include sensor spikes)", 13, "hist"), ("Plant typical 5-min move\n(P95, absolute)", 14, "hist"),
    ("Software min today", 10, "soft"), ("Software max today", 10, "soft"),
    ("Software max step today (+/-)", 13, "soft"),
    ("ABSOLUTE FLOOR\n(never below)", 13, "in"), ("ABSOLUTE CEILING\n(never above)", 13, "in"),
    ("Normal low", 10, "in"), ("Normal high", 10, "in"),
    ("MAX SINGLE STEP\n(+/- per recommendation)", 15, "in"), ("Min minutes between changes", 12, "in"),
    ("REASON for the FLOOR", 34, "why"), ("REASON for the CEILING", 34, "why"),
    ("REASON for the step limit", 30, "why"), ("Depends on\n(State 1/2/3, LAFR CV grade, kiln feed...)", 26, "why"),
    ("Confirmed by (name)", 16, "sign"), ("Date", 12, "sign"), ("Status", 13, "sign"),
    ("Check (automatic)", 24, "chk"), ("plant_settings.json key", 34, "chk"),
]
GROUPS = [("Parameter", "ref", 1, 5), ("What the plant has actually done (reference)", "hist", 6, 10),
          ("What the software allows today (reference)", "soft", 11, 13),
          ("YOUR LIMITS - operators fill in", "in", 14, 19),
          ("WHY - operators fill in", "why", 20, 23), ("Sign-off", "sign", 24, 26),
          ("Checks / mapping", "chk", 27, 28)]
# column letters used by formulas
C_FLOOR, C_CEIL, C_NLO, C_NHI, C_RF, C_RC = "N", "O", "P", "Q", "T", "U"
C_CHECK = "AA"


def check_formula(r):
    f, c, lo, hi, rf, rc = (f"{C_FLOOR}{r}", f"{C_CEIL}{r}", f"{C_NLO}{r}", f"{C_NHI}{r}",
                            f"{C_RF}{r}", f"{C_RC}{r}")
    return (f'=IF(COUNT({f}:{c})=0,"",IF(COUNT({f}:{c})<2,"Enter both floor and ceiling",'
            f'IF({f}>={c},"Floor must be below ceiling",'
            f'IF(AND(ISNUMBER({lo}),{lo}<{f}),"Normal low is below the floor",'
            f'IF(AND(ISNUMBER({hi}),{hi}>{c}),"Normal high is above the ceiling",'
            f'IF(OR({rf}="",{rc}=""),"Add a reason for floor and ceiling","OK"))))))')


def sheet_guardrails(wb, stats):
    ws = wb.create_sheet("1_Guardrails")
    title(ws, "1 - Guardrails for every parameter the advisor recommends",
          "Fill the YELLOW cells. Floor and ceiling are hard stops; the reason columns matter as much as the numbers. "
          "Leave a number blank and write 'don't know' in the reason if you are unsure. The grey 'plant' columns show "
          "what the plant has really done, to help you judge - they are not limits.", span=14)
    for name, key, c1, c2 in GROUPS:
        ws.merge_cells(start_row=4, start_column=c1, end_row=4, end_column=c2)
        cell(ws, f"{L(c1)}4", name, F_HEAD, GROUP[key], CENTER)
        for c in range(c1 + 1, c2 + 1):
            ws[f"{L(c)}4"].fill = GROUP[key]
            ws[f"{L(c)}4"].border = BOX
    ws.row_dimensions[4].height = 28
    for i, (h, w, g) in enumerate(GUARD_COLS, 1):
        cell(ws, f"{L(i)}5", h, F_HEAD, GROUP[g], CENTER)
        ws.column_dimensions[L(i)].width = w
    ws.row_dimensions[5].height = 62

    # Example row (invented values; clearly marked)
    ex = 6
    vals = {1: "ex", 2: "EXAMPLE - Preheater fan speed (not an advisor parameter; numbers invented to show the format)",
            3: "423FN010_ST", 4: "rpm", 5: "Example only - ignore this row.",
            14: 850, 15: 1050, 16: 920, 17: 1000, 18: 25, 19: 10,
            20: "Below 850 rpm the draft is too weak and CO rises at the kiln inlet.",
            21: "Above 1,050 rpm the fan motor reaches its rated current.",
            22: "A jump of more than 25 rpm upsets the pressure profile.",
            23: "State 3 only", 24: "A. Operator", 25: date(2026, 10, 19), 26: "Pending",
            28: "(example - no key)"}
    for i in range(1, len(GUARD_COLS) + 1):
        cell(ws, f"{L(i)}{ex}", vals.get(i), F_EXAMPLE, FILL_GREY)
    ws[f"Y{ex}"].number_format = "dd-mmm-yyyy"
    cell(ws, f"{C_CHECK}{ex}", check_formula(ex), F_EXAMPLE, FILL_GREY)
    ws.row_dimensions[ex].height = 64

    # Real rows. hist = tag whose history feeds the reference columns (None = not measured)
    S = stats
    rows = [
        ("1", "Calciner outlet temperature", "423CI400_TT_4", "degC", "423CI400_TT_4",
         "This is the value the advisor tries to steer. Give the temperatures at which you would NOT want the "
         "calciner steered any further - too cold (raw meal under-calcined, free lime rises) and too hot (build-up, "
         "ring or cyclone-blockage risk, refractory wear). These are the hard limits, not the target band: the "
         "bands for State 1/2/3 are on sheet 3.",
         880, 940, "n/a (target, not a lever)", "temperature_bands.State N  (see sheet 3; Q5.1-5.3)"),
        ("2", "Calciner coal firing - TOTAL", "467RL400_TPH", "t/h", "467RL400_TPH",
         "The total coal the calciner burns - the only coal flow the advisor can see today. The floor protects "
         "against too little fuel (flame instability, poor calcination); the ceiling protects against the feeder, "
         "burner, fan draft or CO limit being exceeded. The advisor raises and lowers this directly.",
         0, 35, "15% of range, or 100% of current if smaller",
         "optimizer.levers.467RL400_TPH.min / .max / .max_step  (Q3.1, Q7.x)"),
        ("2a", "   of which: PC coal", "(no separate tag)", "t/h", None,
         "CLARIFY: what do you mean by 'PC coal' - coal fired at the precalciner, or petcoke? Is it fed by its own "
         "feeder with its own flow reading? If yes, tell us the tag so the advisor can limit and recommend it on "
         "its own. The ceiling asked for here is the highest level this coal may be fired.",
         None, None, "n/a", "NEW - no key yet; needs a separate flow tag"),
        ("2b", "   of which: Indian coal", "(no separate tag)", "t/h", None,
         "Same questions as PC coal. Please give the HIGHEST level of Indian coal that may be fired in the "
         "calciner, and why (feeder capacity, ash/volatile content, CO behaviour, cost).",
         None, None, "n/a", "NEW - no key yet; needs a separate flow tag"),
        ("3", "LAFR feed rate - PC (precalciner firing)", "LAFR_FT", "l/h", "LAFR_FT",
         "Liquid AFR to the precalciner. Limits come from pump and line capacity, line pressure and nozzle limits - "
         "and from calorific value: LOW-CV LAFR LOWERS the calciner temperature, HIGH-CV LAFR RAISES it, so the same "
         "l/h behaves differently by grade. Fill sheet 2 for the grade-dependent limits; give the overall hard "
         "limits here.",
         0, 9000, "15% of range, or 100% of current if smaller",
         "optimizer.levers.LAFR_FT.min / .max / .max_step  (Q1.1, Q7.x)"),
        ("4", "LAFR feed rate - TAD (tertiary air duct)", "K3V10_F1", "l/h", "K3V10_F1",
         "Liquid AFR to the tertiary air duct. Same CV logic as the PC line (low CV lowers temperature, high CV raises "
         "it). Also tell us whether PC and TAD can run together at their maximum, or share a pump or header limit.",
         0, 4000, "15% of range, or 100% of current if smaller",
         "optimizer.levers.K3V10_F1.min / .max / .max_step  (Q1.2, Q7.x)"),
        ("5", "SAFR feed rate (solid AFR)", "261WF080_TPH", "t/h", "261WF080_TPH",
         "Solid AFR to the calciner line, shown on the dashboard. It sits at zero some of the time (trips and "
         "restarts). Limits come from feeder capacity, blockage risk and the thermal-substitution limit.",
         0, 30, "15% of range, or 100% of current if smaller",
         "optimizer.levers.261WF080_TPH.min / .max / .max_step  (Q1.3, Q7.x)"),
    ]
    r = ex + 1
    for num, name, tag, unit, hist, why, smin, smax, sstep, key in rows:
        cell(ws, f"A{r}", num, F_BOLD, FILL_GREY, CENTER)
        cell(ws, f"B{r}", name, F_BOLD, FILL_GREY)
        cell(ws, f"C{r}", tag, F_BODY, FILL_GREY)
        cell(ws, f"D{r}", unit, F_BODY, FILL_GREY, CENTER)
        cell(ws, f"E{r}", why, F_BODY, FILL_GREY)
        # plant history: formulas pointing at 5_History_Reference
        if hist:
            hr = HIST_ROW[hist]
            fmt = "0" if DECIMALS[hist] == 0 else f"0.{'0' * DECIMALS[hist]}"
            for col, src in (("F", "F"), ("G", "G"), ("H", "H"), ("I", "J"), ("J", "L")):
                cell(ws, f"{col}{r}", f"={HS}!{src}{hr}", F_LINK, FILL_GREY, CENTER, fmt=fmt)
        else:
            for col in "FGHIJ":
                cell(ws, f"{col}{r}", "n/a", F_NOTE, FILL_GREY, CENTER)
        # software today (from data/plant_settings.README.md defaults and twin/optimize.py)
        cell(ws, f"K{r}", smin, F_DATA if isinstance(smin, (int, float)) else F_NOTE, FILL_GREY, CENTER)
        cell(ws, f"L{r}", smax, F_DATA if isinstance(smax, (int, float)) else F_NOTE, FILL_GREY, CENTER)
        cell(ws, f"M{r}", sstep, F_NOTE, FILL_GREY, CENTER)
        for col in "NOPQRS":
            cell(ws, f"{col}{r}", None, F_BODY, FILL_IN, CENTER)
        for col in "TUVW":
            cell(ws, f"{col}{r}", None, F_BODY, FILL_IN)
        cell(ws, f"X{r}", None, F_BODY, FILL_IN)
        cell(ws, f"Y{r}", None, F_BODY, FILL_IN, CENTER, fmt="dd-mmm-yyyy")
        cell(ws, f"Z{r}", "Pending", F_BODY, FILL_IN, CENTER)
        cell(ws, f"{C_CHECK}{r}", check_formula(r), F_BODY, FILL_GREY, CENTER)
        cell(ws, f"AB{r}", key, F_NOTE, FILL_GREY)
        ws.row_dimensions[r].height = 118 if len(why) > 260 else 100
        r += 1
    last = r - 1

    # validation
    dv_num = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0",
                            allow_blank=True, showErrorMessage=True,
                            errorTitle="Number needed", error="Enter a number (0 or more) in the unit shown.")
    ws.add_data_validation(dv_num)
    dv_num.add(f"N{ex + 1}:S{last}")
    dv_status = DataValidation(type="list", formula1=STATUS, allow_blank=False)
    ws.add_data_validation(dv_status)
    dv_status.add(f"Z{ex + 1}:Z{last}")

    # notes
    n = last + 2
    notes = [
        "Software columns come from data/plant_settings.README.md (lever min/max = PLC range) and twin/optimize.py "
        "(step = 15% of range or 100% of current value, whichever is smaller). They are what the advisor does TODAY "
        "because no plant limit has been confirmed.",
        "Plant columns: production hours only (kiln feed above 461 t/h), 1-minute history 2025-08-10 to 2026-07-18; "
        "percentiles exclude time at zero for the fuel flows. See sheet 5 for the full table and caveats.",
        "A 'Plant max' above the PLC range (for example SAFR 39.9 vs a 30 t/h range) usually means a sensor spike or "
        "a range setting that is out of date - please say which it is.",
    ]
    for t in notes:
        ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=13)
        cell(ws, f"A{n}", t, F_NOTE, None, WRAP, border=None)
        ws.row_dimensions[n].height = 30
        n += 1
    ws.freeze_panes = "C6"
    landscape(ws, "4:5")
    return ws


def sheet_fuel(wb):
    ws = wb.create_sheet("2_Fuel_Grades")
    title(ws, "2 - Fuel grades: calorific value and flow limits",
          "The same flow of two different fuels gives a different amount of heat, so limits and the temperature effect "
          "depend on the fuel grade. Please fill the YELLOW cells for each fuel you actually fire. Add rows below the "
          "table for any grade that is missing.", span=14)
    hdr = ["Fuel", "Fired at", "Tag", "Effect on calciner temperature (as described by the plant)",
           "Software assumes CV today (kcal/kg)\n(estimate, not plant-confirmed)", "Plant CV - low\n(kcal/kg)",
           "Plant CV - high\n(kcal/kg)", "Density\n(kg/l, liquids)", "MIN flow", "MAX flow", "Flow unit",
           "REASON for these limits", "How do you know the current grade?\n(lab, tank, supplier sheet...)",
           "How often does the grade change?", "Confirmed by (name)", "Status"]
    wd = [30, 14, 17, 36, 18, 12, 12, 11, 10, 10, 8, 38, 34, 24, 16, 13]
    for i, (h, w) in enumerate(zip(hdr, wd), 1):
        fill = FILL_HEAD if i in (1, 2, 3, 4, 5) else GROUP["in"]
        cell(ws, f"{L(i)}4", h, F_HEAD, fill, CENTER)
        ws.column_dimensions[L(i)].width = w
    ws.row_dimensions[4].height = 76

    ex = [ "EXAMPLE - fuel X (invented numbers)", "PC", "(none)", "Example only", None, 5200, 5800, None, 2.0, 18.0, "t/h",
           "Below 2 t/h the flame is unstable; above 18 t/h the feeder is at its limit.",
           "Daily lab sample", "Changes weekly", "A. Operator", "Pending"]
    for i, v in enumerate(ex, 1):
        cell(ws, f"{L(i)}5", v, F_EXAMPLE, FILL_GREY)
    ws.row_dimensions[5].height = 46

    fuels = [
        ("PC coal  (coal fired at the precalciner)", "PC", "(no separate tag)",
         "Raises temperature (more heat). CLARIFY: is 'PC coal' petcoke, or coal at the precalciner?", 6000, None, "t/h"),
        ("Indian coal", "PC", "(no separate tag)",
         "Raises temperature (more heat). Please give the HIGHEST level you can fire.", 6000, None, "t/h"),
        ("LAFR - LOW calorific value  (PC line)", "PC", "LAFR_FT",
         "LOWERS temperature (low-CV liquid AFR cools the calciner)", 5500, 0.95, "l/h"),
        ("LAFR - HIGH calorific value  (PC line)", "PC", "LAFR_FT",
         "RAISES temperature (high-CV liquid AFR adds heat)", 5500, 0.95, "l/h"),
        ("LAFR - LOW calorific value  (TAD line)", "TAD", "K3V10_F1",
         "LOWERS temperature (low-CV liquid AFR cools the calciner)", 5500, 0.95, "l/h"),
        ("LAFR - HIGH calorific value  (TAD line)", "TAD", "K3V10_F1",
         "RAISES temperature (high-CV liquid AFR adds heat)", 5500, 0.95, "l/h"),
        ("LAFR - other grade, if any (name it)", "PC / TAD", "LAFR_FT / K3V10_F1", "Please describe", 5500, 0.95, "l/h"),
        ("SAFR - solid alternative fuel", "Calciner line", "261WF080_TPH",
         "Raises temperature slightly (adds heat, slower response)", 4000, None, "t/h"),
    ]
    r = 6
    for name, at, tag, eff, cv, dens, unit in fuels:
        cell(ws, f"A{r}", name, F_BOLD, FILL_GREY)
        cell(ws, f"B{r}", at, F_BODY, FILL_GREY, CENTER)
        cell(ws, f"C{r}", tag, F_BODY, FILL_GREY)
        cell(ws, f"D{r}", eff, F_BODY, FILL_GREY)
        cell(ws, f"E{r}", cv, F_DATA, FILL_GREY, CENTER, fmt="#,##0")
        for col in "FGHIJ":
            cell(ws, f"{col}{r}", None, F_BODY, FILL_IN, CENTER)
        cell(ws, f"H{r}", None, F_BODY, FILL_IN, CENTER)
        if dens is not None:
            ws[f"H{r}"].comment = Comment(f"Software assumes {dens} kg/l (estimate).", "Guardrail workbook")
        cell(ws, f"K{r}", unit, F_BODY, FILL_GREY, CENTER)
        for col in "LMN":
            cell(ws, f"{col}{r}", None, F_BODY, FILL_IN)
        cell(ws, f"O{r}", None, F_BODY, FILL_IN)
        cell(ws, f"P{r}", "Pending", F_BODY, FILL_IN, CENTER)
        ws.row_dimensions[r].height = 50
        r += 1
    last = r - 1
    dv = DataValidation(type="list", formula1=STATUS, allow_blank=False)
    ws.add_data_validation(dv)
    dv.add(f"P6:P{last}")
    dvn = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True,
                         showErrorMessage=True, errorTitle="Number needed", error="Enter a number (0 or more).")
    ws.add_data_validation(dvn)
    dvn.add(f"F6:J{last}")

    n = last + 2
    for t in (
        "Software CV assumptions are the defaults in data/plant_settings.README.md (coal 6,000, LAFR 5,500, SAFR 4,000 "
        "kcal/kg; LAFR density 0.95 kg/l). They are published-typical Indian-cement values, NOT this plant's numbers.",
        "The software uses ONE CV for all LAFR, so today it cannot tell a low-CV from a high-CV day. Your answers "
        "here decide whether the advisor needs a CV input.",
        "Temperature-effect directions in column D follow the plant's own description (low-CV LAFR lowers, high-CV "
        "LAFR raises the calciner temperature). Please correct anything that is wrong.",
    ):
        ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=12)
        cell(ws, f"A{n}", t, F_NOTE, None, WRAP, border=None)
        ws.row_dimensions[n].height = 30
        n += 1
    ws.freeze_panes = "B6"
    landscape(ws, "4:4")


def sheet_safety(wb):
    ws = wb.create_sheet("3_Safety_and_Bands")
    title(ws, "3 - Temperature bands, safety interlocks and quality limits",
          "These decide when the advisor must hold back whatever the model predicts. The software values are today's "
          "defaults. Please confirm or correct each one and give the reason.", span=11)
    hdr = ["Item", "Tag", "Unit", "Rule", "Software value today\n(low, or the limit)",
           "Software HIGH\n(bands only)", "PLANT value\n(low, or the limit)", "PLANT HIGH\n(bands only)",
           "REASON", "plant_settings.json key (question)", "Confirmed by (name)", "Status"]
    wd = [38, 17, 8, 8, 16, 14, 16, 14, 42, 38, 16, 13]
    for i, (h, w) in enumerate(zip(hdr, wd), 1):
        fill = FILL_HEAD if i <= 6 else (GROUP["in"] if i <= 9 else (GROUP["chk"] if i == 10 else GROUP["sign"]))
        cell(ws, f"{L(i)}4", h, F_HEAD, fill, CENTER)
        ws.column_dimensions[L(i)].width = w
    ws.row_dimensions[4].height = 58

    ex = ["EXAMPLE - invented limit", "(example)", "ppm", "<", 1000, None, 900, None,
          "Example only: above 900 ppm the electrostatic precipitator trips.", "(example)", "A. Operator", "Pending"]
    for i, v in enumerate(ex, 1):
        cell(ws, f"{L(i)}5", v, F_EXAMPLE, FILL_GREY)
    ws.row_dimensions[5].height = 40

    items = [
        ("Calciner temperature band - State 1", "423CI400_TT_4", "degC", "range", 920, 940,
         "temperature_bands.State 1  (Q5.1)"),
        ("Calciner temperature band - State 2", "423CI400_TT_4", "degC", "range", 900, 920,
         "temperature_bands.State 2  (Q5.2)"),
        ("Calciner temperature band - State 3", "423CI400_TT_4", "degC", "range", 880, 900,
         "temperature_bands.State 3  (Q5.3)"),
        ("Calciner outlet CO - must stay BELOW", "423AM823_CO_PPM", "ppm", "<", 1500, None,
         "interlocks.423AM823_CO_PPM  (Q2.1-2.5)"),
        ("Calciner outlet O2 - must stay ABOVE", "423AM823_O2", "%", ">", 1.5, None,
         "interlocks.423AM823_O2  (Q2.1-2.5)"),
        ("Kiln inlet CO - must stay BELOW", "423AM822_CO_PPM", "ppm", "<", 800, None,
         "interlocks.423AM822_CO_PPM  (Q2.1-2.5)"),
        ("Kiln inlet O2 - must stay ABOVE", "423AM822_O2", "%", ">", 0.5, None,
         "interlocks.423AM822_O2  (Q2.1-2.5)"),
        ("Preheater outlet CO, string 1 - must stay BELOW", "423AM820_CO_PPM", "ppm", "<", 3000, None,
         "interlocks.423AM820_CO_PPM  (Q2.1-2.5)"),
        ("Preheater outlet CO, string 2 - must stay BELOW", "423AM821_CO_PPM", "ppm", "<", 3000, None,
         "interlocks.423AM821_CO_PPM  (Q2.1-2.5)"),
        ("Kiln feed - advisor only active ABOVE", "353RL130_TPH", "t/h", ">", 461, None,
         "process.kiln_running_min_feed_tph  (Q5.12)"),
        ("Clinker free lime - maximum (no heat cut above this)", "lab", "%", "<", 1.8, None,
         "quality.free_lime_max  (Q6.1)  - software value is an ESTIMATE"),
        ("Clinker free lime - minimum (over-burnt; no heat rise below this)", "lab", "%", ">", None, None,
         "quality.free_lime_min  (Q6.1)  - currently OFF"),
        ("Clinker litre weight - minimum (under-burnt)", "lab", "g/l", ">", None, None,
         "quality.liter_weight_min  (Q6.1)  - currently OFF"),
        ("Clinker litre weight - maximum (over-burnt)", "lab", "g/l", "<", None, None,
         "quality.liter_weight_max  (Q6.1)  - currently OFF"),
    ]
    r = 6
    for name, tag, unit, rule, lo, hi, key in items:
        cell(ws, f"A{r}", name, F_BOLD, FILL_GREY)
        cell(ws, f"B{r}", tag, F_BODY, FILL_GREY)
        cell(ws, f"C{r}", unit, F_BODY, FILL_GREY, CENTER)
        cell(ws, f"D{r}", rule, F_BODY, FILL_GREY, CENTER)
        cell(ws, f"E{r}", lo if lo is not None else "off", F_DATA if lo is not None else F_NOTE, FILL_GREY, CENTER)
        cell(ws, f"F{r}", hi if hi is not None else "-", F_DATA if hi is not None else F_NOTE, FILL_GREY, CENTER)
        cell(ws, f"G{r}", None, F_BODY, FILL_IN, CENTER)
        cell(ws, f"H{r}", None, F_BODY, FILL_IN, CENTER)
        cell(ws, f"I{r}", None, F_BODY, FILL_IN)
        cell(ws, f"J{r}", key, F_NOTE, FILL_GREY)
        cell(ws, f"K{r}", None, F_BODY, FILL_IN)
        cell(ws, f"L{r}", "Pending", F_BODY, FILL_IN, CENTER)
        ws.row_dimensions[r].height = 36
        r += 1
    last = r - 1
    dv = DataValidation(type="list", formula1=STATUS, allow_blank=False)
    ws.add_data_validation(dv)
    dv.add(f"L6:L{last}")
    dvn = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True,
                         showErrorMessage=True, errorTitle="Number needed", error="Enter a number (0 or more).")
    ws.add_data_validation(dvn)
    dvn.add(f"G6:H{last}")
    n = last + 2
    for t in ("Software values are from config.py and data/plant_settings.README.md. Interlock values and band edges "
              "are shown as the software holds them today; only the free-lime maximum is flagged in the code as an "
              "estimate. 'off' means the software does not check that limit at all.",
              "Calciner temperature bands are the target the advisor steers to; the absolute floor / ceiling you "
              "give on sheet 1 are the hard limits beyond them."):
        ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=9)
        cell(ws, f"A{n}", t, F_NOTE, None, WRAP, border=None)
        ws.row_dimensions[n].height = 30
        n += 1
    ws.freeze_panes = "B6"
    landscape(ws, "4:4")


def sheet_questions(wb):
    ws = wb.create_sheet("4_Open_Questions")
    title(ws, "4 - Open questions for the CCR operators",
          "Questions that do not fit a table. Please answer in plain words and say WHY.", span=7)
    hdr = ["#", "Topic", "Question", "ANSWER", "REASON / basis", "Answered by (name)", "Date"]
    wd = [5, 22, 70, 46, 42, 18, 12]
    for i, (h, w) in enumerate(zip(hdr, wd), 1):
        fill = FILL_HEAD if i <= 3 else GROUP["in"]
        cell(ws, f"{L(i)}4", h, F_HEAD, fill, CENTER)
        ws.column_dimensions[L(i)].width = w
    ws.row_dimensions[4].height = 30
    qs = [
        ("Speed of change", "How fast can each fuel actually be changed? For example LAFR pump l/h per minute, coal "
                            "feeder t/h per minute, SAFR feeder t/h per minute."),
        ("Response time", "After you change a fuel, how many minutes until the calciner outlet temperature responds? "
                          "Does it differ for coal, LAFR and SAFR?"),
        ("First action when cold", "When the calciner outlet temperature is falling, what do you change first, and "
                                   "by how much? In what order do you then add fuel?"),
        ("First action when hot", "When it is rising, what do you change first, and by how much?"),
        ("LAFR together", "Can LAFR PC and LAFR TAD both run near their maximum at the same time, or do they share a "
                          "pump, header or pressure limit?"),
        ("LAFR grade", "How do you know whether the LAFR is low or high calorific value? Who tells the CCR, and how "
                       "often does it change during a shift or day?"),
        ("Coal types", "Are PC coal and Indian coal fed by separate feeders with separate flow readings? Can they be "
                       "mixed in any ratio? What is the highest level of each?"),
        ("Total AFR share", "What is the maximum share of calciner heat that may come from alternative fuels (the "
                            "thermal substitution rate limit)? Which fuel do you raise first?"),
        ("Emergency temperatures", "At what calciner outlet temperature, high and low, do you take emergency action? "
                                   "What happens at that point (build-up, cyclone blockage, flame-out)?"),
        ("CO and O2", "At what CO or O2 reading do you cut fuel immediately, even before an interlock acts?"),
        ("No-advice situations", "When should the dashboard show NO recommendation? For example kiln start or stop, a "
                                 "feeder trip, an analyser fault, a blocked cyclone, a lab result out of range."),
        ("State dependence", "Do the limits above change between State 1, State 2 and State 3?"),
        ("Kiln feed dependence", "Do the limits change with kiln feed (for example 460 versus 520 t/h)?"),
        ("Equipment limits", "Are there equipment limits we have not asked about - ID fan, feeder, burner, cooler, "
                             "bag filter, power - that cap how much fuel can be added?"),
        ("Ownership", "Who has the authority to change these limits later, and how often should they be reviewed?"),
    ]
    r = 5
    cell(ws, f"A{r}", "ex", F_EXAMPLE, FILL_GREY, CENTER)
    cell(ws, f"B{r}", "EXAMPLE", F_EXAMPLE, FILL_GREY)
    cell(ws, f"C{r}", "Example question - ignore this row.", F_EXAMPLE, FILL_GREY)
    cell(ws, f"D{r}", "Example answer: coal feeder moves 0.5 t/h per minute.", F_EXAMPLE, FILL_GREY)
    cell(ws, f"E{r}", "Feeder drive is limited to 30 rpm per minute.", F_EXAMPLE, FILL_GREY)
    cell(ws, f"F{r}", "A. Operator", F_EXAMPLE, FILL_GREY)
    cell(ws, f"G{r}", date(2026, 10, 19), F_EXAMPLE, FILL_GREY, CENTER, fmt="dd-mmm-yyyy")
    ws.row_dimensions[r].height = 32
    r += 1
    for i, (topic, q) in enumerate(qs, 1):
        cell(ws, f"A{r}", i, F_BOLD, FILL_GREY, CENTER)
        cell(ws, f"B{r}", topic, F_BOLD, FILL_GREY)
        cell(ws, f"C{r}", q, F_BODY, FILL_GREY)
        for col in "DEF":
            cell(ws, f"{col}{r}", None, F_BODY, FILL_IN)
        cell(ws, f"G{r}", None, F_BODY, FILL_IN, CENTER, fmt="dd-mmm-yyyy")
        ws.row_dimensions[r].height = 62
        r += 1
    ws.freeze_panes = "C5"
    landscape(ws, "4:4")


def sheet_history(wb, stats, meta):
    ws = wb.create_sheet("5_History_Reference")
    title(ws, "5 - What the plant has actually done (reference - do not edit)",
          f"Source: data/digital_twin.sqlite, table process_1min (1-minute PLC history, {meta['t0']} to {meta['t1']}). "
          f"Production hours only (feed_gate = 1, kiln feed above 461 t/h): {meta['prod_rows']:,} of {meta['rows']:,} "
          f"minutes. Fuel-flow percentiles exclude time at zero; '% of time at zero' is shown separately. "
          f"5-minute move = absolute change between a minute and the minute 5 later, both in production. "
          f"Maxima can include sensor spikes (the pipeline flags out-of-range values, it does not delete them).",
          span=16)
    ws.row_dimensions[2].height = 58
    for i, (h, g) in enumerate([("Tag", "ref"), ("Parameter", "ref"), ("Unit", "ref"), ("% of time at zero", "hist"),
                                ("P1", "hist"), ("P5", "hist"), ("Median", "hist"), ("P95", "hist"), ("P99", "hist"),
                                ("Max", "hist"), ("5-min move median", "soft"), ("5-min move P95", "soft"),
                                ("5-min move P99", "soft"), ("5-min move max", "soft"),
                                ("PLC range min (tag table)", "chk"), ("PLC range max (tag table)", "chk")], 1):
        cell(ws, f"{L(i)}5", h, F_HEAD, GROUP[g], CENTER)
    ws.row_dimensions[5].height = 44
    widths(ws, {"A": 17, "B": 36, "C": 8, "D": 12, "E": 10, "F": 10, "G": 10, "H": 10, "I": 10, "J": 10,
                "K": 12, "L": 12, "M": 12, "N": 12, "O": 14, "P": 14})
    for tag, name, unit in HIST_TAGS:
        r = HIST_ROW[tag]
        s = stats[tag]
        dp = DECIMALS[tag]
        fmt = "0" if dp == 0 else f"0.{'0' * dp}"
        cell(ws, f"A{r}", tag, F_BODY, FILL_GREY)
        cell(ws, f"B{r}", name, F_BOLD, FILL_GREY)
        cell(ws, f"C{r}", unit, F_BODY, FILL_GREY, CENTER)
        cell(ws, f"D{r}", round(s["zero_pct"], 4), F_DATA, FILL_GREY, CENTER, fmt="0.0%")
        for col, k in zip("EFGHIJ", ("p1", "p5", "p50", "p95", "p99", "mx")):
            cell(ws, f"{col}{r}", round(float(s[k]), 3), F_DATA, FILL_GREY, CENTER, fmt=fmt)
        for col, k in zip("KLMN", ("d50", "d95", "d99", "dmx")):
            cell(ws, f"{col}{r}", round(float(s[k]), 3), F_DATA, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"O{r}", float(s["ymin"]), F_DATA, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"P{r}", float(s["ymax"]), F_DATA, FILL_GREY, CENTER, fmt=fmt)
    n = HIST_FIRST_ROW + len(HIST_TAGS) + 1
    for t in ("All blue numbers on this sheet were computed from the history database by docs/make_guardrail_xlsx.py "
              "and are not edited by hand. Re-run the script to refresh them.",
              "Kiln coal (467RL300_TPH), kiln feed, preheater fan and kiln speed are held fixed by the advisor "
              "(plant decision) and are not listed."):
        ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=14)
        cell(ws, f"A{n}", t, F_NOTE, None, WRAP, border=None)
        ws.row_dimensions[n].height = 28
        n += 1
    ws.freeze_panes = "C6"
    landscape(ws, "5:5")


def sheet_recent(wb):
    ws = wb.create_sheet("6_Recent_Recommendations")
    title(ws, "6 - A dashboard snapshot against plant history (reference)",
          "Source: advisory dashboard screenshot supplied by the project lead, 19 Oct 2026, computed 11:43 for 11:48 "
          "(blue numbers). The comparison columns are formulas against sheet 5. This shows why plant limits are "
          "needed: the advisor's only brake today is its own +/-100%-of-current step cap.", span=14)
    ws.row_dimensions[2].height = 44
    hdr = ["Parameter", "Tag", "Unit", "Current", "Recommended", "Change", "Change as % of current",
           "Plant 5-min move P95", "Plant 5-min move P99", "Plant P5", "Plant P95",
           "Is the STEP normal?", "Is the TARGET normal?"]
    wd = [34, 17, 8, 11, 13, 11, 14, 13, 13, 10, 10, 40, 24]
    for i, (h, w) in enumerate(zip(hdr, wd), 1):
        cell(ws, f"{L(i)}5", h, F_HEAD, GROUP["ref"] if i <= 7 else GROUP["hist"] if i <= 11 else GROUP["chk"], CENTER)
        ws.column_dimensions[L(i)].width = w
    ws.row_dimensions[5].height = 44
    data = [("Calciner outlet temperature", "423CI400_TT_4", "degC", 871.7, 876.3),
            ("Calciner coal firing", "467RL400_TPH", "t/h", 24.62, 27.24),
            ("LAFR feed rate PC", "LAFR_FT", "l/h", 1217, 2426),
            ("LAFR feed rate TAD", "K3V10_F1", "l/h", None, 1017),
            ("SAFR feed rate", "261WF080_TPH", "t/h", 11.60, 7.86)]
    r = 6
    for name, tag, unit, cur, rec in data:
        hr = HIST_ROW[tag]
        dp = DECIMALS[tag]
        fmt = "0" if dp == 0 else f"0.{'0' * dp}"
        cell(ws, f"A{r}", name, F_BOLD, FILL_GREY)
        cell(ws, f"B{r}", tag, F_BODY, FILL_GREY)
        cell(ws, f"C{r}", unit, F_BODY, FILL_GREY, CENTER)
        cell(ws, f"D{r}", cur if cur is not None else "no reading", F_DATA if cur is not None else F_NOTE,
             FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"E{r}", rec, F_DATA, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"F{r}", f'=IF(AND(ISNUMBER(D{r}),ISNUMBER(E{r})),E{r}-D{r},"")', F_BODY, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"G{r}", f'=IF(AND(ISNUMBER(F{r}),ISNUMBER(D{r})),IF(D{r}<>0,F{r}/D{r},""),"")',
             F_BODY, FILL_GREY, CENTER, fmt="0.0%")
        cell(ws, f"H{r}", f"={HS}!L{hr}", F_LINK, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"I{r}", f"={HS}!M{hr}", F_LINK, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"J{r}", f"={HS}!F{hr}", F_LINK, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"K{r}", f"={HS}!H{hr}", F_LINK, FILL_GREY, CENTER, fmt=fmt)
        cell(ws, f"L{r}", (f'=IF(NOT(ISNUMBER(F{r})),"No live reading, so no step to judge",'
                           f'IF(ABS(F{r})>I{r},"LARGER than 99% of the plant\'s own 5-minute moves",'
                           f'IF(ABS(F{r})>H{r},"Larger than 95% of the plant\'s own 5-minute moves",'
                           f'"Within the plant\'s normal 5-minute moves")))'),
             F_BODY, FILL_GREY, CENTER)
        cell(ws, f"M{r}", (f'=IF(E{r}>K{r},"Above the plant\'s P95",IF(E{r}<J{r},"Below the plant\'s P5",'
                           f'"Within the plant\'s P5-P95"))'), F_BODY, FILL_GREY, CENTER)
        ws.row_dimensions[r].height = 34
        r += 1
    n = r + 1
    for t in ("Reading this: the TARGET values are inside what the plant normally runs, but the LAFR PC STEP is larger "
              "than 95% of anything the plant does in 5 minutes - and at +99.3% of current it sits right at the "
              "software's own 100% cap. Operator limits on 'MAX SINGLE STEP' (sheet 1) would replace that cap.",
              "A recommendation is for the next 5 minutes and is advisory only; accepting it logs the decision and "
              "does not write to the DCS."):
        ws.merge_cells(start_row=n, start_column=1, end_row=n, end_column=13)
        cell(ws, f"A{n}", t, F_NOTE, None, WRAP, border=None)
        ws.row_dimensions[n].height = 32
        n += 1
    ws.freeze_panes = "B6"
    landscape(ws, "5:5")


def build() -> Path:
    stats, meta = compute_history()
    wb = Workbook()
    sheet_readme(wb)
    sheet_guardrails(wb, stats)
    sheet_fuel(wb)
    sheet_safety(wb)
    sheet_questions(wb)
    sheet_history(wb, stats, meta)
    sheet_recent(wb)
    wb.calculation.fullCalcOnLoad = True      # Excel computes the formulas on open
    wb.properties.title = "Kiln-3 Calciner Advisor - Guardrail Questionnaire"
    wb.properties.creator = "Calcination Combustion Control"
    wb.save(OUT)
    return OUT


if __name__ == "__main__":
    p = build()
    print(f"wrote {p}  ({p.stat().st_size:,} bytes)")
