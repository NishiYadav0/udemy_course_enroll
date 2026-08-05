"""
utils/poller.py
----------------
ScholarSync — Website-Polling Discovery Layer.

Replaces Telegram as the TRIGGER mechanism. Instead of waiting for a
live push from Telegram (confirmed broken in this deployment — see
PROGRESS_REPORT.md for the full diagnostic trail: membership and
dialog-sync are fine, but zero raw MTProto updates ever arrive, most
likely due to local antivirus/firewall/VPN interference), this module
directly watches the two source websites for newly published courses.

Telegram is still used elsewhere in the pipeline, but ONLY to SEND
alerts (poller_main.py) — that direction never depended on the broken
live-update mechanism, since sending is a request your script
initiates and gets a direct reply to, not something Telegram has to
push to you unprompted.

Discovery strategy
-------------------
Both sites' course-LISTING pages are client-side-rendered SPAs — a
plain HTTP GET shows "0 courses found" / an empty shell even though
real courses exist, confirmed by direct testing. So discovery requires
rendering the listing page with the same Playwright headless-browser
fallback already used elsewhere in this project for JS-only pages.

Polling is deliberately infrequent (see POLL_INTERVAL_SECONDS in
poller_main.py / .env) — freecourse.io's robots.txt explicitly
disallows AI crawlers and asks for a 5-second crawl-delay; a full
listing-page render every few minutes stays well clear of anything
that could be called aggressive scraping.

Persistence
-----------
A local JSON file (SEEN_COURSES_PATH) tracks every course URL already
evaluated, so restarts don't re-process or re-alert the same course
twice. On the very FIRST run (no file exists yet), poller_main.py
bootstraps the seen set from whatever's currently listed WITHOUT
triggering any processing/alerts — otherwise the first run would treat
the entire existing back-catalog as "new" and flood the pipeline (and
your alert channel) with historical courses.
"""

import json
import logging
from pathlib import Path

from utils.scraper import extract_course_links_from_html, fetch_rendered_html

logger = logging.getLogger(__name__)

# Stored in the project root, next to main.py / poller_main.py.
SEEN_COURSES_PATH = Path(__file__).resolve().parent.parent / "seen_courses.json"

# Soft cap on how many seen-URLs we persist — old entries are never
# needed again once a course has scrolled off both sites' listings.
MAX_SEEN_ENTRIES = 5000

# Each site to poll: (name, listing page URL, course-detail path marker)
# path_marker is used to tell "a link to an individual course page"
# apart from nav links, footer links, category links, etc. on the
# same listing page.
SITES: list[dict[str, str]] = [
    {
        "name": "freecourse.io",
        "listing_url": "https://freecourse.io/courses",
        "path_marker": "/courses/",
    },
    {
        "name": "findmycourse.in",
        "listing_url": "https://findmycourse.in/courses",
        "path_marker": "/course/",
    },
]


def is_first_run() -> bool:
    """True if no seen-courses file exists yet on disk."""
    return not SEEN_COURSES_PATH.exists()


def load_seen() -> set[str]:
    """Load the persisted set of already-processed course URLs."""
    if SEEN_COURSES_PATH.exists():
        try:
            data = json.loads(SEEN_COURSES_PATH.read_text(encoding="utf-8"))
            return set(data)
        except Exception as exc:
            logger.warning(
                "Poller: Could not read %s — starting fresh (%s)",
                SEEN_COURSES_PATH, exc,
            )
    return set()


def save_seen(seen: set[str]) -> None:
    """Persist the seen-courses set to disk, soft-capped in size."""
    try:
        entries = list(seen)
        if len(entries) > MAX_SEEN_ENTRIES:
            entries = entries[-MAX_SEEN_ENTRIES:]
        SEEN_COURSES_PATH.write_text(json.dumps(entries), encoding="utf-8")
    except Exception as exc:
        logger.warning("Poller: Could not write %s — %s", SEEN_COURSES_PATH, exc)


def discover_new_courses(seen: set[str]) -> list[tuple[str, str]]:
    """
    Render each site's listing page and return (site_name, course_url)
    pairs for every course URL not already present in `seen`.

    Does NOT mutate `seen` itself — the caller (poller_main.py) decides
    exactly when to mark a URL seen (currently: immediately after
    discovery, before processing, so a crash mid-pipeline never causes
    the same course to be retried forever).

    This function is SYNCHRONOUS (it calls Playwright's sync API
    internally via utils.scraper). Callers running inside an asyncio
    event loop MUST invoke it via loop.run_in_executor(...) — calling
    it directly from a coroutine will trip Playwright's "sync API
    inside asyncio loop" guard.
    """
    new_courses: list[tuple[str, str]] = []

    for site in SITES:
        name = site["name"]
        try:
            # Wait for actual course-card links (not udemy.com links —
            # listing pages never contain those directly, only links to
            # internal course-detail pages). Longer timeout than a
            # single course page since a full grid of cards can take
            # longer to hydrate than one course's content.
            html = fetch_rendered_html(
                site["listing_url"],
                wait_selector=f"a[href*='{site['path_marker']}']",
                wait_timeout_ms=15000,
            )
            if not html:
                logger.warning("Poller: Could not render listing page for %s", name)
                continue

            links = extract_course_links_from_html(
                html, base_url=site["listing_url"], path_marker=site["path_marker"]
            )
            logger.info(
                "Poller: %s listing page shows %d course link(s)", name, len(links)
            )
            if links:
                sample = ", ".join(l.rsplit("/", 1)[-1][:40] for l in links[:3])
                logger.info("Poller: %s sample: %s%s", name, sample, " ..." if len(links) > 3 else "")

            new_for_site = [link for link in links if link not in seen]
            if new_for_site:
                logger.info("Poller: %s has %d NEW course(s) not seen before", name, len(new_for_site))
            for link in new_for_site:
                new_courses.append((name, link))

        except Exception as exc:
            logger.warning("Poller: Error checking %s — %s", name, exc)

    return new_courses
