"""
poller_main.py
---------------
ScholarSync — Entry point (website-polling architecture).

Supersedes main.py's Telegram-listening trigger. Diagnosis showed the
Telegram MTProto connection never delivers live push updates in this
deployment — pull-based calls (get_dialogs, get_chat_history,
get_chat_member) all work perfectly, membership is confirmed, but zero
raw updates arrive over minutes of observation. Most likely cause:
local antivirus/firewall/VPN interference with the persistent
connection (see PROGRESS_REPORT.md for the full trail). Rather than
keep chasing that, this script sidesteps it entirely.

Instead of waiting for Telegram to tell us about a new coupon post,
this script POLLS freecourse.io and findmycourse.in directly every
POLL_INTERVAL_SECONDS, discovers newly published courses, and runs
each one through the exact same pipeline as before:
    Layer 1  keyword filter      -> utils/filter.py
    Layer 3  Udemy metadata      -> utils/udemy.py
    Layer 4  course policy       -> utils/filter.py
    Layer 5  auto-enroll         -> utils/udemy.py
    Layer 6  Telegram alert      -> utils/notifier.py + Pyrogram send

Telegram is used ONLY to send alerts here — sending was never affected
by the broken live-update issue, since it's a request your script
initiates and gets a direct reply to.

FIRST RUN behavior: the very first time this runs, there is no
seen-courses history yet, so both sites' entire current listing would
look "new." To avoid flooding your alert channel with historical
courses, the first run silently bootstraps the seen-list (records
everything currently listed as already-seen) WITHOUT processing or
alerting on any of it. Only genuinely new courses from that point
forward are processed. This takes one poll cycle; after that, normal
operation begins automatically.

Run locally:
    python poller_main.py

Deploy on Oracle Cloud (inside tmux):
    tmux new -s scholarsync
    source venv/bin/activate && python poller_main.py
    (Ctrl+B then D to detach)
"""

import os
import asyncio
import logging

from pyrogram import Client
from pyrogram.errors import FloodWait
from dotenv import load_dotenv

from utils.filter import keyword_match
from utils.scraper import (
    get_page_html_and_udemy_links,
    extract_page_text,
)
from utils.poller import (
    load_seen,
    save_seen,
    is_first_run,
    discover_new_courses,
)
from utils.udemy import (
    process_udemy_link,
    STATUS_SUCCESS,
    STATUS_ALREADY_OWNED,
    STATUS_POLICY_FAIL,
    STATUS_EXPIRED,
    STATUS_TOKEN_EXPIRED,
    STATUS_ERROR,
    STATUS_PARSE_FAIL,
)
from utils.notifier import (
    format_success_alert,
    format_already_enrolled_alert,
    format_token_expiry_alert,
)

# ─────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.client").setLevel(logging.CRITICAL)
logging.getLogger("pyrogram.session").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Environment
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

# How often (seconds) to check both sites for new courses. Kept
# deliberately moderate (default 4 minutes) — see utils/poller.py for
# why fast polling isn't appropriate for these sites.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "240"))

_session_str = os.getenv("SESSION_STRING") or os.getenv("TELEGRAM_SESSION_STRING")
app = Client(
    name           = "scholarsync_session",
    api_id         = API_ID,
    api_hash       = API_HASH,
    session_string = _session_str if _session_str else None,
)

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


async def _process_course(
    client: Client,
    loop: asyncio.AbstractEventLoop,
    site_name: str,
    course_url: str,
) -> None:
    """
    Run one newly-discovered course page through the full pipeline:
    extract Udemy link(s) -> category -> metadata -> policy -> enroll
    -> alert. Mirrors main.py's pipeline_event_processor() logic, just
    triggered by website discovery instead of a Telegram message.
    """
    global _token_expiry_alerted

    log_prefix = f"[{site_name}]"
    logger.info("%s New course discovered → %s", log_prefix, course_url[:100])

    # Single fetch gets us BOTH the page HTML (for category text) and
    # the extracted Udemy link(s) — avoids fetching the same course
    # page twice.
    page_html, udemy_links = await loop.run_in_executor(
        None, get_page_html_and_udemy_links, course_url
    )

    if not udemy_links:
        logger.info("%s No Udemy link found on %s — skipping", log_prefix, course_url[:80])
        return

    page_text = extract_page_text(page_html) if page_html else ""
    if page_text:
        matched, category = keyword_match(page_text)
        if not matched:
            category = "other"
    else:
        category = "other"

    logger.info("%s Category: [%s] | %d Udemy link(s)", log_prefix, category.upper(), len(udemy_links))

    for idx, link in enumerate(udemy_links, start=1):
        status, meta = await loop.run_in_executor(None, process_udemy_link, link, category)

        title  = meta.get("title", link[:50])
        hours  = meta.get("duration_hours", 0.0)
        rating = meta.get("rating", 0.0)

        if status == STATUS_SUCCESS:
            logger.info("%s ✅✅ ENROLLED: %s | %.1fh | ⭐%.1f", log_prefix, title, hours, rating)
            alert = format_success_alert(
                title       = title,
                url         = link,
                hours       = hours,
                rating      = rating,
                category    = category,
                subscribers = meta.get("num_subscribers", 0),
            )
            await _safe_send(client, ALERT_CHANNEL_ID, alert)
            break

        elif status == STATUS_ALREADY_OWNED:
            logger.info("%s ⏩ Already enrolled: %s", log_prefix, title)
            await _safe_send(
                client, ALERT_CHANNEL_ID,
                format_already_enrolled_alert(title, link)
            )
            break

        elif status == STATUS_POLICY_FAIL:
            logger.info("%s ❌ Policy fail: %s", log_prefix, title[:60])
            continue

        elif status == STATUS_EXPIRED:
            logger.info("%s ⏳ Coupon expired on link %d — trying next", log_prefix, idx)
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


async def _poll_cycle(
    client: Client, loop: asyncio.AbstractEventLoop, seen: set[str]
) -> None:
    """One full check of both sites for newly published courses."""
    new_courses = await loop.run_in_executor(None, discover_new_courses, seen)

    if not new_courses:
        logger.info("Poll cycle: no new courses found.")
        return

    logger.info("Poll cycle: %d new course(s) found — processing...", len(new_courses))

    for site_name, course_url in new_courses:
        # Mark seen immediately, before processing — so a crash or
        # error mid-pipeline never causes the same course to be
        # retried forever on every subsequent cycle.
        seen.add(course_url)
        try:
            await _process_course(client, loop, site_name, course_url)
        except Exception as exc:
            logger.warning("[%s] Error processing %s — %s", site_name, course_url[:80], exc)

    save_seen(seen)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
async def _main() -> None:
    loop = asyncio.get_event_loop()

    async with app:
        me = await app.get_me()
        logger.info("Connected to Telegram as %s (id=%s)", me.first_name, me.id)
        logger.info("Alert channel     : %d", ALERT_CHANNEL_ID)
        logger.info("Poll interval     : every %d seconds", POLL_INTERVAL_SECONDS)
        logger.info("Sites monitored   : freecourse.io, findmycourse.in")

        seen = load_seen()

        if is_first_run():
            logger.info(
                "First run detected — bootstrapping seen-course list from "
                "what's currently listed. This will NOT trigger any alerts "
                "or enrollment attempts for pre-existing courses."
            )
            new_courses = await loop.run_in_executor(None, discover_new_courses, seen)
            for _, course_url in new_courses:
                seen.add(course_url)
            save_seen(seen)
            logger.info(
                "Bootstrap complete — %d existing course(s) marked as seen. "
                "From this point on, only genuinely NEW courses will be processed.",
                len(seen),
            )
        else:
            logger.info("Resuming with %d previously-seen course(s) on record.", len(seen))

        logger.info("-" * 60)
        logger.info("Listening for new courses via website polling... (Ctrl+C to stop)")
        logger.info("-" * 60)

        while True:
            try:
                await _poll_cycle(app, loop, seen)
            except Exception as exc:
                logger.error("Poll cycle failed unexpectedly: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    print(
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║   ScholarSync v2.0 — Website-Polling Mode ║\n"
        "║  IIT Madras BS Data Science Auto-Enroll  ║\n"
        "╚══════════════════════════════════════════╝\n"
        "  Trigger source : freecourse.io + findmycourse.in (polled)\n"
        "  Telegram role  : send-only alerts (no live-listening)\n"
    )
    asyncio.run(_main())
