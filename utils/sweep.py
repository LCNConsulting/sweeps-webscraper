# To run a sweep: fetch every URL, compare with the last snapshot, collect results
import csv
import io
import os
import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from utils.fetcher import fetch_html, validate_url
from utils.scraper import extract_items, legacy_new_items, public
from utils.storage import snapshot_key, legacy_snapshot_key, normalize_url, SNAPSHOT_VERSION

MAX_WORKERS = 8          # pages fetched in parallel
MAX_SEEN = 3000          # item ids remembered per page
FIRST_SWEEP_DAYS = 7     # someone's first sweep of a project reports items first seen in this window
REQUIRED_COLUMNS = ("company", "url", "url type")

CHANGED, NO_CHANGE, BASELINE = "Changed", "No Change", "New (baseline saved)"
MANUAL, ERROR = "Check manually", "Error"   # site can't be read automatically / something to fix


@dataclass
class Row:
    line: int            # line number in the CSV (header is line 1)
    company: str
    url_type: str
    url_raw: str
    url: str = None      # validated URL, None if invalid
    error: str = None    # validation error


@dataclass
class RowResult:
    row: Row
    status: str
    message: str = ""
    new_items: list = field(default_factory=list)
    item_count: int = 0
    warning: str = ""    # e.g. the URL now redirects somewhere else


# --- CSV parsing ---
def read_csv(data):
    """Parses the uploaded CSV bytes. Returns (rows, warnings); raises ValueError with a
    user-facing message if the file can't be used at all."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as e:
        raise ValueError(f"The file could not be read as a CSV ({e}). Save it from Excel as 'CSV UTF-8'.")
    if not records or not any(cell.strip() for cell in records[0]):
        raise ValueError("The CSV file is empty.")
    columns = [(name or "").strip().lower() for name in records[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise ValueError("Missing required column(s): " + ", ".join(f"`{c.title()}`" for c in missing)
                         + ". The first row must contain the headers Company, URL, URL Type.")
    index = {c: columns.index(c) for c in REQUIRED_COLUMNS}

    rows = []
    for line, record in enumerate(records[1:], start=2):
        cells = {c: (record[i].strip() if i < len(record) else "") for c, i in index.items()}
        if not any(cells.values()):
            continue  # blank line or trailing ",,"
        row = Row(line=line, company=cells["company"] or "(no company)",
                  url_type=cells["url type"] or "(no type)", url_raw=cells["url"])
        row.url, row.error = validate_url(row.url_raw)
        rows.append(row)

    if not rows:
        raise ValueError("The CSV file has headers but no rows.")

    warnings = [f"Line {r.line} ({r.company} – {r.url_type}): {r.error}" for r in rows if r.error]
    seen = Counter(normalize_url(r.url) for r in rows if r.url)
    warnings += [f"{url} appears {count} times; it will be checked once." for url, count in seen.items() if count > 1]
    return rows, warnings


# --- Who swept when ---
# Snapshot history is shared by everyone, but each person sees what is new since THEIR last
# sweep of the project: every item remembers when it was first seen, and each person's last
# sweep time is stored in the project's ZIP.
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _user_key(name):
    return "_user_" + hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:12] + ".json"


def last_sweep(store, name):
    """When this person last completed a sweep of the project (ISO time), or None."""
    entry = store.get(_user_key(name))
    return entry.get("last_sweep") if isinstance(entry, dict) else None


def record_sweep(store, name, when):
    store.put(_user_key(name), {"user": name.strip(), "last_sweep": when})


def first_sweep_since():
    """Cut-off used for someone's first sweep of a project (no previous sweep on record)."""
    return (datetime.now(timezone.utc) - timedelta(days=FIRST_SWEEP_DAYS)).isoformat(timespec="seconds")


# --- Sweep ---
def _first_seen(previous):
    """{item id: when it was first seen} from a snapshot; '' means before per-person tracking."""
    first_seen = dict.fromkeys(previous.get("seen") or [], "")
    for item in previous.get("items", []):
        first_seen.setdefault(item.get("id"), "")
    first_seen.update(previous.get("first_seen") or {})
    return first_seen


def _snapshot(row, items, first_seen, now):
    """Snapshot of a page: its current items, plus when every item seen so far first appeared
    (so an item that briefly drops off the page and comes back is not reported as new again)."""
    stamps = {item["id"]: first_seen.get(item["id"], now) for item in items}
    for item_id, when in first_seen.items():
        stamps.setdefault(item_id, when)
    return {
        "version": SNAPSHOT_VERSION,
        "url": row.url,
        "company": row.company,
        "url_type": row.url_type,
        "checked_at": now,
        "items": [public(item) for item in items],
        "first_seen": dict(list(stamps.items())[:MAX_SEEN]),
    }


def _last_segment(path):
    segments = [s for s in path.lower().split("/") if s]
    return os.path.splitext(segments[-1])[0] if segments else ""


def redirect_warning(requested, final):
    """A warning when the URL now lands on a different site, the homepage or another page
    (e.g. a retired page redirecting elsewhere), so the CSV can be updated."""
    if not final:
        return ""
    a, b = urlsplit(requested), urlsplit(final)
    host_a = (a.hostname or "").removeprefix("www.")
    host_b = (b.hostname or "").removeprefix("www.")
    moved = (host_a != host_b
             or (_last_segment(b.path) in ("", "index", "default", "home") and _last_segment(a.path) not in ("", "index", "default", "home"))
             or _last_segment(a.path) != _last_segment(b.path))
    if not moved:
        return ""
    return f"This URL now redirects to {final} — the page may have moved. Consider updating the CSV."


def run_sweep(rows, store, on_progress=None, since=None, now=None, max_workers=MAX_WORKERS):
    """Checks every row and returns a list of RowResult (in CSV order).

    An item counts as new if this sweep is the first to see it, or if an earlier sweep (by
    anyone) first saw it after `since` — the ISO time of this person's previous sweep.
    Pages are fetched in parallel threads; parsing and comparison happen here, one page at
    a time, to keep memory low. Snapshots are updated in `store` but NOT saved — the caller
    saves once the results have been shown to the user. `on_progress(done, total, row)` is
    called from this (the calling) thread, so it may update Streamlit elements."""
    now = now or now_iso()
    results = {}
    by_url = {}
    for row in rows:
        if row.error:
            results[row.line] = RowResult(row, ERROR, row.error)
        else:
            by_url.setdefault(normalize_url(row.url), []).append(row)

    # Old snapshots were keyed by Company + URL Type; only trust one if no other row shares that key
    legacy_counts = Counter(legacy_snapshot_key(r.company, r.url_type) for r in rows)

    total, done = len(rows), len(results)
    if on_progress:
        on_progress(done, total, None)

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {executor.submit(fetch_html, group[0].url): url for url, group in by_url.items()}
        for future in as_completed(futures):
            group = by_url[futures[future]]
            try:
                outcome = _process(future.result(), group, store, legacy_counts, since, now)
            except Exception as e:  # never let one bad page stop the sweep
                outcome = {r.line: RowResult(r, ERROR, f"Unexpected error: {e}") for r in group}
            results.update(outcome)
            done += len(group)
            if on_progress:
                on_progress(done, total, group[0])
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    # Drop old-format snapshots that several rows shared, once each of those rows has its own
    for legacy_key, count in legacy_counts.items():
        sharing = [r for r in rows if legacy_snapshot_key(r.company, r.url_type) == legacy_key]
        if count > 1 and store.get(legacy_key) is not None and all(
                r.url and store.get(snapshot_key(r.url)) is not None for r in sharing):
            store.delete(legacy_key)

    return [results[r.line] for r in rows]


def _process(fetched, group, store, legacy_counts, since, now):
    first = group[0]
    warning = redirect_warning(first.url, fetched.final_url)

    def result(row, status, message="", new_items=(), item_count=0):
        return RowResult(row, status, message, list(new_items), item_count, warning)

    if fetched.content is None:
        status = MANUAL if fetched.manual else ERROR
        return {r.line: result(r, status, fetched.error or "Failed to fetch the page.") for r in group}

    items, error = extract_items(fetched.content, fetched.final_url or first.url, legacy_base=first.url)
    if error:
        return {r.line: result(r, MANUAL, error) for r in group}

    key = snapshot_key(first.url)
    previous = store.get(key)
    legacy_key = None
    if previous is None:
        for r in group:
            candidate = legacy_snapshot_key(r.company, r.url_type)
            if legacy_counts[candidate] == 1 and store.get(candidate) is not None:
                previous, legacy_key = store.get(candidate), candidate
                break

    if previous is None:
        store.put(key, _snapshot(first, items, {i["id"]: "" for i in items}, now))
        return {r.line: result(r, BASELINE, "First check of this page — saved as the baseline for future "
                               "comparisons.", item_count=len(items)) for r in group}

    if legacy_key:
        new_items = legacy_new_items(previous, items, fetched.content, first.url)
        new_ids = {i["id"] for i in new_items}
        note = f"{len(new_items)} new item(s) (compared with the previous tool version's snapshot)."
        store.put(key, _snapshot(first, items, {i["id"]: "" for i in items if i["id"] not in new_ids}, now))
        store.delete(legacy_key)  # migrated to the new per-URL snapshot
    else:
        first_seen = _first_seen(previous)
        brand_new = [i for i in items if i["id"] not in first_seen]
        # ...plus items another person's sweep found after this person's last sweep
        new_items = [i for i in items if i["id"] not in first_seen or (since and first_seen[i["id"]] > since)]
        note = f"{len(new_items)} new item(s)."
        if len(new_items) > len(brand_new):
            note = (f"{len(new_items)} new item(s) ({len(new_items) - len(brand_new)} already found by "
                    "someone else's sweep since your last one).")
        if len(brand_new) > 0.8 * len(items) and previous.get("items"):
            note = "Most of the page is different — possibly a redesign or an alternate page. Check manually."
        # Only rewrite the snapshot when the page's items changed, so a sweep where nothing
        # changed doesn't rewrite it
        if brand_new or {i["id"] for i in items} != {i.get("id") for i in previous.get("items", [])}:
            store.put(key, _snapshot(first, items, first_seen, now))

    if new_items:
        public_items = [public(i) for i in new_items]
        return {r.line: result(r, CHANGED, note, public_items, len(items)) for r in group}
    return {r.line: result(r, NO_CHANGE, item_count=len(items)) for r in group}


def results_csv(results):
    """Results as CSV text for download."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Company", "URL Type", "URL", "Status", "Details", "Warning", "New items"])
    for res in results:
        items = "\n".join(f"{i['title']} — {i['link']}" for i in res.new_items[:25])
        writer.writerow([res.row.company, res.row.url_type, res.row.url or res.row.url_raw,
                         res.status, res.message, res.warning, items])
    return out.getvalue()
