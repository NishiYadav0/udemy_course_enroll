"""
utils/scraper.py
----------------
ScholarSync — Layer 2 of the pipeline.

Responsibilities:
  1. Extract all HTTP/HTTPS URLs from a Telegram post text string.
  2. For each URL that is NOT already a udemy.com link, visit the
     intermediary page (findmycourse.in, freecourse.io, etc.) and
     scrape the underlying Udemy coupon link from the page HTML.
  3. Return a de-duplicated list of direct Udemy coupon URLs.

Both known channel patterns are handled:
  - findmycourse.in  : "Enroll Now on Udemy" button → href contains udemy.com
  - freecourse.io    : "Enroll Now - Free" button → href contains udemy.com
  - Generic fallback : any <a href> that contains "udemy.com"
"""

import re
import time
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 20          # seconds — intermediary sites can be slow
MAX_RETRIES     = 3           # retry attempts on network errors
RETRY_DELAY     = 4           # seconds between retries

# Mimic a real browser so sites don't block the scraper
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Regex to pull all raw HTTP/HTTPS URLs out of a block of text
URL_REGEX = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)

# Regex to validate that a URL is a proper Udemy course page with a coupon
UDEMY_COUPON_REGEX = re.compile(
    r"udemy\.com/course/[^/\s]+/?.*[?&]couponCode=[^&\s]+",
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────
def _clean_url(url: str) -> str:
    """Strip trailing punctuation that regex sometimes captures."""
    return url.rstrip(".,;!?)")


def _fetch_page(url: str) -> str | None:
    """
    Download the HTML of a page with retries.
    Returns the response text or None on failure.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                return resp.text
            logger.warning(
                "Scraper: HTTP %s for %s (attempt %d)",
                resp.status_code, url, attempt,
            )
        except requests.exceptions.Timeout:
            logger.warning("Scraper: Timeout on %s (attempt %d)", url, attempt)
        except requests.exceptions.RequestException as exc:
            logger.warning("Scraper: Request error on %s — %s (attempt %d)", url, exc, attempt)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    return None


def _extract_udemy_links_from_html(html: str, base_url: str = "") -> list[str]:
    """
    Parse HTML and return all href values that point to udemy.com.
    Handles both absolute and relative URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []

    for tag in soup.find_all("a", href=True):
        href: str = tag["href"].strip()

        # Resolve relative URLs (e.g., /go/udemy-course)
        if href.startswith("/"):
            href = urljoin(base_url, href)

        if "udemy.com" in href.lower():
            cleaned = _clean_url(href)
            found.append(cleaned)

    return found


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────
def extract_all_urls(text: str) -> list[str]:
    """
    Extract every raw HTTP/HTTPS URL from a Telegram post text.

    Parameters
    ----------
    text : Raw message text or caption

    Returns
    -------
    List of unique URL strings found in the text.
    """
    if not text:
        return []
    raw = URL_REGEX.findall(text)
    cleaned = [_clean_url(u) for u in raw]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in cleaned:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def _fetch_page_with_browser(
    url: str,
    wait_selector: str = "a[href*='udemy.com']",
    wait_timeout_ms: int = 8000,
) -> str | None:
    """
    Fallback for JavaScript-rendered pages (e.g. findmycourse.in) that
    return an empty SPA shell to a plain requests.get() call — the real
    content is injected client-side after the page loads in a real
    browser.

    IMPORTANT: `wait_selector` must match something that ACTUALLY
    appears on the specific page being rendered, or this just wastes
    `wait_timeout_ms` doing nothing useful before capturing whatever
    HTML happens to exist at that point (which may be before the SPA
    has finished rendering). The default ("a[href*='udemy.com']") is
    correct for an individual COURSE page, but WRONG for a course-
    LISTING page — those only ever link to internal course-detail
    pages (e.g. "/course/{slug}"), never directly to udemy.com. Callers
    rendering a listing page must pass a selector that matches THAT
    page's actual content (e.g. "a[href*='/course/']").

    Launches a FRESH headless Chromium instance per call and closes it
    immediately afterward (rather than keeping one running 24/7). This
    is deliberate: this scraper often runs on small free-tier VMs
    (e.g. Oracle's 1 OCPU / 1GB RAM shape), so we only pay the memory
    cost of a browser process when it's actually needed, not as a
    permanent background footprint.

    Requires the optional `playwright` package + a downloaded Chromium
    binary:
        pip install playwright
        playwright install chromium
        playwright install-deps      # Linux servers: system libs for Chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "Scraper: Playwright not installed — cannot render JS-only pages "
            "like findmycourse.in. Run: pip install playwright && "
            "playwright install chromium"
        )
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(url, timeout=REQUEST_TIMEOUT * 1000, wait_until="load")
                try:
                    page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
                except Exception:
                    # Not fatal — page may have fully rendered without
                    # ever showing a matching element (e.g. sold out /
                    # no coupon, or an empty listing page). But if this
                    # keeps happening for a page that SHOULD have
                    # matches, it usually means wait_selector is wrong
                    # for this page, not that the page is empty.
                    logger.debug(
                        "Scraper: wait_for_selector(%r) timed out on %s — "
                        "capturing HTML as-is", wait_selector, url
                    )
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("Scraper: Playwright render failed for %s — %s", url, exc)
        return None


def get_page_html_and_udemy_links(url: str) -> tuple[str, list[str]]:
    """
    Single-fetch variant that returns BOTH the raw page HTML and the
    extracted Udemy links from it — used by utils/poller.py so it
    doesn't have to fetch the same course page twice (once for the
    Udemy link, once again just to get text for category matching,
    since the website-polling architecture has no Telegram post text
    to categorize a course with).

    This handles:
      - findmycourse.in  (JS-rendered, needs the headless-browser
        fallback below)
      - freecourse.io    (server-rendered, plain requests.get() already
        sees the link)
      - Any other redirect / listing site

    Strategy: try the fast, lightweight static fetch first. Only if
    that finds zero Udemy links do we pay the cost of spinning up a
    real headless browser to render JavaScript.

    Returns
    -------
    (html, udemy_links) — html is "" on total failure, udemy_links may
    be empty even when html is present (e.g. sold-out course).
    """
    logger.info("Scraper: Visiting intermediary page → %s", url)
    html = _fetch_page(url)

    links = _extract_udemy_links_from_html(html, base_url=url) if html else []

    if not links:
        logger.info(
            "Scraper: Static fetch found nothing on %s — trying JS-rendered "
            "fallback (headless browser)...", url
        )
        rendered_html = _fetch_page_with_browser(url)
        if rendered_html:
            html = rendered_html
            links = _extract_udemy_links_from_html(rendered_html, base_url=url)

    if not links:
        logger.info("Scraper: No Udemy links found on %s (even after JS render)", url)
    else:
        logger.info("Scraper: Found %d Udemy link(s) on %s", len(links), url)

    return (html or ""), links


def get_udemy_links_from_page(url: str) -> list[str]:
    """
    Visit a single URL and extract every Udemy link found on the page.
    Thin wrapper around get_page_html_and_udemy_links() for callers
    (main.py, test_pipeline.py) that only need the links, not the HTML.

    Parameters
    ----------
    url : The intermediary website URL from the Telegram post

    Returns
    -------
    List of Udemy URLs found on that page (may be empty).
    """
    _, links = get_page_html_and_udemy_links(url)
    return links


def extract_page_text(html: str) -> str:
    """
    Extract visible text from a scraped course page for keyword_match()
    to run against — used by the website-poller architecture, which has
    no Telegram post text to categorize a course with (there's no post
    at all; the course page itself is the only source of context).

    Pulls <title>, all headings, and the general body text, collapsed
    to a single whitespace-separated string. Deliberately broad (not
    just the description) so keyword hits anywhere on the page count —
    same tolerance the original keyword_match() had against full post
    captions.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # Drop script/style noise so their contents don't pollute the text
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    return text


def extract_course_links_from_html(
    html: str, base_url: str, path_marker: str
) -> list[str]:
    """
    Extract all <a href> links on a page whose path contains a given
    marker string (e.g. "/courses/" on freecourse.io or "/course/" on
    findmycourse.in) — i.e. links to individual course detail pages
    found on a course-LISTING page. Used by utils/poller.py to discover
    new courses without depending on Telegram at all.

    Resolves relative URLs against base_url and de-duplicates while
    preserving page order.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    seen: set[str] = set()
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href: str = tag["href"].strip()
        if href.startswith("/"):
            href = urljoin(base_url, href)
        if path_marker in href and href.startswith("http"):
            cleaned = _clean_url(href)
            if cleaned not in seen:
                seen.add(cleaned)
                links.append(cleaned)
    return links


def fetch_rendered_html(
    url: str,
    wait_selector: str = "a[href*='udemy.com']",
    wait_timeout_ms: int = 8000,
) -> str | None:
    """
    Public wrapper around the Playwright headless-browser fetch, for
    callers (like utils/poller.py) that need JS-rendered HTML directly
    — e.g. a course-listing page that is known in advance to be a
    client-side-rendered SPA, where trying the plain static fetch
    first would just waste a request.

    Pass a `wait_selector` matching real content on the target page
    (e.g. "a[href*='/course/']" for a listing page) — the default only
    makes sense for individual Udemy-linking course pages. See the
    docstring on _fetch_page_with_browser() for why this matters.
    """
    return _fetch_page_with_browser(url, wait_selector=wait_selector, wait_timeout_ms=wait_timeout_ms)


def resolve_udemy_links(post_text: str) -> list[str]:
    """
    Master scraper function — called by main.py for every matched post.

    Steps:
      1. Pull all URLs from the post text.
      2. Any URL that is already a udemy.com link → add directly.
      3. Any other URL → visit the page and extract Udemy links.
      4. Return a de-duplicated list of valid Udemy coupon URLs.

    Parameters
    ----------
    post_text : Full text of the Telegram message / caption

    Returns
    -------
    De-duplicated list of direct Udemy coupon URLs.
    """
    all_urls = extract_all_urls(post_text)
    if not all_urls:
        return []

    udemy_links: list[str] = []
    seen: set[str] = set()

    for url in all_urls:
        domain = urlparse(url).netloc.lower()

        if "udemy.com" in domain:
            # Already a direct Udemy URL — validate it has a coupon code
            if _clean_url(url) not in seen:
                udemy_links.append(_clean_url(url))
                seen.add(_clean_url(url))
        else:
            # Intermediary site — scrape it
            extracted = get_udemy_links_from_page(url)
            for link in extracted:
                if link not in seen:
                    udemy_links.append(link)
                    seen.add(link)

    # Final filter: only keep URLs that have a couponCode parameter
    valid = [u for u in udemy_links if "couponCode=" in u]

    if not valid and udemy_links:
        # Some pages embed Udemy links without couponCode — keep them anyway
        # (udemy.py will handle the price check)
        valid = udemy_links

    logger.info(
        "Scraper: Resolved %d valid Udemy link(s) from post", len(valid)
    )
    return valid
