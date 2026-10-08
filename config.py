"""Central configuration for the Calcination Combustion Control digital-twin project.

Everything (ETL + API) reads the database location from here, so switching
between SQLite (bootstrap) and PostgreSQL (production) is a one-line change in .env.
"""
from __future__ import annotations

import json
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

# Dynamic target band selection (State -> calciner temp band in degC)
TEMP_STATE_BANDS = {
    "State 1": (920, 940),
    "State 2": (900, 920),
    "State 3": (880, 900),
}

# --- Fuel economics -------------------------------------------------------
# ESTIMATED DEFAULTS — published-typical Indian-cement values, NOT this plant's
# real numbers. Override per-tag via data/fuel_economics.json (procurement can
# edit without touching code); any field left unset keeps the estimate. Every
# downstream KPI carries the is_estimate flag until real values are supplied.
#
# mass_per_unit converts a tag's native rate to kg/h:
#   t/h streams -> 1000 kg per unit;  l/h streams -> density (kg per litre).
CLINKER_FACTOR = float(os.getenv("CLINKER_FACTOR", "0.62"))   # raw feed t -> clinker t

_FUEL_DEFAULTS = {
    "467RL400_TPH": {"name": "Calciner coal", "role": "coal", "phase": "solid",
                     "unit": "t/h", "mass_per_unit": 1000.0,
                     "gcv_kcal_kg": 6000.0, "price_per_ton": 8000.0, "is_estimate": True},
    "LAFR_FT":      {"name": "Liquid AFR (PC)", "role": "afr", "phase": "liquid",
                     "unit": "l/h", "mass_per_unit": 0.95,
                     "gcv_kcal_kg": 5500.0, "price_per_ton": 2000.0, "is_estimate": True},
    "K3V10_F1":     {"name": "Liquid AFR (TAD)", "role": "afr", "phase": "liquid",
                     "unit": "l/h", "mass_per_unit": 0.95,
                     "gcv_kcal_kg": 5500.0, "price_per_ton": 2000.0, "is_estimate": True},
    "261WF080_TPH": {"name": "Solid AFR", "role": "afr", "phase": "solid",
                     "unit": "t/h", "mass_per_unit": 1000.0,
                     "gcv_kcal_kg": 4000.0, "price_per_ton": 2500.0, "is_estimate": True},
}


def fuel_economics() -> dict:
    """tag -> economics meta: estimated defaults merged with optional overrides
    from data/fuel_economics.json. Overridden fields lose the is_estimate flag
    only when the override explicitly sets it False."""
    out = {k: dict(v) for k, v in _FUEL_DEFAULTS.items()}
    override = ROOT / "data" / "fuel_economics.json"
    if override.exists():
        try:
            user = json.loads(override.read_text())
            for tag, meta in user.items():
                if tag in out and isinstance(meta, dict):
                    out[tag].update(meta)
        except (json.JSONDecodeError, OSError):
            pass
    # plant_settings.json "fuels" wins over both (a set value = no longer an estimate)
    for tag, meta in ((plant_settings().get("fuels") or {}).items()):
        if tag in out and isinstance(meta, dict):
            given = {k: v for k, v in meta.items() if v is not None and not k.startswith("_")}
            out[tag].update(given)
            if "gcv_kcal_kg" in given and "price_per_ton" in given:
                out[tag]["is_estimate"] = False
    return out


# Clinker quality specification (ESTIMATED — edit to plant QC targets, or set
# FREE_LIME_MAX / DOC_MIN in .env). Used as a HARD constraint by the optimiser.
QUALITY_SPECS = {
    "free_lime": {"max": float(os.getenv("FREE_LIME_MAX", "1.8")), "is_estimate": True},
    "doc":       {"min": float(os.getenv("DOC_MIN", "92.0")),      "is_estimate": True},
}

# Objective emphasis: "cost" (min ₹/ton clinker) | "tsr" (max thermal
# substitution) | "coal" (min coal tph). All three KPIs reported regardless.
OPTIMIZER_MODE = os.getenv("OPTIMIZER_MODE", "cost")


# --- Plant settings (data/plant_settings.json) -----------------------------
# One plant-editable file for every value the plant team confirms (see
# data/plant_settings.README.md for the question each key answers). A missing
# file, key or null value keeps the default above, so behaviour only changes
# when the plant sets a value. Bands, interlocks, quality specs, clinker factor,
# kiln-running threshold and state thresholds are applied here at import
# (restart the backend after editing); the optimizer section is read per run.
PLANT_SETTINGS_PATH = ROOT / "data" / "plant_settings.json"


def plant_settings() -> dict:
    """The parsed plant settings file, or {} if absent/unreadable."""
    try:
        return json.loads(PLANT_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _set(value, default):
    return default if value is None else value


def _apply_plant_settings() -> None:
    global CLINKER_FACTOR, KILN_FEED_MIN_TPH
    s = plant_settings()
    proc = s.get("process") or {}
    CLINKER_FACTOR = float(_set(proc.get("clinker_factor"), CLINKER_FACTOR))
    KILN_FEED_MIN_TPH = float(_set(proc.get("kiln_running_min_feed_tph"), KILN_FEED_MIN_TPH))
    for state, band in (s.get("temperature_bands") or {}).items():
        if state in TEMP_STATE_BANDS and band and None not in band:
            TEMP_STATE_BANDS[state] = (float(band[0]), float(band[1]))
    for tag, limit in (s.get("interlocks") or {}).items():
        if tag in INTERLOCKS and limit is not None:
            INTERLOCKS[tag] = (INTERLOCKS[tag][0], float(limit))
    q = s.get("quality") or {}
    if q.get("free_lime_max") is not None:
        QUALITY_SPECS["free_lime"] = {"max": float(q["free_lime_max"]), "is_estimate": False}
    if q.get("doc_min") is not None:
        QUALITY_SPECS["doc"] = {"min": float(q["doc_min"]), "is_estimate": False}


_apply_plant_settings()
