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

# curl_cffi impersonates Chrome's TLS fingerprint (JA3/JA4) so Cloudflare
# Bot Management passes the enrollment POST. Falls back to plain requests for
# read-only GET calls (metadata, ownership check) which are not CF-protected.
try:
    from curl_cffi import requests as cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False
from urllib.parse import urlencode

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
# The User-Agent MUST match the browser that produced UDEMY_CF_CLEARANCE.
# Cloudflare binds cf_clearance to (IP address + exact User-Agent string); send
# a different UA and the clearance is rejected, which is what produced the
# "We couldn't complete this purchase" screen. Override in .env with
# UDEMY_USER_AGENT="<paste the exact UA from your browser>".
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def get_user_agent() -> str:
    """User-Agent used for every Udemy call (must match cf_clearance's UA)."""
    return os.getenv("UDEMY_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def get_udemy_cookies(include_cloudflare: bool = True) -> dict[str, str]:
    """
    Full Udemy cookie jar, assembled from .env.

    Why more than access_token:
      access_token alone authenticates the *api-2.0* JSON endpoints, which is
      why metadata and ownership lookups worked. But /payment/checkout/... is
      a server-rendered Django page whose logged-in state comes from the
      session cookies below. With only access_token the checkout page renders
      the anonymous "1. Log in or create an account" step and enrollment can
      never complete — exactly what checkout_state3.png shows.

    Only non-empty values are included, so this degrades gracefully.
    """
    raw_token = os.getenv("UDEMY_ACCESS_TOKEN", "").strip()
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token[7:]
    # Udemy stores access_token as a QUOTED cookie value. Strip any quotes the
    # user pasted in so we control the quoting ourselves, consistently.
    raw_token = raw_token.strip('"')

    jar: dict[str, str] = {}
    if raw_token:
        jar["access_token"] = f'"{raw_token}"'

    # Cloudflare cookies are bound to the IP address AND User-Agent that
    # created them, so they cannot be copied from your PC to a server. The
    # headless browser therefore asks for include_cloudflare=False and earns
    # its own clearance from the VM's own IP instead (see enroll_browser.py).
    # Login cookies below ARE portable and work from any IP.
    optional = {
        "csrftoken":          os.getenv("UDEMY_CSRF_TOKEN", ""),
        "cf_clearance":       os.getenv("UDEMY_CF_CLEARANCE", "") if include_cloudflare else "",
        "__cf_bm":            os.getenv("UDEMY_CF_BM", "")        if include_cloudflare else "",
        # ── session cookies: required for the checkout page to see you ──
        "dj_session_id":      os.getenv("UDEMY_DJ_SESSION_ID", ""),
        "ud_user_jwt":        os.getenv("UDEMY_USER_JWT", ""),
        "client_id":          os.getenv("UDEMY_CLIENT_ID", ""),
        "ud_cache_user":      os.getenv("UDEMY_USER_ID", ""),
    }
    for name, value in optional.items():
        value = value.strip().strip('"')
        if value:
            jar[name] = value

    # Static hints Udemy's frontend sets for a logged-in session
    if jar.get("ud_cache_user"):
        jar["ud_cache_logged_in"] = "1"

    return jar


def _build_session():
    """
    Build an authenticated session that mimics Chrome's TLS fingerprint.

    Uses curl_cffi (if installed) to impersonate Chrome's TLS handshake
    so Cloudflare Bot Management lets the enrollment POST through.
    Without this, Cloudflare blocks the POST with 403 regardless of
    what cookies/headers are sent.

    Falls back to requests.Session if curl_cffi is unavailable (enrollment
    will likely still fail on CF-protected endpoints but GET calls work).
    """
    jar = get_udemy_cookies()

    common_headers = {
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-US",
        "Content-Type":     "application/json",
        "Origin":           "https://www.udemy.com",
        "Referer":          "https://www.udemy.com/",
        "User-Agent":       get_user_agent(),
        "x-requested-with": "XMLHttpRequest",
        "sec-fetch-dest":   "empty",
        "sec-fetch-mode":   "cors",
        "sec-fetch-site":   "same-origin",
    }
    if jar.get("csrftoken"):
        common_headers["X-CSRFToken"] = jar["csrftoken"]

    if _CFFI_AVAILABLE:
        session = cffi_requests.Session(impersonate="chrome120")
        domain = ".udemy.com"
        logger.debug("Enroll session: curl_cffi chrome120 TLS impersonation")
    else:
        session = requests.Session()
        domain = "www.udemy.com"
        logger.warning(
            "curl_cffi not installed — enrollment may be blocked by Cloudflare. "
            "Run: pip install curl_cffi"
        )

    session.headers.update(common_headers)
    for name, value in jar.items():
        session.cookies.set(name, value, domain=domain)

    missing = [k for k in ("dj_session_id", "ud_user_jwt", "cf_clearance") if k not in jar]
    if missing:
        logger.warning(
            "Udemy session is missing cookies %s — the checkout page will treat "
            "this session as LOGGED OUT and enrollment will fail. See "
            "config/.env.example for how to capture them.", missing
        )

    return session


def _build_headers() -> dict[str, str]:
    """
    Build headers for read-only (GET) requests.
    GETs only need the access_token cookie; no CSRF required.
    """
    raw_access_token = os.getenv("UDEMY_ACCESS_TOKEN", "").strip()
    if raw_access_token.lower().startswith("bearer "):
        raw_access_token = raw_access_token[7:]
    raw_access_token = raw_access_token.strip('"')

    jar = get_udemy_cookies()
    cookie_header = "; ".join(f"{k}={v}" for k, v in jar.items())

    headers = {
        # Keep Authorization header as a fallback for the public API endpoints
        # that still accept it (metadata, ownership check, etc.)
        "Authorization": f"Bearer {raw_access_token}",
        "User-Agent": get_user_agent(),
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US",
        "Referer": "https://www.udemy.com/",
        "x-requested-with": "XMLHttpRequest",
    }
    # Sending the cookies too makes /course-landing-components/{id}/me/ return
    # the *personalised* answer (is_valid_student), not the anonymous one.
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


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


def is_already_enrolled(course_id: int, headers: dict) -> bool:
    """
    Check whether the authenticated user already owns this course, using
    Udemy's subscribed-courses lookup — the only reliable, coupon-independent
    way to know this (the metadata price-check does NOT apply the coupon
    code, so it can never tell "already owned" apart from "coupon expired").

    Fails OPEN (returns False) on any request error, so a lookup hiccup
    never blocks a legitimate enrollment attempt — worst case, a course
    you already own gets one redundant (harmless) enroll attempt instead
    of being silently skipped.
    """
    url = f"{UDEMY_API_BASE}/users/me/subscribed-courses/{course_id}/"
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        return resp.status_code == 200
    except requests.exceptions.RequestException as exc:
        logger.warning("Udemy: Ownership check failed for course_id=%s — %s", course_id, exc)
        return False


def get_coupon_pricing(course_id: int, coupon: str | None) -> dict:
    """
    Ask Udemy what this course ACTUALLY costs *with the coupon applied*.

    This is the endpoint Udemy's own course page uses. Unlike
    /courses/{slug}/ (which only ever reports the undiscounted list price
    and ignores any coupon you pass it), this one genuinely evaluates the
    coupon server-side and tells us the real, post-discount price.

    It also answers two other questions in the same round-trip:
      * is_valid_student  -> do we ALREADY own this course?
      * campaign          -> is the coupon live, and how many uses are left?

    Returns a dict:
        {
          "ok":              bool,   # did the call succeed at all
          "price":           float,  # real price AFTER the coupon (0.0 = free)
          "list_price":      float,  # undiscounted price
          "discount_percent":int,
          "already_owned":   bool,
          "coupon_valid":    bool,   # coupon recognised & currently active
          "uses_remaining":  int | None,
          "coupon_end_time": str | None,
        }

    Fails SAFE: on any error, ok=False and the caller falls back to the
    old list-price behaviour rather than blindly enrolling.
    """
    result = {
        "ok": False, "price": None, "list_price": None,
        "discount_percent": 0, "already_owned": False,
        "coupon_valid": False, "uses_remaining": None, "coupon_end_time": None,
    }

    url = f"{UDEMY_API_BASE}/course-landing-components/{course_id}/me/"
    params = {"components": "purchase"}
    if coupon:
        params["couponCode"] = coupon

    data = _api_get(url, _build_headers(), params)
    if not data or data.get("_status") == 401:
        logger.warning("Udemy: coupon pricing lookup failed for course_id=%s", course_id)
        return result

    try:
        pdata = (data.get("purchase") or {}).get("data") or {}
        pricing = pdata.get("pricing_result") or {}

        price_obj = pricing.get("price") or {}
        list_obj  = pricing.get("list_price") or {}

        result["ok"]               = True
        result["price"]            = float(price_obj.get("amount", 0.0) or 0.0)
        result["list_price"]       = float(list_obj.get("amount", 0.0) or 0.0)
        result["discount_percent"] = int(pricing.get("discount_percent") or 0)
        # Udemy's own flag for "this user already has this course"
        result["already_owned"]    = bool(pdata.get("is_valid_student"))

        campaign = pricing.get("campaign") or {}
        if campaign:
            result["coupon_valid"]    = True
            result["uses_remaining"]  = campaign.get("uses_remaining")
            result["coupon_end_time"] = campaign.get("end_time")
        else:
            # No campaign block => the coupon we sent was not honoured.
            # (A genuinely free course legitimately has no campaign either,
            #  which is why callers must also look at price/list_price.)
            result["coupon_valid"] = not bool(coupon)

        logger.info(
            "Coupon check: course_id=%s price=%.2f (list %.2f, -%d%%) "
            "coupon_valid=%s uses_left=%s owned=%s",
            course_id, result["price"], result["list_price"],
            result["discount_percent"], result["coupon_valid"],
            result["uses_remaining"], result["already_owned"],
        )
    except (TypeError, ValueError, AttributeError) as exc:
        logger.warning("Udemy: could not parse coupon pricing response — %s", exc)
        return {**result, "ok": False}

    return result


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
def auto_enroll(course_id: int, coupon: str | None, session=None, slug: str | None = None) -> str:
    """
    Attempt to enroll in a course using Udemy's checkout API.

    Parameters
    ----------
    course_id : Internal numeric Udemy course ID
    coupon    : Coupon code string (None if no coupon)
    session   : Optional pre-built session; one is created if omitted
    slug      : Course slug — passed to the Playwright browser fallback
                so it can build the correct express checkout URL

    Returns
    -------
    STATUS_SUCCESS | STATUS_ALREADY_OWNED | STATUS_EXPIRED |
    STATUS_TOKEN_EXPIRED | STATUS_ERROR
    """
    if session is None:
        session = _build_session()

    checkout_url = f"{UDEMY_API_BASE}/users/me/subscribed-courses/"

    # The Referer for the enroll POST should be the checkout page, not the
    # homepage — matching what a real browser sends after clicking "Enroll now".
    course_referer = f"https://www.udemy.com/payment/checkout/express/course/{course_id}/"
    if coupon:
        course_referer += f"?couponCode={coupon}"

    payload: dict = {
        "course_id":           course_id,
        "buyable_object_type": "course",
    }
    if coupon:
        payload["discount_code"] = coupon

    try:
        resp = session.post(
            checkout_url,
            json=payload,
            headers={"Referer": course_referer},
            timeout=REQUEST_TIMEOUT,
        )

        logger.debug(
            "Udemy enroll POST → HTTP %s | body: %s",
            resp.status_code, resp.text[:300],
        )

        if resp.status_code in (200, 201):
            return STATUS_SUCCESS

        if resp.status_code == 400:
            body = resp.text.lower()
            if "already" in body or "subscription" in body:
                return STATUS_ALREADY_OWNED
            if "coupon" in body or "discount" in body or "price" in body:
                logger.info("Udemy enroll 400 (coupon rejected): %s", resp.text[:500])
                return STATUS_EXPIRED
            logger.warning("Udemy enroll 400 (unrecognized reason): %s", resp.text[:500])
            return STATUS_EXPIRED

        if resp.status_code == 401:
            return STATUS_TOKEN_EXPIRED

        if resp.status_code == 403:
            # Udemy's /subscribed-courses/ POST is blocked for non-approved API clients.
            # Fall back to Playwright (headless Chrome) which navigates the real checkout
            # flow exactly as a browser would — this bypasses the client restriction.
            logger.info(
                "Udemy API enrollment blocked (403 client restriction) — "
                "falling back to browser-based enrollment via Playwright."
            )
            from utils.enroll_browser import browser_enroll
            return browser_enroll(course_id, slug or str(course_id), coupon)

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

    # ── Step 1.5: Ownership check (BEFORE the policy/price guardrail) ──
    # Must run before evaluate_course_policy(), otherwise an already-owned
    # course gets misclassified as "Coupon expired" — the metadata price
    # never reflects ownership or the coupon, only the real enroll call
    # does, and we don't want to reach that call at all if we already know
    # the answer.
    headers = _build_headers()

    # ── Step 1.5: Coupon-aware price + ownership, in ONE call ──
    # CRITICAL: meta["price"] above is the UNDISCOUNTED list price —
    # /courses/{slug}/ ignores any coupon you hand it. Feeding that value
    # into the price guardrail made EVERY paid coupon course look like
    # "Coupon expired, price=$19.99" and dropped it before enrollment.
    # course-landing-components/{id}/me/ actually evaluates the coupon.
    pricing = get_coupon_pricing(meta["course_id"], coupon)

    if pricing["ok"]:
        # Udemy's own is_valid_student flag — authoritative ownership answer
        if pricing["already_owned"]:
            logger.info("Udemy: Already enrolled (is_valid_student=true) — %s", meta["title"])
            return STATUS_ALREADY_OWNED, meta

        # Overwrite the list price with the REAL post-coupon price
        meta["list_price"]      = meta["price"]
        meta["price"]           = pricing["price"]
        meta["coupon_valid"]    = pricing["coupon_valid"]
        meta["uses_remaining"]  = pricing["uses_remaining"]
        meta["coupon_end_time"] = pricing["coupon_end_time"]

        if coupon and not pricing["coupon_valid"]:
            meta["policy_reason"] = f"Coupon expired (not recognised by Udemy) | {meta['title'][:60]}"
            logger.info("Policy result: DROPPED | %s", meta["policy_reason"])
            return STATUS_EXPIRED, meta
    else:
        # Pricing lookup failed — fall back to the old ownership endpoint so
        # we still don't re-enroll something we already have.
        if is_already_enrolled(meta["course_id"], headers):
            logger.info("Udemy: Already enrolled (ownership fallback) — %s", meta["title"])
            return STATUS_ALREADY_OWNED, meta
        logger.warning(
            "Udemy: coupon price unknown for '%s' — price guardrail will use "
            "the list price and will likely drop this course.", meta["title"]
        )

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

    # ── Step 3: Auto-enroll via browser-like session (cookie + CSRF) ──
    enroll_session = _build_session()
    status = auto_enroll(meta["course_id"], coupon, enroll_session, slug=slug)

    logger.info("Enroll result: %s | %s", status, meta["title"])
    return status, meta
