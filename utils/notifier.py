"""
utils/notifier.py
-----------------
ScholarSync — Layer 6 of the pipeline.

Formats clean Telegram alert messages for different pipeline outcomes.
All functions return a ready-to-send string (no Pyrogram calls here —
that stays in main.py to keep the notifier stateless and testable).
"""

from datetime import datetime


# ─────────────────────────────────────────────────────────────
# Emoji map per category (makes alerts visually scannable)
# ─────────────────────────────────────────────────────────────
CATEGORY_EMOJI: dict[str, str] = {
    "data_science":      "🧠",
    "coding":            "💻",
    "ethical_hacking":   "🔐",
    "digital_marketing": "📈",
    "design":            "🎨",
    "linguistics":       "🗣️",
    "other":             "📚",
}


def _now() -> str:
    """Return current time as a readable string."""
    return datetime.now().strftime("%d %b %Y, %I:%M %p")


# ─────────────────────────────────────────────────────────────
# Success Alert
# ─────────────────────────────────────────────────────────────
def format_success_alert(
    title:    str,
    url:      str,
    hours:    float,
    rating:   float,
    category: str,
    subscribers: int = 0,
) -> str:
    """
    Format the Telegram notification sent when a course is successfully enrolled.

    Parameters
    ----------
    title       : Udemy course title
    url         : Direct Udemy coupon URL
    hours       : Course duration in hours
    rating      : Udemy star rating
    category    : Category from keyword_match()
    subscribers : Total enrolled students (optional)

    Returns
    -------
    Formatted Telegram message string (Markdown-compatible).
    """
    emoji = CATEGORY_EMOJI.get(category, "📚")
    cat_display = category.replace("_", " ").title()

    sub_line = ""
    if subscribers > 0:
        sub_line = f"👥 **Students Enrolled:** {subscribers:,}\n"

    return (
        f"✅ **AUTO-ENROLLED SUCCESSFULLY!**\n"
        f"{'─' * 35}\n"
        f"{emoji} **Category:** {cat_display}\n"
        f"📖 **Course:** {title}\n"
        f"⏱️ **Duration:** {hours:.1f} hours\n"
        f"⭐ **Rating:** {rating:.1f} / 5.0\n"
        f"{sub_line}"
        f"🔗 **Enrollment Link:**\n"
        f"{url}\n"
        f"{'─' * 35}\n"
        f"🕐 **Claimed at:** {_now()}\n"
        f"_ScholarSync — Auto-enrolled for you 🤖_"
    )


# ─────────────────────────────────────────────────────────────
# Already Enrolled Alert (silent — only logs to console, no Telegram)
# ─────────────────────────────────────────────────────────────
def format_already_enrolled_log(title: str) -> str:
    """Console-only log message when course is already owned."""
    return f"⏩ SKIPPED | Already enrolled: {title}"


def format_already_enrolled_alert(title: str, url: str) -> str:
    """
    Light Telegram notification when a course is already in your library.
    Sent to ALERT_CHANNEL so you can see it without noise.
    """
    return (
        f"📚 **Already in Your Library**\n"
        f"{'─' * 35}\n"
        f"📖 **Course:** {title}\n"
        f"\u2139\ufe0f This course was posted with a new coupon,\n"
        f"   but you already own it — no action needed.\n"
        f"{'─' * 35}\n"
        f"🔗 {url}\n"
        f"🕐 {_now()}\n"
        f"_ScholarSync — Skipped (already enrolled) 🤖_"
    )


# ─────────────────────────────────────────────────────────────
# Policy Drop Alert (silent — console only)
# ─────────────────────────────────────────────────────────────
def format_policy_drop_log(title: str, reason: str) -> str:
    """Console-only log message when a course fails the policy check."""
    return f"⏩ DROPPED | {reason} | {title[:60]}"


# ─────────────────────────────────────────────────────────────
# Token Expiry Alert (sends to Telegram — needs user action)
# ─────────────────────────────────────────────────────────────
def format_token_expiry_alert() -> str:
    """
    Telegram alert telling the user their Udemy token has expired.
    Includes exact steps to refresh it.
    """
    return (
        "⚠️ **ACTION REQUIRED — Udemy Token Expired!**\n"
        f"{'─' * 35}\n"
        "ScholarSync could not enroll a course because your\n"
        "Udemy access token has expired.\n\n"
        "**To fix this:**\n"
        "1. Open **udemy.com** in your browser and log in.\n"
        "2. Press **F12** → Application → Cookies → `access_token`\n"
        "3. Copy the full token value.\n"
        "4. SSH into your VM (PuTTY or `ssh ubuntu@<your-vm-ip>`).\n"
        "5. Edit the config:\n"
        "   `nano ~/scholarsync/.env`\n"
        "6. Replace the `UDEMY_ACCESS_TOKEN` value.\n"
        "7. Restart ScholarSync:\n"
        "   `sudo systemctl restart scholarsync`\n\n"
        f"🕐 Alert sent: {_now()}"
    )


# ─────────────────────────────────────────────────────────────
# Startup Banner (printed to console, not sent to Telegram)
# ─────────────────────────────────────────────────────────────
def format_startup_banner(channel_count: int) -> str:
    """ASCII banner printed when ScholarSync starts."""
    return (
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║         ScholarSync v1.0 — Active        ║\n"
        "║  IIT Madras BS Data Science Auto-Enroll  ║\n"
        "╚══════════════════════════════════════════╝\n"
        f"  📡 Monitoring : {channel_count} channel(s)\n"
        f"  🕐 Started at : {_now()}\n"
        "  ─────────────────────────────────────────\n"
        "  Categories & Min Duration:\n"
        "    🧠 Data Science    → > 3 hrs\n"
        "    💻 Coding          → > 3 hrs\n"
        "    🔐 Ethical Hacking → > 3 hrs\n"
        "    📈 Digital Mktg    → > 6 hrs\n"
        "    🎨 Design          → > 6 hrs\n"
        "    🗣️  Linguistics     → > 10 hrs\n"
        "    📚 Other           → > 8 hrs\n"
        "  ─────────────────────────────────────────\n"
        "  Listening for coupons... (Ctrl+C to stop)\n"
    )
