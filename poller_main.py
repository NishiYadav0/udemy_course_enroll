"""
poller_main.py
---------------
ScholarSync — Entry point (website-polling architecture).

Polls freecourse.io and findmycourse.in directly every
POLL_INTERVAL_SECONDS, discovers newly published courses, and runs
each one through the full pipeline:
    Layer 1  keyword filter      -> utils/filter.py
    Layer 3  Udemy metadata      -> utils/udemy.py
    Layer 4  course policy       -> utils/filter.py
    Layer 5  auto-enroll         -> utils/udemy.py
    Layer 6  Telegram alert      -> utils/notifier.py + Telegram send

Supports both Telegram Bot Token (recommended for cloud) and Pyrogram Userbot.
"""

import os
import asyncio
import logging
import requests
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

# Track whether a token-expiry alert was already sent this session
_token_expiry_alerted = False


def create_telegram_client():
    """Creates a Pyrogram client dynamically within the active loop if configured."""
    try:
        from pyrogram import Client
    except ImportError:
        return None

    api_id_val = os.getenv("API_ID")
    api_hash_val = os.getenv("API_HASH")
    session_str = os.getenv("SESSION_STRING") or os.getenv("TELEGRAM_SESSION_STRING")
    bot_token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

    if not api_id_val or not api_hash_val:
        return None

    try:
        api_id = int(api_id_val)
        api_hash = str(api_hash_val)

        if bot_token:
            return Client(
                name="udemy_bot",
                api_id=api_id,
                api_hash=api_hash,
                bot_token=bot_token,
                in_memory=True,
                no_updates=True,
            )
        elif session_str:
            return Client(
                name="scholarsync_session",
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_str,
                in_memory=True,
                no_updates=True,
            )
        elif os.path.exists("scholarsync_session.session"):
            return Client(
                name="scholarsync_session",
                api_id=api_id,
                api_hash=api_hash,
            )
    except Exception as exc:
        logger.warning("Could not initialize Pyrogram client: %s", exc)

    return None


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
async def _safe_send(client, chat_id, text: str) -> None:
    """Send a message via direct Telegram Bot HTTP API or Pyrogram Client."""
    if not chat_id:
        logger.info("No ALERT_CHANNEL_ID configured. Alert text:\n%s", text[:100])
        return

    bot_token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

    # 1. Prefer HTTP Bot API if bot token is provided (super fast, no session strings required)
    if bot_token:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
            if resp.status_code == 200:
                return
            else:
                logger.warning("Telegram Bot API HTTP response: %s", resp.text)
        except Exception as exc:
            logger.warning("Telegram Bot API HTTP send error: %s", exc)

    # 2. Fallback to Pyrogram client if active
    if client is not None:
        try:
            from pyrogram.errors import FloodWait
            await client.send_message(chat_id=chat_id, text=text)
        except FloodWait as e:
            logger.warning("FloodWait: sleeping %d seconds", e.value)
            await asyncio.sleep(e.value)
            await client.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            logger.error("Failed to send Telegram message via Pyrogram: %s", exc)


async def _process_course(
    client,
    loop: asyncio.AbstractEventLoop,
    site_name: str,
    course_url: str,
) -> None:
    """
    Run one newly-discovered course page through the full pipeline:
    extract Udemy link(s) -> category -> metadata -> policy -> enroll
    -> alert.
    """
    global _token_expiry_alerted

    alert_channel_val = os.getenv("ALERT_CHANNEL_ID")
    alert_channel_id = int(alert_channel_val) if alert_channel_val and alert_channel_val.lstrip("-").isdigit() else alert_channel_val

    log_prefix = f"[{site_name}]"
    logger.info("%s New course discovered → %s", log_prefix, course_url[:100])

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
            await _safe_send(client, alert_channel_id, alert)
            break

        elif status == STATUS_ALREADY_OWNED:
            logger.info("%s ⏩ Already enrolled: %s", log_prefix, title)
            await _safe_send(
                client, alert_channel_id,
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
                await _safe_send(client, alert_channel_id, format_token_expiry_alert())
                _token_expiry_alerted = True
            break

        elif status in (STATUS_ERROR, STATUS_PARSE_FAIL):
            logger.warning("%s ⚠️ Error on link %d (%s) — trying next", log_prefix, idx, status)
            continue


async def _poll_cycle(
    client, loop: asyncio.AbstractEventLoop, seen: set[str]
) -> None:
    """One full check of both sites for newly published courses."""
    new_courses = await loop.run_in_executor(None, discover_new_courses, seen)

    if not new_courses:
        logger.info("Poll cycle: no new courses found.")
        return

    logger.info("Poll cycle: %d new course(s) found — processing...", len(new_courses))

    for site_name, course_url in new_courses:
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
    client = create_telegram_client()

    if client:
        try:
            await client.start()
            me = await client.get_me()
            logger.info("Connected to Telegram as %s (id=%s)", getattr(me, "first_name", "Bot"), getattr(me, "id", "?"))
        except Exception as exc:
            logger.warning("Could not start Pyrogram client (%s). Using HTTP Bot API if BOT_TOKEN is set.", exc)
            client = None
    else:
        logger.info("Telegram Userbot session not set. Alerts will be sent via BOT_TOKEN if provided.")

    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "240"))
    logger.info("Alert channel     : %s", os.getenv("ALERT_CHANNEL_ID", "Not set"))
    logger.info("Poll interval     : every %d seconds", poll_interval)
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
    logger.info("Listening for new courses via website polling... (24/7/365 active)")
    logger.info("-" * 60)

    try:
        while True:
            try:
                await _poll_cycle(client, loop, seen)
            except Exception as exc:
                logger.error("Poll cycle failed unexpectedly: %s", exc)
            await asyncio.sleep(poll_interval)
    finally:
        if client and client.is_connected:
            try:
                await client.stop()
            except Exception:
                pass


if __name__ == "__main__":
    print(
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║   ScholarSync v2.0 — Website-Polling Mode ║\n"
        "║  IIT Madras BS Data Science Auto-Enroll  ║\n"
        "╚══════════════════════════════════════════╝\n"
        "  Trigger source : freecourse.io + findmycourse.in (polled)\n"
        "  Telegram role  : send-only alerts\n"
    )
    asyncio.run(_main())
