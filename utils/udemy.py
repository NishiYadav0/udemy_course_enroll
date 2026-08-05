"""
utils/udemy.py
--------------
ScholarSync — Layer 3 & Layer 5 of the pipeline.

Layer 3 : get_course_metadata()
    Calls the Udemy public API to fetch course details:
    id, title, duration, rating, is_paid, badges, and current price.

Layer 5 : auto_enroll()
    Issues a POST to Udemy's internal subscription endpoint to claim
    the course using the authenticated session token (Bearer token from
    browser cookies stored in .env).

process_udemy_link() is the orchestrator called by main.py for each
candidate link — it runs metadata fetch → policy check → enroll in sequence.

Return status strings (used by main.py to decide what to log/notify):
    SUCCESS_ENROLLED    – course claimed successfully
    ALREADY_ENROLLED    – user already owns this course
    POLICY_FAIL         – failed duration / price / rating guardrail
    EXPIRED             – coupon is expired (full price shown)
    TOKEN_EXPIRED       – Udemy access token needs refreshing
    ERROR               – unexpected error
"""

import os
import re
import time
import logging

import requests

from utils.filter import evaluate_course_policy

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
UDEMY_API_BASE  = "https://www.udemy.com/api-2.0"
REQUEST_TIMEOUT = 15
MAX_RETRIES     = 2
RETRY_DELAY     = 3

# Fields we need from the course metadata endpoint
COURSE_FIELDS = (
    "id,title,estimated_content_length,rating,"
    "is_paid,badges,price_detail,num_subscribers,locale"
)

# Status strings returned by process_udemy_link()
STATUS_SUCCESS         = "SUCCESS_ENROLLED"
STATUS_ALREADY_OWNED   = "ALREADY_ENROLLED"
STATUS_POLICY_FAIL     = "POLICY_FAIL"
STATUS_EXPIRED         = "EXPIRED"
STATUS_TOKEN_EXPIRED   = "TOKEN_EXPIRED"
STATUS_ERROR           = "ERROR"
STATUS_PARSE_FAIL      = "PARSE_FAIL"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _build_headers() -> dict[str, str]:
    """Build authenticated request headers using the token from .env."""
    token = os.getenv("UDEMY_ACCESS_TOKEN", "")
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"
    return {
        "Authorization": token,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.udemy.com/",
    }


def _parse_slug_and_coupon(udemy_url: str) -> tuple[str | None, str | None]:
    """
    Extract course slug and coupon code from a Udemy URL.

    Handles:
      https://www.udemy.com/course/SLUG/?couponCode=CODE
      https://www.udemy.com/course/SLUG/                  (no coupon)
    """
    slug_match = re.search(
        r"udemy\.com/course/([^/?#\s]+)", udemy_url, re.IGNORECASE
    )
    coupon_match = re.search(
        r"[?&]couponCode=([^&\s#]+)", udemy_url, re.IGNORECASE
    )

    slug   = slug_match.group(1).rstrip("/") if slug_match else None
    coupon = coupon_match.group(1)            if coupon_match else None
    return slug, coupon


def parse_slug_and_coupon(udemy_url: str) -> tuple[str | None, str | None]:
    """Public wrapper around _parse_slug_and_coupon — used by main.py's
    retry queue to compare coupon codes between scrapes without caring
    about volatile tracking params (im_ref, sharedid, etc.) that change
    on every page load even when the coupon code itself hasn't."""
    return _parse_slug_and_coupon(udemy_url)


def _minutes_to_hours(minutes: int | float | None) -> float:
    """Convert Udemy's estimated_content_length (minutes) to hours."""
    if not minutes:
        return 0.0
    return round(minutes / 60, 2)


def _api_get(url: str, headers: dict, params: dict | None = None) -> dict | None:
    """GET request with retry logic. Returns parsed JSON or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                logger.error("Udemy: 401 Unauthorized — token may be expired.")
                return {"_status": 401}
            if resp.status_code == 404:
                logger.warning("Udemy: 404 — course not found at %s", url)
                return None
            logger.warning(
                "Udemy: GET %s returned HTTP %s (attempt %d)",
                url, resp.status_code, attempt,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Udemy: GET error %s (attempt %d) — %s", url, attempt, exc)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return None


# ─────────────────────────────────────────────────────────────
# Layer 3 — Course Metadata Fetch
# ─────────────────────────────────────────────────────────────
def get_course_metadata(slug: str, coupon: str | None = None) -> dict | None:
    """
    Fetch full course metadata from the Udemy API.

    Parameters
    ----------
    slug   : Course slug extracted from the URL (e.g. "python-bootcamp-2024")
    coupon : Coupon code string (used to check discounted price)

    Returns
    -------
    dict with keys:
        course_id, title, duration_hours, rating, is_paid,
        badges, price, num_subscribers
    or None on failure.
    """
    headers = _build_headers()
    url = f"{UDEMY_API_BASE}/courses/{slug}/"
    params = {f"fields[course]": COURSE_FIELDS}

    data = _api_get(url, headers, params)

    if not data:
        return None

    if data.get("_status") == 401:
        return {"_token_expired": True}

    # Parse price — check if coupon brings it to zero
    price_detail = data.get("price_detail") or {}
    price_amount = price_detail.get("amount", 0.0)
    if isinstance(price_amount, str):
        try:
            price_amount = float(price_amount.replace(",", ""))
        except ValueError:
            price_amount = 0.0

    duration_hours = _minutes_to_hours(data.get("estimated_content_length"))

    # Parse language/locale: Udemy returns e.g. "en_US", "hi_IN", or a dict
    raw_locale = data.get("locale") or "en_US"
    if isinstance(raw_locale, dict):
        raw_locale = raw_locale.get("locale", "en_US")
    lang_code = str(raw_locale)[:2].lower()   # "en", "hi", "de", etc.

    return {
        "course_id":       data.get("id"),
        "title":           data.get("title", "Unknown Course"),
        "duration_hours":  duration_hours,
        "rating":          float(data.get("rating", 0.0)),
        "is_paid":         bool(data.get("is_paid", True)),
        "badges":          data.get("badges", []),
        "price":           price_amount,
        "num_subscribers": data.get("num_subscribers", 0),
        "language":        lang_code,
    }


# ─────────────────────────────────────────────────────────────
# Layer 5 — Auto Enroll
# ─────────────────────────────────────────────────────────────
def auto_enroll(course_id: int, coupon: str | None, headers: dict) -> str:
    """
    Attempt to enroll in a course using the Udemy checkout API.

    Parameters
    ----------
    course_id : Internal numeric Udemy course ID
    coupon    : Coupon code string (None if no coupon)
    headers   : Authenticated headers dict

    Returns
    -------
    STATUS_SUCCESS | STATUS_ALREADY_OWNED | STATUS_EXPIRED | STATUS_ERROR
    """
    checkout_url = f"{UDEMY_API_BASE}/users/me/subscribed-courses/"

    payload: dict = {
        "course_id":           course_id,
        "buyable_object_type": "course",
    }
    if coupon:
        payload["discount_code"] = coupon

    try:
        resp = requests.post(
            checkout_url,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code in (200, 201):
            return STATUS_SUCCESS

        if resp.status_code == 400:
            body = resp.text.lower()
            if "already" in body or "subscription" in body:
                return STATUS_ALREADY_OWNED
            # 400 can also mean coupon is expired / invalid
            if "coupon" in body or "discount" in body or "price" in body:
                return STATUS_EXPIRED
            logger.warning("Udemy enroll 400: %s", resp.text[:200])
            return STATUS_EXPIRED

        if resp.status_code == 401:
            return STATUS_TOKEN_EXPIRED

        if resp.status_code == 403:
            logger.warning("Udemy enroll 403 — may be geo-blocked or rate limited.")
            return STATUS_ERROR

        logger.warning("Udemy enroll unexpected HTTP %s: %s", resp.status_code, resp.text[:200])
        return STATUS_ERROR

    except requests.exceptions.RequestException as exc:
        logger.error("Udemy enroll request error: %s", exc)
        return STATUS_ERROR


# ─────────────────────────────────────────────────────────────
# Orchestrator — called by main.py for each candidate URL
# ─────────────────────────────────────────────────────────────
def process_udemy_link(url: str, category: str) -> tuple[str, dict]:
    """
    Full pipeline for one Udemy URL:
      1. Parse slug + coupon
      2. Fetch course metadata
      3. Run policy evaluation (filter.py)
      4. Auto-enroll if policy passes

    Parameters
    ----------
    url      : Direct Udemy coupon URL
    category : Category string from filter.keyword_match()

    Returns
    -------
    (status_string, metadata_dict)
    metadata_dict contains course info for notification formatting.
    May be empty dict on early failures.
    """
    slug, coupon = _parse_slug_and_coupon(url)

    if not slug:
        logger.warning("Udemy: Could not parse slug from URL — %s", url)
        return STATUS_PARSE_FAIL, {}

    logger.info("Udemy: Processing slug='%s' coupon='%s' category='%s'",
                slug, coupon, category)

    # ── Step 1: Fetch metadata ────────────────────────────────
    meta = get_course_metadata(slug, coupon)

    if not meta:
        logger.warning("Udemy: Metadata fetch failed for slug '%s'", slug)
        return STATUS_ERROR, {}

    if meta.get("_token_expired"):
        logger.error("Udemy: Access token expired!")
        return STATUS_TOKEN_EXPIRED, {}

    # ── Step 2: Policy evaluation ─────────────────────────────
    should_enroll, reason = evaluate_course_policy(
        title          = meta["title"],
        duration_hours = meta["duration_hours"],
        rating         = meta["rating"],
        is_paid        = meta["is_paid"],
        price          = meta["price"],
        badges         = meta["badges"],
        category       = category,
        language       = meta.get("language", "en"),
    )

    logger.info("Policy result: %s", reason)

    if not should_enroll:
        # Stashed here (not just logged) so main.py's retry queue can tell
        # WHY this failed — only "Coupon expired" is worth retrying later,
        # since a different coupon can't fix wrong language/low rating/
        # too-short duration/natively-free.
        meta["policy_reason"] = reason
        return STATUS_POLICY_FAIL, meta

    # ── Step 3: Auto-enroll ───────────────────────────────────
    headers = _build_headers()
    status  = auto_enroll(meta["course_id"], coupon, headers)

    logger.info("Enroll result: %s | %s", status, meta["title"])
    return status, meta
