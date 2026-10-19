"""Live OPC-UA feed -> database.

Connects to the plant OPC-UA server and, every --interval seconds, reads all
process tags in ONE batched request and writes them to the `live_now` table
(single latest row) plus a `process_live` history table. The web app polls
`live_now`, so the dashboards update continuously.

The server has NO historian, so this only ever reads "now" — which is what a
live feed needs.

Usage:
    python -m etl.live_feed                 # real OPC feed, 2 s cadence
    python -m etl.live_feed --interval 3
    python -m etl.live_feed --replay        # no plant network: stream recent
                                            #   history forward as if it were now

Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import time
import datetime as dt

import pandas as pd
from sqlalchemy import create_engine, text

import config

OPC_ENDPOINT = "opc.tcp://10.4.222.139:49320"
NS = 2
PREFIX = "L3.L3_NEW.Root."

# Per-cyclone temperatures — discovered on the OPC server but NOT in the
# original 31-tag combustion list. _TT_1 = gas exit, _TT_2 = cone/dip.
# (423CN150_TT_1 and 423CN250_TT_1 already come from dim_tag.)
EXTRA_CYCLONE = [
    "423CN110_TT_1", "423CN110_TT_2", "423CN120_TT_1", "423CN120_TT_2",
    "423CN130_TT_1", "423CN130_TT_2", "423CN140_TT_1", "423CN150_TT_2",
    "423CN210_TT_1", "423CN210_TT_2", "423CN220_TT_1", "423CN220_TT_2",
    "423CN230_TT_1", "423CN230_TT_2", "423CN240_TT_1", "423CN240_TT_2",
    "423CN250_TT_2",
]

# 2026-09-23: more Kiln-3 pyro tags found on the OPC server (setpoints, drafts,
# NOx, kiln drive, hood pressure, clinker rate, cooler, coal mill). Captured for
# evaluation; NOT yet in process_1min / the model.
EXTRA_PYRO = [
    "467RL400_SPSPM",
    "467RL300_SPSPM",
    "423CI400_PT_2",
    "423CI400_PT_3",
    "423CI400_PT_4",
    "423CN150_PT_1",
    "423CN250_PT_1",
    "423CN140_PT_1",
    "423CN240_PT_1",
    "423CN240_PT_2",
    "423CN110_PT_1",
    "423CN210_PT_1",
    "423CN_PT",
    "423DU415_PT",
    "423DU415_PT1",
    "423DU415_TT1",
    "423AM823_NO",
    "423FN010_JT",
    "423FN010_SPSPM",
    "423FN010_PT_1",
    "423FN010_PT_2",
    "423CN150_LS",
    "423CN250_LS",
    "423CN140_LS",
    "423CN240_LS",
    "433MD140_IT1",
    "433MD140_JT1",
    "433MD140_SPC",
    "433MD140A_SPSPM",
    "433KL100_BT_02",
    "443KH050_PT",
    "353RL130_KF_TPH",
    "353_CLNK_TPH",
    "443CC100_P1",
    "443CC100_SP1SPM",
    "443CC100_TT3",
    "443AM597",
    "443FN590_ST",
    "463WF035_TPH",
    "463WF045_TPH",
    "463RM100_TT2",
]


# --------------------------------------------------------------------------
def make_engine():
    url = config.database_url()
    kw = {"connect_args": {"timeout": 15}} if url.startswith("sqlite") else {}
    return create_engine(url, **kw)


def tag_map(engine) -> pd.DataFrame:
    """DB tag code + OPC node name, from the dim_tag dictionary."""
    dim = pd.read_sql("SELECT tag, ai_tag, control_role FROM dim_tag", engine)
    return dim.dropna(subset=["ai_tag"])


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write_snapshot(engine, ts: str, values: dict, source: str):
    feed = values.get(config.KILN_FEED_TAG)
    row = {**values, "ts": ts, "source": source,
           "feed_gate": bool(feed is not None and feed > config.KILN_FEED_MIN_TPH)}
    df = pd.DataFrame([row])
    df.to_sql("live_now", engine, if_exists="replace", index=False)
    hist = df.drop(columns=["source"])
    try:
        hist.to_sql("process_live", engine, if_exists="append", index=False)
    except Exception:
        # 2026-09-23: column set changed -> ADD the new columns; never "replace"
        # (that dropped the whole live history when cyclone temps were added).
        with engine.begin() as con:
            have = {r[1] for r in con.exec_driver_sql("PRAGMA table_info(process_live)")}
            for c in hist.columns:
                if c not in have:
                    con.exec_driver_sql(f'ALTER TABLE process_live ADD COLUMN "{c}" REAL')
        hist.to_sql("process_live", engine, if_exists="append", index=False)


# --------------------------- REAL OPC FEED --------------------------------
def run_opc(engine, interval: float):
    from opcua import Client, ua

    tm = tag_map(engine)
    print(f"Connecting to {OPC_ENDPOINT} ...")
    while True:                                   # reconnect loop
        client = Client(OPC_ENDPOINT)
        try:
            client.connect()
            print(f"[{dt.datetime.now():%H:%M:%S}] connected. Resolving {len(tm)} tags...")
            nodes, cols = [], []
            for r in tm.itertuples():
                nodes.append(client.get_node(f"ns={NS};s={r.ai_tag}"))
                cols.append(r.tag)
            for code in EXTRA_CYCLONE + EXTRA_PYRO:          # extra server tags
                nodes.append(client.get_node(f"ns={NS};s={PREFIX}{code}.Value"))
                cols.append(code)
            dvs = client.uaclient.get_attributes([n.nodeid for n in nodes], ua.AttributeIds.Value)
            good = [(c, n) for c, n, dv in zip(cols, nodes, dvs) if dv.StatusCode.is_good()]
            print(f"  {len(good)}/{len(cols)} tags live on the server. Streaming every {interval}s...\n")

            gcols = [c for c, _ in good]
            gnodes = [n for _, n in good]
            while True:                            # polling loop
                ts = dt.datetime.now().isoformat(timespec="seconds")
                dvs = client.uaclient.get_attributes([n.nodeid for n in gnodes], ua.AttributeIds.Value)
                values = {c: (_num(dv.Value.Value) if dv.StatusCode.is_good() else None)
                          for c, dv in zip(gcols, dvs)}
                write_snapshot(engine, ts, values, "opc")
                print(f"[{ts}] calciner={values.get('423CI400_TT_4')}°C  "
                      f"feed={values.get('353RL130_TPH')}  ({len(values)} tags)")
                time.sleep(interval)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{dt.datetime.now():%H:%M:%S}] OPC error: {e} — retrying in 5s")
            time.sleep(5)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass


# --------------------------- REPLAY FALLBACK ------------------------------
def run_replay(engine, interval: float, hours: int = 12):
    """Stream the most recent `hours` of history forward, re-stamped to now."""
    cat = pd.read_sql("SELECT tag FROM dim_tag", engine)["tag"].tolist()
    cols = ", ".join(f'"{t}"' for t in cat)
    hist = pd.read_sql(f"SELECT ts, {cols} FROM process_1min ORDER BY ts DESC LIMIT {hours*60}", engine)
    hist = hist.iloc[::-1].reset_index(drop=True)   # chronological
    print(f"REPLAY mode: streaming {len(hist)} historical minutes forward every {interval}s "
          f"(no OPC connection). Data is real but time-shifted.\n")
    i = 0
    while True:
        r = hist.iloc[i % len(hist)]
        ts = dt.datetime.now().isoformat(timespec="seconds")
        values = {t: _num(r[t]) for t in cat}
        write_snapshot(engine, ts, values, "replay")
        print(f"[{ts}] (replay) calciner={values.get('423CI400_TT_4')}°C  feed={values.get('353RL130_TPH')}")
        i += 1
        time.sleep(interval)


# --------------------------- EDGE MIRROR ----------------------------------
def run_edge(engine, interval: float, base: str):
    """Mirror the live plant values another twin deployment (the CCR edge
    device) already reads from OPC, via its read-only GET /api/live. For dev
    machines that are not on the OT network. Uses the edge's own timestamps."""
    import json
    import urllib.request

    url = base.rstrip("/") + "/api/live"
    print(f"EDGE mode: mirroring live plant values from {url} every {interval}s (read-only).\n")
    last_ts = None
    while True:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                live = json.load(r)
            if live.get("source") != "opc":
                print(f"[{dt.datetime.now():%H:%M:%S}] edge feed is '{live.get('source')}', not live OPC — waiting")
            elif live.get("ts") != last_ts:
                last_ts = live["ts"]
                values = {k: _num(v) for k, v in (live.get("values") or {}).items()}
                write_snapshot(engine, last_ts, values, "edge")
                print(f"[{last_ts}] (edge) calciner={values.get('423CI400_TT_4')}°C  "
                      f"feed={values.get('353RL130_TPH')}  ({len(values)} tags)")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{dt.datetime.now():%H:%M:%S}] edge unreachable: {e} — retrying")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between reads")
    ap.add_argument("--replay", action="store_true", help="stream recent history instead of OPC")
    ap.add_argument("--from-edge", nargs="?", const="http://10.4.2.10:8010", metavar="URL",
                    help="mirror live values from another twin's /api/live (default: CCR edge device)")
    args = ap.parse_args()

    engine = make_engine()
    print(f"DB: {config.database_url().split('@')[-1]}")
    try:
        if args.from_edge:
            run_edge(engine, args.interval, args.from_edge)
        elif args.replay:
            run_replay(engine, args.interval)
        else:
            try:
                run_opc(engine, args.interval)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"OPC feed unavailable ({e}). Falling back to REPLAY.")
                run_replay(engine, args.interval)
    except KeyboardInterrupt:
        print("\nLive feed stopped.")


if __name__ == "__main__":
    main()
