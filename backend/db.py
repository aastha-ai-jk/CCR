"""Database access layer. One engine, cached metadata, safe tag validation."""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text

import config

_url = config.database_url()
_kw = {"connect_args": {"timeout": 15}} if _url.startswith("sqlite") else {}
_engine = create_engine(_url, pool_pre_ping=True, **_kw)


def engine():
    return _engine


@lru_cache(maxsize=1)
def tag_catalog() -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM dim_tag", _engine)


@lru_cache(maxsize=1)
def valid_tags() -> frozenset[str]:
    return frozenset(tag_catalog()["tag"].tolist())


@lru_cache(maxsize=1)
def tag_names() -> dict[str, str]:
    c = tag_catalog()
    return dict(zip(c["tag"], c["name"]))


def safe_tags(tags: list[str]) -> list[str]:
    """Keep only tags that exist — prevents SQL injection via column names."""
    v = valid_tags()
    return [t for t in tags if t in v]


def q(sql: str, params: dict | None = None) -> pd.DataFrame:
    return pd.read_sql(text(sql), _engine, params=params or {})


def log_advisory_decision(row: dict) -> None:
    """Audit trail: operator's accept/decline of a recommended move, with the
    reasoning that was shown at the time (never re-derived later, so the log
    reflects what the operator actually saw)."""
    with _engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS advisory_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                decision TEXT NOT NULL,
                operating_state TEXT,
                mode TEXT,
                target_band_low REAL,
                target_band_high REAL,
                live_actual_temp REAL,
                predicted_temp REAL,
                moves_json TEXT,
                reasoning TEXT,
                note TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO advisory_decisions
                (ts, decision, operating_state, mode, target_band_low, target_band_high,
                 live_actual_temp, predicted_temp, moves_json, reasoning, note)
            VALUES
                (:ts, :decision, :operating_state, :mode, :target_band_low, :target_band_high,
                 :live_actual_temp, :predicted_temp, :moves_json, :reasoning, :note)
        """), row)
