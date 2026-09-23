# To store information about a site
import os
import re
import json
import base64
import hashlib
import zipfile
import threading
from io import BytesIO
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# Paths & Config
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_DIR = os.path.join(REPO_ROOT, "data", "snapshots")
GITHUB_SNAPSHOT_DIR = "data/snapshots"
SNAPSHOT_VERSION = 2
GITHUB_TIMEOUT = 30  # seconds per GitHub API request

# One lock per project, so two sessions saving the same project don't interleave
_LOCKS = {}


def _setting(name):
    """Read a setting from Streamlit secrets, falling back to environment variables."""
    try:
        value = st.secrets[name]
    except Exception:
        value = None
    return value or os.getenv(name)


GITHUB_OWNER = _setting("GITHUB_OWNER")
GITHUB_REPO = _setting("GITHUB_REPO")
GITHUB_BRANCH = _setting("GITHUB_BRANCH") or "main"
GITHUB_TOKEN = _setting("GITHUB_TOKEN")


def github_configured():
    return bool(GITHUB_OWNER and GITHUB_REPO and GITHUB_TOKEN)


# --- Naming helpers ---
def clean_project_name(filename):
    """Project name from an uploaded filename: drop the extension and browser duplicate
    suffixes like ' (1)' so re-downloaded copies share the same snapshot history."""
    stem = os.path.splitext(os.path.basename(filename))[0].strip()
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r'[\\/:*?"<>|#%]', "-", stem)
    return stem or "project"


def normalize_url(url):
    """Canonical form of a URL for identity: trimmed, lower-case scheme/host, no fragment."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def snapshot_key(url):
    """ZIP entry name for a URL. Keyed by URL so two rows that share Company + URL Type
    (e.g. three 'Ionis / PR' pages) no longer overwrite each other's snapshot."""
    return "url_" + hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16] + ".json"


def legacy_snapshot_key(company_name, url_type):
    """ZIP entry name used by the previous version of the tool (Company_URL_Type.json)."""
    return f"{company_name}_{url_type}".replace(" ", "_") + ".json"


# --- GitHub helpers ---
def _api_url(repo_path):
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{quote(repo_path)}"


def _headers(accept="application/vnd.github+json"):
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_get_zip(repo_path):
    """Returns (zip bytes, sha) from GitHub, or (None, None) if the file does not exist yet.
    Uses the authenticated contents API, which works for private repos and is not CDN-cached."""
    meta = requests.get(_api_url(repo_path), headers=_headers("application/vnd.github.object"),
                        params={"ref": GITHUB_BRANCH}, timeout=GITHUB_TIMEOUT)
    if meta.status_code == 404:
        return None, None
    meta.raise_for_status()
    sha = meta.json().get("sha")
    raw = requests.get(_api_url(repo_path), headers=_headers("application/vnd.github.raw+json"),
                       params={"ref": GITHUB_BRANCH}, timeout=GITHUB_TIMEOUT)
    raw.raise_for_status()
    return raw.content, sha


def _read_zip(data):
    """Returns {entry name: (bytes, date_time tuple)} for a ZIP given as bytes."""
    entries = {}
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for info in zf.infolist():
            entries[info.filename] = (zf.read(info), info.date_time)
    return entries


def _merge(primary, secondary):
    """Merge two entry dicts, keeping whichever copy of each entry was written most recently."""
    merged = dict(secondary)
    for key, value in primary.items():
        if key not in merged or value[1] >= merged[key][1]:
            merged[key] = value
    return merged


class SnapshotStore:
    """All snapshots for one project, held in memory for the duration of a sweep.

    The ZIP is read once at the start (GitHub and the local copy are merged, newest entry
    wins) and written/pushed once at the end, instead of once per row."""

    def __init__(self, project_name):
        self.project_name = project_name
        self.zip_name = f"snapshots_{project_name}.zip"
        self.local_path = os.path.join(SNAPSHOT_DIR, self.zip_name)
        self.repo_path = f"{GITHUB_SNAPSHOT_DIR}/{self.zip_name}"
        self.entries = {}
        self.updated = {}
        self.deleted = set()
        self.warnings = []

    # --- Loading ---
    def load(self):
        remote, local = {}, {}
        if github_configured():
            try:
                data, _ = _github_get_zip(self.repo_path)
                if data:
                    remote = _read_zip(data)
            except Exception as e:
                self.warnings.append(f"Could not load snapshot history from GitHub ({e}); using the local copy.")
        if os.path.exists(self.local_path):
            try:
                with open(self.local_path, "rb") as f:
                    local = _read_zip(f.read())
            except Exception as e:
                self.warnings.append(f"Local snapshot file is unreadable ({e}); ignoring it.")
        self.entries = _merge(remote, local)
        return self

    @property
    def has_history(self):
        return bool(self.entries)

    def get(self, key):
        """Returns the parsed snapshot stored under key, or None."""
        entry = self.entries.get(key)
        if entry is None:
            return None
        try:
            return json.loads(entry[0].decode("utf-8"))
        except Exception:
            return None

    # --- Saving ---
    def put(self, key, snapshot):
        stamp = datetime.now(timezone.utc).timetuple()[:6]
        value = (json.dumps(snapshot, indent=1, ensure_ascii=False).encode("utf-8"), stamp)
        self.entries[key] = value
        self.updated[key] = value
        self.deleted.discard(key)

    def delete(self, key):
        """Removes an entry (used to drop old-format snapshots once they have been migrated)."""
        self.entries.pop(key, None)
        self.updated.pop(key, None)
        self.deleted.add(key)

    def _zip_bytes(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for key in sorted(self.entries):
                data, stamp = self.entries[key]
                zf.writestr(zipfile.ZipInfo(key, date_time=stamp), data, zipfile.ZIP_DEFLATED)
        return buffer.getvalue()

    def _write_local(self, content):
        """Atomic write (temp file + rename), so a crash never leaves a half-written ZIP."""
        try:
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            tmp_path = self.local_path + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(content)
            os.replace(tmp_path, self.local_path)
            return True
        except OSError as e:
            self.warnings.append(f"Could not write the local snapshot copy ({e}).")
            return False

    def save(self):
        """Writes the ZIP locally and pushes it to GitHub. Returns (ok, message); never raises."""
        if not self.updated and not self.deleted:
            return True, "Nothing changed, so no snapshots needed saving."
        with _LOCKS.setdefault(self.project_name, threading.Lock()):
            return self._save()

    def _absorb(self, other):
        """Merge in entries saved by another run since we loaded (newest wins); the entries
        this run updated or deleted always win."""
        self.entries = _merge(other, self.entries)
        self.entries.update(self.updated)
        for key in self.deleted:
            self.entries.pop(key, None)

    def _save(self):
        if os.path.exists(self.local_path):
            try:
                with open(self.local_path, "rb") as f:
                    self._absorb(_read_zip(f.read()))
            except Exception:
                pass  # unreadable local copy: it gets overwritten below
        local_ok = self._write_local(self._zip_bytes())
        if not github_configured():
            if local_ok:
                return False, "Snapshots saved on this server only — GitHub is not configured."
            return False, "Snapshots could not be saved (GitHub is not configured and the local copy failed)."

        last_error = ""
        for _ in range(3):
            try:
                remote_data, sha = _github_get_zip(self.repo_path)
                if remote_data:
                    self._absorb(_read_zip(remote_data))
                content = self._zip_bytes()
                if remote_data == content:
                    return True, "Snapshot history is already up to date on GitHub."
                body = {
                    "message": f"Bulk snapshot update ({self.project_name})",
                    "content": base64.b64encode(content).decode("ascii"),
                    "branch": GITHUB_BRANCH,
                }
                if sha:
                    body["sha"] = sha
                resp = requests.put(_api_url(self.repo_path), headers=_headers(), json=body, timeout=GITHUB_TIMEOUT)
                if resp.status_code in (200, 201):
                    self._write_local(content)
                    return True, f"Saved {len(self.updated)} snapshot(s) to GitHub."
                last_error = f"GitHub API returned {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in (409, 422):  # only a stale sha is worth retrying
                    break
            except Exception as e:
                last_error = str(e)
        return False, f"Could not push snapshots to GitHub ({last_error}). They are saved on this server for now."
