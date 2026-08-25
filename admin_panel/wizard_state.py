"""
admin_panel/wizard_state.py
-----------------------------
Server-side-only scratch space for the first-run setup wizard.

Values collected across the wizard's several pages — target channels, the
alert channel, Udemy cookies, the Telegram account name once login
succeeds — are held here in memory, keyed by a random token stored in the
admin's browser session cookie. The token itself isn't secret (it's just a
lookup key); the actual values it points at never touch the cookie, so
nothing sensitive is sitting in the browser mid-wizard.

In-memory, single dict, no lock — consistent with this app's existing
single-Gunicorn-worker assumption (see auth.py's LoginThrottle for the same
pattern). Cleared the moment /setup/review finishes writing the real .env,
or automatically after 30 minutes of inactivity if the wizard is abandoned.
"""

import time

_TTL_SECONDS = 2 * 60 * 60  # matches the panel's own default session length
_state: dict[str, dict] = {}


def _expired(entry: dict) -> bool:
    return time.time() - entry.get("_touched", 0) > _TTL_SECONDS


def get(token: str) -> dict:
    entry = _state.get(token)
    if not entry or _expired(entry):
        entry = {"_touched": time.time(), "target_channels": []}
        _state[token] = entry
    return entry


def update(token: str, **values) -> dict:
    entry = get(token)
    entry.update(values)
    entry["_touched"] = time.time()
    return entry


def clear(token: str) -> None:
    _state.pop(token, None)
