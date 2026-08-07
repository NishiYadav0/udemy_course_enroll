"""
test_scrape.py
--------------
DRY RUN. Shows you exactly what the scraper pulls off a coupon website and
exactly what would happen next — WITHOUT enrolling in anything.

This answers the question "is the link my scraper found the same kind of link
that works when I test manually, and would it actually be sent for enrollment?"

It runs the real pipeline up to (but not including) the enroll call:

    website URL  ─►  scrape Udemy links
                 ─►  parse slug + coupon
                 ─►  ask Udemy the REAL post-coupon price
                 ─►  run the policy guardrails
                 ─►  print WOULD ENROLL / WOULD DROP + the reason
                          ↑
                          └── stops here. Nothing is claimed.

USAGE
-----
    # From an intermediary coupon site (what the bot scrapes):
    python test_scrape.py "https://findmycourse.in/course/SOME-COURSE"

    # From raw Telegram post text (paste it in quotes):
    python test_scrape.py --text "FREE course ... Coupon Code:- AUGFREE03 ... https://freecourse.io/courses/x"

    # From a direct Udemy link (same thing test_enrollment.py takes):
    python test_scrape.py "https://www.udemy.com/course/SLUG/?couponCode=CODE"

    # Force a category instead of auto-detecting it:
    python test_scrape.py "<url>" --category coding

Safe to run at any time, including while the live service is running — it makes
only read-only Udemy API calls and never touches the Telegram session.
"""

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="    | %(levelname)-7s %(message)s",
)

from utils.filter import keyword_match, evaluate_course_policy, DURATION_RULES
from utils.scraper import get_page_html_and_udemy_links, resolve_udemy_links
from utils.udemy import (
    parse_slug_and_coupon,
    get_course_metadata,
    get_coupon_pricing,
    extract_coupon_codes_from_text,
)

SEP = "=" * 72


def collect_links(source: str, is_text: bool) -> list[str]:
    """Get Udemy links the same way main.py would."""
    if is_text:
        print("  Source: raw post text")
        return resolve_udemy_links(source)

    if "udemy.com/course/" in source.lower():
        print("  Source: direct Udemy link (no scraping needed)")
        return [source]

    print(f"  Source: intermediary website → {source}")
    _html, links = get_page_html_and_udemy_links(source)
    return links


def main() -> None:
    ap = argparse.ArgumentParser(description="Dry-run the scrape → policy pipeline")
    ap.add_argument("source", nargs="?", help="Website URL or Udemy URL")
    ap.add_argument("--text", help="Raw Telegram post text instead of a URL")
    ap.add_argument("--category", help="Force a category (default: auto-detect)")
    args = ap.parse_args()

    source = args.text or args.source
    if not source:
        ap.print_help()
        sys.exit(1)

    print(SEP)
    print("  ScholarSync — SCRAPE DRY RUN  (nothing will be enrolled)")
    print(SEP)

    # Catch the documentation placeholders being pasted in literally. Without
    # this you get a confusing "no links found" that looks like a scraper bug
    # when really the URL was never real.
    PLACEHOLDERS = ("SOME-COURSE", "/SLUG/", "couponCode=CODE",
                    "courses/x", "YOUR_", "PASTE_", "<url>", "<link>")
    hit = [p for p in PLACEHOLDERS if p in source]
    if hit:
        print(f"\n  ⚠️  That looks like an EXAMPLE placeholder, not a real link:")
        print(f"      found {hit} in your input\n")
        print("  Replace it with a real URL. Two that are known to work:")
        print("    python test_scrape.py \"https://findmycourse.in/course/governance-risk-compliance-risk-registers\"")
        print("    python test_scrape.py \"https://freecourse.io/courses/lpi-linux-essentials-010-160-exam-questions\"")
        print("\n  Or grab any fresh link straight from one of your Telegram channels.")
        sys.exit(1)

    # ── Step 1: category ────────────────────────────────────────────
    if args.category:
        category = args.category
        print(f"\n[1] Category (forced): {category}")
    elif args.text:
        _matched, category = keyword_match(args.text)
        print(f"\n[1] Category (auto from post text): {category}")
    else:
        category = "other"
        print(f"\n[1] Category: {category}  (no post text given — using the strictest bucket)")
    print(f"    Minimum duration for this category: {DURATION_RULES.get(category, DURATION_RULES['other'])}h")

    # ── Step 2: coupon codes written in the post ────────────────────
    post_codes = extract_coupon_codes_from_text(args.text or "")
    if post_codes:
        print(f"\n[2] Coupon codes found in the post text: {post_codes}")
        print("    (used as fallback if the scraped link's coupon is stale)")
    else:
        print("\n[2] No coupon codes written in the post text")

    # ── Step 3: scrape ──────────────────────────────────────────────
    print("\n[3] Extracting Udemy links...")
    links = collect_links(source, bool(args.text))

    if not links:
        print("\n    ❌ NO Udemy links found.")
        print("    The bot would log 'No Udemy links found' and drop this post.")
        sys.exit(0)

    print(f"\n    Found {len(links)} Udemy link(s):")
    for i, link in enumerate(links, 1):
        print(f"      {i}. {link}")

    # ── Step 4: per-link analysis ───────────────────────────────────
    for i, link in enumerate(links, 1):
        print("\n" + SEP)
        print(f"  LINK {i}/{len(links)}")
        print(SEP)

        slug, coupon = parse_slug_and_coupon(link)
        print(f"\n  Parsed slug   : {slug}")
        print(f"  Parsed coupon : {coupon}")
        print(f"  URL shape     : https://www.udemy.com/course/<slug>/?couponCode=<code>")
        print("                  ^ identical to a link you would test by hand")

        if not slug:
            print("\n  ❌ Could not parse a slug — this link would be skipped.")
            continue

        meta = get_course_metadata(slug, coupon)
        if not meta or meta.get("_token_expired"):
            print("\n  ❌ Metadata fetch failed (or token expired).")
            continue

        print(f"\n  Title      : {meta['title']}")
        print(f"  Duration   : {meta['duration_hours']:.1f}h")
        print(f"  Rating     : {meta['rating']:.1f}/5")
        print(f"  Language   : {meta.get('language')}")
        print(f"  List price : ${meta['price']:.2f}")

        pricing = get_coupon_pricing(meta["course_id"], coupon)
        used_coupon = coupon

        if pricing["ok"] and coupon and not pricing["coupon_valid"] and post_codes:
            for alt in post_codes:
                if alt.upper() == coupon.upper():
                    continue
                print(f"\n  ⚠️  '{coupon}' gives no discount — trying post-text code '{alt}'")
                alt_p = get_coupon_pricing(meta["course_id"], alt)
                if alt_p["ok"] and alt_p["coupon_valid"]:
                    print(f"  ✅ '{alt}' WORKS — the bot would use this instead")
                    pricing, used_coupon = alt_p, alt
                    break

        if not pricing["ok"]:
            print("\n  ⚠️  Coupon pricing lookup failed.")
            continue

        print(f"\n  ── What Udemy says about coupon '{used_coupon}' ──")
        print(f"  Price with coupon : ${pricing['price']:.2f}  ({pricing['discount_percent']}% off)")
        print(f"  Coupon valid      : {pricing['coupon_valid']}")
        print(f"  Uses remaining    : {pricing['uses_remaining']}")
        print(f"  Already owned     : {pricing['already_owned']}")

        if pricing["already_owned"]:
            print("\n  ⏩ VERDICT: ALREADY OWNED — the bot would skip it quietly.")
            continue

        should, reason = evaluate_course_policy(
            title=meta["title"],
            duration_hours=meta["duration_hours"],
            rating=meta["rating"],
            is_paid=meta["is_paid"],
            price=pricing["price"],
            badges=meta["badges"],
            category=category,
            language=meta.get("language", "en"),
        )

        print(f"\n  ── Policy ──\n  {reason}")
        if should:
            print("\n  ✅ VERDICT: WOULD ENROLL")
            print(f"     The bot would send this to enrollment:")
            print(f"     https://www.udemy.com/course/{slug}/?couponCode={used_coupon}")
            print("     → express checkout → clicks 'Enroll now' → alert")
        else:
            print("\n  ❌ VERDICT: WOULD DROP  (reason above)")
            if "Coupon expired" in reason and meta["duration_hours"] > 5:
                print("     → but it IS longer than 5h, so it goes to the retry queue")
                print("       (rechecked every 15 min for up to 3 hours)")

    print("\n" + SEP)
    print("  Dry run complete. Nothing was enrolled.")
    print("  To actually enroll one of the links above:")
    print('    xvfb-run -a python test_enrollment.py "<link>"')
    print(SEP)


if __name__ == "__main__":
    main()
