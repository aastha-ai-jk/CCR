"""Refresh process_1min from a 1-second-resolution PLC export (config.CSV_1S_PATH).

This is a higher-fidelity, more current alternative to the monthly PLC_DIR
.xlsx ingestion in etl.load: same 31 tags, 1-second native rate instead of
1-minute, and (in the UAT dataset) extends coverage into Aug 2026. It feeds
the ML twin (etl.train_gbt_twin) with the finest-grained data available.

The file is read in CHUNKS and aggregated to per-minute MEANS via running
sum/count (both additive across chunks) so a chunk boundary never corrupts a
minute's average -- the same approach validated in nn_twin/prep_1min.py.

Run:  python -m etl.load_csv_1s
Effect: REPLACES the process_1min table in config.database_url() (dim_tag,
lab_hotmeal, lab_clinker, roadmap_milestone are untouched).
"""
from __future__ import annotations

import time

import pandas as pd
from sqlalchemy import create_engine, text

import config

CHUNK = 3_000_000
BLANKS = ["", " ", "NA", "N/A", "null", "None"]


def resample_1s_to_1min(csv_path) -> pd.DataFrame:
    header = pd.read_csv(csv_path, nrows=0)
    tags = [c for c in header.columns if c != "Time"]
    print(f"[csv1s] {len(tags)} tag columns in {csv_path}")

    sums: dict[str, pd.Series] = {}
    counts: dict[str, pd.Series] = {}
    n_rows = 0
    t0 = time.time()

    reader = pd.read_csv(csv_path, chunksize=CHUNK, na_values=BLANKS,
                         dtype={t: "float32" for t in tags})
    for i, chunk in enumerate(reader, 1):
        n_rows += len(chunk)
        minute = chunk["Time"].str.slice(0, 16)          # 'YYYY-MM-DD HH:MM'
        g = chunk[tags].groupby(minute)
        s, c = g.sum(min_count=1), g.count()
        for t in tags:
            sums[t] = s[t] if t not in sums else sums[t].add(s[t], fill_value=0)
            counts[t] = c[t] if t not in counts else counts[t].add(c[t], fill_value=0)
        print(f"  [csv1s] chunk {i:>3}  rows so far {n_rows:>12,}  "
              f"{time.time()-t0:6.1f}s")

    idx = sorted(set().union(*[s.index for s in sums.values()]))
    out = pd.DataFrame(index=pd.Index(idx, name="minute"))
    for t in tags:
        tot, cnt = sums[t].reindex(idx), counts[t].reindex(idx)
        out[t] = (tot / cnt).astype("float32")

    out = out.reset_index()
    out["ts"] = pd.to_datetime(out["minute"], format="%Y-%m-%d %H:%M")
    out = out.drop(columns="minute").sort_values("ts").reset_index(drop=True)

    # applicability gate, matching etl.clean.load_all_plc exactly (config.py
    # KILN_FEED_TAG / KILN_FEED_MIN_TPH, strict '>')
    if config.KILN_FEED_TAG in out.columns:
        out["feed_gate"] = out[config.KILN_FEED_TAG] > config.KILN_FEED_MIN_TPH

    print(f"[csv1s] resampled {n_rows:,} 1s rows -> {len(out):,} 1min rows  "
          f"{out['ts'].min()} -> {out['ts'].max()}  ({time.time()-t0:.1f}s)")
    return out


def main() -> None:
    if not config.CSV_1S_PATH.exists():
        raise SystemExit(f"CSV_1S_PATH not found: {config.CSV_1S_PATH} "
                          "(set it in .env)")
    process = resample_1s_to_1min(config.CSV_1S_PATH)

    url = config.database_url()
    print(f"\n[csv1s] target DB: {url.split('@')[-1] if '@' in url else url}")
    engine = create_engine(url)
    process.to_sql("process_1min", engine, if_exists="replace", index=False,
                   chunksize=10_000)
    with engine.begin() as cx:
        cx.execute(text("CREATE INDEX IF NOT EXISTS ix_process_ts ON process_1min (ts)"))

    print(f"\n================ process_1min REFRESHED FROM 1s CSV ================")
    print(f"rows: {len(process):,}   {process['ts'].min()} -> {process['ts'].max()}")
    in_load = int(process.get("feed_gate", pd.Series(dtype=bool)).sum())
    print(f"in-load minutes (feed_gate=1): {in_load:,}")


if __name__ == "__main__":
    main()
