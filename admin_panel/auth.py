"""
admin_panel/auth.py
--------------------
Single-admin authentication for the ScholarSync panel.

There is deliberately no user database — one operator, one account. The
username and a bcrypt-strength hash of the password live in admin_panel/.env
(never the plaintext password). Set/change them with set_password.py, never
by hand-editing the hash.

Also implements a small in-memory login throttle: 5 failed attempts from the
same IP locks that IP out for 15 minutes. This is intentionally simple
(no Redis, no extra process) because the panel runs as a single Gunicorn
worker on a 1GB VM — an in-memory dict is enough and costs nothing.
"""

import time

from flask_login import UserMixin
from werkzeug.security import check_password_hash

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60


class AdminUser(UserMixin):
    """The one and only account this app knows about."""

    def __init__(self, username: str):
        self.id = username
        self.username = username


class LoginThrottle:
    def __init__(self):
        self._fails: dict[str, list[float]] = {}

    def is_locked(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        attempts = [t for t in self._fails.get(ip, []) if now - t < LOCKOUT_SECONDS]
        self._fails[ip] = attempts
        if len(attempts) >= MAX_FAILED_ATTEMPTS:
            remaining = int(LOCKOUT_SECONDS - (now - attempts[0]))
            return True, max(remaining, 0)
        return False, 0

    def record_failure(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)


throttle = LoginThrottle()


def verify_login(config: dict, username: str, password: str) -> bool:
    if username != config.get("ADMIN_USERNAME"):
        return False
    stored_hash = config.get("ADMIN_PASSWORD_HASH", "")
    if not stored_hash:
        return False
    return check_password_hash(stored_hash, password)
