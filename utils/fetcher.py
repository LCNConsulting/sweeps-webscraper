# To access a site
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi import requests

TIMEOUT = (8, 17)            # seconds: connect, then the rest (25 s total per attempt)
MAX_BYTES = 10_000_000       # ignore anything past 10 MB
# Browser fingerprints to try in order. The aliases always map to the newest browser version
# the installed curl_cffi supports — outdated fingerprints (e.g. the old hard-coded
# "chrome120") are rejected outright by the bot protection on many investor-relations sites.
IMPERSONATE = ["chrome", "safari", "firefox"]
# Don't override User-Agent: it must match the impersonated browser or sites reject the request
HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
RETRY_STATUS = {403, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526}

# <title> of bot-protection / challenge pages that are served with status 200
BLOCK_TITLES = re.compile(
    r"just a moment|attention required|access denied|pardon our interruption|request rejected|"
    r"security check|are you a (?:human|robot)|bot verification|ddos-guard|checking your browser|"
    r"you are being redirected", re.I)
TITLE_PATTERN = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


@dataclass
class FetchResult:
    url: str
    content: bytes = None    # raw HTML (bytes, so the parser can detect the page's encoding)
    final_url: str = None
    status: int = None
    error: str = None
    manual: bool = False     # the site blocks automated access: a person has to check it
    elapsed: float = 0.0


def validate_url(raw):
    """Returns (url, error). Adds https:// when the scheme is missing; rejects cells that
    are clearly not links (e.g. a page title pasted instead of the hyperlink)."""
    url = (raw or "").strip()
    if not url:
        return None, "The URL cell is empty."
    if re.search(r"\s", url) or "." not in url:
        return None, (f'"{url}" is not a web address (it looks like a page title). '
                      "Replace it in the CSV with the page's full link, e.g. https://...")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    host = urlsplit(url).hostname or ""
    if "." not in host:
        return None, f'"{raw}" is not a valid web address.'
    return url, None


def _blocked(content):
    match = TITLE_PATTERN.search(content[:20000])
    if match and BLOCK_TITLES.search(match.group(1).decode("utf-8", "ignore")):
        return True
    return b"_cf_chl_opt" in content[:50000] or b"challenge-platform/h/" in content[:50000]


def _describe(status):
    if status == 404 or status == 410:
        return f"Error {status}: page not found. The page may have moved; please update the URL."
    if status in (401, 403):
        return f"Error {status}: access forbidden — the site blocks automated access, so it can't be checked automatically."
    if status == 429:
        return "Error 429: the site is rate-limiting requests. Try again later or check it manually."
    if status and status >= 500:
        return f"Error {status}: the site had a server error. Try again later."
    return f"Error {status}: unexpected response. Please check the URL manually."


def _describe_exception(e):
    """Returns (message, worth_retrying) for a request exception."""
    message = str(e)
    lower = message.lower()
    if "timed out" in lower or "timeout" in lower:
        # Often intermittent: a fresh connection with the next fingerprint usually works
        return f"Timed out after {sum(TIMEOUT)} seconds — the site is too slow or not responding.", True
    if "resolve" in lower or "bad hostname" in lower:
        return "Could not find this website (DNS lookup failed). Check the URL for typos.", False
    if "certificate" in lower:
        return "The site's security certificate could not be verified. Check it manually.", False
    return f"Could not connect ({message.split('. See ')[0][:160]}).", True


def fetch_html(url):
    """Fetches a page. Never raises; returns a FetchResult with either content or an error."""
    start = time.time()
    result = FetchResult(url=url)
    for browser in IMPERSONATE:
        result.manual = False
        try:
            response = requests.get(url, impersonate=browser, headers=HEADERS, timeout=TIMEOUT,
                                    allow_redirects=True, max_redirects=10)
        except Exception as e:
            result.error, retry = _describe_exception(e)
            if retry:
                continue
            break

        result.status = response.status_code
        result.final_url = str(response.url)
        content = response.content[:MAX_BYTES]
        content_type = (response.headers.get("content-type") or "").lower()

        if _blocked(content):
            result.error = ("Blocked by the site's bot protection (it served a challenge page instead of "
                            "the content), so it can't be checked automatically.")
            result.manual = True
            continue

        if response.status_code == 200:
            if content_type and not any(t in content_type for t in ("html", "xml", "text/plain")):
                result.error = (f"This link points to a file ({content_type.split(';')[0]}), "
                                "not a web page. Use the page that lists these files instead.")
                break
            result.content, result.error = content, None
            break

        result.error = _describe(response.status_code)
        result.manual = response.status_code in (401, 403)
        if response.status_code not in RETRY_STATUS:
            break  # e.g. 404: another browser fingerprint won't help

    result.elapsed = round(time.time() - start, 2)
    return result
