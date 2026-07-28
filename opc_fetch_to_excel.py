"""
OPC UA -> Excel fetcher (PLC data) - DATE / TIME-WINDOW COLLECTION
=================================================================
Set the DATE (and optional start/end time) you want to collect data for.
The script connects to the plant OPC UA server and logs LIVE values every
POLL_INTERVAL_SEC seconds throughout that window, writing one timestamped
row per reading to an .xlsx file named after the date, and also printing
every reading to the command prompt.

All tags are read in a SINGLE batched request per cycle (fast), instead of
one round-trip per tag.

IMPORTANT - this server has NO historian:
    The OPC server at OPC_ENDPOINT does NOT support historical reads
    (HistoryRead is unsupported). You therefore CANNOT pull data for a date
    that is already in the past - there is nothing stored to read back.
    You can only:
      * collect for TODAY (from now, or from a later time today), or
      * schedule a FUTURE date/time and let the script wait until then.
    A past window is rejected with a clear message.

Stop early at any time with Ctrl+C (a final save is performed).
"""

import os
import time
import datetime as dt
import openpyxl
from openpyxl.styles import Font
from opcua import Client, ua

# ------------------------- CONFIG -------------------------
EXCEL_PATH  = "PLC & QC Data and Tags.xlsx"      # source workbook with tag list
SHEET_NAME  = "PLC"
TAG_COLUMN_HEADER  = "TAG for AI"
NAME_COLUMN_HEADER = "Parameters"

OPC_ENDPOINT   = "opc.tcp://10.4.222.139:49320"  # host:port of the OPC UA server
NAMESPACE_INDEX = 2

# ---- The date/time window is asked interactively when you run the script ----

POLL_INTERVAL_SEC  = 10        # seconds between readings
SAVE_EVERY         = 1        # save the .xlsx every N readings
RECONNECT_WAIT_SEC = 10       # wait before retrying after a disconnect
PRINT_VALUES       = True     # print each tag's value to the console every reading
# Output file is auto-named:  plc_data_<from>_to_<to>.xlsx
# ------------------------------------------------------------

OUTPUT_XLSX = None   # set at run time from the dates you enter


def load_tags_from_excel(path, sheet_name, tag_col_header, name_col_header):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name]

    header_row_idx = None
    for row in ws.iter_rows(min_row=1, max_row=10):
        for cell in row:
            if cell.value == tag_col_header:
                header_row_idx = cell.row
        if header_row_idx:
            break
    if header_row_idx is None:
        raise ValueError(f"Could not find header '{tag_col_header}' in sheet '{sheet_name}'")

    headers = {c.value: c.column for c in ws[header_row_idx] if c.value}
    tag_col = headers[tag_col_header]
    name_col = headers.get(name_col_header)

    tags = []
    for row in ws.iter_rows(min_row=header_row_idx + 1, max_row=ws.max_row):
        tag_cell = row[tag_col - 1]
        if tag_cell.value is None:
            continue
        name_val = row[name_col - 1].value if name_col else tag_cell.value
        tags.append({
            "name": str(name_val).strip(),
            "node_name": str(tag_cell.value).strip(),
        })
    return tags


def node_id(node_name, ns=NAMESPACE_INDEX):
    return f"ns={ns};s={node_name}"


def candidate_names(node_name):
    cands = [
        node_name,
        node_name.replace("_PPM", ""),
        node_name.replace("K3V10F1", "K3V10_F1"),
        node_name.replace("_TT_1.", "_TT_2."),
    ]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def batch_data_values(client, nodes):
    """Read the Value attribute of many nodes in ONE request.
    Returns a list of ua.DataValue (each carries a StatusCode)."""
    nodeids = [n.nodeid for n in nodes]
    return client.uaclient.get_attributes(nodeids, ua.AttributeIds.Value)


def resolve_nodes(client, tags):
    """Resolve every tag to a working node name using batched reads.
    At most a few round-trips total (one per candidate 'round')."""
    cand_lists = [candidate_names(t["node_name"]) for t in tags]
    working = [None] * len(tags)
    max_rounds = max(len(c) for c in cand_lists)

    for rnd in range(max_rounds):
        idxs = [i for i in range(len(tags))
                if working[i] is None and rnd < len(cand_lists[i])]
        if not idxs:
            break
        nodes = [client.get_node(node_id(cand_lists[i][rnd])) for i in idxs]
        dvs = batch_data_values(client, nodes)
        for i, dv in zip(idxs, dvs):
            if dv.StatusCode.is_good():
                working[i] = cand_lists[i][rnd]

    resolved = []
    for t, w in zip(tags, working):
        if w is None:
            print(f"  ! UNAVAILABLE: {t['name']} ({t['node_name']})")
        elif w != t["node_name"]:
            print(f"  ~ auto-corrected: {t['name']}: {t['node_name']} -> {w}")
        resolved.append({
            "name": t["name"],
            "node_name": w,
            "node": client.get_node(node_id(w)) if w else None,
        })
    return resolved


def read_row(client, resolved):
    """Batch-read all available nodes once; return a value list aligned to
    'resolved' ("" for unavailable or bad-status tags)."""
    avail_nodes = [r["node"] for r in resolved if r["node"] is not None]
    dvs = batch_data_values(client, avail_nodes)
    it = iter(dvs)
    values = []
    for r in resolved:
        if r["node"] is None:
            values.append("")
            continue
        dv = next(it)
        values.append(dv.Value.Value if dv.StatusCode.is_good() else "")
    return values


def build_or_load_workbook(resolved):
    header = ["Timestamp"] + [r["name"] for r in resolved]
    if os.path.isfile(OUTPUT_XLSX):
        try:
            wb = openpyxl.load_workbook(OUTPUT_XLSX)
            print(f"Appending to existing '{OUTPUT_XLSX}'.")
            return wb, wb.active
        except Exception:
            pass
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PLC Data"
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "B2"
    return wb, ws


def save_workbook(wb, ws):
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 12), 40)
    try:
        wb.save(OUTPUT_XLSX)
        return True
    except PermissionError:
        print(f"  ! Could not save '{OUTPUT_XLSX}' (open in Excel?). Will retry next cycle.")
        return False


def _parse_dt(text, is_end):
    """Accept 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS'.
    A date with no time means 00:00:00 for the start and 23:59:59 for the end."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and is_end:
                d = d.replace(hour=23, minute=59, second=59)
            return d
        except ValueError:
            continue
    return None


def prompt_window():
    """Ask the user, at run time, for the From/To date-time to collect."""
    print("=" * 64)
    print("Enter the date/time range to fetch PLC data for.")
    print("Formats accepted:")
    print("   YYYY-MM-DD                 (whole day)")
    print("   YYYY-MM-DD HH:MM           (to the minute)")
    print("   YYYY-MM-DD HH:MM:SS        (to the second)")
    print("Reminder: this server has no history, so the range must be NOW")
    print("or in the FUTURE - past data cannot be fetched.")
    print("=" * 64)
    while True:
        s = input("From  (blank = now)          : ").strip()
        start = dt.datetime.now() if s == "" else _parse_dt(s, is_end=False)
        if start is None:
            print("  -> could not understand that date/time, try again.\n")
            continue

        e = input("To    (blank = end of that day): ").strip()
        if e == "":
            end = start.replace(hour=23, minute=59, second=59)
        else:
            end = _parse_dt(e, is_end=True)
            if end is None:
                print("  -> could not understand that date/time, try again.\n")
                continue

        if end <= start:
            print("  -> 'To' must be after 'From', try again.\n")
            continue
        return start, end


def output_filename(start, end):
    if start.date() == end.date():
        return f"plc_data_{start:%Y-%m-%d}.xlsx"
    return f"plc_data_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.xlsx"


def wait_until(start):
    now = dt.datetime.now()
    if now >= start:
        return
    print(f"Start time {start} is in the future. Waiting {(start-now).total_seconds()/60:.1f} min...")
    while dt.datetime.now() < start:
        time.sleep(min(30, max(1, (start - dt.datetime.now()).total_seconds())))


def main():
    global OUTPUT_XLSX
    start, end = prompt_window()
    now = dt.datetime.now()
    if end < now:
        print("=" * 64)
        print(f"REQUESTED WINDOW IS IN THE PAST ({start} -> {end}).")
        print("This OPC server has no historian, so past data cannot be")
        print("fetched. Please enter a range that is now or in the future.")
        print("=" * 64)
        return

    OUTPUT_XLSX = output_filename(start, end)
    print(f"\nCollection window: {start}  ->  {end}")
    print(f"Output file: {OUTPUT_XLSX}\n")

    tags = load_tags_from_excel(EXCEL_PATH, SHEET_NAME, TAG_COLUMN_HEADER, NAME_COLUMN_HEADER)
    print(f"Loaded {len(tags)} tags from '{SHEET_NAME}' sheet.")

    wait_until(start)

    wb = ws = None
    count = 0
    pending_save = False

    try:
        while dt.datetime.now() < end:   # outer reconnect loop
            client = Client(OPC_ENDPOINT)
            try:
                client.connect()
                print(f"[{dt.datetime.now()}] Connected to {OPC_ENDPOINT}")

                print("Resolving tags against the server...")
                resolved = resolve_nodes(client, tags)
                avail = sum(1 for r in resolved if r["node"] is not None)
                print(f"{avail}/{len(resolved)} tags available on the server.\n")

                if wb is None:
                    wb, ws = build_or_load_workbook(resolved)

                while dt.datetime.now() < end:   # inner polling loop
                    ts = dt.datetime.now().isoformat(timespec="seconds")
                    values = read_row(client, resolved)
                    ws.append([ts] + values)
                    count += 1
                    pending_save = True

                    if count % SAVE_EVERY == 0 and save_workbook(wb, ws):
                        pending_save = False

                    print(f"\n[{ts}]  row {count}  ({avail} live tags)")
                    if PRINT_VALUES:
                        for r, v in zip(resolved, values):
                            print(f"    {r['name']:42s} {v}")

                    time.sleep(POLL_INTERVAL_SEC)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[{dt.datetime.now()}] Connection error: {e}")
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass

            if dt.datetime.now() < end:
                print(f"Retrying connection in {RECONNECT_WAIT_SEC}s...")
                time.sleep(RECONNECT_WAIT_SEC)

        print(f"\nReached end of window ({end}).")

    except KeyboardInterrupt:
        print("\nStopping (Ctrl+C).")
    finally:
        if wb is not None and pending_save:
            print("Saving final data...")
            while not save_workbook(wb, ws):
                time.sleep(2)
        if wb is not None:
            print(f"Saved {count} rows to '{OUTPUT_XLSX}'")


if __name__ == "__main__":
    main()
