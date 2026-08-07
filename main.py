"""
main.py
-------
ScholarSync — Entry point.

Starts a Pyrogram Userbot that:
  1. Connects to Telegram as your personal account (using API_ID / API_HASH).
  2. Silently listens to TARGET_CHANNELS (the muted coupon channels).
  3. For every new post, runs the full 6-layer pipeline:
       Layer 1  keyword filter      → utils/filter.py
       Layer 2  web scraper         → utils/scraper.py
       Layer 3  Udemy metadata      → utils/udemy.py
       Layer 4  course policy       → utils/filter.py
       Layer 5  auto-enroll         → utils/udemy.py
       Layer 6  Telegram alert      → utils/notifier.py  +  Pyrogram send
  4. Sends a confirmation alert to ALERT_CHANNEL_ID on successful enrollment.
  5. Sends a token-expiry warning to ALERT_CHANNEL_ID if Udemy 401s.

Run locally:
    python main.py

Deploy on Oracle Cloud (inside tmux):
    tmux new -s scholarsync
    source venv/bin/activate && python main.py
    (Ctrl+B then D to detach)
"""

import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging

from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from dotenv import load_dotenv

from utils.filter   import keyword_match
from utils.cache    import COURSE_CACHE, ENROLLED_SLUGS
from utils.scraper  import (resolve_udemy_links, extract_all_urls,
                            get_page_html_and_udemy_links, slug_from_url)
from utils.udemy    import (
    process_udemy_link,
    extract_coupon_codes_from_text,
    parse_slug_and_coupon,
    STATUS_SUCCESS,
    STATUS_ALREADY_OWNED,
    STATUS_POLICY_FAIL,
    STATUS_EXPIRED,
    STATUS_TOKEN_EXPIRED,
    STATUS_ERROR,
    STATUS_PARSE_FAIL,
)
from utils.retry_queue import (
    load_queue,
    add_to_queue,
    remove_from_queue,
    update_entry,
    is_too_old,
    RETRY_INTERVAL_SECONDS,
    RETRY_MAX_AGE_HOURS,
)
from utils.notifier import (
    format_success_alert,
    format_already_enrolled_log,
    format_already_enrolled_alert,
    format_policy_drop_log,
    format_token_expiry_alert,
    format_startup_banner,
)

# ─────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
# Suppress Pyrogram's overly verbose internal logs
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.client").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.session").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Bounded worker pool for all blocking work (scraping + enrolling)
# ─────────────────────────────────────────────────────────────
# run_in_executor(PIPELINE_EXECUTOR, ...) uses Python's DEFAULT ThreadPoolExecutor, which
# holds (cpu_count + 4) threads — 5 on this 1 OCPU VM. Every one of those
# threads can launch its own headless Chromium, and each costs 250-400 MB on a
# box with 1 GB total.
#
# Observed live on 2026-08-07: four posts landed between 08:52:01 and 08:53:01,
# each starting a browser. Not one scrape ever finished, and Telegram's own
# connection was starved out:
#     ERROR   Send failed: ConnectionResetError Connection lost
#     WARNING Retrying "messages.GetDialogs" due to: Request timed out
#
# One worker means posts are processed strictly one after another. Nothing is
# lost — later posts simply queue — and coupons remain valid for days, so a
# minute of waiting costs nothing.
PIPELINE_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.getenv("SCHOLARSYNC_WORKERS", "1")),
    thread_name_prefix="scholarsync",
)


# ─────────────────────────────────────────────────────────────
# Global asyncio exception handler
# Suppresses non-fatal "Peer id invalid" errors that Pyrogram
# raises when it receives updates from channels that are not
# in its local SQLite peer cache yet. These come from OTHER
# channels you're a member of — NOT our target channels.
# Our filters.chat() handler still works perfectly.
# ─────────────────────────────────────────────────────────────
def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exception = context.get("exception")
    if exception is not None:
        msg = str(exception)
        # Suppress known Pyrogram internal peer lookup misses
        if any(kw in msg for kw in (
            "Peer id invalid",
            "ID not found",
            "peer_id",
        )):
            return  # Silently drop — non-fatal, target channels unaffected
    # Log everything else at DEBUG so it doesn't clutter the console
    logger.debug("Async background error: %s", context.get("message", "unknown"))

# ─────────────────────────────────────────────────────────────
# Load environment variables
# ─────────────────────────────────────────────────────────────
load_dotenv()

def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Please fill it in your .env file."
        )
    return val

API_ID           = int(_require_env("API_ID"))
API_HASH         = _require_env("API_HASH")
ALERT_CHANNEL_ID = int(_require_env("ALERT_CHANNEL_ID"))

_raw_channels = _require_env("TARGET_CHANNELS")
TARGET_CHANNELS: list[int] = [
    int(ch.strip()) for ch in _raw_channels.split(",") if ch.strip()
]

# ─────────────────────────────────────────────────────────────
# Pyrogram client
# Session file name: scholarsync_session.session
# (stored in the project root — DO NOT commit to git)
# ─────────────────────────────────────────────────────────────
app = Client(
    name    = "scholarsync_session",
    api_id  = API_ID,
    api_hash= API_HASH,
)

# Fast O(1) lookup set — used in the handler instead of filters.chat()
# (filters.chat requires SQLite peer lookup which silently fails for
#  channels not yet cached in the session file)
TARGET_CHANNELS_SET: set[int] = set(TARGET_CHANNELS)

# Track whether a token-expiry alert was already sent this session
_token_expiry_alerted = False


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
async def _safe_send(client: Client, chat_id: int, text: str) -> None:
    """Send a message, handling FloodWait gracefully."""
    try:
        await client.send_message(chat_id=chat_id, text=text)
    except FloodWait as e:
        logger.warning("FloodWait: sleeping %d seconds", e.value)
        await asyncio.sleep(e.value)
        await client.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc)


def _extract_text(message: Message) -> str | None:
    """Return the text content of a message regardless of media type."""
    return message.text or message.caption or None


def _extract_button_urls(message: Message) -> list[str]:
    """
    Extract URLs from Telegram inline keyboard buttons.

    This is the FIX for the critical bug:
    Coupon channels post an 'Enroll Now' button whose URL is NOT in the
    message text — it lives inside message.reply_markup.inline_keyboard.
    Without this function, we would never see the intermediary site URL
    and the scraper would always return zero Udemy links.

    Handles both InlineKeyboardMarkup and ReplyKeyboardMarkup.
    """
    urls: list[str] = []
    try:
        markup = message.reply_markup
        if markup is None:
            return urls
        # InlineKeyboardMarkup: rows of InlineKeyboardButton objects
        if hasattr(markup, "inline_keyboard"):
            for row in markup.inline_keyboard:
                for btn in row:
                    url = getattr(btn, "url", None)
                    if url and url.startswith("http"):
                        urls.append(url)
    except Exception as exc:
        logger.debug("Button URL extraction error: %s", exc)
    return urls


def _extract_intermediary_urls(post_text: str, button_urls: list[str]) -> list[str]:
    """
    Return every non-Udemy URL referenced by this post (post text URLs +
    button URLs) — these are the ORIGINAL source pages (freecourse.io,
    findmycourse.in, etc.), which the retry worker needs to re-visit
    later to look for a fresh coupon code.
    """
    urls = extract_all_urls(post_text) if post_text else []
    urls = urls + button_urls
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if "udemy.com" not in u.lower() and u not in seen:
            seen.add(u)
            result.append(u)
    return result


# ─────────────────────────────────────────────────────────────
# Heartbeat — proves the bot is alive every 10 minutes
# ─────────────────────────────────────────────────────────────
async def _heartbeat() -> None:
    """
    Prints a single line to terminal every 10 minutes.
    If you see this, ScholarSync is alive and listening.
    Connection timeout warnings from Pyrogram are normal—it auto-reconnects.
    """
    interval = 600  # 10 minutes
    while True:
        await asyncio.sleep(interval)
        from datetime import datetime
        now = datetime.now().strftime("%H:%M:%S")
        logger.info(
            "\U0001f49a Bot alive | %s | Monitoring %d channel(s) | "
            "Waiting for coupon posts...",
            now, len(TARGET_CHANNELS)
        )


# ─────────────────────────────────────────────────────────────
# Retry worker — periodically re-checks queued courses for a fresh
# coupon code the original scrape missed (see utils/retry_queue.py).
# Only runs on courses > RETRY_ELIGIBLE_MIN_HOURS that failed with
# specifically an expired-coupon reason.
# ─────────────────────────────────────────────────────────────
async def _retry_worker(client: Client) -> None:
    global _token_expiry_alerted
    loop = asyncio.get_event_loop()

    while True:
        await asyncio.sleep(RETRY_INTERVAL_SECONDS)

        queue = load_queue()
        if not queue:
            continue

        logger.info("🔁 Retry worker: checking %d queued course(s)...", len(queue))

        for entry in queue:
            course_url = entry["course_url"]

            if is_too_old(entry):
                logger.info(
                    "🔁 Retry worker: giving up on %s (past %.0fh limit, %d attempt(s))",
                    course_url, RETRY_MAX_AGE_HOURS, entry.get("attempts", 0),
                )
                remove_from_queue(course_url)
                continue

            # use_cache=False is essential here. The retry worker exists
            # precisely to notice coupons ADDED to a page after we first read
            # it, so serving it a cached copy of that earlier read would defeat
            # the entire feature.
            html, links = await loop.run_in_executor(
                PIPELINE_EXECUTOR, get_page_html_and_udemy_links, course_url, False
            )

            tried_coupons = list(entry.get("tried_coupons", []))
            new_links: list[tuple[str, str | None]] = []
            for link in links:
                _, coupon_code = parse_slug_and_coupon(link)
                if coupon_code and coupon_code not in tried_coupons:
                    new_links.append((link, coupon_code))

            if not new_links:
                continue  # nothing new on the page yet — stays queued

            logger.info(
                "🔁 Retry worker: %d new coupon(s) found for %s",
                len(new_links), course_url,
            )

            enrolled = False
            for link, coupon_code in new_links:
                status, meta = await loop.run_in_executor(
                    PIPELINE_EXECUTOR, process_udemy_link, link, entry["category"]
                )
                if coupon_code:
                    tried_coupons.append(coupon_code)

                if status == STATUS_SUCCESS:
                    title = meta.get("title", link[:50])
                    logger.info("🔁 ✅✅ RETRY ENROLLED: %s", title)
                    alert = format_success_alert(
                        title       = title,
                        url         = link,
                        hours       = meta.get("duration_hours", 0.0),
                        rating      = meta.get("rating", 0.0),
                        category    = entry["category"],
                        subscribers = meta.get("num_subscribers", 0),
                    )
                    await _safe_send(client, ALERT_CHANNEL_ID, alert)
                    remove_from_queue(course_url)
                    enrolled = True
                    break

                elif status == STATUS_ALREADY_OWNED:
                    logger.info("🔁 ⏩ Already enrolled (found via retry): %s", course_url)
                    remove_from_queue(course_url)
                    enrolled = True
                    break

                elif status == STATUS_TOKEN_EXPIRED:
                    if not _token_expiry_alerted:
                        await _safe_send(client, ALERT_CHANNEL_ID, format_token_expiry_alert())
                        _token_expiry_alerted = True
                    break

                # STATUS_POLICY_FAIL / STATUS_EXPIRED / STATUS_ERROR / etc.
                # → this coupon code is now "tried", move on to the next one

            if not enrolled:
                update_entry(course_url, tried_coupons, entry.get("attempts", 0) + 1)


# ─────────────────────────────────────────────────────────────
# Main event handler — fires on EVERY Telegram message/update.
# We listen to ALL messages and do a fast set-based ID check
# inside the handler — this bypasses the peer-cache lookup that
# filters.chat() requires and silently fails for new channel IDs.
# ─────────────────────────────────────────────────────────────
@app.on_message(filters.all)
async def pipeline_event_processor(client: Client, message: Message) -> None:
    """
    The full 6-layer ScholarSync pipeline, triggered per Telegram message.
    """
    global _token_expiry_alerted

    # ── GATE: Only process messages from our target channels ───
    if not message.chat or message.chat.id not in TARGET_CHANNELS_SET:
        return

    # ── Extract text + button URLs from the post ─────────────
    post_text    = _extract_text(message) or ""
    button_urls  = _extract_button_urls(message)
    # Original source page(s) — needed later if we have to queue this
    # course for a coupon retry (see utils/retry_queue.py).
    intermediary_urls = _extract_intermediary_urls(post_text, button_urls)

    # If there's no text AND no buttons, nothing to process
    if not post_text and not button_urls:
        return

    channel_id = message.chat.id
    msg_id     = message.id
    log_prefix = f"[ch={channel_id} msg={msg_id}]"

    # ── Log every incoming post from target channels ──────────
    preview = post_text[:80].replace("\n", " ") if post_text else "[no text — button-only post]"
    logger.info("\n%s", "─" * 60)
    logger.info("%s ✉️  New post | %d chars | %d button(s) | %s...",
                log_prefix, len(post_text), len(button_urls), preview)
    if button_urls:
        logger.info("%s 🔘 Button URLs: %s", log_prefix, button_urls)

    # ─────────────────────────────────────────────────────────
    # LAYER 1 — Keyword Filter (on post text only)
    # If no text, skip keyword filter and go straight to button URLs
    # ─────────────────────────────────────────────────────────
    if post_text:
        matched, category = keyword_match(post_text)
        if not matched:
            # Even if text doesn't match, the button might lead to a valid course
            # — treat as "other" category so duration/policy still applies
            category = "other"
            logger.info("%s ⚠️  No keyword match in text — using 'other' category", log_prefix)
        else:
            logger.info("%s ✅ Keyword matched → category: [%s]", log_prefix, category.upper())
    else:
        # Button-only post (no visible text)
        category = "other"
        logger.info("%s ℹ️  Button-only post — category: [OTHER]", log_prefix)

    # ─────────────────────────────────────────────────────────
    # LAYER 1.5 — PRE-SCRAPE SKIP  (cheap, runs before any browser)
    # ─────────────────────────────────────────────────────────
    # Your three channels mirror each other: the same course is posted to all
    # of them within a minute or two. Scraping is the single most expensive
    # step (~20s and ~300 MB for a headless Chromium), and until now it ran
    # BEFORE we had any idea which course the post was about.
    #
    # But both coupon sites reuse Udemy's own slug in their URLs — verified
    # 4/4 against live data — so the course can be identified straight from
    # the link, for free:
    #     https://freecourse.io/courses/lpi-linux-essentials-010-160-exam-questions
    #     https://www.udemy.com/course/lpi-linux-essentials-010-160-exam-questions/
    #
    # If EVERY course this post points at is one we already own, there is
    # nothing a scrape could tell us, so we stop here and save the browser.
    #
    # Conservative by design: slug_from_url() returns None whenever it isn't
    # confident, and an unknown slug always falls through to the normal path.
    # The worst case is that we fail to skip — never that we skip wrongly.
    if intermediary_urls:
        slugs = [slug_from_url(u) for u in intermediary_urls]
        known = [s for s in slugs if s and ENROLLED_SLUGS.peek(s)[0]]
        if known and len(known) == len([s for s in slugs if s]):
            logger.info(
                "%s ⏭️  Skipping before scrape — already own %s (saved a browser launch)",
                log_prefix, ", ".join(known[:3]),
            )
            return

    # ─────────────────────────────────────────────────────────
    # LAYER 2 — Web Scraper: Extract Udemy links
    # Sources: (a) URLs in post text, (b) URLs from inline buttons
    # ─────────────────────────────────────────────────────────
    logger.info("%s 🔍 Resolving Udemy links from text + buttons...", log_prefix)
    loop = asyncio.get_event_loop()

    # Resolve from post text (handles intermediary URLs in text body)
    udemy_from_text: list[str] = await loop.run_in_executor(
        PIPELINE_EXECUTOR, resolve_udemy_links, post_text
    ) if post_text else []

    # Resolve from button URLs (the Enroll button URL — this is the critical fix!)
    udemy_from_buttons: list[str] = []
    for btn_url in button_urls:
        extracted = await loop.run_in_executor(
            PIPELINE_EXECUTOR, resolve_udemy_links, btn_url
        )
        udemy_from_buttons.extend(extracted)
        # If the button URL itself is already a Udemy link, add it directly
        if "udemy.com" in btn_url.lower():
            udemy_from_buttons.append(btn_url)

    # Combine both sources, deduplicate, preserve order
    seen: set[str] = set()
    udemy_links: list[str] = []
    for link in udemy_from_text + udemy_from_buttons:
        if link not in seen:
            seen.add(link)
            udemy_links.append(link)

    if not udemy_links:
        logger.info("%s ❌ No Udemy links found in text or buttons — dropping", log_prefix)
        return

    logger.info("%s 🔗 Found %d Udemy link(s) — processing...", log_prefix, len(udemy_links))

    # ─────────────────────────────────────────────────────────
    # LAYERS 3–5 — Metadata → Policy → Enroll
    # Loop through links and stop at first successful enrollment
    # ─────────────────────────────────────────────────────────
    enrollment_done = False

    # Coupon codes printed in the post text itself. Used as a fallback when the
    # coupon embedded in the scraped URL turns out to be stale — coupon sites
    # do not always refresh their links when the instructor issues a new code.
    # Observed live: a post said "Coupon Code:- AUGFREE03" while the scraped
    # freecourse.io link still carried the previous month's "JULFREE02".
    post_coupons = extract_coupon_codes_from_text(post_text or "")
    if post_coupons:
        logger.info("%s 🎟️  Coupon codes in post text: %s", log_prefix, post_coupons)

    for idx, link in enumerate(udemy_links, start=1):
        logger.info("%s 📦 Link %d/%d → %s", log_prefix, idx, len(udemy_links), link[:80])

        # ── Skip a course already decided in the last few minutes ──
        # Your three channels mirror each other, so the identical course
        # routinely arrives 2-3 times within a minute. Re-running it costs
        # Udemy API calls and possibly a whole browser enrollment, and would
        # emit a duplicate alert. Keyed on slug+coupon so volatile tracking
        # params (im_ref, utm_*) don't defeat the match.
        _slug, _coupon = parse_slug_and_coupon(link)

        # (a) Do we already OWN this course? Then no coupon can matter — a
        #     second code cannot enrol you twice. Keyed on slug alone, so this
        #     catches a different coupon for the same course too.
        if _slug and ENROLLED_SLUGS.peek(_slug)[0]:
            logger.info("%s ⏭️  Already own '%s' — skipping (any coupon is moot)",
                        log_prefix, _slug)
            continue

        # (b) Did we already DECIDE this exact course+coupon recently? Keyed on
        #     slug::coupon, so a genuinely different coupon still gets a fair
        #     try — the first one may simply have been dead.
        course_key = f"{_slug}::{_coupon}"
        _seen, _prev = COURSE_CACHE.peek(course_key)
        if _seen:
            logger.info("%s ⏭️  Already handled this course moments ago (%s) — skipping",
                        log_prefix, _prev)
            continue

        # Run synchronous Udemy API calls in thread pool
        status, meta = await loop.run_in_executor(
            PIPELINE_EXECUTOR, process_udemy_link, link, category, post_coupons
        )

        # Remember TERMINAL outcomes only. A transient ERROR or an expired
        # token must stay retryable, so those are deliberately not recorded.
        if status in (STATUS_SUCCESS, STATUS_ALREADY_OWNED,
                      STATUS_POLICY_FAIL, STATUS_EXPIRED):
            COURSE_CACHE.remember(course_key, status)

        # Ownership is coupon-independent and long-lived — record it separately
        # so every future post about this course is skipped before scraping.
        if status in (STATUS_SUCCESS, STATUS_ALREADY_OWNED) and _slug:
            ENROLLED_SLUGS.remember(_slug, status)

        title = meta.get("title", link[:50])
        hours = meta.get("duration_hours", 0.0)
        rating = meta.get("rating", 0.0)
        lang = meta.get("language", "?")

        # ── Handle each possible status ───────────────────────

        if status == STATUS_SUCCESS:
            logger.info("%s ✅✅ ENROLLED: %s | %.1fh | ⭐%.1f | lang=%s",
                        log_prefix, title, hours, rating, lang)
            alert = format_success_alert(
                title       = title,
                url         = link,
                hours       = hours,
                rating      = rating,
                category    = category,
                subscribers = meta.get("num_subscribers", 0),
            )
            await _safe_send(client, ALERT_CHANNEL_ID, alert)
            enrollment_done = True
            # In case this course was already queued for retries from an
            # earlier failed attempt, it's resolved now — stop retrying it.
            for src_url in intermediary_urls:
                remove_from_queue(src_url)
            break

        elif status == STATUS_ALREADY_OWNED:
            logger.info("%s ⏩ Already enrolled: %s", log_prefix, title)
            # Send a quiet info alert to the alert channel
            await _safe_send(
                client, ALERT_CHANNEL_ID,
                format_already_enrolled_alert(title, link)
            )
            for src_url in intermediary_urls:
                remove_from_queue(src_url)
            break  # Already own it — no point trying other coupons

        elif status == STATUS_POLICY_FAIL:
            logger.info("%s ❌ Policy fail: %s", log_prefix, title[:60])
            reason = meta.get("policy_reason", "")
            # Only "coupon expired" is worth retrying later — a different
            # coupon can't fix wrong language / low rating / too short /
            # natively free.
            if "Coupon expired" in reason:
                _, coupon_code = parse_slug_and_coupon(link)
                for src_url in intermediary_urls:
                    add_to_queue(src_url, category, hours, tried_coupon=coupon_code)
            continue

        elif status == STATUS_EXPIRED:
            logger.info("%s ⏳ Coupon expired on link %d — trying next", log_prefix, idx)
            _, coupon_code = parse_slug_and_coupon(link)
            for src_url in intermediary_urls:
                add_to_queue(src_url, category, hours, tried_coupon=coupon_code)
            continue

        elif status == STATUS_TOKEN_EXPIRED:
            if not _token_expiry_alerted:
                logger.error("%s ⚠️ Udemy token expired! Sending alert.", log_prefix)
                await _safe_send(client, ALERT_CHANNEL_ID, format_token_expiry_alert())
                _token_expiry_alerted = True
            break

        elif status in (STATUS_ERROR, STATUS_PARSE_FAIL):
            logger.warning("%s ⚠️ Error on link %d (%s) — trying next", log_prefix, idx, status)
            continue

    if enrollment_done:
        logger.info("%s 🎉 Done — enrolled successfully!", log_prefix)
    else:
        logger.info("%s ⏭️ Done — no enrollment this post", log_prefix)
    logger.info("%s", "─" * 60)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
async def _main() -> None:
    """
    Async entry point.
    Warms up the Pyrogram peer cache for target channels before
    starting to listen — ensures filters and logging work correctly.
    """
    async with app:
        # ── Warm up peer cache for target channels ──────────────
        # This resolves each channel ID and stores it in the SQLite
        # session, eliminating the silent filter failure we saw.
        logger.info("Warming up peer cache for %d channel(s)...", len(TARGET_CHANNELS))
        for ch_id in TARGET_CHANNELS:
            try:
                chat = await app.get_chat(ch_id)
                logger.info(
                    "  ✅ Cached: %s  (id=%d)",
                    (chat.title or chat.username or "?")[:40], ch_id
                )
            except Exception as exc:
                logger.warning("  ⚠️  Could not cache channel %d: %s", ch_id, exc)

        # ── Sync dialogs — REQUIRED for live channel updates ─────
        # This is the actual root cause of "bot runs but never reacts
        # to new posts": get_chat() only resolves peer info, it does
        # NOT register the channel's internal update state (pts) with
        # Pyrogram. Telegram's MTProto layer will not push new-message
        # updates for a channel until the client has done at least one
        # full get_dialogs() pass after login — this is what actually
        # tells Pyrogram "track this channel for live updates".
        # Without this, main.py can sit forever with a perfect peer
        # cache and never fire pipeline_event_processor() for new posts.
        logger.info("Syncing dialogs (required for live channel updates)...")
        dialog_count = 0
        async for _ in app.get_dialogs():
            dialog_count += 1
        logger.info("✅ Dialog sync complete — %d dialog(s) synced.", dialog_count)

        logger.info("🚀 Peer cache ready — listening for new posts...")
        logger.info("-" * 60)

        # Start heartbeat
        asyncio.get_event_loop().create_task(_heartbeat())

        # Start retry worker (coupon rechecks for courses > 5h)
        asyncio.get_event_loop().create_task(_retry_worker(app))

        # Block until Ctrl+C
        await idle()


if __name__ == "__main__":
    print(format_startup_banner(len(TARGET_CHANNELS)))
    logger.info("ScholarSync starting... monitoring %d channel(s)", len(TARGET_CHANNELS))
    logger.info("Alert channel : %d", ALERT_CHANNEL_ID)
    logger.info("Target channels: %s", TARGET_CHANNELS)
    logger.info("Language filter: English (en) + Hindi (hi) only")
    logger.info("Heartbeat      : every 10 minutes")
    logger.info("Retry queue    : courses > 5h, rechecked every 15 min, dropped after 3h")

    # Install global exception handler to silence Pyrogram peer-resolution
    # noise from non-target channels
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(_asyncio_exception_handler)

    loop.run_until_complete(_main())
