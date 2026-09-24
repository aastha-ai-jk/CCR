"""eLIMS clinker-quality ingester (CLK-3) -> lab_freelime_live table.

Pulls XRF clinker lab results (free lime + oxide suite) from the eLIMS DCS API
and upserts them into SQLite, keyed by elims_id. These become the ground-truth
LABELS for the free-lime soft sensor. Advisory/observe-only — read from eLIMS,
write only our own table.

Auth + request contract (see ~/Downloads/QC_LIVE.py):
  POST form {user,password} -> token "Bearer <jwt>"  (strip the prefix)
  POST JSON {"request":[{section_name,material_name,from_date,to_date}]}
       with Authorization: Bearer <jwt>; dates "YYYY-MM-DD 00.00.00" (dots).
  Success: status.status_code == "1".

Run:
  python -m etl.freelime_feed --backfill 2026-01-01   # one-time history load
  python -m etl.freelime_feed --interval 900          # live poll (today), 15 min
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import ssl
import time
import urllib.parse
import urllib.request as U

import pandas as pd
from sqlalchemy import create_engine, text

import config

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

SECTION = "CLK-3"
MATERIAL = "Clinker"
# eLIMS field -> our column. 'lime' is FREE LIME (the soft-sensor target).
FIELD_MAP = {
    "elims_id": "elims_id", "xrf test datetime": "ts",
    "lime": "free_lime", "LSF": "lsf", "SM": "sm",
    "C3S": "c3s", "C2S": "c2s", "C3A": "c3a", "C4AF": "c4af",
    "CaO": "cao", "SiO2": "sio2", "Al2O3": "al2o3", "Fe2O3": "fe2o3",
    "MgO": "mgo", "SO3": "so3", "Na2O": "na2o", "K2O": "k2o", "LIQ": "liq",
}
_DDL = """
CREATE TABLE IF NOT EXISTS lab_freelime_live (
  elims_id TEXT PRIMARY KEY, ts TEXT, free_lime REAL, lsf REAL, sm REAL,
  c3s REAL, c2s REAL, c3a REAL, c4af REAL, cao REAL, sio2 REAL, al2o3 REAL,
  fe2o3 REAL, mgo REAL, so3 REAL, na2o REAL, k2o REAL, liq REAL
)
"""


def _creds() -> dict:
    out = {}
    with open(config.ROOT / ".env.elims") as fh:
        for line in fh:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                out[k] = v
    return out


def _auth(c: dict) -> str:
    body = urllib.parse.urlencode({"user": c["ELIMS_USER"], "password": c["ELIMS_PASS"]}).encode()
    r = U.urlopen(U.Request(c["ELIMS_URL_TOKEN"], data=body,
                            headers={"Content-Type": "application/x-www-form-urlencoded"}),
                  context=_CTX, timeout=30)
    tok = json.loads(r.read()).get("token", "")
    return tok.split(" ", 1)[1] if tok.startswith("Bearer ") else tok


def _fetch_day(c: dict, token: str, day: str) -> list[dict]:
    payload = json.dumps({"request": [{
        "section_name": SECTION, "material_name": MATERIAL,
        "from_date": f"{day} 00.00.00", "to_date": f"{day} 23.59.59"}]}).encode()
    try:
        r = U.urlopen(U.Request(c["ELIMS_URL_DATA"], data=payload,
                                headers={"Content-Type": "application/json",
                                         "Authorization": f"Bearer {token}"}),
                      context=_CTX, timeout=30)
    except U.HTTPError as e:
        if e.code == 401:
            return None  # signal re-auth
        return []
    except Exception:
        return []
    try:
        j = json.loads(r.read())
    except json.JSONDecodeError:
        return []
    if isinstance(j, dict) and j.get("status", {}).get("status_code") == "1":
        return j.get("response_data", []) or []
    return []


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(records: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        low = {str(k).lower(): k for k in rec.keys()}
        row = {}
        for src, col in FIELD_MAP.items():
            k = low.get(src.lower())
            val = rec.get(k) if k else None
            row[col] = val if col in ("elims_id", "ts") else _num(val)
        if row.get("elims_id") is not None:
            out.append(row)
    return out


def _upsert(eng, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "elims_id")
    sql = (f"INSERT INTO lab_freelime_live ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(elims_id) DO UPDATE SET {updates}")
    with eng.begin() as con:
        con.execute(text(sql), rows)
    return len(rows)


def _engine():
    url = config.database_url()
    kw = {"connect_args": {"timeout": 15}} if url.startswith("sqlite") else {}
    return create_engine(url, **kw)


def _pull(eng, c, token, days) -> tuple[int, str]:
    total = 0
    for day in days:
        recs = _fetch_day(c, token, day)
        if recs is None:                       # token expired
            token = _auth(c)
            recs = _fetch_day(c, token, day) or []
        total += _upsert(eng, _rows(recs))
    return total, token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=str, help="start date YYYY-MM-DD for a one-time history load")
    ap.add_argument("--interval", type=float, default=900.0, help="live poll seconds (today only)")
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
        print(f"[freelime] backfilling {len(days)} days {days[0]}..{days[-1]}")
        n, token = _pull(eng, c, token, days)
        print(f"[freelime] backfill upserted {n} clinker records")
        if args.once:
            return

    print(f"[freelime] live poll every {args.interval}s (today) — OBSERVE-ONLY")
    while True:
        try:
            n, token = _pull(eng, c, token, [dt.date.today().isoformat()])
            print(f"[{dt.datetime.now():%H:%M:%S}] upserted {n} of today")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[freelime] tick error: {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
