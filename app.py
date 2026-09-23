import os
import hmac
import html
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

# Page Config & Styling
st.set_page_config(
    page_title="Sweeps Change Detection Platform",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

from utils.storage import SnapshotStore, clean_project_name, github_configured
from utils.sweep import (read_csv, run_sweep, results_csv, last_sweep, record_sweep, first_sweep_since, now_iso,
                         FIRST_SWEEP_DAYS, CHANGED, NO_CHANGE, BASELINE, MANUAL, ERROR)

load_dotenv()


def get_password():
    try:
        return st.secrets["APP_PASSWORD"]
    except Exception:
        return os.getenv("APP_PASSWORD")


PASSWORD = get_password()

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    :root {
        --primary-blue: #1e3a8a;
        --secondary-blue: #3b82f6;
        --success-green: #059669;
        --error-red: #dc2626;
        --new-yellow: #fcca05;
        --baseline-grey: #64748b;
    }

    .lcn-header {
        background: linear-gradient(135deg, var(--primary-blue) 0%, var(--secondary-blue) 100%);
        padding: 2rem;
        border-radius: 10px;
        color: white;
        text-align: center;
        margin-bottom: 2rem;
    }

    .company-name {
        font-family: 'Inter', sans-serif;
        font-size: 2.5rem;
        font-weight: 700;
        margin: 0;
        color: white;
    }

    .tagline {
        font-family: 'Inter', sans-serif;
        font-size: 1.1rem;
        font-weight: 400;
        color: rgba(255, 255, 255, 0.9);
        margin-top: 0.5rem;
    }

    .status-success {
        background-color: #ecfdf5;
        border-left: 4px solid var(--success-green);
        padding: 0.75rem;
        border-radius: 6px;
        margin: 0.5rem 0;
        color: #065f46;
    }

    .status-new {
        background-color: #fcf3cf;
        border-left: 4px solid var(--new-yellow);
        padding: 0.75rem;
        border-radius: 6px;
        margin: 0.5rem 0;
        color: #5f5806;
    }

    .status-error {
        background-color: #fef2f2;
        border-left: 4px solid var(--error-red);
        padding: 0.75rem;
        border-radius: 6px;
        margin: 0.5rem 0;
        color: #991b1b;
    }

    .status-baseline {
        background-color: #f1f5f9;
        border-left: 4px solid var(--baseline-grey);
        padding: 0.75rem;
        border-radius: 6px;
        margin: 0.5rem 0;
        color: #334155;
    }

    .status-manual {
        background-color: #eff6ff;
        border-left: 4px solid var(--secondary-blue);
        padding: 0.75rem;
        border-radius: 6px;
        margin: 0.5rem 0;
        color: #1e3a8a;
    }

    .status-new a, .status-error a, .status-success a, .status-baseline a, .status-manual a {
        color: inherit;
    }

    .stButton > button, .stFormSubmitButton > button {
        background: linear-gradient(135deg, var(--primary-blue), var(--secondary-blue));
        color: white;
        border-radius: 8px;
        padding: 0.75rem 2rem;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# Header
def create_header():
    st.markdown("""
    <div class="lcn-header">
        <h1 class="company-name">LCN Consulting</h1>
        <p class="tagline">Detecting Competitor Website Changes</p>
    </div>
    """, unsafe_allow_html=True)


# Sidebar
def create_sidebar():
    with st.sidebar:
        st.write(f"👤 Signed in as **{st.session_state.user_name}**")
        st.markdown("### 📋 Platform Overview")
        st.write("Monitor competitor websites for changes. Each person sees what is new since "
                 "**their own** last sweep of a project, even if someone else swept it in between.")

        st.markdown("### 📊 Required Data Format")
        st.write("**CSV columns needed:** `Company`, `URL`, `URL Type`")
        st.write("Each URL must be a full web address (https://...). Save from Excel as "
                 "**CSV UTF-8** if names contain special characters.")

        st.markdown("### 🧭 Result Types")
        st.write("🆕 **Changed** – new items appeared since your last sweep.")
        st.write("✅ **No Change** – nothing new since your last sweep.")
        st.write("📌 **New** – first time this page is checked; saved as the baseline.")
        st.write("👀 **Check manually** – the site blocks automated access or loads its listing with "
                 "JavaScript, so it has to be looked at by a person.")
        st.write("🚨 **Error** – the page could not be checked (bad URL, page gone, site down); see the reason.")

        if not github_configured():
            st.warning("GitHub is not configured, so snapshot history is only kept on this server.")


# Session State for Login
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Login Page
if not st.session_state.authenticated:
    st.title("Login")
    if not PASSWORD:
        st.error("APP_PASSWORD is not configured. Add it to the Streamlit secrets or a .env file.")
        st.stop()
    with st.form("login"):
        name_input = st.text_input("Your name", help="Used to show you everything that is new since "
                                   "your own last sweep. Use the same name every time.")
        password_input = st.text_input("Enter Password", type="password")
        submitted = st.form_submit_button("Login")
    if submitted:
        if not name_input.strip():
            st.error("Please enter your name.")
        elif hmac.compare_digest(password_input.encode(), PASSWORD.encode()):
            st.session_state.authenticated = True
            st.session_state.user_name = " ".join(name_input.split())
            st.rerun()  # Immediately refresh to show upload page
        else:
            st.error("Incorrect password")
    st.stop()


def _fmt_time(iso):
    return datetime.fromisoformat(iso).strftime("%b %d, %Y %H:%M UTC")


def _link(url, text=None):
    safe = html.escape(url or "", quote=True)
    return f'<a href="{safe}" target="_blank">{html.escape(text or url or "")}</a>'


def _label(res):
    row = res.row
    return f"{html.escape(row.company)} ({html.escape(row.url_type)})"


def _warning(res):
    return f"<br><small>⚠️ {html.escape(res.warning)}</small>" if res.warning else ""


def show_results(summary):
    results = summary["results"]
    counts = {status: sum(1 for r in results if r.status == status)
              for status in (CHANGED, NO_CHANGE, BASELINE, MANUAL, ERROR)}

    st.markdown(f"## Summary — {html.escape(summary['project'])}")
    st.caption(f"Checked {len(results)} rows at {summary['finished']} in {summary['elapsed']:.0f} seconds. "
               f"{summary.get('since_note', '')}")
    cols = st.columns(5)
    cols[0].metric("🆕 Changed", counts[CHANGED])
    cols[1].metric("✅ No Change", counts[NO_CHANGE])
    cols[2].metric("📌 New (baseline)", counts[BASELINE])
    cols[3].metric("👀 Check manually", counts[MANUAL])
    cols[4].metric("🚨 Errors", counts[ERROR])
    redirected = sum(1 for r in results if r.warning)
    if redirected:
        st.caption(f"⚠️ {redirected} row(s) now redirect to a different page — see the notes below and update those URLs.")

    saved_ok, saved_msg = summary["saved"]
    (st.success if saved_ok else st.warning)(saved_msg)
    for warning in summary["store_warnings"]:
        st.warning(warning)

    st.download_button("Download results (CSV)", results_csv(results).encode("utf-8-sig"),
                       file_name=f"sweep_{summary['project']}_{summary['finished'][:10]}.csv", mime="text/csv")

    st.markdown("### Changes")
    changed = [r for r in results if r.status == CHANGED]
    if not changed:
        st.markdown("No changes.")
    for res in changed:
        st.markdown(f'<div class="status-new">🆕 {_label(res)} - Changed · {_link(res.row.url, "open page")}'
                    f'<br><small>{html.escape(res.message)}</small>{_warning(res)}</div>', unsafe_allow_html=True)
        with st.expander(f"Show new items ({len(res.new_items)})"):
            lines = []
            for item in res.new_items[:50]:
                date = f" — {html.escape(item['date'])}" if item.get("date") else ""
                lines.append(f"<li>{_link(item['link'], item['title'])}{date}</li>")
            if len(res.new_items) > 50:
                lines.append(f"<li>…and {len(res.new_items) - 50} more (see the CSV download)</li>")
            st.markdown("<ul>" + "".join(lines) + "</ul>", unsafe_allow_html=True)

    st.markdown("### No Changes")
    unchanged = [r for r in results if r.status == NO_CHANGE]
    if not unchanged:
        st.markdown("None.")
    for res in unchanged:
        st.markdown(f'<div class="status-success">✅ {_label(res)} - No Change{_warning(res)}</div>',
                    unsafe_allow_html=True)

    baseline = [r for r in results if r.status == BASELINE]
    if baseline:
        st.markdown("### New (baseline saved)")
        for res in baseline:
            st.markdown(f'<div class="status-baseline">📌 {_label(res)} - First check, baseline saved · '
                        f'{_link(res.row.url, "open page")}{_warning(res)}</div>', unsafe_allow_html=True)

    manual = [r for r in results if r.status == MANUAL]
    if manual:
        st.markdown("### Check manually")
        for res in manual:
            st.markdown(f'<div class="status-manual">👀 {_label(res)} - {html.escape(res.message)} · '
                        f'{_link(res.row.url, "open page")}{_warning(res)}</div>', unsafe_allow_html=True)

    st.markdown("### Errors")
    errors = [r for r in results if r.status == ERROR]
    if not errors:
        st.markdown("No errors.")
    for res in errors:
        target = _link(res.row.url, "open page") if res.row.url else html.escape(res.row.url_raw or "")
        st.markdown(f'<div class="status-error">🚨 {_label(res)} - {html.escape(res.message)}'
                    f'<br><small>{target}</small>{_warning(res)}</div>', unsafe_allow_html=True)


# Main Upload Page
create_header()
create_sidebar()

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

# Summary of the last sweep (kept until the next one)
if st.session_state.get("summary"):
    show_results(st.session_state.summary)
    st.divider()

st.markdown("## 📂 Upload Configuration File")

# File Inputter
uploaded_file = st.file_uploader(
    "Upload Competitor Configuration File",
    type=['csv'],
    help="Upload CSV file containing competitor URLs",
    key=f"uploaded_file_{st.session_state.uploader_key}"
)

if uploaded_file:
    try:
        rows, warnings = read_csv(uploaded_file.getvalue())
    except ValueError as e:
        st.error(str(e))
        st.stop()

    unique_urls = len({r.url for r in rows if r.url})
    st.write(f"**{uploaded_file.name}** — {len(rows)} rows, {unique_urls} unique URLs.")
    if warnings:
        with st.expander(f"⚠️ {len(warnings)} issue(s) found in the file (these rows will be reported as errors or checked once)"):
            for w in warnings:
                st.write("• " + w)

    project_name = clean_project_name(st.text_input(
        "Project name (snapshot history is kept per project — use the same name every time for the same sweep)",
        value=clean_project_name(uploaded_file.name),
    ))

    if st.button("🔍 Run sweep"):
        store = SnapshotStore(project_name).load()
        if not store.has_history:
            st.info(f"No snapshot history found for “{project_name}” — this sweep will save the baseline.")

        # Report everything first seen since this person's own last sweep of the project
        user = st.session_state.user_name
        sweep_time = now_iso()
        since = last_sweep(store, user)
        if since:
            since_note = f"Changes are since your last sweep of this project ({_fmt_time(since)})."
        else:
            since = first_sweep_since()
            since_note = (f"This is your first sweep of this project as “{user}”, so changes from the "
                          f"last {FIRST_SWEEP_DAYS} days are shown.")

        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def on_progress(done, total, row):
            progress_bar.progress(done / total if total else 1.0)
            if row is not None:
                status_text.write(f"Checked {done} of {total} rows — last: {row.company} ({row.url_type})")

        start = datetime.now()
        results = run_sweep(rows, store, on_progress, since=since, now=sweep_time)
        progress_bar.empty()
        status_text.write("Saving snapshots…")

        # Snapshots (and this person's sweep time) are only saved once the sweep is complete,
        # so an interrupted sweep never hides changes: they will be detected again next run.
        record_sweep(store, user, sweep_time)
        st.session_state.summary = {
            "project": project_name,
            "results": results,
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "elapsed": (datetime.now() - start).total_seconds(),
            "since_note": since_note,
            "saved": store.save(),
            "store_warnings": store.warnings,
        }

        # Reset uploader but keep results
        st.session_state.uploader_key += 1
        st.rerun()


# Logout Button
if st.session_state.authenticated:
    if st.button("Logout"):
        st.session_state.authenticated = False
        st.session_state.pop("summary", None)
        st.session_state.pop("user_name", None)
        st.rerun()
