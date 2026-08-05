"""
test_pipeline.py
-----------------
Diagnostic script — run this AFTER stopping main.py (Ctrl+C).
Both scripts share the same Pyrogram session file, and SQLite will
throw "database is locked" if both run at the same time.

This answers three questions WITHOUT waiting for a live post to arrive:

  1. Is your account actually a joined dialog-participant of the two
     target channels? (This is what determines whether Telegram pushes
     you live updates for them at all — separate from whether you can
     merely *view* a public channel.)
  2. Can we pull the real, current latest posts from those channels
     right now, on demand?
  3. Does the full pipeline (keyword filter -> scraper -> Udemy
     metadata -> policy -> enroll attempt) correctly process a REAL
     captured post end-to-end?

Run:
    python test_pipeline.py
"""

import asyncio
import os

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import UserNotParticipant

load_dotenv()

API_ID   = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TARGET_CHANNELS = [
    int(x.strip()) for x in os.getenv("TARGET_CHANNELS", "").split(",") if x.strip()
]

from utils.filter import keyword_match
from utils.scraper import resolve_udemy_links, get_udemy_links_from_page
from utils.udemy import process_udemy_link

app = Client("scholarsync_session", api_id=API_ID, api_hash=API_HASH)


def extract_button_urls(message) -> list[str]:
    urls: list[str] = []
    markup = message.reply_markup
    if markup and hasattr(markup, "inline_keyboard"):
        for row in markup.inline_keyboard:
            for btn in row:
                url = getattr(btn, "url", None)
                if url:
                    urls.append(url)
    return urls


async def main() -> None:
    # Playwright's sync API refuses to run in any thread that has an
    # active asyncio event loop (which this whole script's main thread
    # does). main.py avoids this by running scraper/Udemy calls via
    # loop.run_in_executor(...) — a separate worker thread with no
    # event loop. We do the same here so this diagnostic accurately
    # mirrors production behavior instead of tripping Playwright's guard.
    loop = asyncio.get_event_loop()

    async with app:
        print("=" * 70)
        print("STEP 1 — Is each target channel in your account's dialog list?")
        print("=" * 70)
        dialog_ids = set()
        async for dialog in app.get_dialogs():
            dialog_ids.add(dialog.chat.id)
        print(f"  (Total dialogs synced: {len(dialog_ids)})")
        for ch_id in TARGET_CHANNELS:
            in_dialogs = ch_id in dialog_ids
            tag = "✅ IS in your dialog list" if in_dialogs else "❌ NOT in your dialog list — THIS IS LIKELY THE BUG"
            print(f"  Channel {ch_id}: {tag}")

        print()
        print("=" * 70)
        print("STEP 2 — Direct membership check via get_chat_member('me')")
        print("=" * 70)
        for ch_id in TARGET_CHANNELS:
            try:
                member = await app.get_chat_member(ch_id, "me")
                print(f"  Channel {ch_id}: ✅ You are a member — status: {member.status}")
            except UserNotParticipant:
                print(f"  Channel {ch_id}: ❌ UserNotParticipant — you are NOT a member of this channel!")
            except Exception as exc:
                print(f"  Channel {ch_id}: ⚠️  Could not check — {exc}")

        print()
        print("=" * 70)
        print("STEP 3 — Pulling the 3 most recent posts from each channel (on demand)")
        print("=" * 70)
        for ch_id in TARGET_CHANNELS:
            print(f"\n--- Channel {ch_id} ---")
            try:
                async for msg in app.get_chat_history(ch_id, limit=3):
                    text = msg.text or msg.caption or "[no text]"
                    btn_urls = extract_button_urls(msg)
                    print(f"  [msg_id={msg.id}] date={msg.date} {text[:100]!r}")
                    if btn_urls:
                        print(f"    button urls: {btn_urls}")
                    # Inspect entities for hidden hyperlinks (text_link) —
                    # these carry a URL that never appears in the visible
                    # text and are NOT inline keyboard buttons either.
                    entities = (msg.entities or []) + (msg.caption_entities or [])
                    for ent in entities:
                        ent_url = getattr(ent, "url", None)
                        print(f"    entity: type={ent.type} offset={ent.offset} length={ent.length} url={ent_url}")
            except Exception as exc:
                print(f"  ⚠️  Could not fetch history: {exc}")

        print()
        print("=" * 70)
        print("STEP 4 — Running the FULL pipeline on the latest real post of each channel")
        print("=" * 70)
        for ch_id in TARGET_CHANNELS:
            print(f"\n--- Channel {ch_id} ---")
            try:
                async for msg in app.get_chat_history(ch_id, limit=1):
                    post_text = msg.text or msg.caption or ""
                    button_urls = extract_button_urls(msg)
                    print(f"  Post date   : {msg.date}")
                    print(f"  Post text (FULL): {post_text!r}")
                    print(f"  Button urls : {button_urls}")
                    entities = (msg.entities or []) + (msg.caption_entities or [])
                    entity_urls = [getattr(e, "url", None) for e in entities if getattr(e, "url", None)]
                    print(f"  Entity (hidden hyperlink) urls: {entity_urls}")

                    if post_text:
                        matched, category = keyword_match(post_text)
                        print(f"  Keyword match: {matched} -> category={category}")
                    else:
                        category = "other"
                        print("  No text — treating as category 'other'")

                    udemy_links: list[str] = []
                    if post_text:
                        udemy_links += await loop.run_in_executor(None, resolve_udemy_links, post_text)
                    for burl in button_urls + entity_urls:
                        udemy_links += await loop.run_in_executor(None, resolve_udemy_links, burl)
                        if "udemy.com" in burl.lower():
                            udemy_links.append(burl)
                    udemy_links = list(dict.fromkeys(udemy_links))
                    print(f"  Resolved Udemy links: {udemy_links}")

                    if not udemy_links:
                        print("  ⚠️  No Udemy links resolved — scraper found nothing on this post.")

                    for link in udemy_links:
                        status, meta = await loop.run_in_executor(None, process_udemy_link, link, category)
                        print(f"    -> {link[:90]}")
                        print(f"       status={status}")
                        print(f"       meta={meta}")
            except Exception as exc:
                print(f"  ⚠️  Error: {exc}")

        print()
        print("=" * 70)
        print("STEP 5 — Direct scraper test on the two known intermediary sites")
        print("(deterministic — doesn't depend on whatever the latest live post is)")
        print("=" * 70)
        KNOWN_TEST_URLS = [
            "https://findmycourse.in/course/governance-risk-compliance-risk-registers",
            "https://freecourse.io/courses/lpi-linux-essentials-010-160-exam-questions",
        ]
        for test_url in KNOWN_TEST_URLS:
            print(f"\n--- {test_url} ---")
            links = await loop.run_in_executor(None, get_udemy_links_from_page, test_url)
            print(f"  Extracted Udemy links: {links}")

        print()
        print("=" * 70)
        print("Diagnosis complete — share this full output.")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
