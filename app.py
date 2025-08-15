# app.py  — AI Fleet Manager Dashboard (robust headers version)

import os
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Fleet Manager Dashboard", layout="wide")
st.title("🚚 AI Fleet Manager Dashboard")
st.write("Upload your fleet spreadsheet to check upcoming **Services** and **MOTs**.")

# ---------- Helpers ----------
def parse_date_safe(val):
    if pd.isna(val):
        return None
    if isinstance(val, (pd.Timestamp, datetime)):
        return pd.Timestamp(val).to_pydatetime()
    # Excel serials
    if isinstance(val, (int, float)) and val > 20000:
        return datetime(1899, 12, 30) + timedelta(days=float(val))
    # Fallback parser
    dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.to_pydatetime()

def pick(cols, *aliases):
    """Return the first matching column (case/space-insensitive) or None."""
    norm = {c.strip().lower(): c for c in cols}
    for a in aliases:
        key = a.strip().lower()
        if key in norm:
            return norm[key]
    return None

def think_actions(df, due_miles=500, due_days=30):
    # normalise a view of the column names
    cols = df.columns.tolist()

    veh_col = pick(cols, "reg", "registration", "vehicle", "vrm")
    mileage_col = pick(cols, "current mileage", "mileage", "odometer", "current_mileage")
    last_service_mileage_col = pick(cols, "service last mileage", "last service mileage", "last_service_mileage")
    service_interval_col = pick(cols, "service interval (miles)", "service interval", "service_interval_miles")
    service_due_at_col = pick(cols, "service mileage due at", "service due at", "service_due_at")
    miles_to_service_col = pick(cols, "miles_to_service", "miles to service")

    last_mot_date_col = pick(cols, "last mot date", "last mot", "last_mot_date")
    mot_expiry_col = pick(cols, "mot date required", "mot expiry", "mot due", "mot_due_date")

    # Optional per-vehicle email recipient
    email_col = pick(cols, "email", "manager email", "contact email", "recipient")

    actions = []
    now = datetime.now()

    for i, row in df.iterrows():
        vehicle = str(row.get(veh_col, f"Vehicle {i+1}")) if veh_col else f"Vehicle {i+1}"
        recipient = str(row.get(email_col)) if email_col and pd.notna(row.get(email_col)) else ""

        # ------- Service logic -------
        svc_due = False
        svc_status = ""
        svc_reason = ""

        try:
            if miles_to_service_col and pd.notna(row.get(miles_to_service_col)):
                mleft = float(row.get(miles_to_service_col))
            elif service_due_at_col and mileage_col and pd.notna(row.get(service_due_at_col)) and pd.notna(row.get(mileage_col)):
                mleft = float(row.get(service_due_at_col)) - float(row.get(mileage_col))
            elif mileage_col and last_service_mileage_col and service_interval_col and all(pd.notna(row.get(c)) for c in [mileage_col, last_service_mileage_col, service_interval_col]):
                mleft = (float(row.get(last_service_mileage_col)) + float(row.get(service_interval_col))) - float(row.get(mileage_col))
            else:
                mleft = None

            if mleft is None:
                svc_reason = "Missing service data."
            else:
                if mleft <= 0:
                    svc_due = True
                    svc_status = "Due"
                    svc_reason = f"Overdue by {int(abs(mleft))} miles."
                elif mleft <= due_miles:
                    svc_due = True
                    svc_status = "Due soon"
                    svc_reason = f"Within {int(mleft)} miles of service."
                else:
                    svc_reason = f"{int(mleft)} miles remaining to next service."
        except Exception:
            svc_reason = "Insufficient/invalid service data."

        if svc_due:
            actions.append({
                "Vehicle": vehicle,
                "Action": "Service",
                "Status": svc_status,
                "Reason": svc_reason,
                "Recipient": recipient
            })

        # ------- MOT logic -------
        expiry = None
        try:
            if mot_expiry_col and pd.notna(row.get(mot_expiry_col)):
                expiry = parse_date_safe(row.get(mot_expiry_col))
            elif last_mot_date_col and pd.notna(row.get(last_mot_date_col)):
                last_mot = parse_date_safe(row.get(last_mot_date_col))
                if last_mot:
                    expiry = last_mot + relativedelta(years=1)
        except Exception:
            expiry = None

        if expiry:
            days_left = (expiry - now).days
            if days_left < 0:
                actions.append({
                    "Vehicle": vehicle,
                    "Action": "MOT",
                    "Status": "Overdue",
                    "Reason": f"Expired {-days_left} days ago on {expiry.strftime('%d %b %Y')}.",
                    "Recipient": recipient,
                    "MOT Expiry": expiry.date()
                })
            elif days_left <= due_days:
                actions.append({
                    "Vehicle": vehicle,
                    "Action": "MOT",
                    "Status": "Due soon",
                    "Reason": f"Expires in {days_left} days on {expiry.strftime('%d %b %Y')}.",
                    "Recipient": recipient,
                    "MOT Expiry": expiry.date()
                })

    return pd.DataFrame(actions), {
        "veh_col": veh_col, "mileage_col": mileage_col,
        "last_service_mileage_col": last_service_mileage_col,
        "service_interval_col": service_interval_col,
        "service_due_at_col": service_due_at_col,
        "miles_to_service_col": miles_to_service_col,
        "last_mot_date_col": last_mot_date_col,
        "mot_expiry_col": mot_expiry_col,
        "email_col": email_col
    }

# ---------- UI: file upload ----------
uploaded = st.file_uploader("Upload Fleet Spreadsheet (.xlsx or .csv)", type=["xlsx", "csv"])
due_miles = st.number_input("Service ‘due soon’ threshold (miles)", 100, 5000, 500, 50)
due_days = st.number_input("MOT ‘due soon’ threshold (days)", 7, 120, 30, 1)

if not uploaded:
    st.info("Upload your Excel/CSV to begin. Your template headings like **Reg**, **Current Mileage**, **Service Interval (Miles)**, **Service Last Mileage**, **Service Mileage Due At**, **MOT Date Required** are supported.")
    st.stop()

# Read the file
try:
    if uploaded.name.lower().endswith(".csv"):
        df = pd.read_csv(uploaded)
    else:
        df = pd.read_excel(uploaded)
except Exception as e:
    st.error(f"Couldn't read file: {e}")
    st.stop()

# Show a peek of the data
with st.expander("Preview uploaded data"):
    st.dataframe(df.head(50), use_container_width=True)

# Run logic
actions_df, mapping = think_actions(df, due_miles=due_miles, due_days=due_days)

# Mapping info to reassure
with st.expander("Detected column mapping"):
    st.json(mapping)

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

# Table
st.subheader("Actions required")
if actions_df.empty:
    st.success("No actions required based on current thresholds. 🎉")
else:
    st.dataframe(actions_df, use_container_width=True)
    # Download
    csv = actions_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download actions as CSV", csv, "fleet_actions.csv", "text/csv")
