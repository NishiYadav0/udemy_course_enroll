"""
test_enrollment.py
-------------------
Direct enrollment test — bypasses EVERY guardrail (category, duration,
rating, language, price pre-check) and attempts the REAL Udemy
enrollment call for exactly one URL you provide. Use this to confirm,
with total certainty, whether:

  1. Your UDEMY_ACCESS_TOKEN is valid and correctly authenticated.
  2. A specific coupon code is genuinely accepted by Udemy's own
     checkout endpoint (not just "looks free" in the metadata call,
     which does NOT apply the coupon — only this actual call does).
  3. Enrollment really completes (or tells you exactly why not).

This does NOT touch main.py's policy engine at all — it's a raw,
minimal test of Layer 3 (metadata) + Layer 5 (auto-enroll) only.

Run (from the project folder, with your real .env already set up):
    python test_enrollment.py "https://www.udemy.com/course/SLUG/?couponCode=CODE"

Safe to run any time — it does NOT run while main.py's systemd service
is also active on the same course link, since Udemy simply reports
"already enrolled" if you already own it (no duplicate charge/action
risk — coupon-based enrollment isn't a purchase, and repeating this
test on an already-claimed course is harmless).
"""

import logging
import sys

from dotenv import load_dotenv

load_dotenv()

# Without this, Python's root logger defaults to WARNING and every logger.info()
# in utils/enroll_browser.py is silently discarded — which made the enrollment
# step look like a black box. Show the play-by-play instead.
logging.basicConfig(
    level=logging.INFO,
    format="    | %(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)

from utils.udemy import (
    parse_slug_and_coupon,
    get_course_metadata,
    get_coupon_pricing,
    is_already_enrolled,
    auto_enroll,
    _build_headers,
    _build_session,
    get_udemy_cookies,
    STATUS_SUCCESS,
    STATUS_ALREADY_OWNED,
    STATUS_EXPIRED,
    STATUS_TOKEN_EXPIRED,
    STATUS_ERROR,
)

SEP = "=" * 70


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python test_enrollment.py \"<udemy-course-url-with-coupon>\"")
        print("Example:")
        print('  python test_enrollment.py "https://www.udemy.com/course/'
              'some-course/?couponCode=ABCDEF1234"')
        sys.exit(1)

    url = sys.argv[1]

    print(SEP)
    print("  ScholarSync — Direct Enrollment Test")
    print("  (bypasses ALL policy guardrails — tests the raw Udemy call)")
    print(SEP)

    # ── Step 1: Parse slug + coupon from the URL ────────────────────
    slug, coupon = parse_slug_and_coupon(url)
    print(f"\n[1] Parsed from URL:")
    print(f"    slug   = {slug!r}")
    print(f"    coupon = {coupon!r}")
    if not slug:
        print("\n❌ Could not parse a course slug from that URL — check the format.")
        sys.exit(1)
    if not coupon:
        print("\n⚠️  No coupon code found in that URL — this course would be")
        print("    enrolled at its normal listed price, which is almost")
        print("    certainly not what you want to test. Double-check the URL.")

    # ── Step 2: Fetch metadata (informational only, NOT a gate here) ─
    print(f"\n[2] Fetching course metadata from Udemy...")
    meta = get_course_metadata(slug, coupon)

    if not meta:
        print("\n❌ Metadata fetch FAILED — could not reach Udemy or course")
        print("   not found. Check your internet connection and the slug.")
        sys.exit(1)

    if meta.get("_token_expired"):
        print("\n[!] TOKEN EXPIRED — your UDEMY_ACCESS_TOKEN in .env is no")
        print("   longer valid. Refresh it: udemy.com → F12 → Application →")
        print("   Cookies → access_token, then update .env.")
        sys.exit(1)

    print(f"    [OK] Title           : {meta['title']}")
    print(f"    [OK] Duration        : {meta['duration_hours']:.1f} hours")
    print(f"    [OK] Rating          : {meta['rating']:.1f}/5")
    print(f"    [OK] Language        : {meta.get('language', '?')}")
    print(f"    [OK] Is paid course  : {meta['is_paid']}")
    print(f"    [!]  List price      : ${meta['price']:.2f}  (undiscounted)")

    # ── Step 2b: Cookie sanity check ────────────────────────────────
    jar = get_udemy_cookies()
    required = ["access_token", "csrftoken", "dj_session_id", "ud_user_jwt", "cf_clearance"]
    print(f"\n[2b] Session cookies loaded from .env:")
    for name in required:
        print(f"    {'[OK]' if name in jar else '[MISSING]'} {name}")
    if any(n not in jar for n in required):
        print("    [!] Missing cookies mean Udemy's CHECKOUT page will see you as")
        print("        logged out, and enrollment cannot complete. See")
        print("        config/.env.example for how to capture each one.")

    # ── Step 3: Coupon-aware price + ownership (the real check) ─────
    print(f"\n[3] Asking Udemy what this course costs WITH the coupon applied...")
    pricing = get_coupon_pricing(meta["course_id"], coupon)

    if not pricing["ok"]:
        print("    [!] Coupon pricing lookup failed -- falling back to ownership check.")
        if is_already_enrolled(meta["course_id"], _build_headers()):
            print("    [OK] You ALREADY OWN this course.")
            return
    else:
        print(f"    List price      : ${pricing['list_price']:.2f}")
        print(f"    Price w/ coupon : ${pricing['price']:.2f} "
              f"({pricing['discount_percent']}% off)")
        print(f"    Coupon valid    : {pricing['coupon_valid']}")
        print(f"    Uses remaining  : {pricing['uses_remaining']}")
        print(f"    Coupon expires  : {pricing['coupon_end_time']}")
        print(f"    Already owned   : {pricing['already_owned']}")

        if pricing["already_owned"]:
            print("\n" + SEP)
            print("  RESULT: ALREADY ENROLLED -- nothing further to test here.")
            print(SEP)
            return
        if coupon and not pricing["coupon_valid"]:
            print("\n" + SEP)
            print("  RESULT: COUPON EXPIRED / INVALID -- Udemy does not recognise it.")
            print(SEP)
            return
        if pricing["price"] > 0.50:
            print("\n" + SEP)
            print(f"  RESULT: NOT FREE -- costs ${pricing['price']:.2f} with the coupon.")
            print(SEP)
            return
        print("    [->] Coupon is LIVE and brings this to $0.00 -- proceeding to enroll.")

    headers = _build_headers()

    # ── Step 4: THE REAL TEST — actual enrollment call ──────────────
    print(f"\n[4] Submitting the REAL enrollment request to Udemy")
    print(f"    (course_id={meta['course_id']}, coupon={coupon!r})...")
    from utils.udemy import _build_session
    enroll_session = _build_session()
    # slug matters: the Playwright fallback needs it to build the correct
    # express-checkout return_path. Without it the fallback used the numeric
    # id as the slug and Udemy bounced the redirect.
    status = auto_enroll(meta["course_id"], coupon, enroll_session, slug=slug)

    print("\n" + SEP)
    if status == STATUS_SUCCESS:
        print("  [OK] RESULT: SUCCESS -- the coupon WAS sent to Udemy and")
        print("  enrollment genuinely completed. Go check 'My Courses' on")
        print("  udemy.com -- this course should now be there.")
    elif status == STATUS_ALREADY_OWNED:
        print("  [->] RESULT: Udemy says you already own this (race with Step 3,")
        print("  or ownership changed between the two calls). Either way,")
        print("  no error -- the account already has this course.")
    elif status == STATUS_EXPIRED:
        print("  [X]  RESULT: Udemy's REAL checkout rejected this coupon --")
        print("  it is genuinely expired or invalid. This is the authoritative")
        print("  answer (unlike the metadata price-check, this call actually")
        print("  used the coupon code).")
    elif status == STATUS_TOKEN_EXPIRED:
        print("  [!]  RESULT: Your access token expired between Step 2 and here.")
        print("  Refresh it in .env and re-run this test.")
    else:
        print(f"  [!]  RESULT: {status} -- check the terminal output above this")
        print("  line for the exact HTTP response Udemy returned.")
    print(SEP)


if __name__ == "__main__":
    main()
