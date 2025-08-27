import streamlit as st
import pandas as pd
import json
from datetime import date, datetime, timezone
import altair as alt
import requests
import gspread
from gspread_dataframe import set_with_dataframe, get_as_dataframe
from google.oauth2.service_account import Credentials

DEFAULT_SETTINGS = {
    "personal_target_hours": 35.0,
    "tier_threshold_minutes": 30.0,  # fallback for tiered tasks if not set per task
    "bonus_milestones": [
        {"hours": 20.0, "bonus": 43.50},
        {"hours": 35.0, "bonus": 101.50}
    ],
    "extra_earnings": 0.0,          # running total added to current earnings (USD)
    # FX controls
    "use_live_fx": True,
    "usd_mxn_rate": 18.50,          # last known USD->MXN (used & saved)
    "usd_mxn_source": "manual",
    "usd_mxn_last_updated": "",     # ISO timestamp (UTC)
}

# ---------- Use Google Sheets for saving data ----------
GSHEETS_ID = st.secrets.get("GSHEETS_ID", "")
GCP_SA = st.secrets.get("gcp_service_account", None)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
WS_TASKS = "tasks"
WS_SETTINGS = "settings"
WS_RATES = "task_rates"

# ---------- Google Sheets Functions ----------
def _open_gsheet():
    if not GCP_SA or not GSHEETS_ID:
        raise RuntimeError("Streamlit Secrets not properly configured.")
    creds = Credentials.from_service_account_info(GCP_SA, scopes=SCOPES)
    client = gspread.authorize(creds)
    try:
        return client.open_by_key(GSHEETS_ID)
    except gspread.SpreadsheetNotFound:
        st.error("Spreadsheet not found. Double-check GSHEETS_ID (the string between /d/ and /edit in the URL).")
        st.stop()
    except Exception as e:
        st.error(f"Google Sheets error: {e}")
        st.stop()

def _ensure_ws(sh, title, rows=1000, cols=26):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))

# ---------- Data Functions ----------
def load_settings():
    sh = _open_gsheet()
    ws = _ensure_ws(sh, WS_SETTINGS, rows=10, cols=2)
    raw = ws.acell("A1").value
    if not raw:
        ws.update("A1", [[json.dumps(DEFAULT_SETTINGS, indent=2)]], value_input_option="RAW")
        return DEFAULT_SETTINGS.copy()
    try:
        s = json.loads(raw)
    except Exception:
        s = {}
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return s

def save_settings(settings: dict):
    sh = _open_gsheet()
    ws = _ensure_ws(sh, WS_SETTINGS, rows=10, cols=2)
    ws.update("A1", [[json.dumps(settings, indent=2)]], value_input_option="RAW")

def load_task_rates():
    sh = _open_gsheet()
    ws = _ensure_ws(sh, WS_RATES)
    df = get_as_dataframe(ws, evaluate_formulas=True, header=0)
    if df is None or df.empty:
        return {}
    df = df.fillna("")
    out = {}
    for _, r in df.iterrows():
        name = str(r.get("task", "")).strip()
        if not name:
            continue
        out[name] = {
            "hourly_rate": float(r.get("hourly_rate", 0) or 0),
            "discounted_rate": float(r.get("discounted_rate", 0) or 0),
            "pricing_mode": str(r.get("pricing_mode", "manual")).lower(),
            "tier_threshold_minutes": float(r.get("tier_threshold_minutes", DEFAULT_SETTINGS["tier_threshold_minutes"]) or 0),
        }
    return out

def save_task_rates(rates: dict):
    sh = _open_gsheet()
    ws = _ensure_ws(sh, WS_RATES)
    rows = []
    for k, v in rates.items():
        rows.append({
            "task": k,
            "hourly_rate": v.get("hourly_rate", 0.0),
            "discounted_rate": v.get("discounted_rate", 0.0),
            "pricing_mode": v.get("pricing_mode", "manual"),
            "tier_threshold_minutes": v.get("tier_threshold_minutes", DEFAULT_SETTINGS["tier_threshold_minutes"]),
        })
    df = pd.DataFrame(rows, columns=["task","hourly_rate","discounted_rate","pricing_mode","tier_threshold_minutes"])
    ws.clear()
    if df.empty:
        ws.update("A1:E1", [df.columns.tolist()])
    else:
        set_with_dataframe(ws, df, include_index=False, include_column_header=True, resize=True)

def load_tasks() -> pd.DataFrame:
    sh = _open_gsheet()
    ws = _ensure_ws(sh, WS_TASKS)
    df = get_as_dataframe(ws, evaluate_formulas=True, header=0)
    if df is None or df.empty:
        df = pd.DataFrame(columns=["date","task","minutes","seconds","rate_type"])
        set_with_dataframe(ws, df, include_index=False, include_column_header=True, resize=True)
        return df
    expected = ["date","task","minutes","seconds","rate_type"]
    for col in expected:
        if col not in df.columns:
            df[col] = pd.NA
    df = df[expected]
    df["minutes"] = pd.to_numeric(df["minutes"], errors="coerce").fillna(0).astype(int)
    df["seconds"] = pd.to_numeric(df["seconds"], errors="coerce").fillna(0).astype(int)
    df["rate_type"] = df["rate_type"].fillna("full")
    try:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    except Exception:
        df["date"] = df["date"].astype(str)
    return df.reset_index(drop=True)

def save_tasks(df: pd.DataFrame):
    sh = _open_gsheet()
    ws = _ensure_ws(sh, WS_TASKS)
    out = df.copy()
    out = out[["date","task","minutes","seconds","rate_type"]]
    set_with_dataframe(ws, out, include_index=False, include_column_header=True, resize=True)

# ---------- Live FX Functions ----------
def fetch_usd_mxn_rate() -> tuple[float, str]:
    """Try multiple public sources for USD->MXN. Returns (rate, source_name)."""
    sources = [
        ("https://api.frankfurter.app/latest?from=USD&to=MXN", "frankfurter"),
        ("https://open.er-api.com/v6/latest/USD", "erapi"),
        ("https://api.exchangerate.host/latest?base=USD&symbols=MXN", "exchangerate_host"),
    ]
    for url, name in sources:
        try:
            r = requests.get(url, timeout=6)
            r.raise_for_status()
            js = r.json()
            if name == "frankfurter":
                rate = float(js["rates"]["MXN"])  # {'rates': {'MXN': 16.8}, 'base': 'USD', 'date': 'YYYY-MM-DD'}
            elif name == "erapi":
                rate = float(js["rates"]["MXN"])   # {'result':'success','rates':{'MXN':16.8}, ...}
            else:  # exchangerate_host
                rate = float(js["rates"]["MXN"])  # {'rates': {'MXN': 16.8}, 'base': 'USD', ...}
            if rate > 0:
                return rate, name
        except Exception:
            continue
    raise RuntimeError("No FX source reachable")

# ---------- Compute Functions ----------
def compute_task_earning(minutes: int, seconds: int, rate_type: str, task_rates: dict, task_name: str) -> float:
    m = int(minutes)
    s = int(seconds)
    total_minutes = m + s / 60.0

    task_rate = task_rates.get(task_name, {})
    hr = float(task_rate.get("hourly_rate", 0.0))
    dr = float(task_rate.get("discounted_rate", 0.0))
    mode = str(task_rate.get("pricing_mode", "manual")).lower()
    thr = float(task_rate.get("tier_threshold_minutes", DEFAULT_SETTINGS["tier_threshold_minutes"]))

    if mode == "tiered":
        if total_minutes <= thr:
            return (total_minutes / 60.0) * hr
        else:
            return (thr / 60.0) * hr + ((total_minutes - thr) / 60.0) * dr
    else:
        rate = hr if str(rate_type).lower() == "full" else dr
        return (total_minutes / 60.0) * rate

def compute_metrics(tasks_df: pd.DataFrame, settings: dict, task_rates: dict) -> dict:
    df = tasks_df.copy()
    if df.empty:
        return {
            "total_hours": 0.0,
            "total_earnings": float(settings.get("extra_earnings", 0.0)),
            "avg_task_minutes": 0.0,
            "avg_task_earnings": 0.0,
            "bonuses_earned": 0.0,
            "bonuses_unlocked": [],
            "remaining_hours_to_target": settings["personal_target_hours"],
            "tasks_needed": None,
            "task_count": 0,
        }

    df["earning"] = df.apply(
        lambda r: compute_task_earning(r["minutes"], r["seconds"], r["rate_type"], task_rates, r["task"]), axis=1
    )

    total_minutes = df["minutes"].sum() + df["seconds"].sum() / 60.0
    total_hours = total_minutes / 60.0
    total_earnings = df["earning"].sum() + float(settings.get("extra_earnings", 0.0))

    avg_task_minutes = (df["minutes"] + df["seconds"] / 60.0).mean()
    avg_task_earnings = df["earning"].mean()

    milestones = sorted(settings.get("bonus_milestones", []), key=lambda x: x.get("hours", 0.0))
    bonuses_unlocked = [m for m in milestones if total_hours >= float(m.get("hours", 0.0))]
    if bonuses_unlocked:
        last = max(bonuses_unlocked, key=lambda x: float(x.get("hours", 0.0)))
        bonuses_earned = float(last.get("bonus", 0.0))
    else:
        bonuses_earned = 0.0


    remaining_hours = max(0.0, float(settings["personal_target_hours"]) - total_hours)
    tasks_needed = None
    if avg_task_minutes and avg_task_minutes > 0:
        tasks_needed = int((remaining_hours * 60.0 + avg_task_minutes - 1e-9) // avg_task_minutes)

    return {
        "total_hours": total_hours,
        "total_earnings": total_earnings,  # USD
        "avg_task_minutes": avg_task_minutes or 0.0,
        "avg_task_earnings": avg_task_earnings or 0.0,
        "bonuses_earned": bonuses_earned,
        "bonuses_unlocked": bonuses_unlocked,
        "remaining_hours_to_target": remaining_hours,
        "tasks_needed": tasks_needed,
        "task_count": int(len(df)),
        "per_task": df,
    }

# --------------------- UI -------------------
st.set_page_config(page_title="Outlier Earnings Dashboard", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")
st.title("📈 Outlier Earnings Dashboard")

settings = load_settings()
task_rates = load_task_rates()

# On-first-load attempt a live FX refresh if enabled and no previous timestamp
if settings.get("use_live_fx", True) and not settings.get("usd_mxn_last_updated"):
    try:
        rate, src = fetch_usd_mxn_rate()
        settings["usd_mxn_rate"] = rate
        settings["usd_mxn_source"] = src
        settings["usd_mxn_last_updated"] = datetime.now(timezone.utc).isoformat()
        save_settings(settings)
    except Exception:
        pass

# ---------- Sidebar ----------
with st.sidebar:
    st.header("⚙️ Settings")
    settings["personal_target_hours"] = st.number_input("Personal target (hrs)", min_value=0.0, value=float(settings["personal_target_hours"]))

    # Extra earnings (USD)
    settings["extra_earnings"] = st.number_input(
        "Extra earnings (USD)", min_value=0.0, value=float(settings.get("extra_earnings", 0.0)),
        help="Running total added to Current earnings (USD)."
    )

    # FX controls
    st.subheader("💱 USD → MXN")
    settings["use_live_fx"] = st.checkbox("Use live rate", value=bool(settings.get("use_live_fx", True)))
    if settings["use_live_fx"]:
        col_fx1, col_fx2 = st.columns([1,1])
        with col_fx1:
            if st.button("↻ Refresh live rate"):
                try:
                    rate, src = fetch_usd_mxn_rate()
                    settings["usd_mxn_rate"] = rate
                    settings["usd_mxn_source"] = src
                    settings["usd_mxn_last_updated"] = datetime.now(timezone.utc).isoformat()
                    save_settings(settings)
                    st.success(f"Live rate {rate:.4f} from {src}")
                except Exception as e:
                    st.error(f"Could not fetch rate: {e}")
        with col_fx2:
            st.metric("Live rate", f"{settings.get('usd_mxn_rate', 0.0):.4f}")
        st.caption(
            f"Source: **{settings.get('usd_mxn_source','—')}** · Updated: **{settings.get('usd_mxn_last_updated','—')}**"
        )
    else:
        settings["usd_mxn_rate"] = st.number_input(
            "Manual USD → MXN rate", min_value=0.0, step=0.01,
            value=float(settings.get("usd_mxn_rate", DEFAULT_SETTINGS["usd_mxn_rate"]))
        )

    st.subheader("🎯 Bonus milestones")
    bm_df = pd.DataFrame(settings.get("bonus_milestones", []))
    if bm_df.empty:
        bm_df = pd.DataFrame([{"hours": 0.0, "bonus": 0.0}])
    edited_bm = st.data_editor(bm_df, num_rows="dynamic", width='stretch', key="bm_editor")

    st.subheader("🛠️ Task Rates")
    rates_df = pd.DataFrame([
        {"task": k, **v} for k, v in task_rates.items()
    ])
    if rates_df.empty:
        rates_df = pd.DataFrame([{ "task": "", "hourly_rate": 0.0, "discounted_rate": 0.0, "pricing_mode": "tiered", "tier_threshold_minutes": 30.0}])
    edited_rates = st.data_editor(rates_df, num_rows="dynamic", width='stretch', key="rates_editor")

    if st.button("💾 Save settings"):
        settings["bonus_milestones"] = [
            {"hours": float(row.get("hours", 0.0)), "bonus": float(row.get("bonus", 0.0))}
            for _, row in edited_bm.fillna(0).iterrows()
            if float(row.get("hours", 0.0)) > 0 or float(row.get("bonus", 0.0)) > 0
        ]
        save_settings(settings)
        task_rates = {
            str(row["task"]): {
                "hourly_rate": float(row.get("hourly_rate", 0.0)),
                "discounted_rate": float(row.get("discounted_rate", 0.0)),
                "pricing_mode": row.get("pricing_mode", "manual"),
                "tier_threshold_minutes": float(row.get("tier_threshold_minutes", 5.0))
            }
            for _, row in edited_rates.fillna(0).iterrows() if str(row["task"]).strip()
        }
        save_task_rates(task_rates)
        st.success("Settings and task rates saved.")

st.divider()

# ---------- KPIs ----------
metrics = compute_metrics(load_tasks(), settings, task_rates)

# Compute MXN conversion for current earnings. Remove 4.5%  from Paypal commission.
earnings_usd = float(metrics["total_earnings"] + metrics["bonuses_earned"]) if metrics else 0.0
earnings_mxn = 0.955 * earnings_usd * float(settings.get("usd_mxn_rate", DEFAULT_SETTINGS["usd_mxn_rate"]))

m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    st.metric("Total hours", f"{metrics['total_hours']:.2f} h")
with m2:
    st.metric("Bonuses earned", f"${metrics['bonuses_earned']:.2f}")
with m3:
    st.metric("Number of tasks", f"{int(metrics.get('task_count', 0))}")
with m4:
    st.metric("Earnings (USD)", f"${earnings_usd:.2f}")
with m5:
    st.metric("Earnings (MXN)", f"${earnings_mxn:,.2f}")

# ---------- Chart ----------
st.subheader("🏁 Progress to personal target")
target_hours = float(settings.get("personal_target_hours", 0.0))
current_hours = float(metrics["total_hours"]) if metrics else 0.0
st.caption(f"{current_hours:.2f} / {target_hours:.2f} hrs")
try:
    target = target_hours if target_hours > 0 else 1.0
    base_df = pd.DataFrame({"start": [0.0], "end": [target]})
    fill_df = pd.DataFrame({"start": [0.0], "end": [min(current_hours, target)]})
    rules_df = pd.DataFrame([
        {"at": min(float(m.get("hours", 0.0)), target), "label": f"{m.get('hours')}h"}
        for m in settings.get("bonus_milestones", [])
    ])

    bg = alt.Chart(base_df).mark_bar(color="#d1d5db").encode(
        x=alt.X("start:Q", scale=alt.Scale(domain=[0, target]), title=None),
        x2="end:Q",
        y=alt.value(18)
    ).properties(height=52)

    fill = alt.Chart(fill_df).mark_bar(color="#3b82f6").encode(
        x=alt.X("start:Q", scale=alt.Scale(domain=[0, target])),
        x2="end:Q",
        y=alt.value(18),
        tooltip=[alt.Tooltip("end:Q", title="Hours", format=".2f")]
    )

    if not rules_df.empty:
        points = alt.Chart(rules_df).mark_point(shape="diamond", size=150, filled=True, color="#ff0000").encode(
            x=alt.X("at:Q", scale=alt.Scale(domain=[0, target])),
            y=alt.value(16)
        )
        chart = bg + fill + points
    else:
        chart = bg + fill

    st.altair_chart(chart, use_container_width=True)
except Exception:
    st.caption("Milestone markers unavailable.")

st.divider()

# ---------- Task Logger ----------
st.subheader("📝 Log a task")
with st.form("task_form", clear_on_submit=True):
    c1, c2, c3, c4, c5 = st.columns([1, 1, 1, 1, 1])
    with c1:
        t_date = st.date_input("Date", value=date.today())
    with c2:
        task_options = list(task_rates.keys())
        t_task = st.selectbox("Task name", options=task_options if task_options else ["(add task types in Settings)"])
    with c3:
        t_minutes = st.number_input("Minutes", min_value=0, step=1, value=0)
    with c4:
        t_seconds = st.number_input("Seconds", min_value=0, max_value=59, step=1, value=0)
    with c5:
        t_mode = task_rates.get(t_task, {}).get("pricing_mode", "manual")
        if t_mode == "manual":
            rate_type = st.selectbox("Rate type", options=["full", "discounted"], index=0)
        else:
            rate_type = "full"
            st.caption("")
        submitted = st.form_submit_button("➕ Add task")

    if submitted:
        if t_task == "(add task types in Settings)":
            st.error("Please add task types in Settings first.")
        else:
            df = load_tasks()
            new_row = {
                "date": pd.to_datetime(t_date).strftime("%Y-%m-%d"),
                "task": t_task.strip(),
                "minutes": int(t_minutes),
                "seconds": int(t_seconds),
                "rate_type": rate_type,
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            save_tasks(df)
            st.success("Task added.")
            st.rerun()

# ---------- Task Table ----------
st.subheader("📚 Task log")
_tasks_raw = load_tasks()
if not _tasks_raw.empty:
    tasks_display = _tasks_raw.copy()
    tasks_display["earning"] = tasks_display.apply(
        lambda r: compute_task_earning(r["minutes"], r["seconds"], r["rate_type"], task_rates, r["task"]), axis=1
    )
    tasks_display = tasks_display[["date", "task", "minutes", "seconds", "earning"]]
else:
    tasks_display = pd.DataFrame(columns=["date", "task", "minutes", "seconds", "earning"])

if "delete" not in tasks_display.columns:
    tasks_display["delete"] = False

edited_display = st.data_editor(
    tasks_display,
    num_rows="fixed",
    width='stretch',
    key="tasks_editor",
    column_config={
        "minutes": st.column_config.NumberColumn("minutes", step=1),
        "seconds": st.column_config.NumberColumn("seconds", step=1),
        "earning": st.column_config.NumberColumn("earning", format="$%.2f", help="Per-task earning (read-only)"),
        "delete": st.column_config.CheckboxColumn("delete")
    },
    disabled=["date", "task", "minutes", "seconds", "earning"],
)

col_tb, col_tc = st.columns([1,1])
with col_tb:
    if st.button("🗑️ Delete checked rows"):
        ed = edited_display.copy()
        to_keep = ~ed.get("delete", pd.Series(False, index=ed.index)).fillna(False)
        ed = ed.loc[to_keep, ["date", "task", "minutes", "seconds"]].reset_index(drop=True)
        # keep rate_type from raw where possible (defaults to 'full' for mismatched lengths)
        rate_series = _tasks_raw.get("rate_type", pd.Series([], dtype="object")).reset_index(drop=True)
        rate_series = rate_series.reindex(range(len(ed))).fillna("full")
        new_raw = pd.DataFrame({
            "date": ed.get("date"),
            "task": ed.get("task"),
            "minutes": pd.to_numeric(ed.get("minutes"), errors="coerce").fillna(0).astype(int),
            "seconds": pd.to_numeric(ed.get("seconds"), errors="coerce").fillna(0).astype(int),
            "rate_type": rate_series,
        })
        save_tasks(new_raw)
        st.success("Selected rows deleted.")
        st.rerun()
with col_tc:
    if st.button("🧹 Clear all tasks"):
        save_tasks(pd.DataFrame(columns=["date", "task", "minutes", "seconds", "rate_type"]))
        st.warning("All tasks cleared.")
        st.rerun()

# ---------- Manual vs Tiered ----------
with st.expander("What do Manual vs Tiered pricing mean?"):
    st.markdown(
        """
**Manual:** You choose **full** or **discounted** for each task.  
**Tiered:** The first **N minutes** (its threshold) are billed at that task type’s **full** rate; the rest at its **discounted** rate. No rate-type picker needed.

You set these on the **Task Rates** table in the sidebar.
        """
    )