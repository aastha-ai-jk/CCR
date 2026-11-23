"""Fold matured live-feed data into the training historian.

``etl.live_feed`` streams the plant into ``process_live`` (~2 s cadence).
This job resamples those rows to 1-minute means and appends them to
``process_1min`` — the table every model trains on — so retraining actually
learns the plant's CURRENT regime instead of drifting away from it.

Idempotent: only minutes newer than the historian's last timestamp (and older
than now-5 min, so partially-filled minutes never land) are inserted, in the
historian's existing timestamp format.

Run: python -m twin.ingest_live
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import create_engine, text

import config


def _engine():
    url = config.database_url()
    kw = {"connect_args": {"timeout": 30}} if url.startswith("sqlite") else {}
    return create_engine(url, **kw)


def run() -> int:
    eng = _engine()
    hist_cols = pd.read_sql("SELECT * FROM process_1min LIMIT 1", eng).columns.tolist()
    tags = [c for c in hist_cols if c not in ("ts", "feed_gate")]
    last = pd.read_sql("SELECT MAX(ts) AS m FROM process_1min", eng)["m"][0]

    # NB: historian ts = "YYYY-MM-DD HH:MM:SS.ffffff", live ts = ISO "…T…".
    # String comparison across the two formats over-selects on the same date,
    # so do a coarse date-prefix filter in SQL and the precise boundary in
    # pandas after parsing both sides properly.
    last_dt = pd.to_datetime(last, format="mixed")
    cutoff_dt = dt.datetime.now() - dt.timedelta(minutes=5)
    live = pd.read_sql(text("SELECT * FROM process_live WHERE substr(ts,1,10) >= :d0"),
                       eng, params={"d0": last_dt.strftime("%Y-%m-%d")})
    if not len(live):
        print("[ingest] nothing new")
        return 0

    live["ts"] = pd.to_datetime(live["ts"], errors="coerce", format="mixed")
    live = live.dropna(subset=["ts"])
    live = live[live["ts"] > last_dt].set_index("ts").sort_index()
    if not len(live):
        print("[ingest] nothing new after boundary")
        return 0
    keep = [c for c in tags if c in live.columns]
    for c in keep:
        live[c] = pd.to_numeric(live[c], errors="coerce")

    minute = live[keep].resample("1min").mean()
    minute = minute.dropna(how="all")
    minute = minute[minute.index < cutoff_dt.replace(second=0, microsecond=0)]
    if not len(minute):
        print("[ingest] no complete new minutes")
        return 0

    out = minute.reset_index()
    # match the historian's exact format (with microseconds) so every parser
    # downstream sees one consistent style
    out["ts"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    feed = pd.to_numeric(out.get(config.KILN_FEED_TAG), errors="coerce")
    out["feed_gate"] = (feed > config.KILN_FEED_MIN_TPH).astype(int)
    for c in tags:                                            # schema-complete row
        if c not in out.columns:
            out[c] = None
    out = out[hist_cols]

    out.to_sql("process_1min", eng, if_exists="append", index=False)
    print(f"[ingest] appended {len(out):,} minutes "
          f"({out['ts'].iloc[0]} -> {out['ts'].iloc[-1]}), "
          f"feed_gate=1 on {int(out['feed_gate'].sum()):,}")
    return len(out)


if __name__ == "__main__":
    run()
