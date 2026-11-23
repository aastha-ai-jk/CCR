"""Daily Recommendation Summary email (admin dashboard numbers, by mail).

Run once a day from cron (edge test, 08:00):

    python -m backend.daily_report            # build and send
    python -m backend.daily_report --dry-run  # print, send nothing

Three blocks, same counting as the admin Recommendation Summary
(``decisions.summary``): yesterday, the last 7 days up to yesterday, and the
total since REPORT_START. Mail settings come from the environment
(``.env.secrets``, never committed): SMTP_HOST, SMTP_PORT (25), SMTP_FROM,
REPORT_TO (comma-separated), optional SMTP_USER / SMTP_PASSWORD and
SMTP_STARTTLS=1. ADVISORY ONLY — reads the database, writes nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import smtplib
from email.message import EmailMessage

import config  # noqa: F401  (loads .env / .env.secrets)
from backend import decisions

REPORT_START = dt.date(2026, 11, 2)   # first day counted in "since start"


def _sum(rows: list[dict]) -> dict:
    out = {k: sum(r[k] for r in rows) for k in ("total", "accepted", "declined", "no_action")}
    out["days"] = len(rows)
    return out


def build(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    yday = today - dt.timedelta(days=1)
    n = max(1, (today - REPORT_START).days + 1)
    rows = [r for r in decisions.summary(n)
            if REPORT_START.isoformat() <= r["day"] <= yday.isoformat()]
    last7 = (yday - dt.timedelta(days=6)).isoformat()
    return {
        "yesterday": yday,
        "blocks": [
            (f"Yesterday ({yday:%d %b %Y})", _sum([r for r in rows if r["day"] == yday.isoformat()])),
            (f"Last 7 days ({dt.date.fromisoformat(last7):%d %b} - {yday:%d %b})",
             _sum([r for r in rows if r["day"] >= last7])),
            (f"Since start ({REPORT_START:%d %b} - {yday:%d %b})", _sum(rows)),
        ],
        "days": sorted(rows, key=lambda r: r["day"], reverse=True)[:7],
    }


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def render(rep: dict) -> tuple[str, str, str]:
    """(subject, plain text, html)."""
    y = rep["blocks"][0][1]
    subject = (f"CCR Kiln-3 AI Advisor - daily report {rep['yesterday']:%d %b %Y}: "
               f"{y['total']} recommendations, {y['accepted']} accepted, {y['declined']} declined")
    head = ("Period", "Total recommendations", "Accepted", "Declined", "No action", "Response rate")
    lines = [" | ".join(head)]
    trs = []
    for name, b in rep["blocks"]:
        cells = (name, b["total"], b["accepted"], b["declined"], b["no_action"],
                 _pct(b["accepted"] + b["declined"], b["total"]))
        lines.append(" | ".join(str(c) for c in cells))
        trs.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    note = ("Counted per parameter (SAFR, calciner temp; calciner coal only up to 15 Nov 2026, "
            "it is an instantaneous value since 16 Nov): a new recommendation "
            "is advice shown on the CCR screen that changed meaningfully (coal > 2.5 t/h, "
            "SAFR > 1.5 t/h, calciner temp > 5 C), at most once per parameter per 10 min "
            "(max 288 a day; 432 up to 15 Nov). One Accept all click counts for each parameter. "
            "Response rate = (accepted + declined) / total. Advisory only.")
    day_trs = "".join(
        f"<tr><td>{dt.date.fromisoformat(r['day']):%d %b %Y}</td><td>{r['total']}</td>"
        f"<td>{r['accepted']}</td><td>{r['declined']}</td><td>{r['no_action']}</td></tr>"
        for r in rep["days"])
    text = "\n".join(lines) + "\n\n" + note
    th = "".join(f"<th>{h}</th>" for h in head)
    style = ("table{border-collapse:collapse;font-family:Calibri,Arial,sans-serif;font-size:14px}"
             "td,th{border:1px solid #ccc;padding:6px 10px;text-align:right}"
             "td:first-child,th:first-child{text-align:left}th{background:#223140;color:#fff}")
    html = (f"<html><head><style>{style}</style></head><body>"
            f"<p><b>CCR Kiln-3 AI Advisor - Recommendation Summary</b></p>"
            f"<table><tr>{th}</tr>{''.join(trs)}</table>"
            f"<p><b>Day by day (last 7 days)</b></p><table><tr><th>Date</th>"
            f"<th>Total</th><th>Accepted</th><th>Declined</th><th>No action</th></tr>"
            f"{day_trs}</table><p style='color:#666;font-size:12px'>{note}</p></body></html>")
    return subject, text, html


def send(subject: str, text: str, html: str) -> None:
    host = os.getenv("SMTP_HOST")
    to = [a.strip() for a in (os.getenv("REPORT_TO") or "").split(",") if a.strip()]
    sender = os.getenv("SMTP_FROM")
    if not (host and to and sender):
        raise SystemExit("SMTP_HOST, SMTP_FROM and REPORT_TO must be set in .env.secrets")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT") or 25), timeout=30) as s:
        if os.getenv("SMTP_STARTTLS") == "1":
            s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD") or "")
        s.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print the report, send nothing")
    args = ap.parse_args()
    subject, text, html = render(build())
    if args.dry_run:
        print(subject, "\n", text, sep="")
        return
    send(subject, text, html)
    print(f"[daily_report] sent: {subject}")


if __name__ == "__main__":
    main()
