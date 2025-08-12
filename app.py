import streamlit as st
from dotenv import load_dotenv
import os
import csv
import gc
import io
import time
from openpyxl import load_workbook
from utils.scraper import extract_items, clean_html
from utils.storage import load_previous_snapshot, save_snapshot, detect_new_items, push_bulk_snapshots
from utils.fetcher import fetch_html

load_dotenv()
PASSWORD = os.getenv("APP_PASSWORD") or st.secrets["APP_PASSWORD"]
CHUNK_SIZE = 2 # Num of rows processed at a time

# Page Config & Styling
st.set_page_config(
    page_title="Sweeps Change Detection Platform",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    :root {
        --primary-blue: #1e3a8a;
        --secondary-blue: #3b82f6;
        --success-green: #059669;
        --error-red: #dc2626;
        --new-yellow: #fcca05;
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
    
    .stButton > button {
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
        st.markdown("### 📋 Platform Overview")
        st.write("Monitor competitor websites for changes.")
        
        st.markdown("### 📊 Required Data Format")
        st.write("**CSV columns needed:** `Company`, `URL`, `URL Type`")

# Session State for Login
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Login Page
if not st.session_state.authenticated:
    st.title("Login")
    password_input = st.text_input("Enter Password", type="password")
    if st.button("Login"):
        if password_input == PASSWORD:
            st.session_state.authenticated = True
            st.success("Login successful!")
            st.rerun()  # Immediately refresh to show upload page
        else:
            st.error("Incorrect password")
    st.stop()

# Main Upload Page
create_header()
create_sidebar()
st.markdown("## 📂 Upload Configuration File")

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

for key in ["changes", "no_changes", "errors"]:
    if key not in st.session_state:
        st.session_state[key] = []

# Summary
if st.session_state.changes or st.session_state.no_changes or st.session_state.errors:
    st.markdown("## Summary")
    st.markdown("### Changes")
    if st.session_state['changes']:
        for item in st.session_state['changes']:
            st.markdown(item, unsafe_allow_html=True)
    else:
        st.markdown("No changes.")
    
    st.markdown("### No Changes")
    if st.session_state['no_changes']:
        for item in st.session_state['no_changes']:
            st.markdown(item, unsafe_allow_html=True)
    else:
        st.markdown("None.")

    st.markdown("### Errors")
    if st.session_state['errors']:
        for item in st.session_state['errors']:
            st.markdown(item, unsafe_allow_html=True)
    else:
        st.markdown("No errors.")

# File Inputter
uploaded_file = st.file_uploader(
    "Upload Competitor Configuration File",
    type=['csv'],
    help="Upload CSV file containing competitor URLs",
    key=f"uploaded_file_{st.session_state.uploader_key}"
)

# Generate rows from the CSV file
def csv_row_generator(file):
    try:
        text = io.TextIOWrapper(file, encoding="utf-8-sig")
        reader = csv.DictReader(text)
        reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    except Exception as e:
        st.error(f"Error reading CSV file: {e}")
        st.stop()
    # Ensure required columns exist (case-insensitive)
    required_cols = {"url", "company", "url type"}
    lower_headers = set(reader.fieldnames)
    if not required_cols.issubset(lower_headers):
        st.error(f"Missing required columns: {required_cols - lower_headers}")
        st.stop()
    for row in reader:
        row = {k.strip().lower(): v for k, v in row.items()} # To be safe
        yield {
            "url": row["url"],
            "company": row["company"], 
            "url type": row["url type"]
        }

# Read an uploaded file
if uploaded_file:
    st.write(f"Processing file: {uploaded_file.name}")
    project_name = os.path.splitext(uploaded_file.name)[0]

    changes, no_changes, errors = [], [], []
    start = time.time()
    rows = csv_row_generator(uploaded_file)
    st.write("File read in", time.time() - start, "seconds")
    results_container = st.container()
    progress_bar = st.progress(0)

    buffer = []
    total_processed = 0

    # Process in chunks
    for row in rows:
        buffer.append(row)
        
        # Reach threshold chunk size
        if len(buffer) >= CHUNK_SIZE:
            for entry in buffer:
                u, c, t = entry["url"], entry ["company"], entry["url type"] # Read item in each column
                status_box = results_container.empty()

                try:
                    html, source, status_code = fetch_html(u)

                    if html:
                        cleaned_html = clean_html(html)
                        del html # Free memory early

                        items, error = extract_items(cleaned_html, u)

                        # Actually scrape all the important information
                        if error:
                            errors.append(f'<div class="status-error">🚨"⚠️ Could not extract structured content from {c} ({t}): {error}\n"</div>')
                            continue

                        # Compare to previous saves: Has there been a change?
                        previous = load_previous_snapshot(project_name, c, t)
                        new_items = detect_new_items(previous, items)
                        del previous # Free memory

                        if new_items:
                            changes.append(f'<div class="status-new">🆕 {c} ({t}) - Changed</div>')
                        # for item in new_items:
                        #     results_log.append(f"    - {item['title']} ({item['timestamp']})\n")
                        #     results_log.append(f"      Link: {item['link']}\n")
                        else:
                            no_changes.append(f'<div class="status-success">✅ {c} ({t}) - No Change</div>')

                        save_snapshot(project_name, c, t, items) # Save current version of the site
                        del items
                        gc.collect()
                    else:
                        if status_code == 404:
                            errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {status_code}. Website does not exist. </div>')
                        elif status_code == 403:
                            errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {status_code}. Forbidden (bot detected).</div>')
                        else:
                            errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {status_code}. Failed to fetch, please check URL manually.</div>')

                except Exception as error:
                    errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {error}</div>')
                total_processed += 1
                progress_bar.progress(min(1.0, total_processed / (total_processed + 3)))
                gc.collect()
            buffer.clear()

     # Process leftover rows that weren't chunked
    if buffer:
        for entry in buffer:
            u, c, t = entry["url"], entry["company"], entry["url type"]


            try:
                html, source, status_code = fetch_html(u)

                if html:
                    cleaned_html = clean_html(html)
                    del html # Free memory early

                    items, error = extract_items(cleaned_html, u)
                    if error:
                        errors.append(f'<div class="status-error">🚨"⚠️ Could not extract structured content from {c} ({t}): {error}\n"</div>')
                        continue

                    previous = load_previous_snapshot(c, t)
                    new_items = detect_new_items(previous, items)
                    del previous # Free memory

                    if new_items:
                        changes.append(f'<div class="status-new">🆕 {c} ({t}) - Changed</div>')
                    else:
                        no_changes.append(f'<div class="status-success">✅ {c} ({t}) - No Change</div>')

                    save_snapshot(c, t, items)
                    del items
                    gc.collect()
                else:
                    if status_code == 404:
                        errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {status_code}. Website does not exist. </div>')
                    elif status_code == 403:
                        errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {status_code}. Forbidden (bot detected).</div>')
                    else:
                        errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {status_code}. Failed to fetch, please check URL manually.</div>')

            except Exception as error:
                errors.append(f'<div class="status-error">🚨 {c} ({t}) - Error {error}</div>')
            total_processed += 1
            progress_bar.progress(min(1.0, total_processed / (total_processed + 3)))
            gc.collect()

    push_bulk_snapshots(project_name) # Push changes to GitHub

    progress_bar.empty()

    st.session_state.changes = changes
    st.session_state.no_changes = no_changes
    st.session_state.errors = errors

    # Reset uploader but keep results
    uploaded_file = None
    st.session_state.uploader_key += 1
    st.rerun()
    

# Logout Button
if st.session_state.authenticated:
    if st.button("Logout"):
        st.session_state.authenticated = False
        st.rerun()
