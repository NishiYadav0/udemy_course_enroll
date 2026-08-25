"""
admin_panel/setup_state.py
----------------------------
Answers two yes/no questions by looking at real, on-disk artifacts —
deliberately no separate "setup complete" flag file to drift out of sync
with reality (same philosophy as Archives: list what's actually there,
never trust a marker):

  is_admin_claimed()   — does an admin account exist yet?
  is_bot_configured()  — has the bot got everything it needs to run: a
                          real .env (not just placeholders) AND an
                          authorized Telegram session file?

Both are cheap file reads, safe to call on every request.
"""

import os

from dotenv import dotenv_values

REQUIRED_BOT_KEYS = [
    "API_ID", "API_HASH", "TARGET_CHANNELS", "ALERT_CHANNEL_ID",
    "UDEMY_ACCESS_TOKEN", "UDEMY_CSRF_TOKEN", "UDEMY_DJ_SESSION_ID",
    "UDEMY_USER_JWT",
]

SESSION_FILENAME = "scholarsync_session.session"


def is_admin_claimed(panel_env_path: str) -> bool:
    if not os.path.exists(panel_env_path):
        return False
    cfg = dotenv_values(panel_env_path)
    return bool(cfg.get("ADMIN_USERNAME") and cfg.get("ADMIN_PASSWORD_HASH"))


def is_bot_configured(bot_env_path: str, scholarsync_root: str) -> bool:
    session_path = os.path.join(scholarsync_root, SESSION_FILENAME)
    if not os.path.exists(session_path):
        return False
    if not os.path.exists(bot_env_path):
        return False
    cfg = dotenv_values(bot_env_path)
    return all(cfg.get(k) for k in REQUIRED_BOT_KEYS)


def session_path(scholarsync_root: str) -> str:
    return os.path.join(scholarsync_root, SESSION_FILENAME)
