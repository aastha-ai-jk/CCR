"""eLIMS hot-meal ingester (Hot Mill-3, sides A and B) -> lab_hotmeal_live table.

Hot-meal DOC (degree of calcination) is one of the state-vote inputs, so the
advisor needs it live. Same eLIMS API and login (.env.elims) as
etl.freelime_feed; upserts by elims_id. Advisory/observe-only — reads eLIMS,
writes only our own table.

Run:
  python -m etl.hotmeal_feed --backfill 2026-10-01 --once   # history, then exit
  python -m etl.hotmeal_feed --interval 900                 # live poll (today), 15 min
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.request as U

from sqlalchemy import text

from etl.freelime_feed import _CTX, _auth, _creds, _engine, _num

SECTION = "Hot Mill-3"
MATERIALS = {"Hot Meal Side- A": "A", "Hot Meal Side- B": "B"}
FIELD_MAP = {"elims_id": "elims_id", "xrf test datetime": "ts", "DOC": "doc", "SO3": "so3",
             "CL": "cl", "LOI": "loi", "Na2O": "na2o", "K2O": "k2o"}
_DDL = """
CREATE TABLE IF NOT EXISTS lab_hotmeal_live (
  elims_id TEXT PRIMARY KEY, ts TEXT, side TEXT, doc REAL, so3 REAL, cl REAL,
  loi REAL, na2o REAL, k2o REAL
)
"""


def _fetch_day(c: dict, token: str, material: str, day: str) -> list[dict] | None:
    payload = json.dumps({"request": [{
        "section_name": SECTION, "material_name": material,
        "from_date": f"{day} 00.00.00", "to_date": f"{day} 23.59.59"}]}).encode()
    try:
        r = U.urlopen(U.Request(c["ELIMS_URL_DATA"], data=payload,
                                headers={"Content-Type": "application/json",
                                         "Authorization": f"Bearer {token}"}),
                      context=_CTX, timeout=30)
    except U.HTTPError as e:
        return None if e.code == 401 else []        # None = token expired
    except Exception:
        return []
    try:
        j = json.loads(r.read())
    except json.JSONDecodeError:
        return []
    if isinstance(j, dict) and j.get("status", {}).get("status_code") == "1":
        return j.get("response_data", []) or []
    return []


def _rows(records: list[dict], side: str) -> list[dict]:
    out = []
    for rec in records:
        low = {str(k).lower(): k for k in rec.keys()}
        row = {"side": side}
        for src, col in FIELD_MAP.items():
            k = low.get(src.lower())
            v = rec.get(k) if k else None
            row[col] = v if col in ("elims_id", "ts") else _num(v)
        if row.get("elims_id") is not None:
            row["elims_id"] = str(row["elims_id"])
            out.append(row)
    return out


def _upsert(eng, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (f"INSERT INTO lab_hotmeal_live ({', '.join(cols)}) VALUES "
           f"({', '.join(':' + c for c in cols)}) ON CONFLICT(elims_id) DO UPDATE SET "
           + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "elims_id"))
    with eng.begin() as con:
        con.execute(text(sql), rows)
    return len(rows)


def _pull(eng, c, token, days) -> tuple[int, str]:
    total = 0
    for day in days:
        for material, side in MATERIALS.items():
            recs = _fetch_day(c, token, material, day)
            if recs is None:
                token = _auth(c)
                recs = _fetch_day(c, token, material, day) or []
            total += _upsert(eng, _rows(recs, side))
    return total, token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=str, help="start date YYYY-MM-DD for a one-time history load")
    ap.add_argument("--interval", type=float, default=900.0, help="live poll seconds (today)")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    c = _creds()
    eng = _engine()
    with eng.begin() as con:
        con.execute(text(_DDL))
    token = _auth(c)
    today = dt.date.today()
    if args.backfill:
        start = dt.date.fromisoformat(args.backfill)
        days = [(start + dt.timedelta(d)).isoformat() for d in range((today - start).days + 1)]
        print(f"[hotmeal] backfilling {len(days)} days {days[0]}..{days[-1]}")
        n, token = _pull(eng, c, token, days)
        print(f"[hotmeal] backfill upserted {n} hot-meal records")
        if args.once:
            return
    print(f"[hotmeal] live poll every {args.interval}s (today) — OBSERVE-ONLY")
    while True:
        try:
            # today and yesterday: late lab entries for the previous day still land
            days = [(dt.date.today() - dt.timedelta(1)).isoformat(), dt.date.today().isoformat()]
            n, token = _pull(eng, c, token, days)
            print(f"[{dt.datetime.now():%H:%M:%S}] upserted {n}")
            if args.once:                      # one live poll, then exit
                return
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[hotmeal] tick error: {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
