# app.py — AI Fleet Manager Dashboard (fixed-path via sidebar + email)
import os
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Fleet Manager Dashboard", layout="wide")
st.title("🚚 AI Fleet Manager Dashboard")
st.caption("This reads a spreadsheet from your repo (set the path in the sidebar) and can email the current actions.")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG (sidebar): point to the sheet inside your repo
# e.g. "Fleet_Manager_Template_UK.xlsx" (repo root) or "data/Fleet_Manager_Template_UK.xlsx"
DEFAULT_FILE_PATH = "Fleet_Manager_Template_UK.xlsx"
FILE_PATH = st.sidebar.text_input("Spreadsheet path (relative to repo)", value=DEFAULT_FILE_PATH).strip()

# Thresholds
DUE_MILES_DEFAULT = 500
DUE_DAYS_DEFAULT = 30
colA, colB = st.sidebar.columns(2)
due_miles = colA.number_input("Service ‘due soon’ (miles)", 100, 5000, DUE_MILES_DEFAULT, 50)
due_days  = colB.number_input("MOT ‘due soon’ (days)", 7, 120, DUE_DAYS_DEFAULT, 1)

# Email defaults
def get_secret(name, default=""):
    # Prefer Streamlit Secrets; fall back to environment variables
    return st.secrets.get(name, os.getenv(name, default))
# ──────────────────────────────────────────────────────────────────────────────

# Helpers
def parse_date_safe(val):
    if pd.isna(val): return None
    if isinstance(val, (pd.Timestamp, datetime)): return pd.Timestamp(val).to_pydatetime()
    if isinstance(val, (int, float)) and val > 20000:
        return datetime(1899, 12, 30) + timedelta(days=float(val))  # Excel serial
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

        # Service logic
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

        # MOT logic
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

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    import smtplib

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("text", "csv")
    part.set_payload(csv_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{csv_name}"')
    msg.attach(part)

    s = smtplib.SMTP(host, port, timeout=30)
    try:
        if use_tls: s.starttls()
        s.login(user, pwd)
        s.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
        s.quit()

# Load the spreadsheet from repo
try:
    if FILE_PATH.lower().endswith(".csv"):
        df = pd.read_csv(FILE_PATH)
    else:
        df = pd.read_excel(FILE_PATH)
except Exception as e:
    st.error(f"Could not read `{FILE_PATH}` from the repository. "
             f"Check the path/name and that the file is committed to Git. Error: {e}")
    st.stop()

with st.expander("Preview spreadsheet"):
    st.dataframe(df.head(50), use_container_width=True)

# Compute actions
actions_df = think_actions(df, due_miles=due_miles, due_days=due_days)

# Metrics
svc_cnt = len(actions_df[actions_df["Action"]=="Service"])
mot_cnt = len(actions_df[actions_df["Action"]=="MOT"])
overdue_cnt = len(actions_df[(actions_df["Action"]=="Service") & (actions_df["Status"]=="Due")]) + \
              len(actions_df[(actions_df["Action"]=="MOT") & (actions_df["Status"]=="Overdue")])

c1, c2, c3, c4 = st.columns(4)
c1.metric("Service actions", svc_cnt)
c2.metric("MOT actions", mot_cnt)
c3.metric("Overdue items", overdue_cnt)
c4.metric("Data timestamp", datetime.now().strftime("%d %b %Y %H:%M"))

st.subheader("Actions required")
if actions_df.empty:
    st.success("No actions required based on current thresholds. 🎉")
else:
    st.dataframe(actions_df, use_container_width=True)
    csv_bytes = actions_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download actions as CSV", csv_bytes, "fleet_actions.csv", "text/csv")

    # Email
    to_default = get_secret("EMAIL_DEFAULT_TO","")
    to_addr = st.text_input("Send to", value=to_default, placeholder="fleet@yourdomain.co.uk")
    if st.button("✉ Email me this report"):
        try:
            subject = f"[Fleet] Actions report — {datetime.now().strftime('%d %b %Y %H:%M')}"
            body = (f"Hi team,\n\nAttached is the current Fleet actions report (Service/MOT).\n\n"
                    f"Service actions: {svc_cnt}\nMOT actions: {mot_cnt}\nOverdue items: {overdue_cnt}\n\n"
                    f"Thanks,\nAI Fleet Manager")
            send_email_with_csv(to_addr, subject, body, csv_bytes)
            st.success(f"Email sent to {to_addr}")
        except Exception as e:
            st.error(f"Email failed: {e}")
