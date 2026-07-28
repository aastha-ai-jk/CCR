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
