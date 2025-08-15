# To store information about a site
import os
import json
import hashlib
import base64
import requests
import zipfile
import streamlit as st
from io import BytesIO
from dotenv import load_dotenv
load_dotenv()

# Paths & Config
SNAPSHOT_DIR = os.path.join('data', 'snapshots')
ZIP_FILENAME = "snapshots.zip"
ZIP_PATH_LOCAL = os.path.join(SNAPSHOT_DIR, ZIP_FILENAME)

if "UPDATED_FILES" not in st.session_state:
    st.session_state.UPDATED_FILES = set()

try:
    from streamlit.runtime.secrets import secrets
    GITHUB_OWNER = secrets["GITHUB_OWNER"]
    GITHUB_REPO = secrets["GITHUB_REPO"]
    GITHUB_BRANCH = secrets["GITHUB_BRANCH"]
    GITHUB_TOKEN = secrets["GITHUB_TOKEN"]
except:
    GITHUB_OWNER = os.getenv("GITHUB_OWNER")
    GITHUB_REPO = os.getenv("GITHUB_REPO")
    GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not GITHUB_TOKEN:
    raise RuntimeError("❌ No GitHub token found! Set GITHUB_TOKEN in Streamlit secrets or environment variables.")

# --- Helpers ---
def get_zip_path(project_name):
    """Get local path for project's snapshot ZIP."""
    return os.path.join(SNAPSHOT_DIR, f"snapshots_{project_name}.zip")

def get_snapshot_key(company_name, url_type):
    """Generates a consistent JSON filename inside the ZIP."""
    return f"{company_name}_{url_type}".replace(" ", "_") + ".json"

def _load_zip_from_github(project_name):
    """Fetches snapshots.zip from GitHub."""
    try:
        zip_filename = f"snapshots_{project_name}.zip"
        zip_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/data/snapshots/{zip_filename}"
        r = requests.get(zip_url)
        if r.status_code == 200:
            return zipfile.ZipFile(BytesIO(r.content))
    except Exception as e:
        print(f"⚠️ Could not load ZIP from GitHub: {e}")
    return None

def _load_local_zip(project_name):
    """Loads local snapshots.zip if it exists."""
    path = get_zip_path(project_name)
    if os.path.exists(path):
        return zipfile.ZipFile(path, "r")
    return None

# --- Snapshot Loading ---
def load_previous_snapshot(project_name, company_name, url_type):
    key = get_snapshot_key(company_name, url_type)

    # 1. Try local zip
    local_zip = _load_local_zip(project_name)
    if local_zip and key in local_zip.namelist():
        with local_zip.open(key) as f:
            return json.load(f)

    # 2. Try GitHub zip
    gh_zip = _load_zip_from_github(project_name)
    if gh_zip and key in gh_zip.namelist():
        with gh_zip.open(key) as f:
            return json.load(f)

    return []

# --- Snapshot Saving ---
def save_snapshot(project_name, company_name, url_type, data):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    zip_path = get_zip_path(project_name)

    # Ensure local zip exists (create if missing)
    if not os.path.exists(zip_path):
        with zipfile.ZipFile(zip_path, "w") as _:
            pass

    # Create a temp buffer to rewrite ZIP with updated JSON
    buffer = BytesIO()
    key = get_snapshot_key(company_name, url_type)

    with zipfile.ZipFile(zip_path, "r") as old_zip, \
         zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as new_zip:
        # Copy over everything except the updated file
        for item in old_zip.namelist():
            if item != key:
                new_zip.writestr(item, old_zip.read(item))
        # Write the updated JSON
        new_zip.writestr(key, json.dumps(data, indent=2))

    # Save updated ZIP locally
    with open(zip_path, "wb") as f:
        f.write(buffer.getvalue())

    UPDATED_FILES.add((project_name, key))

# --- Hashing & Change Detection ---
def hash_item(item):
    data = f"{item.get('title','')}|{item.get('timestamp','')}|{item.get('link','')}"
    return hashlib.md5(data.encode('utf-8')).hexdigest()

def detect_new_items(previous, current):
    prev_hashes = set(hash_item(item) for item in previous)
    return [item for item in current if hash_item(item) not in prev_hashes]

# --- Push ZIP if changed ---
def push_bulk_snapshots(project_name):
    project_updates = [key for proj, key in UPDATED_FILES if proj == project_name]
    if not project_updates:
        print("No changes detected — skipping push.")
        return

    # Encode new ZIP
    zip_path = get_zip_path(project_name)
    with open(zip_path, "rb") as f:
        new_zip_data = f.read()
    encoded_content = base64.b64encode(new_zip_data).decode("utf-8")

    # Check remote SHA
    zip_filename = f"snapshots_{project_name}.zip"
    github_zip_path = f"data/snapshots/{zip_filename}"
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{github_zip_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    response = requests.get(api_url, headers=headers)
    sha = response.json().get("sha") if response.status_code == 200 else None

    # Push if contents differ
    print("Token loaded?", bool(GITHUB_TOKEN), "Length:", len(GITHUB_TOKEN) if GITHUB_TOKEN else None)
    push = True
    if sha:
        remote_content = requests.get(f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{github_zip_path}")
        if remote_content.status_code == 200 and remote_content.content == new_zip_data:
            print("✅ Remote ZIP matches local — no push needed.")
            push = False

    if push:
        commit_data = {
            "message": f"Bulk snapshot update ({project_name})",
            "content": encoded_content,
            "branch": GITHUB_BRANCH
        }
        if sha:
            commit_data["sha"] = sha
        
        print("Local zip exists?", os.path.exists(get_zip_path(project_name)))
        print("Updated files:", UPDATED_FILES)

        put_response = requests.put(api_url, headers=headers, json=commit_data)
        print("GitHub API status:", put_response.status_code, put_response.text)
        if put_response.status_code not in [200, 201]:
            print(f"❌ Failed to push ZIP for {project_name}: {put_response.text}")
        else:
            print("✅ Bulk snapshots pushed to GitHub.")
