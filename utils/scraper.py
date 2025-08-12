# To scrape the contents of a site
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin

def extract_items(html_content, base_url):
    junk_keywords = ["skip", "main menu", "footer", "cookie"] # Don't include html with these words
    soup = BeautifulSoup(html_content, 'html.parser')
    items = []

    candidates = soup.find_all(['article', 'li', 'tr', 'div', 'section', 'p'])

    for tag in candidates:
        text = tag.get_text(strip=True)
        if not text or any(j in text.lower() for j in junk_keywords):
            continue

        link_tag = tag.find('a')
        date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', text)

        if link_tag and text:
            raw_link = link_tag.get('href')
            full_link = urljoin(base_url, raw_link) if raw_link else base_url

            items.append({
                "title": text[:150],
                "link": full_link,
                "timestamp": date_match.group(0) if date_match else "unknown"
            })

    if not items:
        return [], "No extractable content found (unsupported structure)"

    return items, None

def clean_html(html):
    """
    Removes dynamic, frequently changing content that should not trigger a 'new update' detection.
    """
    if not html:
        return ""

    # Remove common timestamp patterns (e.g., '2025-07-27', '12:45 PM')
    html = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', html)  # ISO dates
    html = re.sub(r'\b\d{1,2}:\d{2}(?:\s?[APMapm]{2})?\b', '', html)  # time of day
    html = re.sub(r'\d{4}/\d{2}/\d{2}', '', html)  # alt date format
    html = re.sub(r'(Last updated|Published on)[^<]+', '', html, flags=re.IGNORECASE)

    # Optional: Remove numbers that could be dynamic (e.g., counters, views)
    html = re.sub(r'\b\d+\b', '', html)

    return html