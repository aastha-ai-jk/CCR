import requests
import json
import os
import time
import pandas as pd
from datetime import datetime, timedelta

# ==========================================================================
#  eLIMS QC Data Fetcher  (CLK-3 only)  -  History + Real-Time
#  - On start: loads all data from HISTORY_START_DATE up to today.
#  - Then: refreshes TODAY's data every REFRESH_SECONDS (new lab results
#    get appended automatically, deduplicated by elims_id).
#  - Output: timestamp + the 7 tags from the "QC" sheet, sorted by time.
# ==========================================================================

# --- Configuration ---
TAGS_FILE   = r"D:\QC_Data\PLC & QC Data and Tags.xlsx"   # source of the tags
TAGS_SHEET  = "QC"                                        # sheet holding QC tags
OUTPUT_DIR  = r"D:\QC_NBH"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "result.xlsx")
SHEET_NAME  = "CLK-3"

# How far back to pull historical data (format: DD-MM-YYYY).
# Set it to today's date if you only want the current day.
HISTORY_START_DATE = "01-07-2026"

REFRESH_SECONDS = 300                # real-time poll interval (5 minutes)

SECTION = "CLK-3"                     # only this section
MATERIALS = ["Clinker", "Hotmeal"]   # materials queried for CLK-3
TIMESTAMP_COL = "xrf test datetime"  # test time kept as first column

# If the API expects a different material_name than the label used in the
# Excel sheet, map it here (excel_label -> api_material_name).
MATERIAL_ALIASES = {
    # "Hotmeal": "Hot Meal",   # <- set the real API name if Hotmeal has data
}

# API endpoints / credentials
URL_TOKEN = "https://elims.jkcement.com:8443/jk_nbh_dcs/user"
URL_DATA  = "https://elims.jkcement.com:8443/jk_nbh_dcs/elims_data/Display_ElimsData"
CREDENTIALS = {'user': 'uBab+wlT1bo=', 'password': '5XIx9/U2ZQkzelqGaJ2sVA=='}


# --- Load the 7 tags (which columns to keep) from the Excel sheet ---
def load_tags():
    df = pd.read_excel(TAGS_FILE, sheet_name=TAGS_SHEET, header=2)
    return [str(t).strip() for t in df["TAG"].dropna().tolist()]


# --- Build the list of dates from HISTORY_START_DATE to today ---
def date_range():
    try:
        start = datetime.strptime(HISTORY_START_DATE, '%d-%m-%Y').date()
    except ValueError:
        print(f" ! Bad HISTORY_START_DATE '{HISTORY_START_DATE}', using today only.")
        start = datetime.now().date()
    today = datetime.now().date()
    if start > today:
        start = today
    days, d = [], start
    while d <= today:
        days.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    return days


# --- Authentication ---
def authenticate():
    resp = requests.post(
        URL_TOKEN,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        data=CREDENTIALS,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Auth failed: server returned status {resp.status_code}")
    full_token = resp.json().get('token')
    if not (full_token and full_token.startswith("Bearer ")):
        raise RuntimeError("Auth failed: token not found in response.")
    return full_token.split(" ")[1]


# --- Fetch CLK-3 records for one material on a given date ---
def fetch_records(token, material, day):
    payload = json.dumps({
        "request": [{
            "section_name": SECTION,
            "material_name": material,
            "from_date": f"{day} 00.00.00",
            "to_date":   f"{day} 23.59.59",
        }]
    })
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
    resp = requests.post(URL_DATA, headers=headers, data=payload, timeout=30)

    if resp.status_code == 401:
        return None                          # signal caller to re-authenticate
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict) and data.get('status', {}).get('status_code') == '1':
        return data.get('response_data', [])
    return []


def get_records(token_box, material, day):
    """Fetch with a one-time re-auth if the token has expired."""
    records = fetch_records(token_box['token'], material, day)
    if records is None:
        print("  Token expired, re-authenticating...")
        token_box['token'] = authenticate()
        records = fetch_records(token_box['token'], material, day)
    return records or []


# --- Add raw records into the master store (keyed by elims_id) ---
def ingest(master, records, tags):
    lower_tags = {t.lower(): t for t in tags}
    for rec in records:
        rid = rec.get('elims_id')
        if rid is None:
            continue
        key_by_lower = {k.lower(): k for k in rec.keys()}
        row = {TIMESTAMP_COL: rec.get(TIMESTAMP_COL)}
        for low, tag in lower_tags.items():
            src = key_by_lower.get(low)
            row[tag] = rec.get(src) if src is not None else None
        master[str(rid)] = row


# --- Write the master store (timestamp + tags only), sorted by time ---
def save(master, tags):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    columns = [TIMESTAMP_COL] + tags
    if master:
        df = pd.DataFrame(list(master.values()))
        df = df.sort_values(TIMESTAMP_COL).reset_index(drop=True)
        df = df[columns]
    else:
        df = pd.DataFrame(columns=columns)
    try:
        with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
        return len(df)
    except PermissionError:
        print(f" !! '{OUTPUT_FILE}' is open in Excel. Close it so it can update.")
        return None


# ==========================================================================
#  Main
# ==========================================================================
def main():
    tags = load_tags()
    print(f"Keeping columns: {[TIMESTAMP_COL] + tags}")
    print(f"Section: {SECTION} | Output: {OUTPUT_FILE}")

    token_box = {'token': authenticate()}
    print("Authenticated.")

    master = {}

    # ---- Step 1: one-time historical load ----
    days = date_range()
    print(f"\nLoading history for {len(days)} day(s) "
          f"({days[0]} .. {days[-1]})...")
    for day in days:
        for label in MATERIALS:
            api_material = MATERIAL_ALIASES.get(label, label)
            ingest(master, get_records(token_box, api_material, day), tags)
    n = save(master, tags)
    print(f"History loaded: {n} record(s) saved.\n")

    # ---- Step 2: real-time refresh of today's data ----
    print(f"Now polling today's data every {REFRESH_SECONDS}s (Ctrl+C to stop).")
    while True:
        cycle_start = datetime.now()
        today = cycle_start.strftime('%Y-%m-%d')
        before = len(master)
        for label in MATERIALS:
            api_material = MATERIAL_ALIASES.get(label, label)
            ingest(master, get_records(token_box, api_material, today), tags)
        total = save(master, tags)
        added = len(master) - before
        stamp = cycle_start.strftime('%H:%M:%S')
        if total is not None:
            msg = f"+{added} new" if added else "no new records"
            print(f"[{stamp}] {msg} (total {total}).")

        elapsed = (datetime.now() - cycle_start).total_seconds()
        time.sleep(max(0, REFRESH_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        print(f"\n!! Fatal error: {e}")
