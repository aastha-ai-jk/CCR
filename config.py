"""Central configuration for the Calcination Combustion Control digital-twin project.

Everything (ETL + API) reads the database location from here, so switching
between SQLite (bootstrap) and PostgreSQL (production) is a one-line change in .env.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --- Database -------------------------------------------------------------
USE_SQLITE = os.getenv("USE_SQLITE", "0") == "1"
_SQLITE_PATH = os.getenv("SQLITE_PATH", "./data/digital_twin.sqlite")


def sqlite_url() -> str:
    p = (ROOT / _SQLITE_PATH).resolve() if not os.path.isabs(_SQLITE_PATH) else Path(_SQLITE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{p.as_posix()}"


def database_url() -> str:
    """Return the active SQLAlchemy URL (SQLite bootstrap or configured Postgres)."""
    if USE_SQLITE:
        return sqlite_url()
    return os.getenv("DATABASE_URL", sqlite_url())


# --- Raw data locations ---------------------------------------------------
PLC_DIR = Path(os.getenv("PLC_DIR", r"D:/PLC Historical Data"))
HOTMEAL_DIR = Path(os.getenv("HOTMEAL_DIR", r"D:/hotmeal"))
QC_DIR = Path(os.getenv("QC_DIR", r"D:/QC_NBH"))
OPC_DIR = Path(os.getenv("OPC_DIR", r"D:/OPC"))
TAGS_XLSX = Path(os.getenv("TAGS_XLSX", r"D:/QC_NBH/PLC & QC Data and Tags (1).xlsx"))

# 1-second-resolution PLC export (same 31 tags as the monthly PLC_DIR .xlsx
# files, just finer-grained and more current). Used by etl.load_csv_1s to
# refresh process_1min with a higher-fidelity source -> feeds the ML twin.
CSV_1S_PATH = Path(os.getenv("CSV_1S_PATH", r"D:/ccr_clean_1s.csv"))

# --- Domain constants (from Combustion Control logic doc) -----------------
KILN_FEED_TAG = "353RL130_TPH"
KILN_FEED_MIN_TPH = 461.0          # control actions only apply above this
CALCINER_TEMP_TAG = "423CI400_TT_4"

# Safety interlocks (ppm / %) — the twin's recommendations must respect these
INTERLOCKS = {
    "423AM823_CO_PPM": ("<", 1500),   # PC outlet CO
    "423AM823_O2":     (">", 1.5),    # PC outlet O2
    "423AM822_CO_PPM": ("<", 800),    # kiln inlet CO
    "423AM822_O2":     (">", 0.5),    # kiln inlet O2
    "423AM820_CO_PPM": ("<", 3000),   # PH outlet CO string-1
    "423AM821_CO_PPM": ("<", 3000),   # PH outlet CO string-2
}

# --- Fuel economics (for the savings breakdown) ---------------------------
# Prices are OPTIONAL and default to 0 = "not configured". When a price is 0 the
# app shows ENERGY ONLY for that stream and no money figure, so nothing is ever
# fabricated. Put your own landed costs in .env to switch currency on.
CURRENCY = os.getenv("CURRENCY", "INR")
COAL_PRICE_PER_T = float(os.getenv("COAL_PRICE_PER_T", "0") or 0)
KILN_COAL_PRICE_PER_T = float(os.getenv("KILN_COAL_PRICE_PER_T", "0") or 0) or COAL_PRICE_PER_T
LAFR_PRICE_PER_L = float(os.getenv("LAFR_PRICE_PER_L", "0") or 0)
SAFR_PRICE_PER_T = float(os.getenv("SAFR_PRICE_PER_T", "0") or 0)

# Calorific values (kcal/kg) — CCR screen: Coal CV 5200, AFR Calorific SP 2500.
COAL_CV_KCAL_KG = float(os.getenv("COAL_CV_KCAL_KG", "5200"))
AFR_LIQ_CV_KCAL_KG = float(os.getenv("AFR_LIQ_CV_KCAL_KG", "2500"))
AFR_SOL_CV_KCAL_KG = float(os.getenv("AFR_SOL_CV_KCAL_KG", "2500"))
# Liquid AFR is metered volumetrically (l/h); density converts it to mass.
LAFR_DENSITY_KG_PER_L = float(os.getenv("LAFR_DENSITY_KG_PER_L", "1.0"))

# Clinker output per tonne of kiln feed — used only to express the saving as
# specific heat consumption (kcal/kg clinker).
CLINKER_PER_FEED = float(os.getenv("CLINKER_PER_FEED", "0.63"))

KCAL_TO_GJ = 4.1868e-6          # 1 kcal = 4.1868 kJ

# Dynamic target band selection (State -> calciner temp band in degC)
TEMP_STATE_BANDS = {
    "State 1": (920, 940),
    "State 2": (900, 920),
    "State 3": (880, 900),
}
