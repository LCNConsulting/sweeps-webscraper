# To scrape the contents of a site
import re
import hashlib
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup, UnicodeDammit

try:
    import lxml  # noqa: F401  (much faster than html.parser on large pages)
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

MAX_ITEMS = 2000

# Page furniture that is never part of a news/events listing
REMOVE_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "canvas", "object",
               "select", "option", "button", "input", "textarea"]
REMOVE_ROLES = ["navigation", "banner", "contentinfo", "search", "dialog", "alertdialog", "menu", "menubar"]
CONSENT_PATTERN = re.compile(r"cookie|consent|onetrust|gdpr|truste|cc-window|cc-banner|usercentrics", re.I)
SITE_CHROME_PATTERN = re.compile(r"(?:^|[\s_-])(?:header|footer|masthead)(?:$|[\s_-])", re.I)

# Link texts that say nothing about the item itself; use the surrounding text instead
GENERIC_LINK_TEXT = {
    "read more", "learn more", "more", "view", "view more", "view all", "see more", "see all", "details",
    "more details", "download", "pdf", "webcast", "click here", "here", "watch", "listen", "register",
    "continue reading", "read full release", "full release", "presentation", "slides", "audio", "video",
    "open", "link", "go", "info", "more info", "read", "read article", "read the release", "archive",
    "arrow", "pdf version", "view html", "add to calendar", "add to outlook", "add to google calendar",
    "»", "›", "→", ">", "+", "下载", "查看", "更多", "详情", "了解更多", "查看详情", "阅读更多",
}
# Short call-to-action phrases ("Listen to webcast", "Read the full article here") and bare file names
GENERIC_PATTERN = re.compile(
    r"^(?:read|click|listen|register|view|download|watch|add to|continue|see|learn|go to|visit|access|"
    r"open|replay|more)\b.{0,35}$", re.I)
FILE_NAME = re.compile(r"^[\w.()-]+\.(?:pdf|rtf|xlsx?|docx?|pptx?|zip|txt|htm|html)$", re.I)

# Query parameters that change on every page load without the content changing
VOLATILE_PARAMS = {
    "gclid", "fbclid", "mc_cid", "mc_eid", "_", "t", "ts", "timestamp", "cb", "cachebuster", "nocache",
    "rand", "random", "nonce", "token", "csrf", "_token", "sid", "sessionid", "session", "session_id",
    "jsessionid", "phpsessid", "_ga", "_gl", "__hstc", "__hssc", "__hsfp", "hsctatracking",
}
# Share buttons and obfuscated e-mail links: their URLs embed the page itself or change per load
SKIP_LINKS = re.compile(
    r"/cdn-cgi/l/email-protection|sharer\.php|share\.php|sharearticle|/intent/(?:tweet|post)|"
    r"addtoany\.com|pinterest\.com/pin/create|wa\.me/\?text=", re.I)

RELATIVE_TIME = re.compile(
    r"\b\d+\s+(?:second|sec|minute|min|hour|hr|day|week|month|year)s?\s+ago\b|\b(?:just now|yesterday|today)\b", re.I)
MARKET_DATA = re.compile(
    r"\b(?:nasdaq|nyse|otcqx|tsx|asx|hkex|stock price|share price|last trade|last price|volume|market cap|"
    r"delayed|day'?s range|52[- ]week|(?:data|prices?|quotes?) as of)\b", re.I)

MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_PATTERN = re.compile(
    rf"\b{MONTHS}\s+\d{{1,2}},?\s+\d{{4}}\b"          # September 22, 2026 / Sep 22 2026
    rf"|\b\d{{1,2}}\s+{MONTHS},?\s+\d{{4}}\b"         # 22 September 2026
    r"|\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"              # 2026-09-22 / 2026/09/22 / 2026.09.22
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"                      # 09/22/2026
    r"|\d{4}年\d{1,2}月\d{1,2}日", re.I)                # 2026年9月22日


def _text(tag):
    return " ".join(tag.get_text(" ", strip=True).split())


def _hash(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def normalize_link(url):
    """Absolute link without fragment, tracking/cache-busting parameters or session ids."""
    parts = urlsplit(url)
    path = re.sub(r";jsessionid=[^/?#]*", "", parts.path, flags=re.I)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in VOLATILE_PARAMS and not k.lower().startswith(("utm_", "utmzb_"))]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _find_date(text):
    match = DATE_PATTERN.search(text)
    return match.group(0) if match else ""


def _is_generic(text):
    return (len(text) < 4 or text.lower().strip(" :.-|") in GENERIC_LINK_TEXT
            or bool(GENERIC_PATTERN.match(text) or FILE_NAME.match(text)))


def _context(tag, own_text):
    """Text of the nearest enclosing block that says more than the link itself (e.g. the
    list item or card around a 'Read more' link)."""
    node = tag.parent
    for _ in range(5):
        if node is None or node.name in ("body", "main", "html", "[document]"):
            break
        text = _text(node)
        if len(text) > len(own_text) + 3:
            return text if len(text) <= 400 else ""
        node = node.parent
    return ""


# --- Page clean-up ---
def _remove(tags, keep=lambda tag: False):
    for tag in tags:
        if not tag.decomposed and not keep(tag):  # skip children of already-removed elements
            tag.decompose()


def _ident(tag):
    return " ".join([tag.get("id") or ""] + (tag.get("class") or []))


def _is_site_chrome(tag):
    """True for the site-wide header/footer. Some site builders (e.g. Webflow) also use
    <header> for ordinary content sections, so only drop ones that look like page chrome."""
    if tag.find_parent(["main", "article", "li", "td", "dd"]) or tag.find_parent(attrs={"role": "main"}):
        return False
    if _wraps_content(tag):
        return False
    return bool(tag.find("nav") or SITE_CHROME_PATTERN.search(_ident(tag)))


def _wraps_content(tag):
    """True if the element contains the page's main content (seen on sites whose broken HTML
    leaves the menu <nav> unclosed, so it swallows the whole page)."""
    return bool(tag.find("main") or tag.find(attrs={"role": "main"}))


def _strip_page_furniture(soup):
    _remove(soup.find_all(REMOVE_TAGS))
    site_chrome = [tag for tag in soup.find_all(["header", "footer"]) if _is_site_chrome(tag)]  # before menus go
    _remove(soup.find_all(["nav", "dialog"]), keep=_wraps_content)
    _remove(soup.find_all(attrs={"role": REMOVE_ROLES}), keep=_wraps_content)
    _remove(soup.find_all(attrs={"aria-hidden": "true"}), keep=_wraps_content)
    _remove(site_chrome)
    _remove(soup.find_all(["div", "section", "aside", "p"]),
            keep=lambda tag: not CONSENT_PATTERN.search(_ident(tag)) or _wraps_content(tag))


# --- Extraction ---
def _page_key(url):
    parts = urlsplit(url)
    return parts.netloc.lower(), parts.path.rstrip("/")


def extract_items(html_content, base_url, legacy_base=None):
    """Returns (items, error). Each item is {id, title, link, date}; `id` identifies the item
    across runs (the link, or the text for listing rows without a link). Only the page's main
    content area is read, so menus, footers and cookie banners don't trigger false changes.
    `base_url` is the page's final URL (after redirects); `legacy_base` the URL as written in
    the CSV, which the previous version of the tool resolved links against."""
    soup = BeautifulSoup(html_content, PARSER)

    base_tag = soup.find("base", href=True)
    page_url = urljoin(base_url, base_tag["href"]) if base_tag else base_url
    page_link = normalize_link(base_url)

    _strip_page_furniture(soup)
    root = soup.find("main") or soup.find(attrs={"role": "main"})
    if root is None or (len(_text(root)) < 200 and len(root.find_all("a", href=True)) < 3):
        # No usable main region: read the whole page, but without any header/footer blocks
        _remove(soup.find_all(["header", "footer"]),
                keep=lambda tag: tag.find_parent(["article", "li", "td", "dd"]) or _wraps_content(tag))
        root = soup.body or soup

    items = {}  # id -> item, in page order

    def add(kind, item_id, title, quality, link, date, legacy=None):
        title = title[:200]
        item = items.get(item_id)
        if item is None:
            items[item_id] = {"id": item_id, "title": title, "link": link, "date": date,
                              "_kind": kind, "_quality": quality, "_legacy_link": legacy}
        else:  # same link seen again (e.g. headline + 'Read more'): keep the most descriptive title
            if quality > item["_quality"]:
                item["title"], item["_quality"] = title, quality
            item["date"] = item["date"] or date

    # 1. Linked items (press releases, event pages, PDFs, ...)
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")) or SKIP_LINKS.search(href):
            continue
        text = _text(a) or (a.get("title") or a.get("aria-label") or "").strip()
        if not text:
            img = a.find("img", alt=True)
            text = img["alt"].strip() if img else ""
        context = _context(a, text)
        quality = 2
        if _is_generic(text):
            text, quality = (context, 1) if context else (text, 0)
        if not text:
            continue
        link = normalize_link(urljoin(page_url, href))
        add("link", _hash("link", link), text, quality, link, _find_date(context or text),
            legacy_link(href, legacy_base or base_url))

    # 2. Text without links: listing rows (e.g. event calendars that only show text) and
    #    paragraphs (e.g. a pipeline page where a programme moves to the next phase)
    for tag in root.find_all(["h1", "h2", "h3", "h4", "h5", "li", "tr", "dt", "p"]):
        if tag.find("a", href=True) or tag.find(["li", "tr"]):
            continue
        if tag.name == "p" and tag.find_parent(["li", "tr", "dt", "h1", "h2", "h3", "h4", "h5"]):
            continue  # already part of the enclosing row
        text = _text(tag)
        if not (30 if tag.name == "p" else 8) <= len(text) <= 400 or MARKET_DATA.search(text):
            continue
        if sum(ch.isdigit() or ch in "$%€£¥+-." for ch in text) > 0.3 * len(text):
            continue  # mostly numbers: share prices, counters
        add("para" if tag.name == "p" else "text", _hash("text", RELATIVE_TIME.sub("", text).lower().strip()),
            text, 2, page_link, _find_date(text))

    soup.decompose()

    items = list(items.values())
    if _looks_unreadable(items, base_url):
        return [], ("The page loads its news/event listing with JavaScript, which this tool can't read "
                    "(only the page's heading and menus are in the HTML).")
    return items[:MAX_ITEMS], None


def _looks_unreadable(items, base_url):
    """A page whose listing is loaded by JavaScript still has its title/breadcrumb in the HTML
    (e.g. a "Press releases" heading linking to itself). Don't let that pass as content, or the
    row would report "No Change" forever."""
    this_page, site_root = _page_key(base_url), _page_key(urljoin(base_url, "/"))
    other_pages = [i for i in items if i["_kind"] == "link" and _page_key(i["link"]) not in (this_page, site_root)]
    content_links = [i for i in other_pages if i["date"] or (i["_quality"] == 2 and len(i["title"]) >= 20)]
    # Intro paragraphs ("Browse our press releases...") are static, so only dated ones count
    text_rows = [i for i in items
                 if (i["_kind"] == "text" and (i["date"] or len(i["title"]) >= 30)) or (i["_kind"] == "para" and i["date"])]
    # ...but a single data-rich table row (e.g. a pipeline table) is real content
    long_rows = [i for i in text_rows if i["_kind"] == "text" and len(i["title"]) >= 100]
    return len(other_pages) < 2 and not content_links and len(text_rows) < 3 and not long_rows


def public(item):
    """An item without the internal fields (the ones starting with '_')."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


# --- Compatibility with snapshots saved by the previous version of the tool ---
# The old version stripped every number from the raw HTML before parsing, kept only the first
# link of each block, and stored items as a plain list of {title, link, timestamp}. To keep
# change detection working on the first run after the upgrade, the current page is re-read
# with exactly the old rules and compared link-for-link.
def _legacy_clean(text):
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", text)
    text = re.sub(r"\b\d{1,2}:\d{2}(?:\s?[APMapm]{2})?\b", "", text)
    text = re.sub(r"\d{4}/\d{2}/\d{2}", "", text)
    return text


def legacy_link(href, page_url):
    """A link as the previous version of the tool would have stored it."""
    return urljoin(page_url, re.sub(r"\b\d+\b", "", _legacy_clean(href)))


def legacy_new_items(previous, items, html_content, page_url):
    """Items that are new compared with an old-format snapshot (a list of dicts)."""
    html = UnicodeDammit(html_content).unicode_markup if isinstance(html_content, bytes) else html_content
    html = re.sub(r"(Last updated|Published on)[^<]+", "", _legacy_clean(html), flags=re.I)
    html = re.sub(r"\b\d+\b", "", html)
    soup = BeautifulSoup(html, "html.parser")
    current = set()
    for tag in soup.find_all(["article", "li", "tr", "div", "section", "p"]):
        text = tag.get_text(strip=True)
        if not text or any(j in text.lower() for j in ("skip", "main menu", "footer", "cookie")):
            continue
        a = tag.find("a")
        if a:
            current.add(urljoin(page_url, a["href"]) if a.get("href") else page_url)
    soup.decompose()

    old = {item.get("link") for item in previous if isinstance(item, dict)}
    added = current - old
    # Report them using the new, cleaner items (links with their numbers intact)
    return [item for item in items if item.get("_legacy_link") in added]
