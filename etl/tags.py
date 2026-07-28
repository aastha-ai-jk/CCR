"""Build the tag dictionary (dim_tag).

Combines three sources of truth:
  * PLC monthly file header rows  -> engineering Y-min / Y-max / unit / description
  * PLC & QC Tags mapping file     -> human parameter name + OPC AI address
  * ROLE_MAP below (domain)        -> control role + subsystem, from the logic doc

The result is the catalogue that powers the Data Explorer, drives out-of-range
outlier flagging, and defines which signals the twin/optimizer treat as
target / manipulated / disturbance / quality / status.
"""
from __future__ import annotations

import glob
import pandas as pd

import config

# code -> (control_role, subsystem)
# roles: target | safety | mv | dv | quality | status
ROLE_MAP: dict[str, tuple[str, str]] = {
    "423CI400_TT_4":  ("target", "Calciner"),      # PRIMARY controlled variable
    # combustion-safety measurements (interlocks)
    "423AM823_CO_PPM": ("safety", "Calciner"),
    "423AM823_O2":     ("safety", "Calciner"),
    "423AM822_CO_PPM": ("safety", "Kiln inlet"),
    "423AM822_O2":     ("safety", "Kiln inlet"),
    "423AM820_CO_PPM": ("safety", "Preheater"),
    "423AM821_CO_PPM": ("safety", "Preheater"),
    "423AM820_O2":     ("dv", "Preheater"),
    "423AM821_O2":     ("dv", "Preheater"),
    # manipulated variables (levers)
    "467RL400_TPH": ("mv", "Calciner"),            # calciner coal  (primary lever)
    "LAFR_FT":      ("mv", "Calciner"),            # liquid AFR -> PC
    "K3V10_F1":     ("mv", "Calciner"),            # liquid AFR -> TAD (tags file: K3V10F1)
    "261WF080_TPH": ("mv", "Calciner"),            # solid AFR
    "467RL300_TPH": ("mv", "Kiln"),                # kiln coal (kiln-side lever)
    "423FN010_ST":  ("mv", "Preheater"),           # PH fan speed / draft (optional lever)
    # disturbance / state variables (measured, define the target band)
    "353RL130_TPH": ("dv", "Kiln"),                # kiln feed (>461 tph gate)
    "433MD140_TQ":  ("dv", "Kiln"),                # torque   (state selector)
    "433MD140_ST1": ("dv", "Kiln"),                # kiln speed
    "423AM822_NO":  ("dv", "Kiln inlet"),          # NOx      (state selector)
    "443KH050_TT1": ("dv", "Kiln"),                # secondary air temp (state selector)
    "423DU415_TT":  ("dv", "Kiln"),                # tertiary air temp
    "423CN_TEMP":   ("dv", "Preheater"),           # PH outlet temp
    "423CN150_TT_1": ("dv", "Preheater"),          # cyclone-5 string-1 exit temp
    "423CN250_TT_1": ("dv", "Preheater"),          # cyclone-5 string-2 exit temp
    "423KR405_TT_3": ("dv", "Preheater"),          # feed-pipe temp s1
    "423KR405_TT_4": ("dv", "Preheater"),          # feed-pipe temp s2
    "423FN010_IT":  ("dv", "Preheater"),           # PH fan load
    # analyzer status
    "423AM820_RUN": ("status", "Preheater"),
    "423AM821_RUN": ("status", "Preheater"),
    "423AM823_RUN": ("status", "Calciner"),
    "423AM822_RUN": ("status", "Kiln inlet"),
}


def _latest_plc_file() -> str:
    files = glob.glob(str(config.PLC_DIR / "*.xlsx"))
    if not files:
        raise FileNotFoundError(f"No PLC xlsx found in {config.PLC_DIR}")
    return sorted(files)[-1]


def _read_tag_mapping() -> pd.DataFrame:
    """Parse the 'PLC' sheet of the tags mapping file into code -> (name, ai_addr)."""
    raw = pd.read_excel(config.TAGS_XLSX, sheet_name="PLC", header=None)
    # header row is the one containing 'S.No.'
    hdr = raw.index[raw[0].astype(str).str.strip() == "S.No."][0]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = ["sno", "parameter", "uom", "fls_tag", "ai_tag"] + list(df.columns[5:])
    df = df.dropna(subset=["fls_tag"])
    df["fls_tag"] = df["fls_tag"].astype(str).str.strip()
    return df[["parameter", "uom", "fls_tag", "ai_tag"]]


def build_tag_dictionary() -> pd.DataFrame:
    plc = pd.read_excel(_latest_plc_file(), sheet_name=0, nrows=4)  # header + 3 meta rows
    meta = plc.set_index("Time").T  # index = tag code, cols = Y-max/Y-min/Unit/Description
    meta.columns = [str(c).strip() for c in meta.columns]

    mapping = _read_tag_mapping().set_index("fls_tag")

    rows = []
    for code in meta.index:
        m = meta.loc[code]
        role, subsystem = ROLE_MAP.get(code, ("dv", "Other"))
        # reconcile TAD naming difference between PLC file (K3V10_F1) and tags file (K3V10F1)
        map_key = "K3V10F1" if code == "K3V10_F1" else code
        name = mapping.loc[map_key, "parameter"] if map_key in mapping.index else code
        ai_tag = mapping.loc[map_key, "ai_tag"] if map_key in mapping.index else None
        rows.append({
            "tag": code,
            "name": str(name).rstrip(": ").strip() if pd.notna(name) else code,
            "uom": (None if pd.isna(m.get("Unit")) else str(m.get("Unit")).replace("�", "deg")),
            "y_min": pd.to_numeric(m.get("Y-min"), errors="coerce"),
            "y_max": pd.to_numeric(m.get("Y-max"), errors="coerce"),
            "description": None if pd.isna(m.get("Description")) else str(m.get("Description")),
            "control_role": role,
            "subsystem": subsystem,
            "ai_tag": None if (ai_tag is None or pd.isna(ai_tag)) else str(ai_tag),
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    d = build_tag_dictionary()
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(d.to_string())
    print("\nrole counts:\n", d["control_role"].value_counts())
