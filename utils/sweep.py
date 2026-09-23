# To run a sweep: fetch every URL, compare with the last snapshot, collect results
import csv
import io
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from utils.fetcher import fetch_html, validate_url
from utils.scraper import extract_items, new_items_since, legacy_new_items, public
from utils.storage import snapshot_key, legacy_snapshot_key, normalize_url, SNAPSHOT_VERSION

MAX_WORKERS = 8          # pages fetched in parallel
MAX_SEEN = 3000          # item ids remembered per page
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


# --- Sweep ---
def _snapshot(row, items, previous=None):
    """Snapshot of a page: its current items, plus every item id seen on earlier checks (so an
    item that briefly drops off the page and comes back is not reported as new again)."""
    seen = [item["id"] for item in items]
    if isinstance(previous, dict):
        seen += previous.get("seen") or [item.get("id") for item in previous.get("items", [])]
    return {
        "version": SNAPSHOT_VERSION,
        "url": row.url,
        "company": row.company,
        "url_type": row.url_type,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": [public(item) for item in items],
        "seen": list(dict.fromkeys(seen))[:MAX_SEEN],
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


def run_sweep(rows, store, on_progress=None, max_workers=MAX_WORKERS):
    """Checks every row and returns a list of RowResult (in CSV order).

    Pages are fetched in parallel threads; parsing and comparison happen here, one page at
    a time, to keep memory low. Snapshots are updated in `store` but NOT saved — the caller
    saves once the results have been shown to the user. `on_progress(done, total, row)` is
    called from this (the calling) thread, so it may update Streamlit elements."""
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
                outcome = _process(future.result(), group, store, legacy_counts)
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


def _process(fetched, group, store, legacy_counts):
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
        store.put(key, _snapshot(first, items))
        return {r.line: result(r, BASELINE, "First check of this page — saved as the baseline for future "
                               "comparisons.", item_count=len(items)) for r in group}

    if legacy_key:
        new_items = legacy_new_items(previous, items, fetched.content, first.url)
        note = f"{len(new_items)} new item(s) (compared with the previous tool version's snapshot)."
        store.put(key, _snapshot(first, items))
        store.delete(legacy_key)  # migrated to the new per-URL snapshot
    else:
        new_items = new_items_since(previous, items)
        note = f"{len(new_items)} new item(s)."
        if len(new_items) > 0.8 * len(items) and previous.get("items"):
            note = "Most of the page is different — possibly a redesign or an alternate page. Check manually."
        # Only rewrite the snapshot when the page's items changed, so a sweep where nothing
        # changed doesn't create a GitHub commit
        if {i["id"] for i in items} != {i.get("id") for i in previous.get("items", [])}:
            store.put(key, _snapshot(first, items, previous))

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
