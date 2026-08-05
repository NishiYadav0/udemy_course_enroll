"""
utils/retry_queue.py
---------------------
ScholarSync — Coupon Retry Queue.

Some source sites (e.g. freecourse.io) update the SAME course page with
a fresh coupon code after the original one expires, instead of posting
a brand new Telegram message. Since main.py only scrapes a course page
once — at the moment the Telegram post first arrives — it can miss a
coupon the site adds minutes or hours later (confirmed directly: a
course was scraped at 11:39 PM with only an expired coupon present,
but the site's own "Last updated" timestamp showed 11:55 PM — 16
minutes AFTER our scrape — for when a second, working coupon was
added).

This module persists a small queue of courses whose most recent coupon
attempt failed specifically with "coupon expired" (checkout price
still full price) or an expired-coupon response during the actual
enroll call — NOT courses dropped for wrong language, low rating,
being natively free, or too short for their category, since a
different coupon code can never fix any of those.

Per user preference, only courses LONGER than RETRY_ELIGIBLE_MIN_HOURS
are queued at all — short courses aren't worth the extra scraping load
of periodic re-visits.

A background worker in main.py (_retry_worker) re-visits each queued
course page every RETRY_INTERVAL_SECONDS, looking specifically for a
coupon CODE that wasn't already tried and failed (comparing just the
coupon code, not the full URL — tracking params like im_ref/sharedid
change on every page load even when the coupon itself hasn't). A
queued course is given up on after RETRY_MAX_AGE_HOURS.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Stored in the project root, next to main.py.
RETRY_QUEUE_PATH = Path(__file__).resolve().parent.parent / "retry_queue.json"

# Only queue courses LONGER than this many hours for a retry attempt.
RETRY_ELIGIBLE_MIN_HOURS = 5.0

# How often the background worker re-checks queued courses.
RETRY_INTERVAL_SECONDS = 900  # 15 minutes

# Give up on (and drop) a queued course after this long since first seen.
RETRY_MAX_AGE_HOURS = 3.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_queue() -> list[dict]:
    """Load the persisted retry queue from disk. Returns [] if none exists."""
    if RETRY_QUEUE_PATH.exists():
        try:
            return json.loads(RETRY_QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "RetryQueue: Could not read %s — starting fresh (%s)",
                RETRY_QUEUE_PATH, exc,
            )
    return []


def save_queue(queue: list[dict]) -> None:
    """Persist the retry queue to disk."""
    try:
        RETRY_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("RetryQueue: Could not write %s — %s", RETRY_QUEUE_PATH, exc)


def add_to_queue(
    course_url: str,
    category: str,
    duration_hours: float,
    tried_coupon: str | None,
) -> None:
    """
    Queue a course for periodic coupon rechecks, unless it's already
    queued or too short to be worth retrying (per RETRY_ELIGIBLE_MIN_HOURS).
    """
    if not course_url or duration_hours <= RETRY_ELIGIBLE_MIN_HOURS:
        return

    queue = load_queue()

    for entry in queue:
        if entry["course_url"] == course_url:
            # Already queued — just make sure this coupon code is marked tried
            if tried_coupon and tried_coupon not in entry["tried_coupons"]:
                entry["tried_coupons"].append(tried_coupon)
                save_queue(queue)
            return

    queue.append({
        "course_url":     course_url,
        "category":       category,
        "duration_hours": duration_hours,
        "tried_coupons":  [tried_coupon] if tried_coupon else [],
        "first_seen":     _now_iso(),
        "attempts":       0,
    })
    save_queue(queue)
    logger.info(
        "RetryQueue: Queued '%s' (%.1fh, category=%s) for coupon rechecks every %d min",
        course_url, duration_hours, category, RETRY_INTERVAL_SECONDS // 60,
    )


def remove_from_queue(course_url: str) -> None:
    """Remove a course from the retry queue (e.g. after success or giving up)."""
    queue = load_queue()
    new_queue = [e for e in queue if e["course_url"] != course_url]
    if len(new_queue) != len(queue):
        save_queue(new_queue)


def update_entry(course_url: str, tried_coupons: list[str], attempts: int) -> None:
    """Persist updated tried-coupon list / attempt count for one entry."""
    queue = load_queue()
    for entry in queue:
        if entry["course_url"] == course_url:
            entry["tried_coupons"] = tried_coupons
            entry["attempts"] = attempts
            break
    save_queue(queue)


def is_too_old(entry: dict) -> bool:
    """True if this entry has been queued longer than RETRY_MAX_AGE_HOURS."""
    try:
        first_seen = datetime.fromisoformat(entry["first_seen"])
        age_hours = (datetime.now(timezone.utc) - first_seen).total_seconds() / 3600
        return age_hours >= RETRY_MAX_AGE_HOURS
    except Exception:
        return True  # malformed entry — safest to drop it
