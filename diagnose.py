"""
diagnose.py
-----------
ScholarSync Diagnostic Tool

Run this AFTER stopping main.py (Ctrl+C).

Tests:
  1. Correct channel IDs for @Udemyfree4_u and @Udemy4
  2. Scraper on real freecourse.io / findmycourse.in URLs
  3. Udemy access token validity
"""

import os
import re
import sys
import asyncio
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

SEP = "=" * 65

print(SEP)
print("  ScholarSync Diagnostic Tool")
print(SEP)

# ─────────────────────────────────────────────────────────────
# PART 1 — Resolve Channel IDs
# ─────────────────────────────────────────────────────────────
print("\n[1] Resolving real channel IDs for your target channels...")

API_ID   = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

TARGET_USERNAMES = [
    "Udemyfree4_u",
    "Udemy4",
]

from pyrogram import Client

async def resolve_channels():
    try:
        async with Client(
            "scholarsync_session",
            api_id=API_ID,
            api_hash=API_HASH,
        ) as app:
            print(f"\n  {'Username':<20} {'Channel ID (use this in .env)':<28} {'Title'}")
            print(f"  {'-'*20} {'-'*28} {'-'*30}")
            ids_found = []
            for uname in TARGET_USERNAMES:
                try:
                    chat  = await app.get_chat(uname)
                    cid   = chat.id
                    title = (chat.title or chat.username or "")[:30]
                    print(f"  @{uname:<19} {cid:<28} {title}")
                    ids_found.append(str(cid))
                except Exception as e:
                    print(f"  @{uname:<19} ERROR → {e}")

            if ids_found:
                print()
                print("  ✅ Paste this into your .env file:")
                print(f"  TARGET_CHANNELS={','.join(ids_found)}")
            return ids_found

    except Exception as e:
        err = str(e)
        if "database is locked" in err:
            print()
            print("  ⚠️  SESSION LOCKED — main.py is still running!")
            print("  → Stop main.py first (Ctrl+C), then re-run diagnose.py")
            print("  → Skipping channel ID step, continuing with other tests...")
        else:
            print(f"  ❌ Pyrogram error: {e}")
        return []

ids = asyncio.run(resolve_channels())

# ─────────────────────────────────────────────────────────────
# PART 2 — Scraper tests on real URLs from your screenshots
# ─────────────────────────────────────────────────────────────
print()
print(SEP)
print("[2] Testing scraper on URLs from your screenshots...")
print(SEP)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TEST_URLS = [
    # Screenshot 2 — freecourse.io link directly in post text
    "https://freecourse.io/courses/governance-risk-compliance-risk-registers",
    # General findmycourse.in domain test
    "https://findmycourse.in",
]

def test_url(url: str):
    print(f"\n  Testing: {url}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        print(f"  HTTP Status : {resp.status_code}")
        print(f"  Final URL   : {resp.url}")

        if resp.status_code != 200:
            print("  ❌ Page not reachable")
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = resp.text

        # 1. Find <a href> tags pointing to udemy.com
        udemy_links = []
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if "udemy.com" in href.lower():
                udemy_links.append(href)

        # 2. Find coupon codes anywhere in the HTML
        coupons = re.findall(r'couponCode=([A-Z0-9a-z\-_]+)', page_text)
        # 3. Find raw udemy.com URLs in HTML (even without <a> tag)
        raw_udemy = re.findall(r'https://[^\s"\'<>]*udemy\.com[^\s"\'<>]*', page_text)

        if udemy_links:
            print(f"  ✅ Udemy links in <a> tags ({len(udemy_links)}):")
            for lnk in udemy_links[:3]:
                print(f"     → {lnk[:110]}")
        else:
            print("  ❌ No Udemy <a href> links found")

        if raw_udemy:
            print(f"  ✅ Raw Udemy URLs in HTML ({len(raw_udemy)}):")
            for u in raw_udemy[:3]:
                print(f"     → {u[:110]}")
        else:
            print("  ❌ No raw Udemy URLs in page HTML either")

        if coupons:
            print(f"  🎟  Coupon codes found: {list(set(coupons))[:3]}")
        else:
            print("  ⚠️  No couponCode= parameters found in HTML")

        # Print all href attributes for deeper analysis
        all_hrefs = [t["href"] for t in soup.find_all("a", href=True)]
        print(f"\n  ℹ️  Total <a href> tags: {len(all_hrefs)}")
        if all_hrefs:
            print("  ℹ️  All hrefs (first 10):")
            for h in all_hrefs[:10]:
                print(f"     {h[:100]}")

    except requests.exceptions.ConnectionError:
        print("  ❌ Could not connect — check internet / site may be down")
    except Exception as e:
        print(f"  ❌ Error: {e}")

for url in TEST_URLS:
    test_url(url)

# ─────────────────────────────────────────────────────────────
# PART 3 — Udemy Token Test
# ─────────────────────────────────────────────────────────────
print()
print(SEP)
print("[3] Testing Udemy access token...")
print(SEP)

token = os.getenv("UDEMY_ACCESS_TOKEN", "").strip()
if not token.startswith("Bearer "):
    token = f"Bearer {token}"

api_headers = {
    "Authorization": token,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# Test with a known popular course slug
test_slug = "100-days-of-code"
url = (
    f"https://www.udemy.com/api-2.0/courses/{test_slug}/?"
    f"fields[course]=id,title,is_paid,price_detail,locale"
)

print(f"\n  Token prefix : {token[:30]}...")
print(f"  Test API URL : {url[:80]}...")

try:
    resp = requests.get(url, headers=api_headers, timeout=12)
    print(f"  HTTP Status  : {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"\n  ✅ TOKEN IS VALID!")
        print(f"  Course found : {data.get('title', 'N/A')}")
        print(f"  is_paid      : {data.get('is_paid', 'N/A')}")
        locale_raw = data.get('locale') or {}
        if isinstance(locale_raw, dict):
            locale_code = locale_raw.get('locale', 'N/A')
        else:
            locale_code = str(locale_raw)
        print(f"  locale       : {locale_code}")

    elif resp.status_code == 401:
        print()
        print("  ❌ TOKEN EXPIRED (401 Unauthorized)")
        print("  → Steps to get a new token:")
        print("    1. Open Chrome and log in to udemy.com")
        print("    2. Press F12 → Application tab → Cookies → udemy.com")
        print("    3. Find the cookie named: access_token")
        print("    4. Copy its value and paste in .env as:")
        print("       UDEMY_ACCESS_TOKEN=Bearer <paste_value_here>")

    elif resp.status_code == 404:
        print("  ⚠️  Course slug not found (but 404 ≠ expired — auth passed)")
        print("  ✅ Token appears valid (no 401)")

    else:
        print(f"  ⚠️  Unexpected status {resp.status_code}")
        print(f"  Response: {resp.text[:300]}")

except Exception as e:
    print(f"  ❌ Error contacting Udemy API: {e}")

# ─────────────────────────────────────────────────────────────
# PART 4 — Current .env summary
# ─────────────────────────────────────────────────────────────
print()
print(SEP)
print("[4] Current .env configuration summary")
print(SEP)
current_channels = os.getenv("TARGET_CHANNELS", "NOT SET")
alert_ch         = os.getenv("ALERT_CHANNEL_ID", "NOT SET")
print(f"\n  TARGET_CHANNELS  (current) : {current_channels}")
print(f"  ALERT_CHANNEL_ID (current) : {alert_ch}")
if ids:
    print(f"\n  TARGET_CHANNELS  (correct) : {','.join(ids)}")
    if current_channels.strip() != ','.join(ids):
        print("  ⚠️  MISMATCH — bot is listening to the WRONG channels!")
    else:
        print("  ✅ Channel IDs are correct")

print()
print(SEP)
print("  Diagnosis complete — share the full output above.")
print(SEP)
