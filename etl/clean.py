"""Cleaning transforms for the four data rivers.

Design contract (see docs/ROADMAP.md §1):
  * strip PLC header/metadata rows -> tag dictionary lives in dim_tag
  * robust mixed-format timestamp parsing (day/month-swapped Excel cells + text)
  * physical range check against each tag's engineering Y-min/Y-max
  * lab timestamps parsed to a common index for as-of joins
"""
from __future__ import annotations

import glob
from datetime import datetime

import pandas as pd

import config
from etl.tags import build_tag_dictionary


# ---------------------------------------------------------------------------
def parse_time_cell(v):
    """PLC Time cells are mixed: Excel datetimes stored day/month-swapped for
    day<=12, and US 'MM/DD/YYYY HH:MM:SS' text for day>12. Normalise both."""
    if pd.isna(v):
        return pd.NaT
    if isinstance(v, (pd.Timestamp, datetime)):
        d = pd.Timestamp(v)
        if d.day <= 12:
            try:
                return pd.Timestamp(year=d.year, month=d.day, day=d.month,
                                    hour=d.hour, minute=d.minute, second=d.second)
            except ValueError:
                return d
        return d
    return pd.to_datetime(str(v), format="%m/%d/%Y %H:%M:%S", errors="coerce")


def _plc_files() -> list[str]:
    return sorted(glob.glob(str(config.PLC_DIR / "*.xlsx")))


def clean_plc_file(path: str, ranges: dict[str, tuple[float, float]]) -> tuple[pd.DataFrame, dict]:
    """Return (clean wide dataframe indexed by ts, per-file oor-count dict)."""
    df = pd.read_excel(path, sheet_name=0, skiprows=[1, 2, 3, 4], header=0)
    df["ts"] = df["Time"].map(parse_time_cell)
    df = df.drop(columns=["Time"]).dropna(subset=["ts"])
    df = df.drop_duplicates(subset=["ts"]).sort_values("ts").set_index("ts")

    oor: dict[str, int] = {}
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        if col in ranges:
            lo, hi = ranges[col]
            if pd.notna(lo) and pd.notna(hi) and hi > lo:
                mask = (df[col] < lo) | (df[col] > hi)
                oor[col] = int(mask.sum())
                # FLAG only, do NOT delete: some engineering Y-max values in the
                # PLC metadata are unreliable (e.g. 423CN_TEMP Y-max=100 vs real
                # ~330 degC). Out-of-range counts feed the QA dashboard; the twin
                # stage applies robust outlier handling. Only kill impossible negatives.
                if pd.notna(lo) and lo >= 0:
                    df.loc[df[col] < lo, col] = pd.NA
    return df, oor


def load_all_plc() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Clean + concatenate every PLC month.

    Returns (process_wide, dim_tag, data_quality)."""
    dim = build_tag_dictionary()
    ranges = {r.tag: (r.y_min, r.y_max) for r in dim.itertuples()}

    frames, oor_total = [], {}
    for f in _plc_files():
        wide, oor = clean_plc_file(f, ranges)
        frames.append(wide)
        for k, v in oor.items():
            oor_total[k] = oor_total.get(k, 0) + v
        print(f"  [plc] {f.split(chr(92))[-1]:<20} rows={len(wide):>7}  {wide.index.min()} -> {wide.index.max()}")

    process = pd.concat(frames).sort_index()
    process = process[~process.index.duplicated(keep="last")]

    # derived: applicability gate from the logic doc
    if config.KILN_FEED_TAG in process.columns:
        process["feed_gate"] = process[config.KILN_FEED_TAG] > config.KILN_FEED_MIN_TPH

    # data-quality summary per tag
    dq_rows = []
    n = len(process)
    for r in dim.itertuples():
        if r.tag not in process.columns:
            continue
        s = process[r.tag]
        dq_rows.append({
            "tag": r.tag, "name": r.name, "control_role": r.control_role,
            "n": n, "n_valid": int(s.notna().sum()),
            "pct_valid": round(100 * s.notna().mean(), 2),
            "n_out_of_range": int(oor_total.get(r.tag, 0)),
            "vmin": float(s.min()) if s.notna().any() else None,
            "vmax": float(s.max()) if s.notna().any() else None,
            "vmean": float(s.mean()) if s.notna().any() else None,
        })
    dq = pd.DataFrame(dq_rows)
    return process.reset_index(), dim, dq


def _parse_lab_dt(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.astype(str).str.replace(".0$", "", regex=True), errors="coerce")


def load_hotmeal() -> pd.DataFrame:
    path = config.HOTMEAL_DIR / "hotmeal_A_and_B_20250721_20260721.xlsx"
    out = []
    for sheet, side in [("Side A", "A"), ("Side B", "B")]:
        d = pd.read_excel(path, sheet_name=sheet)
        d = d.rename(columns={"xrf test datetime": "ts", "CL": "cl", "SO3": "so3",
                              "Na2O": "na2o", "K2O": "k2o", "LOI": "loi", "DOC": "doc"})
        d["ts"] = _parse_lab_dt(d["ts"])
        d["side"] = side
        out.append(d[["ts", "side", "na2o", "k2o", "so3", "cl", "loi", "doc"]])
    df = pd.concat(out).dropna(subset=["ts"]).sort_values("ts")
    return df.reset_index(drop=True)


# eLIMS section for THIS kiln. result.xlsx (QC_NBH.py) holds one sheet per
# section (CLK-1, CLK-2, CLK-3, ...); reading the first sheet loaded Kiln 1.
CLINKER_SECTION = "CLK-3"
_CLINKER_COLS = {"xrf test datetime": "ts", "Degree of Calcination": "doc",
                 "Cl": "cl", "CL": "cl", "So3": "so3", "SO3": "so3",
                 "Ltr.Wt.": "liter_weight", "lime": "free_lime", "C3S": "c3s", "LIQ": "liq"}
CLINKER_COLUMNS = ["ts", "doc", "cl", "so3", "liter_weight", "free_lime", "c3s", "liq"]


def load_clinker() -> pd.DataFrame:
    """Kiln-3 clinker QC from result.xlsx: the CLINKER_SECTION sheet when the
    workbook has one sheet per section, else rows of that section only."""
    path = config.QC_DIR / "result.xlsx"
    sheets = pd.ExcelFile(path).sheet_names
    if CLINKER_SECTION in sheets:
        d = pd.read_excel(path, sheet_name=CLINKER_SECTION)
    else:
        d = pd.read_excel(path, sheet_name=0)
        if "section name" in d.columns:
            d = d[d["section name"] == CLINKER_SECTION]
    d = d.rename(columns=_CLINKER_COLS)
    for c in CLINKER_COLUMNS:
        if c not in d.columns:
            d[c] = pd.NA
    d = d[CLINKER_COLUMNS].copy()
    d["ts"] = (pd.to_datetime(d["ts"], errors="coerce")
               if pd.api.types.is_datetime64_any_dtype(d["ts"]) else _parse_lab_dt(d["ts"]))
    d = validate_clinker(d)
    d = d.dropna(subset=["ts"]).drop_duplicates(subset=["ts"]).sort_values("ts")
    return d.reset_index(drop=True)


def load_clinker_csv(path) -> pd.DataFrame:
    """Plant monthly clinker reports merged to one CSV (columns Time, Ltr.Wt.,
    F.CAO, C3S, LIQ, source_file). F.CAO = free lime. Times are rounded to the
    hour; refresh_clinker drops rows that duplicate an eLIMS sample."""
    d = pd.read_csv(path)
    d = d.rename(columns={"Time": "ts", "F.CAO": "free_lime", "Ltr.Wt.": "liter_weight",
                          "C3S": "c3s", "LIQ": "liq"})
    for c in CLINKER_COLUMNS:
        if c not in d.columns:
            d[c] = pd.NA
    d = d[CLINKER_COLUMNS].copy()
    d["ts"] = pd.to_datetime(d["ts"], errors="coerce")
    d = validate_clinker(d)
    return d.dropna(subset=["ts"]).drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)


def validate_clinker(d: pd.DataFrame) -> pd.DataFrame:
    """Numeric coerce + blank physically impossible values (row kept)."""
    d = d.copy()
    for c in CLINKER_COLUMNS[1:]:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
            lo, hi = _CLINKER_VALID.get(c, (None, None))
            if lo is not None:
                d.loc[~d[c].between(lo, hi), c] = pd.NA
    return d


# Physically possible ranges; outside = lab entry / instrument error (seen:
# free lime -4.24 and 110.78 %, liter weight 0, C3S -332).
_CLINKER_VALID = {"free_lime": (0.0, 10.0), "liter_weight": (800.0, 1600.0),
                  "c3s": (0.0, 100.0), "liq": (0.0, 60.0), "so3": (0.0, 10.0),
                  "cl": (0.0, 5.0), "doc": (50.0, 100.0)}
