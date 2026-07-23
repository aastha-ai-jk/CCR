"""
Fetch HISTORICAL Hot Meal (eLIMS / NBH DCS) data over many months and save to Excel.

It pulls the data in monthly chunks (robust against server-side row caps and the
10-minute token expiry), de-duplicates by elims_id, sorts by test datetime, and
writes everything to one .xlsx workbook. Each material goes on its own sheet, so
Hot Meal Side- A and Side- B land in the SAME workbook as two separate sheets.

Usage:
    # Last 12 months of both Side- A and Side- B (default) into one workbook
    python fetch_hotmeal_history.py

    # Just one side
    python fetch_hotmeal_history.py --materials "Hot Meal Side- A"

    # An explicit date range
    python fetch_hotmeal_history.py --start "2025-09-01" --end "2026-07-22"
"""

import argparse
import json
import sys
import time
from datetime import datetime

import requests
import urllib3
from dateutil.relativedelta import relativedelta
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Configuration (from API_NBH_eLIMS documentation)
# ---------------------------------------------------------------------------
AUTH_URL = "https://elims.jkcement.com:8443/jk_nbh_dcs/user"
DATA_URL = "https://elims.jkcement.com:8443/jk_nbh_dcs/elims_data/Display_ElimsData"

USER = "uBab+wlT1bo="
PWD = "5XIx9/U2ZQkzelqGaJ2sVA=="

TIMEOUT = 120                 # seconds per request
TOKEN_TTL = 8 * 60            # refresh the token if older than this (tokens die at 10 min)
API_DT_FMT = "%Y-%m-%d %H.%M.%S"   # format the API expects

DEFAULTS = {
    "section_name": "Hot Mill-3",
    "material_names": ["Hot Meal Side- A", "Hot Meal Side- B"],
}


class Client:
    """Holds a session and transparently refreshes the Bearer token."""

    def __init__(self, verify=True):
        self.session = requests.Session()
        self.verify = verify
        self._token = None
        self._token_time = 0.0

    def _refresh_token(self):
        resp = self.session.post(
            AUTH_URL,
            data={"user": USER, "password": PWD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=TIMEOUT,
            verify=self.verify,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("token")
        if not token or not token.strip():
            raise RuntimeError(f"Authentication failed: {body.get('user')}")
        self._token = token.strip()
        self._token_time = time.time()

    def token(self):
        if self._token is None or (time.time() - self._token_time) > TOKEN_TTL:
            self._refresh_token()
        return self._token

    def fetch(self, section, material, from_date, to_date):
        payload = {
            "request": [{
                "section_name": section,
                "material_name": material,
                "from_date": from_date,
                "to_date": to_date,
            }]
        }
        resp = self.session.post(
            DATA_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json", "Authorization": self.token()},
            timeout=TIMEOUT,
            verify=self.verify,
        )
        resp.raise_for_status()
        return resp.json().get("response_data", []) or []


def month_windows(start, end):
    """Yield (from_dt, to_dt) monthly windows covering [start, end]."""
    cur = start
    while cur < end:
        nxt = min(cur + relativedelta(months=1), end)
        yield cur, nxt
        cur = nxt


def _to_number(value):
    """Convert numeric-looking strings to numbers so Excel treats them as numbers."""
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return value
    return value


def _fill_sheet(ws, records):
    """Write one material's records into an existing worksheet with formatting."""
    if not records:
        ws.append(["No records found for this material in the selected range."])
        return

    preferred = [
        "elims_id", "section name", "material name",
        "from date", "to date", "xrf test datetime",
        "Na2O", "K2O", "SO3", "CL", "LOI", "DOC",
    ]
    keys = list(records[0].keys())
    fieldnames = [k for k in preferred if k in keys] + [k for k in keys if k not in preferred]

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF")
    ws.append(fieldnames)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for rec in records:
        ws.append([_to_number(rec.get(k, "")) for k in fieldnames])

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(fieldnames))}{len(records) + 1}"
    for i, name in enumerate(fieldnames, start=1):
        width = max(len(str(name)), *(len(str(r.get(name, ""))) for r in records)) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 30)


def save_workbook(sheets, out_path):
    """Write multiple (sheet_title, records) pairs into a single .xlsx workbook."""
    wb = Workbook()
    total = 0
    for idx, (title, records) in enumerate(sheets):
        ws = wb.active if idx == 0 else wb.create_sheet()
        ws.title = title[:31]
        _fill_sheet(ws, records)
        total += len(records)

    wb.save(out_path)
    print(f"Wrote {total} rows across {len(sheets)} sheet(s) to {out_path}")


def parse_dt(s):
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Unrecognized date: {s}")


def main():
    parser = argparse.ArgumentParser(description="Fetch historical Hot Meal eLIMS data.")
    parser.add_argument("--section", default=DEFAULTS["section_name"])
    parser.add_argument("--materials", nargs="+", default=DEFAULTS["material_names"],
                        help="One or more material names; each becomes a sheet in the workbook")
    parser.add_argument("--months", type=int, default=12,
                        help="How many months back from today (ignored if --start given)")
    parser.add_argument("--start", type=parse_dt, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=parse_dt, help="End date YYYY-MM-DD (default: now)")
    parser.add_argument("--out", default="hotmeal_history.xlsx")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification")
    args = parser.parse_args()

    end = args.end or datetime.now()
    start = args.start or (end - relativedelta(months=args.months))

    verify = not args.insecure
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    client = Client(verify=verify)
    sheets = []     # (sheet_title, records) — one entry per material

    for material in args.materials:
        print(f"Fetching '{material}' [{args.section}] "
              f"from {start:%Y-%m-%d} to {end:%Y-%m-%d} (monthly chunks)...")

        by_id = {}      # elims_id -> record (de-dup across overlapping windows)
        no_id = []      # records without an elims_id (kept as-is)

        for w_start, w_end in month_windows(start, end):
            f = w_start.strftime(API_DT_FMT)
            t = w_end.strftime(API_DT_FMT)
            try:
                rows = client.fetch(args.section, material, f, t)
            except (requests.RequestException, RuntimeError) as e:
                print(f"  {w_start:%Y-%m} : FAILED ({e})", file=sys.stderr)
                continue
            for r in rows:
                eid = str(r.get("elims_id", "")).strip()
                if eid:
                    by_id[eid] = r
                else:
                    no_id.append(r)
            print(f"  {w_start:%Y-%m-%d} -> {w_end:%Y-%m-%d} : {len(rows):5d} rows "
                  f"(unique so far: {len(by_id)})")

        records = list(by_id.values()) + no_id
        # Sort by test datetime when available
        records.sort(key=lambda r: str(r.get("xrf test datetime", "")))

        print(f"Total unique records for '{material}': {len(records)}")
        sheets.append((material, records))

    save_workbook(sheets, args.out)


if __name__ == "__main__":
    main()
