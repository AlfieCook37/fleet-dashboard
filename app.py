import streamlit as st
import pandas as pd
import os
from datetime import datetime

# --- Page setup ---
st.set_page_config(page_title="Fleet Manager Dashboard", layout="wide")
st.title("🚚 AI Fleet Manager Dashboard")
st.write("This tool checks your fleet spreadsheet for upcoming MOTs and services.")

# --- File input ---
uploaded_file = st.file_uploader("Upload Fleet Spreadsheet (.xlsx)", type="xlsx")

if uploaded_file:
    try:
        df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Error reading file: {e}")
        st.stop()

    # --- Check for required columns ---
    required_columns = [
        'reg',
        'current mileage',
        'service last mileage',
        'service interval (miles)',
        'service mileage due at',
        'miles_to_service',
        'mot date required'
    ]

    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        st.error(f"Missing required columns: {', '.join(missing_columns)}")
        st.stop()

    # --- Run checks ---
    today = datetime.today()
    df['service_needed'] = df['miles_to_service'] <= 500
    df['mot_needed'] = pd.to_datetime(df['mot date required'], errors='coerce').apply(
        lambda d: (d - today).days <= 30 if pd.notnull(d) else False
    )

    actions = df[(df['service_needed']) | (df['mot_needed'])]

    # --- Display results ---
    st.subheader("Summary")
    col1, col2 = st.columns(2)
    col1.metric("Vehicles needing service", int(df['service_needed'].sum()))
    col2.metric("Vehicles needing MOT", int(df['mot_needed'].sum()))

    st.subheader("Actions Required")
    st.dataframe(actions)

    # --- Download CSV ---
    csv = actions.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="Download Actions as CSV",
        data=csv,
        file_name="fleet_actions.csv",
        mime="text/csv",
    )
else:
    st.info("Upload your Excel fleet spreadsheet to begin.")
