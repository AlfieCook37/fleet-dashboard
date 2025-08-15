# app.py — AI Fleet Manager Dashboard (fixed-path sheet + email)
import os
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Fleet Manager Dashboard", layout="wide")
st.title("🚚 AI Fleet Manager Dashboard")
st.caption("This reads a fixed spreadsheet from the repo and can email the current actions.")

# --------- CONFIG: set this to where your sheet sits in the repo ---------
FILE_PATH = "Fleet_Manager_Template_UK.xlsx"  # e.g. "data/Fleet_Manager_Template_UK.xlsx" if in /data
DUE_MILES_DEFAULT = 500
DUE_DAYS_DEFAULT = 30

# --------- SMTP via Streamlit Secrets (preferred) or env fallback ---------
def get_secret(name, default=""):
    return st.secrets.get(name, os.getenv(name, default))

# --------- Helpers ---------
def parse_date_safe(val):
    if pd.isna(val): return None
    if isinstance(val, (pd.Timestamp, datetime)): return pd.Timestamp(val).to_pydatetime()
    if isinstance(val, (int, float)) and val > 20000:
        return datetime(1899, 12, 30) + timedelta(days=float(val))
    dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
    return None if pd.isna(dt) else dt.to_pydatetime()

def pick(cols, *aliases):
    norm = {c.strip().lower(): c for c in cols}
    for a in aliases:
        k = a.strip().lower()
        if k in norm: return norm[k]
    return None

def think_actions(df, due_miles=DUE_MILES_DEFAULT, due_days=DUE_DAYS_DEFAULT):
    cols = df.columns.tolist()
    veh_col  = pick(cols, "reg","registration","vehicle","vrm")
    mile_col = pick(cols, "current mileage","mileage","odometer","current_mileage")
    last_svc_col = pick(cols, "service last mileage","last service mileage","last_service_mileage")
    interval_col = pick(cols, "service interval (miles)","service interval","service_interval_miles")
    due_at_col   = pick(cols, "service mileage due at","service due at","service_due_at")
    mleft_col    = pick(cols, "miles_to_service","miles to service")
    last_mot_col = pick(cols, "last mot date","last mot","last_mot_date")
    mot_exp_col  = pick(cols, "mot date required","mot expiry","mot due","mot_due_date")
    email_col    = pick(cols, "email","manager email","contact email","recipient")

    now = datetime.now()
    actions = []

    for i, row in df.iterrows():
        vehicle = str(row.get(veh_col, f"Vehicle {i+1}")) if veh_col else f"Vehicle {i+1}"
        recipient = str(row.get(email_col)) if email_col and pd.notna(row.get(email_col)) else get_secret("EMAIL_DEFAULT_TO","")

        # --- Service logic ---
        svc_due, svc_status, svc_reason = False, "", ""
        try:
            if mleft_col and pd.notna(row.get(mleft_col)):
                mleft = float(row.get(mleft_col))
            elif due_at_col and mile_col and pd.notna(row.get(due_at_col)) and pd.notna(row.get(mile_col)):
                mleft = float(row.get(due_at_col)) - float(row.get(mile_col))
            elif mile_col and last_svc_col and interval_col and all(pd.notna(row.get(c)) for c in [mile_col,last_svc_col,interval_col]):
                mleft = (float(row.get(last_svc_col)) + float(row.get(interval_col))) - float(row.get(mile_col))
            else:
                mleft = None

            if mleft is None:
                svc_reason = "Missing service data."
            else:
                if mleft <= 0:
                    svc_due, svc_status, svc_reason = True, "Due", f"Overdue by {int(abs(mleft))} miles."
                elif mleft <= due_miles:
                    svc_due, svc_status, svc_reason = True, "Due soon", f"Within {int(mleft)} miles of service."
                else:
                    svc_reason = f"{int(mleft)} miles remaining to next service."
        except Exception:
            svc_reason = "Insufficient/invalid service data."

        if svc_due:
            actions.append({"Vehicle": vehicle, "Action": "Service", "Status": svc_status, "Reason": svc_reason, "Recipient": recipient})

        # --- MOT logic ---
        expiry = None
        if mot_exp_col and pd.notna(row.get(mot_exp_col)):
            expiry = parse_date_safe(row.get(mot_exp_col))
        elif last_mot_col and pd.notna(row.get(last_mot_col)):
            last = parse_date_safe(row.get(last_mot_col))
            expiry = last + relativedelta(years=1) if last else None

        if expiry:
            days_left = (expiry - now).days
            if days_left < 0:
                actions.append({"Vehicle": vehicle, "Action": "MOT", "Status": "Overdue",
                                "Reason": f"Expired {-days_left} days ago on {expiry.strftime('%d %b %Y')}.",
                                "Recipient": recipient, "MOT Expiry": expiry.date()})
            elif days_left <= due_days:
                actions.append({"Vehicle": vehicle, "Action": "MOT", "Status": "Due soon",
                                "Reason": f"Expires in {days_left} days on {expiry.strftime('%d %b %Y')}.",
                                "Recipient": recipient, "MOT Expiry": expiry.date()})
    return pd.DataFrame(actions)

def send_email_with_csv(to_addr, subject, body, csv_bytes, csv_name="fleet_actions.csv"):
    host = get_secret("SMTP_HOST"); port = int(get_secret("SMTP_PORT","587"))
    user = get_secret("SMTP_USER");  pwd  = get_secret("SMTP_PASS")
    use_tls = (str(get_secret("SMTP_TLS","1")) == "1")
    from_name = get_secret("SMTP_FROM_NAME","AI Fleet Manager")
    from_addr = get_secret("SMTP_FROM", user or "no-reply@example.com")
    if not (host and user and pwd and to_addr):
        raise RuntimeError("SMTP settings or recipient missing.")

    from email.mime.multipart import MIME
