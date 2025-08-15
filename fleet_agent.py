#!/usr/bin/env python3
"""
Fleet Agent — autonomous loop with memory
- Observes: reads the fleet spreadsheet
- Thinks: applies rules (Service due <=500 miles; MOT due <=30 days)
- Acts: sends emails (or logs) and records what it did in a SQLite memory to avoid duplicates
- Plans: runs once, or loops every N hours

USAGE:
  python fleet_agent.py --input /path/to/Fleet_Manager_Template_UK.xlsx --outdir /path/to/output --hours 6 --loop --send

Config via env:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_TLS=1, SMTP_FROM, SMTP_FROM_NAME, EMAIL_DEFAULT_TO
  SUPPRESS_DAYS=7  (do not re-notify the same action within this many days)
  DUE_MILES_THRESHOLD=500
  DUE_DAYS_THRESHOLD=30

Notes:
- This is rule-based for reliability. You can plug in an LLM later just for nicer emails or exception handling.
"""

import os, time, argparse, sqlite3, hashlib
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
import numpy as np
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

DB_FILE = "fleet_agent_memory.sqlite"

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def ensure_db(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS actions_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_key TEXT UNIQUE,
            vehicle TEXT,
            action TEXT,
            status TEXT,
            reason TEXT,
            mot_expiry TEXT,
            recipient TEXT,
            sent_at TEXT
        )
    """)
    conn.commit()
    return conn

def hash_action(vehicle, action, status, reason, mot_expiry, recipient):
    base = f"{vehicle}|{action}|{status}|{reason}|{mot_expiry or ''}|{recipient}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def pick(cols, *cands):
    for c in cands:
        if c in cols:
            return c
    return None

def parse_date_safe(val):
    import pandas as pd
    from datetime import datetime, timedelta
    if pd.isna(val):
        return None
    if isinstance(val, (pd.Timestamp, datetime)):
        return pd.Timestamp(val).to_pydatetime()
    try:
        if isinstance(val, (int, float)) and val > 20000:
            return datetime(1899, 12, 30) + timedelta(days=float(val))
    except Exception:
        pass
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.to_pydatetime()
    except Exception:
        return None

def read_sheet(path, sheet=None):
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    else:
        xl = pd.ExcelFile(path)
        sheet = sheet or xl.sheet_names[0]
        df = xl.parse(sheet)
    cols = [str(c).strip().lower() for c in df.columns]
    df.columns = cols
    return df

def think_actions(df, now):
    cols = df.columns
    veh_col = pick(cols, "reg", "registration", "vehicle", "vrm")
    mileage_col = pick(cols, "current mileage", "mileage", "odometer")
    last_service_mileage_col = pick(cols, "last service mileage", "service last mileage", "last_service_mileage")
    service_interval_col = pick(cols, "service interval (miles)", "service interval")
    service_due_at_col = pick(cols, "service mileage due at", "service_due_at", "service due at")
    miles_to_service_col = pick(cols, "miles_to_service", "miles to service")
    last_mot_date_col = pick(cols, "last mot date", "last mot", "last_mot_date")
    mot_expiry_col = pick(cols, "mot expiry", "mot date required", "mot due", "mot_due_date")
    email_col = pick(cols, "email", "manager email", "contact email", "recipient")

    due_miles = env_int("DUE_MILES_THRESHOLD", 500)
    due_days = env_int("DUE_DAYS_THRESHOLD", 30)

    actions = []
    for i, row in df.iterrows():
        vehicle = str(row.get(veh_col, f"Vehicle {i+1}"))
        recipient = str(row.get(email_col)) if email_col and pd.notna(row.get(email_col)) else os.getenv("EMAIL_DEFAULT_TO", "fleet.manager@example.com")

        # Service logic
        due_service = False; status_s=""; reason_s=""
        try:
            if miles_to_service_col and pd.notna(row.get(miles_to_service_col)):
                mleft = float(row.get(miles_to_service_col))
                if mleft <= 0:
                    due_service = True; status_s="Due"; reason_s=f"Overdue by {int(abs(mleft))} miles."
                elif mleft <= due_miles:
                    due_service = True; status_s="Due soon"; reason_s=f"Within {int(mleft)} miles of service."
                else:
                    reason_s=f"{int(mleft)} miles remaining to next service."
            elif service_due_at_col and mileage_col and pd.notna(row.get(service_due_at_col)) and pd.notna(row.get(mileage_col)):
                cur_v = float(row.get(mileage_col)); due_v = float(row.get(service_due_at_col))
                mleft = due_v - cur_v
                if mleft <= 0:
                    due_service = True; status_s="Due"; reason_s=f"Overdue by {int(abs(mleft))} miles (due at {int(due_v)}, current {int(cur_v)})."
                elif mleft <= due_miles:
                    due_service = True; status_s="Due soon"; reason_s=f"Within {int(mleft)} miles (due at {int(due_v)}, current {int(cur_v)})."
                else:
                    reason_s=f"{int(mleft)} miles remaining (due at {int(due_v)})."
            elif mileage_col and last_service_mileage_col and service_interval_col and all(pd.notna(row.get(c)) for c in [mileage_col, last_service_mileage_col, service_interval_col]):
                cur_v = float(row.get(mileage_col)); last_v=float(row.get(last_service_mileage_col)); int_v=float(row.get(service_interval_col))
                mleft = (last_v + int_v) - cur_v
                if mleft <= 0:
                    due_service=True; status_s="Due"; reason_s=f"Overdue by {int(abs(mleft))} miles (interval {int(int_v)}, last at {int(last_v)})."
                elif mleft <= due_miles:
                    due_service=True; status_s="Due soon"; reason_s=f"Within {int(mleft)} miles (interval {int(int_v)}, last at {int(last_v)})."
                else:
                    reason_s=f"{int(mleft)} miles remaining to next service."
            else:
                reason_s = "Missing service data."
        except Exception:
            reason_s = "Insufficient/invalid service data."

        if due_service:
            actions.append({"Vehicle": vehicle, "Action": "Service", "Status": status_s, "Reason": reason_s, "Recipient": recipient, "MOT Expiry": None})

        # MOT logic
        expiry = None
        if mot_expiry_col and pd.notna(row.get(mot_expiry_col)):
            expiry = parse_date_safe(row.get(mot_expiry_col))
        elif last_mot_date_col and pd.notna(row.get(last_mot_date_col)):
            last_mot = parse_date_safe(row.get(last_mot_date_col))
            if last_mot:
                expiry = last_mot + relativedelta(years=1)

        if expiry:
            days_left = (expiry - now).days
            if days_left < 0:
                actions.append({"Vehicle": vehicle, "Action": "MOT", "Status": "Overdue", "Reason": f"Expired {-days_left} days ago on {expiry.strftime('%d %b %Y')}.", "Recipient": recipient, "MOT Expiry": expiry.date()})
            elif days_left <= due_days:
                actions.append({"Vehicle": vehicle, "Action": "MOT", "Status": "Due soon", "Reason": f"Expires in {days_left} days on {expiry.strftime('%d %b %Y')}.", "Recipient": recipient, "MOT Expiry": expiry.date()})
        else:
            # No action if no date; could optionally alert for missing data
            pass

    return actions

def build_email(vehicle, action, status, reason, recipient, mot_expiry=None):
    subject = f"[Fleet] {vehicle}: {action} {status}".strip()
    body_lines = ["Hi team", "", f"Vehicle: {vehicle}", f"Action: {action} ({status})", f"Reason: {reason}"]
    if action == "MOT" and mot_expiry:
        body_lines.append(f"MOT expiry: {mot_expiry.strftime('%d %b %Y') if hasattr(mot_expiry,'strftime') else str(mot_expiry)}")
    body_lines += ["", "Please schedule this and update the tracker once booked.", "", "Thanks,", "AI Fleet Manager"]
    return subject, "\n".join(body_lines)

def send_mail(to_addr, subject, body):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    use_tls = os.getenv("SMTP_TLS", "1") == "1"
    from_name = os.getenv("SMTP_FROM_NAME", "AI Fleet Manager")
    from_addr = os.getenv("SMTP_FROM", user or "no-reply@example.com")

    if not host or not user or not pwd:
        raise RuntimeError("SMTP env vars not fully set (SMTP_HOST, SMTP_USER, SMTP_PASS required).")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_addr

    server = smtplib.SMTP(host, port, timeout=30)
    try:
        if use_tls:
            server.starttls()
        server.login(user, pwd)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
        server.quit()

def act(actions, outdir, send_emails, db_conn, suppress_days):
    import pandas as pd
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(outdir, f"fleet_actions_{stamp}.csv")
    out_xlsx = os.path.join(outdir, f"fleet_actions_{stamp}.xlsx")

    # Deduplicate using memory
    cur = db_conn.cursor()
    actions_to_send = []
    for a in actions:
        key = hash_action(a["Vehicle"], a["Action"], a["Status"], a["Reason"], str(a.get("MOT Expiry") or ""), a["Recipient"])
        cur.execute("SELECT sent_at FROM actions_sent WHERE action_key=?", (key,))
        row = cur.fetchone()
        if row:
            last = datetime.fromisoformat(row[0])
            if datetime.now() - last < timedelta(days=suppress_days):
                continue  # skip
        actions_to_send.append((key, a))

    # Persist outputs
    df = pd.DataFrame([a for _, a in actions_to_send]) if actions_to_send else pd.DataFrame(columns=["Vehicle","Action","Status","Reason","Recipient","MOT Expiry"])
    df.to_csv(out_csv, index=False)
    with pd.ExcelWriter(out_xlsx) as w:
        df.to_excel(w, index=False, sheet_name="Actions")

    # Email/send
    for key, a in actions_to_send:
        subject, body = build_email(a["Vehicle"], a["Action"], a["Status"], a["Reason"], a["Recipient"], a.get("MOT Expiry"))
        if send_emails and not df.empty:
            try:
                send_mail(a["Recipient"], subject, body)
                status_note = "EMAIL SENT"
            except Exception as e:
                status_note = f"EMAIL FAILED: {e}"
        else:
            status_note = "DRY-RUN (no --send)"
        # Record memory
        cur.execute("""INSERT OR REPLACE INTO actions_sent(action_key, vehicle, action, status, reason, mot_expiry, recipient, sent_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (key, a["Vehicle"], a["Action"], a["Status"], a["Reason"], str(a.get("MOT Expiry") or ""), a["Recipient"], datetime.now().isoformat()))
        db_conn.commit()
        print(f"{status_note}: {a['Vehicle']} {a['Action']} -> {a['Recipient']}")

    print("Wrote:", out_csv, "and", out_xlsx)

def once(args):
    df = read_sheet(args.input, args.sheet)
    now = datetime.now()
    actions = think_actions(df, now)
    conn = ensure_db(os.path.join(args.outdir, DB_FILE))
    act(actions, args.outdir, args.send, conn, env_int("SUPPRESS_DAYS", 7))

def loop(args):
    hours = args.hours
    while True:
        print("=== Fleet Agent tick ===", datetime.now().isoformat())
        try:
            once(args)
        except Exception as e:
            print("Error during tick:", e)
        if not args.loop:
            break
        time.sleep(int(hours * 3600))

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--hours", type=float, default=6.0, help="Run every N hours when --loop is set")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    loop(args)

if __name__ == "__main__":
    main()
