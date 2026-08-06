"""
utils/enroll_browser.py
-----------------------
Enrollment via Playwright with playwright-stealth v2.x:
- Stealth hides headless Chrome signals so Cloudflare passes the SPA's XHR
- Navigate to express checkout URL → SPA POSTs to express cart → gets price=0 → auto-redirect to /cart/success
"""

import os
import time
import logging
import threading

logger = logging.getLogger(__name__)

STATUS_SUCCESS       = "SUCCESS_ENROLLED"
STATUS_ALREADY_OWNED = "ALREADY_ENROLLED"
STATUS_EXPIRED       = "EXPIRED"
STATUS_TOKEN_EXPIRED = "TOKEN_EXPIRED"
STATUS_ERROR         = "ERROR"


# Where Chrome keeps its profile on the VM. Cookies it earns itself — above
# all cf_clearance — persist here between runs, so the Cloudflare challenge is
# solved once rather than on every enrollment.
BROWSER_PROFILE_DIR = os.path.expanduser(
    os.getenv("UDEMY_BROWSER_PROFILE", "~/.scholarsync_browser")
)

# Only one browser at a time.
#
# main.py dispatches process_udemy_link() via loop.run_in_executor(), so two
# Telegram posts arriving together — or a post landing while the retry worker
# fires — put two threads in here at once. Both would launch Chromium against
# the SAME persistent profile directory, which Chrome refuses ("profile
# appears to be in use"), and both enrollments would fail. Serialising costs
# nothing: coupons stay valid for days, and a queued course waits ~55s.
_BROWSER_LOCK = threading.Lock()

# How long browser_enroll() will keep trying before giving up, and how long it
# pauses between rounds. The loop exits the moment enrollment is confirmed, so
# a generous deadline costs nothing on the common fast path — it only buys
# patience for courses whose checkout button takes a while to enable.
ENROLL_DEADLINE_S   = int(os.getenv("UDEMY_ENROLL_DEADLINE", "240"))
ENROLL_POLL_GAP_MS  = int(os.getenv("UDEMY_ENROLL_POLL_GAP_MS", "3000"))

CF_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
)


def _is_cloudflare_challenge(page) -> bool:
    """True while Cloudflare's interstitial is on screen."""
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    if any(m in title for m in CF_CHALLENGE_MARKERS):
        return True
    try:
        body = (page.locator("body").inner_text(timeout=3_000) or "").lower()
    except Exception:
        return False
    return any(m in body for m in CF_CHALLENGE_MARKERS)


def _wait_out_cloudflare(page, timeout_ms: int = 30_000) -> bool:
    """
    Sit through Cloudflare's managed challenge.

    A genuine (non-headless) Chrome running from this machine's own IP normally
    clears it automatically within a few seconds and is issued a cf_clearance
    cookie for THIS server. That cookie is then persisted in the profile
    directory, so subsequent runs skip the challenge entirely.

    Returns True if the challenge cleared (or was never shown).
    """
    if not _is_cloudflare_challenge(page):
        return True

    logger.info("Cloudflare challenge shown — waiting for it to clear...")
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(2_000)
        waited += 2_000
        if not _is_cloudflare_challenge(page):
            logger.info("Cloudflare challenge cleared after %.0fs", waited / 1000)
            return True

    logger.error("Cloudflare challenge did NOT clear after %.0fs", timeout_ms / 1000)
    return False


def _get_cookies() -> list[dict]:
    """
    Full cookie jar for the headless browser, in Playwright's format.

    Previously this only set access_token (+ CF cookies), which authenticates
    the JSON API but NOT the server-rendered /payment/checkout/ page — so the
    browser landed on the anonymous "Log in or create an account" checkout and
    could never finish. get_udemy_cookies() adds the Django session cookies
    that make Udemy recognise the browser as logged in.
    """
    from utils.udemy import get_udemy_cookies

    # include_cloudflare=False on purpose: cf_clearance / __cf_bm captured on
    # your PC are bound to your home IP and User-Agent, so injecting them into
    # a browser on the server actively HURTS — Cloudflare sees a clearance
    # issued to a different address and challenges immediately. The browser
    # earns its own instead and stores it in BROWSER_PROFILE_DIR.
    return [
        {"name": name, "value": value, "domain": ".udemy.com", "path": "/"}
        for name, value in get_udemy_cookies(include_cloudflare=False).items()
    ]


def _is_logged_out(page) -> bool:
    """
    Detect the anonymous checkout page before wasting time on it.

    Udemy renders a "Log in or create an account" step when the session isn't
    recognised. Catching this explicitly turns a silent 45-second timeout into
    a clear, actionable TOKEN_EXPIRED result.
    """
    try:
        body = page.locator("body").inner_text(timeout=3_000).lower()
    except Exception:
        return False
    signals = ("log in or create an account", "enter your email for order confirmation")
    return any(s in body for s in signals)


def _is_success(url: str) -> bool:
    return "cart/success" in url or "my-courses" in url or "my-learning" in url


# Selectors for the final purple "Enroll now" button on the express checkout
# page, most specific first. Udemy A/B-tests this button constantly, so we try
# stable data attributes before falling back to visible text.
ENROLL_SELECTORS = [
    '[data-purpose="submit-checkout"]',
    '[data-purpose="checkout-button"]',
    '[data-testid="checkout-submit-button"]',
    'button:has-text("Enroll now")',
    'button:has-text("Complete Checkout")',
    'button:has-text("Complete checkout")',
    'form[data-purpose="checkout-form"] button[type="submit"]',
    'button[type="submit"]',
]


ENROLL_TEXTS = ("enroll now", "complete checkout", "complete purchase", "place order")

# Find AND click the enroll button entirely inside the page, in ONE call.
#
# WHY: every Playwright locator call (is_visible, is_enabled, inner_text) is a
# separate CDP round-trip. On this VM each costs several seconds, so walking
# 8 selectors x 8 elements took ~94 SECONDS before the first click was even
# attempted. Doing the whole search in JS costs a single round-trip.
#
# It also sidesteps Playwright's "element is not stable" actionability check,
# which never passes on Udemy's animated checkout button — the normal click
# timed out every single time and only the JS click ever worked.
_JS_CLICK_ENROLL = """
(texts) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const s = window.getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const nodes = Array.from(
    document.querySelectorAll('button, [role="button"], input[type="submit"]')
  );
  for (const el of nodes) {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (!visible(el)) continue;
    const label = (el.innerText || el.value || '').trim().toLowerCase();
    if (!label) continue;
    if (texts.some(t => label.includes(t))) {
      el.click();
      return label.slice(0, 60);
    }
  }
  return null;
}
"""


def _js_click_enroll(page) -> bool:
    """Single-round-trip search-and-click. Returns True if a button was hit."""
    try:
        label = page.evaluate(_JS_CLICK_ENROLL, list(ENROLL_TEXTS))
    except Exception as exc:
        logger.debug("In-page click failed: %s", exc)
        return False
    if label:
        logger.info("Clicked enroll button in-page (text=%r)", label)
        return True
    return False


def _try_click(page, element, why: str) -> bool:
    """
    Click an element, falling back to a JS click if the normal one is blocked.

    Playwright's click() performs actionability checks and will refuse if
    another node (a sticky footer, a cookie banner, an invisible overlay)
    covers the target. dispatchEvent bypasses that, which is what we want for
    a button we have already confirmed is visible and enabled.
    """
    try:
        element.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass
    try:
        element.click(timeout=5_000)
        logger.info("Clicked enroll button via %s", why)
        return True
    except Exception as exc:
        logger.info("Normal click failed on %s (%s) — trying JS click", why, exc)
    try:
        element.evaluate("el => el.click()")
        logger.info("Clicked enroll button via %s (JS)", why)
        return True
    except Exception as exc:
        logger.info("JS click also failed on %s: %s", why, exc)
    return False


def _click_enroll(page, attempts: int = 2) -> bool:
    """
    Find the real "Enroll now" button and click it.

    SPEED NOTE: an earlier version used attempts=3 with an 8s visibility wait
    plus a 10s "wait for enabled" loop on EACH of 8 selectors. Worst case that
    is 3 x 8 x 18s = ~7 MINUTES of dead waiting whenever no button exists —
    which is the normal case for express checkout, since it completes the free
    order by itself. Timeouts here are now tight, and the caller polls
    ownership instead of relying on this function.
    """
    for attempt in range(1, attempts + 1):

        # ── Pass 0: in-page JS scan. Fast, and it actually works. ────────
        # This is now the primary path; the locator passes below are only a
        # safety net for the case where page.evaluate is unavailable.
        if _js_click_enroll(page):
            return True

        # ── Pass 1: known selectors, checking EVERY match, not just .first ──
        #
        # THE BUG THIS REPLACES: this used page.locator(sel).first. Udemy
        # renders ~17 buttons including hidden desktop/mobile duplicates, so
        # .first regularly resolved to a HIDDEN copy of "Enroll now". The code
        # then waited for that hidden element to become visible, timed out, and
        # moved on — while the real, visible, enabled button sat untouched.
        for selector in ENROLL_SELECTORS:
            try:
                loc = page.locator(selector)
                total = loc.count()
                for i in range(min(total, 8)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible() or not el.is_enabled():
                            continue
                        label = (el.inner_text() or "").strip().replace("\n", " ")[:40]
                        if _try_click(page, el, f"{selector} [{i}] {label!r}"):
                            return True
                    except Exception:
                        continue
            except Exception as exc:
                logger.debug("Selector %s unusable: %s", selector, exc)
                continue

        # ── Pass 2: text scan over every button on the page ──────────────
        # This is exactly what _dump_diagnostics() does, and it is what found
        # the real button when pass 1 missed it. Belt and braces.
        try:
            for el in page.locator("button").all():
                try:
                    if not el.is_visible() or not el.is_enabled():
                        continue
                    text = (el.inner_text() or "").strip().lower()
                    if any(t in text for t in ENROLL_TEXTS):
                        if _try_click(page, el, f"text scan {text[:30]!r}"):
                            return True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Button text scan failed: %s", exc)

        if attempt < attempts:
            logger.info("No clickable enroll button yet (attempt %d/%d)", attempt, attempts)
            page.wait_for_timeout(2000)

    return False


def _poll_ownership(course_id: int, timeout_s: int = 25, interval_s: int = 2) -> bool:
    """
    Poll Udemy until the course shows up as owned, or we give up.

    Express checkout finalises the free order asynchronously — a single
    ownership check immediately after page load is often a fraction too early,
    while waiting a flat 30s wastes time when it lands in 4s. Polling gets the
    answer as soon as it is true and costs nothing when it already is.
    """
    import time

    deadline = time.monotonic() + timeout_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if _verify_enrolled(course_id):
            logger.info("Ownership confirmed after %d check(s)", attempt)
            return True
        time.sleep(interval_s)
    return False


def _safe_screenshot(page, path: str) -> None:
    """
    Take a diagnostic screenshot WITHOUT ever being able to break the run.

    Playwright's screenshot() blocks on "waiting for fonts to load", which on a
    minimal server can hang until its 30s timeout and then raise. That
    exception used to escape browser_enroll() entirely and crash the caller —
    turning a completed enrollment into a stack trace. A screenshot is a
    debugging nicety; it must never decide the outcome.
    """
    try:
        page.screenshot(path=path, timeout=8_000, animations="disabled")
        logger.info("Saved diagnostic screenshot: %s", path)
    except Exception as exc:
        logger.debug("Screenshot %s skipped (%s)", path, exc)


def _verify_enrolled(course_id: int) -> bool:
    """
    Ask Udemy's API whether the account now owns this course.

    This is GROUND TRUTH and outranks anything the page did or didn't do.
    Udemy's express checkout completes a 100%-off order on page load and then
    redirects — often with no button to press at all — so "we never found an
    Enroll button" absolutely does not mean "we failed to enroll".
    """
    try:
        from utils.udemy import is_already_enrolled, _build_headers
        return is_already_enrolled(course_id, _build_headers())
    except Exception as exc:
        logger.warning("Post-enroll ownership check failed: %s", exc)
        return False


def _dump_diagnostics(page) -> None:
    """
    Record exactly what the server's browser was looking at when it gave up.

    Without this, a failure tells us only "no button" — which is useless for
    deciding whether the page was still loading, showed an error, or rendered
    a button shape our selectors don't match. Writes enroll_failed.png and
    enroll_failed.html next to the script, and logs every visible button.
    """
    try:
        logger.error("Final URL   : %s", page.url)
        logger.error("Page title  : %s", page.title())
    except Exception:
        pass

    try:
        buttons = page.locator("button").all()
        logger.error("Visible buttons on the page (%d total):", len(buttons))
        shown = 0
        for btn in buttons:
            if shown >= 15:
                break
            try:
                if not btn.is_visible():
                    continue
                text = (btn.inner_text() or "").strip().replace("\n", " ")[:45]
                state = "disabled" if btn.is_disabled() else "ENABLED"
                logger.error("    [%-8s] %r", state, text)
                shown += 1
            except Exception:
                continue
        if shown == 0:
            logger.error("    (none visible — page probably still loading or blocked)")
    except Exception as exc:
        logger.error("Could not enumerate buttons: %s", exc)

    _safe_screenshot(page, "enroll_failed.png")
    try:
        with open("enroll_failed.html", "w", encoding="utf-8") as fh:
            fh.write(page.content())
        logger.error("Saved page HTML to enroll_failed.html")
    except Exception:
        pass


def _read_error_toast(page) -> str | None:
    """
    Read Udemy's inline failure message, e.g.
    "We couldn't complete this purchase. Please try again."
    Udemy shows this instead of navigating when it rejects an order.
    """
    for selector in ['[role="alert"]', '.ud-alert', '[data-purpose="checkout-message"]']:
        try:
            node = page.locator(selector).first
            if node.count() and node.is_visible():
                text = (node.inner_text() or "").strip()
                if text:
                    return text.replace("\n", " ")[:200]
        except Exception:
            continue
    return None


def browser_enroll(course_id: int, slug: str, coupon: str | None) -> str:
    """Serialised entry point — see _BROWSER_LOCK above."""
    waiting = not _BROWSER_LOCK.acquire(blocking=False)
    if waiting:
        logger.info("Another enrollment is using the browser — queueing behind it")
        _BROWSER_LOCK.acquire()
    try:
        return _browser_enroll_locked(course_id, slug, coupon)
    finally:
        _BROWSER_LOCK.release()


def _browser_enroll_locked(course_id: int, slug: str, coupon: str | None) -> str:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.error("Playwright not installed.")
        return STATUS_ERROR

    # Apply playwright-stealth v2.x if available
    stealth_obj = None
    try:
        from playwright_stealth import Stealth
        stealth_obj = Stealth(
            navigator_webdriver=True,   # Hide webdriver flag
            chrome_app=True,
            chrome_csi=True,
            chrome_load_times=True,
            navigator_plugins=True,
            navigator_languages=True,
            media_codecs=True,
            hairline=True,
        )
        logger.info("playwright-stealth v2.x loaded")
    except Exception as e:
        logger.warning("playwright-stealth not available: %s", e)

    # Parameter name matters: Udemy's real express-checkout URL (captured from
    # a genuine browser session) uses couponCode=, NOT discountCode=. With the
    # wrong name the coupon is silently ignored and the cart is full price.
    checkout_url = (
        f"https://www.udemy.com/payment/checkout/express/course/{course_id}/"
        f"?ref=course_landing_page&checkout_type=express"
        f"&return_path=%2Fcourse%2F{slug}%2F&course_id={course_id}"
    )
    if coupon:
        checkout_url += f"&couponCode={coupon}"

    logger.info("browser_enroll: course_id=%s slug=%s coupon=%s", course_id, slug, coupon)
    t0 = time.monotonic()
    # Headless Chrome is trivially detectable and Cloudflare challenges it on
    # sight. Under Xvfb we run a REAL windowed Chrome (see the setup steps in
    # ORACLE_DEPLOYMENT_GUIDE.md §16), which clears the challenge normally.
    # Set UDEMY_BROWSER_HEADLESS=1 to force headless for debugging.
    headless = os.getenv("UDEMY_BROWSER_HEADLESS", "").strip() == "1"
    if not headless and not os.getenv("DISPLAY"):
        logger.warning(
            "No DISPLAY set — falling back to headless, which Cloudflare will "
            "almost certainly challenge. Install Xvfb and run under "
            "xvfb-run (see ORACLE_DEPLOYMENT_GUIDE.md section 16)."
        )
        headless = True

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)

    with sync_playwright() as p:
        # launch_persistent_context (not launch + new_context): the profile on
        # disk is what lets the cf_clearance this browser earns SURVIVE between
        # runs, so the Cloudflare challenge is solved once, not every time.
        context = p.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized",
            ],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            # NOTE: user_agent is deliberately NOT overridden. Chromium's own
            # UA matches its real engine, platform and TLS fingerprint. The
            # previous code forced UDEMY_USER_AGENT here, which on this server
            # advertised "Android 15; Pixel 9 ... Mobile Safari" from a desktop
            # Linux Chromium — a contradiction Cloudflare flags instantly.
        )
        browser = context  # persistent context owns the browser lifetime
        context.add_cookies(_get_cookies())
        page = context.pages[0] if context.pages else context.new_page()

        # playwright-stealth 2.x: apply per PAGE. (The old code called
        # hook_playwright_context(context), which expects the playwright
        # instance, not a BrowserContext — hence the
        # "'BrowserContext' object has no attribute 'chromium'" warning.)
        if stealth_obj is not None:
            try:
                stealth_obj.apply_stealth_sync(page)
                logger.info("Stealth applied to page")
            except Exception as exc:
                logger.warning("Could not apply stealth: %s", exc)

        # Watch for the checkout API calls so we can explain failures precisely
        seen = {"cart_status": None, "cart_price": None, "order_status": None,
                "order_body": None, "succeeded": False}

        def on_response(resp):
            url = resp.url
            try:
                if "shopping-carts/me/express" in url and resp.request.method == "POST":
                    seen["cart_status"] = resp.status
                    data = resp.json()
                    entry = (data.get("express") or [{}])[0]
                    pp = entry.get("purchase_price") or {}
                    seen["cart_price"] = pp.get("amount")
                    logger.info("Express cart: HTTP %s price=%s", resp.status, seen["cart_price"])
                elif "/payment/checkout-submit" in url or "/orders/" in url:
                    seen["order_status"] = resp.status
                    body = resp.text()[:600]
                    seen["order_body"] = body
                    # Udemy answers the submit with {"status":"succeeded", ...}.
                    # This is the EARLIEST and most authoritative confirmation
                    # available — earlier than the SPA redirect and earlier than
                    # the ownership API. Previously it was logged and ignored,
                    # costing ~24s of pointless waiting after every success.
                    if '"succeeded"' in body:
                        seen["succeeded"] = True
                    logger.info("Checkout submit: HTTP %s | %s", resp.status, body[:200])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
        except PWTimeout:
            logger.warning("Checkout URL load timed out — continuing anyway")

        # Cloudflare may interpose its challenge before the checkout page
        # renders. A real windowed Chrome on this machine's own IP clears it
        # by itself; wait that out and let the profile keep the clearance.
        if not _wait_out_cloudflare(page):
            _safe_screenshot(page, "enroll_cloudflare.png")
            browser.close()
            return STATUS_ERROR

        # Let the checkout SPA hydrate (it fetches pricing before enabling the button)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass

        current_url = page.url
        logger.info("Landed on: %s (%.0fs elapsed)", current_url, time.monotonic() - t0)

        if "login" in current_url or "/join/" in current_url:
            browser.close()
            return STATUS_TOKEN_EXPIRED

        if _is_success(current_url):
            logger.info("Already redirected to success page — enrollment complete")
            browser.close()
            return STATUS_SUCCESS
        if _is_logged_out(page):
            logger.error(
                "Checkout page rendered LOGGED OUT — Udemy did not recognise the "
                "session cookies. Refresh them with apply_cookies.py "
                "(access_token / dj_session_id / ud_user_jwt) and re-run."
            )
            _safe_screenshot(page, "enroll_logged_out.png")
            browser.close()
            return STATUS_TOKEN_EXPIRED

        if _is_cloudflare_challenge(page):
            logger.error(
                "Still on a Cloudflare challenge. Run under Xvfb so Chrome is "
                "a real windowed browser (ORACLE_DEPLOYMENT_GUIDE.md section 16)."
            )
            _safe_screenshot(page, "enroll_cloudflare.png")
            browser.close()
            return STATUS_ERROR

        # ── Adaptive enroll loop ─────────────────────────────────────────
        #
        # Why a loop instead of "check once, click once, check once":
        #
        #   * Some courses are finalised by express checkout on its own within
        #     seconds and never show a button at all.
        #   * Others DO render an "Enroll now" button, but only after the SPA
        #     finishes fetching pricing — which can take a while on a small VM.
        #
        # A fixed sequence can only ever suit one of those. This loop keeps
        # alternating "am I enrolled yet?" with "is the button clickable yet?"
        # until either succeeds or the deadline expires. It exits in ~10s on
        # the fast path and stays patient on the slow one, instead of the old
        # behaviour of burning ~7 minutes of fixed timeouts on every course.
        deadline = t0 + ENROLL_DEADLINE_S
        round_no = 0

        while time.monotonic() < deadline:
            round_no += 1
            elapsed = time.monotonic() - t0
            logger.info("Enroll round %d (%.0fs elapsed, %.0fs left)",
                        round_no, elapsed, deadline - time.monotonic())

            if seen.get("succeeded"):
                logger.info("Udemy checkout-submit already reported success (%.0fs)",
                            time.monotonic() - t0)
                browser.close()
                return STATUS_SUCCESS

            if _verify_enrolled(course_id):
                logger.info("ENROLLED — confirmed by Udemy after %.0fs (round %d)",
                            elapsed, round_no)
                browser.close()
                return STATUS_SUCCESS

            if _is_success(page.url):
                logger.info("Success URL reached after %.0fs: %s", elapsed, page.url)
                browser.close()
                return STATUS_SUCCESS

            # One pass over the selectors — cheap, because absent selectors are
            # skipped instantly via count().
            if _click_enroll(page, attempts=1):
                logger.info("Clicked enroll at %.0fs — waiting for confirmation",
                            time.monotonic() - t0)
                # Poll the three success signals once a second rather than
                # blocking on a single 12s wait_for_url that usually times out
                # even when the order already went through.
                for _ in range(15):
                    if seen.get("succeeded"):
                        logger.info("Udemy confirmed checkout succeeded (%.0fs)",
                                    time.monotonic() - t0)
                        browser.close()
                        return STATUS_SUCCESS
                    if _is_success(page.url):
                        logger.info("Redirected to success page (%.0fs)",
                                    time.monotonic() - t0)
                        browser.close()
                        return STATUS_SUCCESS
                    page.wait_for_timeout(1000)

            message = _read_error_toast(page)
            if message:
                logger.warning("Checkout message: %s", message)
                low = message.lower()
                if "coupon" in low or "discount" in low or "no longer" in low:
                    _safe_screenshot(page, "enroll_failed.png")
                    browser.close()
                    return STATUS_EXPIRED

            if seen["order_status"] not in (None, 200, 201):
                logger.warning("checkout-submit HTTP %s | %s",
                               seen["order_status"], seen["order_body"])

            page.wait_for_timeout(ENROLL_POLL_GAP_MS)

            # Every 5th round, reload the checkout page. Udemy's SPA sometimes
            # settles into a state where the button never enables; a fresh load
            # re-runs the express-cart call and usually clears it.
            if round_no % 5 == 0 and time.monotonic() < deadline - 20:
                logger.info("Reloading checkout page (round %d, %.0fs elapsed)",
                            round_no, time.monotonic() - t0)
                try:
                    page.goto(checkout_url, wait_until="domcontentloaded", timeout=20_000)
                    _wait_out_cloudflare(page, timeout_ms=15_000)
                except Exception as exc:
                    logger.debug("Reload failed: %s", exc)

        # ── Deadline reached: record everything, then let ownership decide ──
        logger.error("Enrollment did not complete within %ds", ENROLL_DEADLINE_S)
        _dump_diagnostics(page)
        browser.close()

        if _verify_enrolled(course_id):
            logger.info("Late ownership confirmation — treating as success")
            return STATUS_SUCCESS
        return STATUS_ERROR
