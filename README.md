# lcn-sweeps

Project for Rutgers MBS Externship

Streamlit app that checks competitor websites (press releases, events, presentations, SEC
filings…) for new items since the last sweep.

## Using it

1. Log in and upload a CSV with the columns `Company`, `URL`, `URL Type` (save from Excel as
   **CSV UTF-8**). Each URL must be the full link to the page (`https://...`).
2. Check the project name — snapshot history is kept per project, so use the same name every
   time for the same sweep — and click **Run sweep**.
3. Each row is reported as:
   - **Changed** – new items appeared since the last sweep (expand the row to see them)
   - **No Change** – nothing new
   - **New (baseline saved)** – first time this page is checked
   - **Check manually** – the site blocks automated access or loads its listing with
     JavaScript, so a person has to look at it
   - **Error** – bad URL, page gone, site down… (the reason is shown)

   Rows whose URL now redirects to a different page get a warning so the CSV can be updated.
   Results can be downloaded as a CSV.

Snapshots are only saved once a sweep has finished, so an interrupted sweep never hides a change.

## Configuration

Set these in the Streamlit secrets (or a local `.env` file):

| Setting | Purpose |
|---|---|
| `APP_PASSWORD` | Login password |
| `GITHUB_TOKEN` | Token with write access to the repo's contents (stores snapshot history) |
| `GITHUB_OWNER`, `GITHUB_REPO` | Repository that stores `data/snapshots/` |
| `GITHUB_BRANCH` | Branch for the snapshots (default `main`) |

Without the GitHub settings the app still works, but snapshot history is only kept on the
server and is lost when it restarts.

## Running locally

```
pip install -r requirements.txt
streamlit run app.py
```

## Code

- `app.py` – Streamlit UI
- `utils/sweep.py` – CSV parsing and the sweep itself (parallel fetching, comparison)
- `utils/fetcher.py` – downloads pages (browser impersonation via `curl_cffi`)
- `utils/scraper.py` – extracts items (links and listing rows) from a page's main content
- `utils/storage.py` – snapshot ZIPs, synced to GitHub

`main.py` and `templates/` are the original Flask prototype and are not used by the Streamlit app.
