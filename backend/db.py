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


def _ensure_advisory_decisions_table(conn) -> None:
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
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(advisory_decisions)")).fetchall()}
    if "reason_category" not in cols:
        conn.execute(text("ALTER TABLE advisory_decisions ADD COLUMN reason_category TEXT"))


def log_advisory_decision(row: dict) -> None:
    """Audit trail: operator's accept/decline of a recommended move, with the
    reasoning that was shown at the time (never re-derived later, so the log
    reflects what the operator actually saw)."""
    row.setdefault("reason_category", None)
    with _engine.begin() as conn:
        _ensure_advisory_decisions_table(conn)
        conn.execute(text("""
            INSERT INTO advisory_decisions
                (ts, decision, operating_state, mode, target_band_low, target_band_high,
                 live_actual_temp, predicted_temp, moves_json, reasoning, note, reason_category)
            VALUES
                (:ts, :decision, :operating_state, :mode, :target_band_low, :target_band_high,
                 :live_actual_temp, :predicted_temp, :moves_json, :reasoning, :note, :reason_category)
        """), row)


def recent_advisory_decisions(limit: int = 50) -> list[dict]:
    """Most recent decisions first, for the shift-dashboard decision log and
    its aggregate charts. Empty list if nothing logged yet."""
    try:
        df = q("SELECT * FROM advisory_decisions ORDER BY id DESC LIMIT :n", {"n": limit})
    except Exception:
        return []
    rows = df.to_dict("records")
    for r in rows:
        for k, v in r.items():
            try:
                if pd.isna(v):
                    r[k] = None
            except (TypeError, ValueError):
                pass
    return rows


def latest_decision_for_situation(operating_state: str, direction: str) -> dict | None:
    """The most recent operator decision (accept OR decline) for this
    operating state + temperature direction (above/below/within its band) --
    the "situation" the operator is currently facing. Direction is derived
    from each historical row's own band/temp rather than stored directly, so
    it stays correct even if band definitions are ever retuned.

    Used to "lock in" a decline: if the last decision for this exact
    situation was a decline, the operator's own reasoning is resurfaced next
    time the plant lands back in it -- until a future accept for the same
    situation clears the lock."""
    try:
        df = q("""
            SELECT ts, decision, target_band_low, target_band_high, live_actual_temp,
                   predicted_temp, moves_json, reasoning, note, reason_category
            FROM advisory_decisions
            WHERE operating_state = :state
            ORDER BY id DESC
        """, {"state": operating_state})
    except Exception:
        return None  # table not created yet -- no decisions logged so far
    for _, row in df.iterrows():
        lo, hi, t = row["target_band_low"], row["target_band_high"], row["live_actual_temp"]
        if pd.isna(lo) or pd.isna(hi) or pd.isna(t):
            continue
        d = "above" if t > hi else "below" if t < lo else "within"
        if d == direction:
            return row.to_dict()
    return None
